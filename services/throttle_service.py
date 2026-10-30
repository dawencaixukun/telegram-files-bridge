# -*- coding: utf-8 -*-
"""
services/throttle_service.py — TG 请求节流器（随机节奏 + 每日预算 + 跨日自动续跑）
================================================================================
为什么存在
----------
用户反馈：「虽然限制了下载数量，但我限制 5 个，它能一直下」——怕 TG 账号被封。

根因不是并发数：后端 `autoDownloadLimit` 只限制**同时在跑**的任务数，第 1 个
一结束第 2 个立刻顶上，请求速率恒为「零间隔满负荷」。Telegram 反滥用看的是
**请求速率随时间的形状**，不是瞬时并发。

本服务控的是形状：
1. **随机间隔**：排队文件之间插 6~25 秒（平衡档）随机间隔，避免机械等距；
2. **首波限流**：一次提交最多 `concurrencyMax` 个立即下发，其余进队列按间隔放行；
3. **每日预算**：当天放行文件数封顶（平衡档 200），用完即整批挂起；
4. **次日自动续跑**：预算耗尽后队列原地保留，跨过次日 `resumeHour` 自动继续；
   队列落盘（`.throttle_queue.json`），bridge 重启不丢；
5. **缩略图补图纳入**：云端归档翻页时每张卡片一次 GetMessage，是第二大 TG
   请求源。补图计入同一预算，但只占 `thumbBudgetShare` 的份额，防止翻页把
   下载预算吃光；预算不足时**跳过补图**（落占位图，次日再补）而不是阻塞页面。

设计取舍
--------
* 小批量直通：`len(files) <= concurrencyMax` 且预算充足时**不排队、不延迟**，
  直接交给调用方照常提交。一次点击 2~4 个文件的形状与真人无异，排队只会让
  交互变卡；真正的风险是「一次倒进几百个」与「永不停的零间隔」。
* 不在本服务内直接调 `BACKEND.start_download_multiple`：提交动作仍留在路由层，
  保持原有的查重/水位/风控熔断顺序与错误处理语义（测试也依赖这一点）。
* 放行节奏只看「上次放行时间 + 随机间隔」，不去猜后端任务何时结束 —— 后端
  有并发上限，本服务只负责把请求摊到时间轴上。
"""
import asyncio
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from core.state import (
    _THROTTLE_CONFIG, _THROTTLE_PROFILES, _THROTTLE_DEFAULT_PROFILE,
    _THROTTLE_STATE, _THROTTLE_QUEUE, _THROTTLE_QUEUE_MAX,
    _THROTTLE_CONFIG_FILE, _THROTTLE_STATE_FILE, _THROTTLE_QUEUE_FILE,
    _throttle_config_save, _throttle_config_load,
    _throttle_state_save, _throttle_state_load,
    _throttle_queue_save, _throttle_queue_load,
    _throttle_today, _tasks_cache_invalidate,
)
from core.logging import log, LOG_STORE

# 缩略图补图最多可占用日预算的份额：翻长页面不能把当天的下载额度吃光。
_THUMB_BUDGET_SHARE = 0.4

_throttle_lock = asyncio.Lock()
_throttle_task: Optional[asyncio.Task] = None
_THROTTLE_TICK = 5.0  # 调度循环最长休眠（秒），兼作预算/跨日检查周期

# 后台循环暂停开关：仅供测试把「节奏放行」变成确定性操作。
# 生产路径不会打开它；打开后后台循环仍存活（不丢任务），只是不再主动放行。
_THROTTLE_LOOP_PAUSED = False


def throttle_pause_dispatch() -> None:
    """暂停后台自动放行（测试用；队列与预算状态保持不变）。"""
    global _THROTTLE_LOOP_PAUSED
    _THROTTLE_LOOP_PAUSED = True


def throttle_resume_dispatch() -> None:
    """恢复后台自动放行。"""
    global _THROTTLE_LOOP_PAUSED
    _THROTTLE_LOOP_PAUSED = False


# ---------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------
def _clamp_int(v: Any, lo: int, hi: int, dflt: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return dflt
    return max(lo, min(hi, n))


def _clamp_float(v: Any, lo: float, hi: float, dflt: float) -> float:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return dflt
    return max(lo, min(hi, n))


def _apply_profile(profile: str) -> bool:
    """把预设参数回填进生效配置（保留 enabled / scope / resumeHour）。"""
    prof = _THROTTLE_PROFILES.get(str(profile or ""))
    if not prof:
        return False
    _THROTTLE_CONFIG["profile"] = str(profile)
    for k in ("concurrencyMin", "concurrencyMax", "dailyBudget"):
        _THROTTLE_CONFIG[k] = prof[k]
    for k in ("intervalMin", "intervalMax"):
        _THROTTLE_CONFIG[k] = prof[k]
    return True


def throttle_enabled_for(kind: str) -> bool:
    """该来源是否处于节流覆盖范围（kind: browse/submit/api/watch/thumb）。"""
    if not _THROTTLE_CONFIG.get("enabled", True):
        return False
    scope = _THROTTLE_CONFIG.get("scope") or {}
    return bool(scope.get(kind, True))


def _budget_total() -> int:
    try:
        return max(0, int(_THROTTLE_CONFIG.get("dailyBudget") or 0))
    except (TypeError, ValueError):
        return 0


def _rollover_if_needed() -> bool:
    """跨日重置当日用量。返回 True 表示刚刚发生跨日重置。"""
    today = _throttle_today()
    if str(_THROTTLE_STATE.get("date") or "") == today:
        return False
    _THROTTLE_STATE["date"] = today
    _THROTTLE_STATE["used"] = 0
    _THROTTLE_STATE["thumbUsed"] = 0
    _THROTTLE_STATE["submitted"] = 0
    _throttle_state_save()
    return True


def _used_today() -> int:
    _rollover_if_needed()
    try:
        return max(0, int(_THROTTLE_STATE.get("used") or 0))
    except (TypeError, ValueError):
        return 0


def _budget_left() -> int:
    """剩余可放行文件数；0 表示不限额（返回一个很大的哨兵）。"""
    total = _budget_total()
    if total <= 0:
        return 10 ** 9
    return max(0, total - _used_today())


def _budget_exhausted() -> bool:
    return _budget_left() <= 0


def _consume_budget(n: int, is_thumb: bool = False) -> None:
    if n <= 0:
        return
    _rollover_if_needed()
    _THROTTLE_STATE["used"] = int(_THROTTLE_STATE.get("used") or 0) + int(n)
    if is_thumb:
        _THROTTLE_STATE["thumbUsed"] = int(_THROTTLE_STATE.get("thumbUsed") or 0) + int(n)
    else:
        _THROTTLE_STATE["submitted"] = int(_THROTTLE_STATE.get("submitted") or 0) + int(n)
        _THROTTLE_STATE["lastSubmitAt"] = time.time()
    _throttle_state_save()


def _thumb_budget_left() -> int:
    """缩略图可用的剩余额度（受总预算与 40% 份额双重约束）。"""
    left = _budget_left()
    total = _budget_total()
    if total <= 0:
        return left
    cap = int(total * _THUMB_BUDGET_SHARE)
    used_thumb = int(_THROTTLE_STATE.get("thumbUsed") or 0)
    return max(0, min(left, cap - used_thumb))


def _random_interval() -> float:
    lo = _clamp_float(_THROTTLE_CONFIG.get("intervalMin"), 0.0, 3600.0, 6.0)
    hi = _clamp_float(_THROTTLE_CONFIG.get("intervalMax"), 0.0, 3600.0, 25.0)
    if hi < lo:
        lo, hi = hi, lo
    return random.uniform(lo, hi)


def _concurrency_max() -> int:
    return _clamp_int(_THROTTLE_CONFIG.get("concurrencyMax"), 1, 20, 4)


def _next_resume_at() -> float:
    """下次预算重置（次日 resumeHour）的时间戳；用于界面倒计时。"""
    hour = _clamp_int(_THROTTLE_CONFIG.get("resumeHour"), 0, 23, 0)
    now = time.time()
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, 0, 0, -1))
    if target <= now:
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, hour, 0, 0, 0, 0, -1))
    return target


def _past_resume_point() -> bool:
    """当前是否已过今日的续跑钟点（resumeHour 之前不放行跨日积压）。"""
    hour = _clamp_int(_THROTTLE_CONFIG.get("resumeHour"), 0, 23, 0)
    return time.localtime().tm_hour >= hour


# ---------------------------------------------------------------------
# 对外：配置读写 / 状态
# ---------------------------------------------------------------------
def throttle_config_public() -> Dict[str, Any]:
    """设置页消费的完整配置 + 预设说明（不含队列内容）。"""
    return {
        "enabled": bool(_THROTTLE_CONFIG.get("enabled", True)),
        "profile": str(_THROTTLE_CONFIG.get("profile") or _THROTTLE_DEFAULT_PROFILE),
        "scope": dict(_THROTTLE_CONFIG.get("scope") or {}),
        "concurrencyMin": _clamp_int(_THROTTLE_CONFIG.get("concurrencyMin"), 1, 20, 2),
        "concurrencyMax": _concurrency_max(),
        "intervalMin": _clamp_float(_THROTTLE_CONFIG.get("intervalMin"), 0.0, 3600.0, 6.0),
        "intervalMax": _clamp_float(_THROTTLE_CONFIG.get("intervalMax"), 0.0, 3600.0, 25.0),
        "dailyBudget": _budget_total(),
        "budgetBehavior": str(_THROTTLE_CONFIG.get("budgetBehavior") or "suspend_next_day"),
        "resumeHour": _clamp_int(_THROTTLE_CONFIG.get("resumeHour"), 0, 23, 0),
        "profiles": {
            k: {"label": v["label"], "desc": v["desc"],
                "concurrencyMin": v["concurrencyMin"], "concurrencyMax": v["concurrencyMax"],
                "intervalMin": v["intervalMin"], "intervalMax": v["intervalMax"],
                "dailyBudget": v["dailyBudget"]}
            for k, v in _THROTTLE_PROFILES.items()
        },
        "scopeLabels": {
            "browse": "浏览页勾选下载",
            "submit": "链接提交 / 一键直投",
            "api": "接口直投（/api/tg/quick-download）",
            "watch": "频道监听自动入队",
            "thumb": "缩略图补图（GetMessage）",
        },
    }


def throttle_status() -> Dict[str, Any]:
    """今日预算、队列深度、是否挂起、下次续跑时间。"""
    _rollover_if_needed()
    total = _budget_total()
    used = _used_today()
    queued = len(_THROTTLE_QUEUE)
    return {
        "enabled": bool(_THROTTLE_CONFIG.get("enabled", True)),
        "profile": str(_THROTTLE_CONFIG.get("profile") or _THROTTLE_DEFAULT_PROFILE),
        "date": str(_THROTTLE_STATE.get("date") or ""),
        "budget": total,
        "used": used,
        "thumbUsed": int(_THROTTLE_STATE.get("thumbUsed") or 0),
        "submitted": int(_THROTTLE_STATE.get("submitted") or 0),
        "remaining": (None if total <= 0 else max(0, total - used)),
        "exhausted": _budget_exhausted(),
        "queuedCount": queued,
        "suspendedCount": sum(1 for w in _THROTTLE_QUEUE.values() if w.get("suspended")),
        "nextResumeAt": _next_resume_at(),
        "resumeHour": _clamp_int(_THROTTLE_CONFIG.get("resumeHour"), 0, 23, 0),
        "concurrencyMin": _clamp_int(_THROTTLE_CONFIG.get("concurrencyMin"), 1, 20, 2),
        "concurrencyMax": _concurrency_max(),
        "intervalMin": _clamp_float(_THROTTLE_CONFIG.get("intervalMin"), 0.0, 3600.0, 6.0),
        "intervalMax": _clamp_float(_THROTTLE_CONFIG.get("intervalMax"), 0.0, 3600.0, 25.0),
        "lastSubmitAt": float(_THROTTLE_STATE.get("lastSubmitAt") or 0.0),
    }


def throttle_save_config(body: Dict[str, Any]) -> Tuple[bool, str]:
    """应用并持久化配置。返回 (ok, message)。"""
    if not isinstance(body, dict):
        return False, "配置必须是对象"
    if "enabled" in body:
        _THROTTLE_CONFIG["enabled"] = bool(body.get("enabled"))
    prof = str(body.get("profile") or "").strip()
    if prof:
        if prof not in _THROTTLE_PROFILES:
            return False, f"未知档位: {prof}"
        _apply_profile(prof)
    scope = body.get("scope")
    if isinstance(scope, dict):
        cur = _THROTTLE_CONFIG.setdefault("scope", {})
        for k in list(cur.keys()):
            if k in scope:
                cur[k] = bool(scope[k])
    if "concurrencyMax" in body:
        _THROTTLE_CONFIG["concurrencyMax"] = _clamp_int(body.get("concurrencyMax"), 1, 20, 4)
    if "concurrencyMin" in body:
        _THROTTLE_CONFIG["concurrencyMin"] = _clamp_int(body.get("concurrencyMin"), 1, 20, 2)
    if int(_THROTTLE_CONFIG.get("concurrencyMin") or 1) > _concurrency_max():
        _THROTTLE_CONFIG["concurrencyMin"] = _concurrency_max()
    if "intervalMin" in body:
        _THROTTLE_CONFIG["intervalMin"] = _clamp_float(body.get("intervalMin"), 0.0, 3600.0, 6.0)
    if "intervalMax" in body:
        _THROTTLE_CONFIG["intervalMax"] = _clamp_float(body.get("intervalMax"), 0.0, 3600.0, 25.0)
    if float(_THROTTLE_CONFIG.get("intervalMin") or 0) > float(_THROTTLE_CONFIG.get("intervalMax") or 0):
        _THROTTLE_CONFIG["intervalMin"], _THROTTLE_CONFIG["intervalMax"] = (
            _THROTTLE_CONFIG["intervalMax"], _THROTTLE_CONFIG["intervalMin"])
    if "dailyBudget" in body:
        # 0 是合法值（不限额），负数归零
        try:
            _THROTTLE_CONFIG["dailyBudget"] = max(0, int(body.get("dailyBudget") or 0))
        except (TypeError, ValueError):
            pass
    if "resumeHour" in body:
        _THROTTLE_CONFIG["resumeHour"] = _clamp_int(body.get("resumeHour"), 0, 23, 0)
    _throttle_config_save()
    log.info("TG 节流配置已更新: 开关=%s 档位=%s 并发=%s~%s 间隔=%s~%ss 日预算=%s 续跑钟点=%s",
             _THROTTLE_CONFIG.get("enabled"), _THROTTLE_CONFIG.get("profile"),
             _THROTTLE_CONFIG.get("concurrencyMin"), _THROTTLE_CONFIG.get("concurrencyMax"),
             _THROTTLE_CONFIG.get("intervalMin"), _THROTTLE_CONFIG.get("intervalMax"),
             _THROTTLE_CONFIG.get("dailyBudget"), _THROTTLE_CONFIG.get("resumeHour"))
    return True, "节流配置已保存"


# ---------------------------------------------------------------------
# 对外：缩略图补图额度
# ---------------------------------------------------------------------
def throttle_thumb_allow() -> bool:
    """缩略图补图是否放行。放行即记账（一次 GetMessage ≈ 一次请求）。

    未纳入覆盖范围 / 节流关闭 → 恒放行（保持既有行为）。
    预算不足 → 拒绝，调用方落占位图，次日再补。
    """
    if not throttle_enabled_for("thumb"):
        return True
    if _thumb_budget_left() <= 0:
        log.info("TG 节流：缩略图补图预算已用尽（今日 %s 次），跳过补图，次日自动恢复",
                 _THROTTLE_STATE.get("thumbUsed"))
        return False
    _consume_budget(1, is_thumb=True)
    return True


# ---------------------------------------------------------------------
# 对外：提交编排
# ---------------------------------------------------------------------
def _queue_entry(payload: Dict[str, Any], meta: Dict[str, Any], source: str, idx: int) -> Dict[str, Any]:
    now = time.time()
    return {
        "id": f"wait_throttle_{int(now * 1000)}_{idx}",
        "filename": str(meta.get("filename") or meta.get("name") or f"file_{payload.get('fileId')}"),
        "size": int(meta.get("size_bytes") or 0),
        "size_str": str(meta.get("size") or "—"),
        "source": source,
        "msg_id": str(payload.get("messageId") or "—"),
        "payload": payload,
        "uniqueId": str(meta.get("uniqueId") or payload.get("uniqueId") or ""),
        "status": "waiting_throttle",
        "created_at": now,
        "due_at": 0.0,
        "suspended": False,
        "error_msg": "",
    }


async def throttle_plan_submit(files: List[Dict[str, Any]], kind: str,
                               meta_by_file: Optional[Dict[str, Any]] = None,
                               source_label: str = "") -> Optional[Dict[str, Any]]:
    """决定这批文件怎么下发。

    返回 None            → 不干预，调用方照常一次性提交全部文件；
    返回 {"submit": [...]} → 调用方提交 submit 里的文件（可能为空），
                            其余已进节流队列，按 queued 告知用户。
    """
    if not files:
        return None
    if not throttle_enabled_for(kind):
        return None
    _rollover_if_needed()
    meta_by_file = meta_by_file or {}
    cap = _concurrency_max()

    # 小批量直通：一次点击 2~4 个与真人无异，排队反而拖慢交互。
    # 但预算已耗尽时仍然整批挂起（用户明确要求「预算用完就挂起次日自动跑」）。
    if _budget_exhausted():
        queued = _enqueue_files(files, meta_by_file, source_label or kind, suspended=True)
        _ensure_loop()
        return {
            "submit": [],
            "queued": queued,
            "suspended": True,
            "message": (f"今日 TG 请求预算已用尽（{_budget_total()} 个），"
                        f"已将 {queued} 个任务挂起排队，次日 "
                        f"{_clamp_int(_THROTTLE_CONFIG.get('resumeHour'), 0, 23, 0):02d}:00 自动续跑"),
        }

    # 立即下发的数量必须同时受「并发上限」与「今日剩余预算」约束。
    # 只看并发上限会让预算形同虚设：预算 2、一次提交 6 个时原本会立刻下发 4 个
    # —— 当天预算当场被击穿，用户设定的上限失去意义。
    head_n = min(cap, _budget_left(), len(files))
    if head_n >= len(files):
        _consume_budget(len(files))
        return None

    if head_n <= 0:
        # 剩余预算不足以放行任何一个（理论上被上面的 exhausted 拦下，防御性兜底）
        queued = _enqueue_files(files, meta_by_file, source_label or kind, suspended=True)
        _ensure_loop()
        return {
            "submit": [],
            "queued": queued,
            "suspended": True,
            "message": f"今日 TG 请求预算已用尽，已将 {queued} 个任务挂起排队，次日自动续跑",
        }

    head = files[:head_n]
    tail = files[head_n:]
    queued = _enqueue_files(tail, meta_by_file, source_label or kind, suspended=False)
    _consume_budget(len(head))
    _ensure_loop()
    lo = _clamp_float(_THROTTLE_CONFIG.get("intervalMin"), 0.0, 3600.0, 6.0)
    hi = _clamp_float(_THROTTLE_CONFIG.get("intervalMax"), 0.0, 3600.0, 25.0)
    log.info("TG 节流：一次性提交 %d 个，先下发 %d 个，其余 %d 个按 %.0f~%.0f 秒随机节奏放行",
             len(files), len(head), queued, lo, hi)
    return {
        "submit": head,
        "queued": queued,
        "suspended": False,
        "message": (f"已按防封节奏下发 {len(head)} 个，其余 {queued} 个进入节流队列"
                    f"（每 {lo:.0f}~{hi:.0f} 秒随机放行 1 个，可在任务队列查看）"),
    }


def _enqueue_files(files: List[Dict[str, Any]], meta_by_file: Dict[str, Any],
                   source: str, suspended: bool) -> int:
    added = 0
    base_ts = int(time.time() * 1000)
    for i, p in enumerate(files):
        if len(_THROTTLE_QUEUE) >= _THROTTLE_QUEUE_MAX:
            log.warning("TG 节流队列已达上限 %s，余下文件拒绝入队", _THROTTLE_QUEUE_MAX)
            break
        fid = p.get("fileId")
        meta = meta_by_file.get(str(fid)) if isinstance(meta_by_file, dict) else None
        if not isinstance(meta, dict):
            meta = {}
        entry = _queue_entry(p, meta, source, i + 1)
        entry["id"] = f"wait_throttle_{base_ts}_{added + 1}"
        if suspended:
            entry["suspended"] = True
            entry["error_msg"] = (f"今日 TG 请求预算已用完（{_budget_total()} 个），"
                                  f"已挂起，次日 {_clamp_int(_THROTTLE_CONFIG.get('resumeHour'), 0, 23, 0):02d}:00 自动续跑")
        else:
            entry["error_msg"] = "按随机节奏排队中，稍后自动放行"
        entry["due_at"] = 0.0
        _THROTTLE_QUEUE[entry["id"]] = entry
        added += 1
    if added:
        _throttle_queue_save()
        _tasks_cache_invalidate()
    return added


def throttle_cancel(task_id: str = "", unique_id: str = "") -> Optional[Dict[str, Any]]:
    """从节流队列移除一条（支持 task id 或 uniqueId）。返回被移除的条目。"""
    hit = None
    if task_id and task_id in _THROTTLE_QUEUE:
        hit = _THROTTLE_QUEUE.pop(task_id)
    else:
        for tid, w in list(_THROTTLE_QUEUE.items()):
            if (task_id and str(w.get("id")) == task_id) or \
               (unique_id and str(w.get("uniqueId")) == unique_id):
                hit = _THROTTLE_QUEUE.pop(tid)
                break
    if hit is not None:
        _throttle_queue_save()
        _tasks_cache_invalidate()
    return hit


def throttle_clear(predicate=None) -> int:
    """清空队列（可按谓词过滤）。返回移除条数。"""
    removed = 0
    for tid, w in list(_THROTTLE_QUEUE.items()):
        if predicate is None or predicate(w):
            _THROTTLE_QUEUE.pop(tid, None)
            removed += 1
    if removed:
        _throttle_queue_save()
        _tasks_cache_invalidate()
    return removed


def throttle_pending_rows() -> List[Dict[str, Any]]:
    """任务列表要合并的节流行（按入队时间升序）。

    每行带上「排队中的还要等多久」/「挂起的何时续跑」：任务页原本只显示
    "按随机节奏排队中"，用户不知道还要等 3 分钟还是明天，只能反复刷新。
    """
    items = sorted(_THROTTLE_QUEUE.values(), key=lambda w: float(w.get("created_at") or 0.0))
    rows = []
    resume_at = _next_resume_at()
    now = time.time()
    for w in items:
        msg = ""
        if w.get("suspended"):
            msg = _resume_hint(resume_at - now)
        else:
            gate = float(_THROTTLE_STATE.get("nextReleaseAt") or 0.0)
            if gate > now:
                msg = "下一个约 %d 秒后放行" % max(1, int(gate - now))
            else:
                msg = "即将放行"
        rows.append(_format_row(w, msg))
    return rows


def _resume_hint(seconds_left: float) -> str:
    """把「距次日续跑还有多久」渲染成人话。"""
    sec = max(0, int(seconds_left))
    hh, rem = divmod(sec, 3600)
    mm = rem // 60
    if hh >= 1:
        return f"今日预算已用完，约 {hh} 小时 {mm} 分后（次日）自动续跑"
    return f"今日预算已用完，约 {mm} 分钟后（次日）自动续跑"


def _format_row(w: Dict[str, Any], error_msg: str = "") -> Dict[str, Any]:
    from core.state import _format_throttle_task
    return _format_throttle_task(w, error_msg)


# ---------------------------------------------------------------------
# 调度循环：按随机间隔放行队列
# ---------------------------------------------------------------------
async def _release_batch(batch: List[Dict[str, Any]]) -> int:
    """把一批条目真正提交给后端；成功后出队并记账。"""
    if not batch:
        return 0
    files = [dict(w.get("payload") or {}) for w in batch]
    files = [f for f in files if f.get("fileId")]
    if not files:
        # 无有效 payload 的空壳：直接清掉，避免占队与重复报警
        for w in batch:
            _THROTTLE_QUEUE.pop(w.get("id"), None)
        _throttle_queue_save()
        return 0
    from core.backend import BACKEND
    try:
        await BACKEND.start_download_multiple({"files": files})
    except Exception as e:  # noqa: BLE001
        log.warning("TG 节流放行提交失败（%d 个，保留在队列下次重试）: %s", len(files), e)
        return 0
    for w in batch:
        _THROTTLE_QUEUE.pop(w.get("id"), None)
    _consume_budget(len(files))
    _throttle_queue_save()
    try:
        from core.backend import BACKEND as _B
        if hasattr(_B, "_cache") and isinstance(getattr(_B, "_cache", None), dict):
            _B._cache.clear()
    except Exception:  # noqa: BLE001
        pass
    _tasks_cache_invalidate()
    log.info("TG 节流：随机节奏放行 %d 个任务（今日已用 %d/%s）",
             len(files), _used_today(), _budget_total() or "不限")
    return len(files)


def _due_entries(now: float) -> List[Dict[str, Any]]:
    """按 due_at 取到期条目（FIFO）。"""
    due = []
    for w in sorted(_THROTTLE_QUEUE.values(), key=lambda x: float(x.get("created_at") or 0.0)):
        if w.get("suspended"):
            continue
        if float(w.get("due_at") or 0.0) <= now:
            due.append(w)
    return due


async def throttle_dispatch_pending(force: bool = False) -> int:
    """放行一轮到期条目。force=True 忽略随机间隔（测试/手动唤醒用）。"""
    if not _THROTTLE_CONFIG.get("enabled", True):
        return 0
    async with _throttle_lock:
        _rollover_if_needed()
        # 预算耗尽：把剩余全部标记挂起（等次日）
        if _budget_exhausted() and _THROTTLE_QUEUE:
            changed = 0
            hint = (f"今日 TG 请求预算已用完（{_budget_total()} 个），"
                    f"已挂起，次日 {_clamp_int(_THROTTLE_CONFIG.get('resumeHour'), 0, 23, 0):02d}:00 自动续跑")
            for w in _THROTTLE_QUEUE.values():
                if not w.get("suspended"):
                    w["suspended"] = True
                    w["error_msg"] = hint
                    changed += 1
            if changed:
                _throttle_queue_save()
                _tasks_cache_invalidate()
                LOG_STORE.append("WARN", f"TG 节流：今日预算用尽，{len(_THROTTLE_QUEUE)} 个任务挂起，次日自动续跑")
            return 0
        # 跨日（或过了续跑钟点）解除挂起
        if not _past_resume_point():
            return 0
        unsuspended = 0
        for w in _THROTTLE_QUEUE.values():
            if w.get("suspended"):
                w["suspended"] = False
                w["error_msg"] = "按随机节奏排队中，稍后自动放行"
                unsuspended += 1
        if unsuspended:
            _throttle_state_resume_stamp()
            _throttle_queue_save()
            _tasks_cache_invalidate()
            LOG_STORE.append("INFO", f"TG 节流：次日续跑，{unsuspended} 个挂起任务恢复排队")
            log.info("TG 节流：次日续跑，解除 %d 个挂起任务", unsuspended)

        now = time.time()
        # 节奏闸门：距上次放行不足一个随机间隔就整体跳过。
        # 必须放在这里而不是「只给队首排 due_at」—— 入队时所有条目 due_at 都是
        # 0（即到期），只推队首会让第 2 条在下一轮立刻到期，间隔形同虚设。
        gate = float(_THROTTLE_STATE.get("nextReleaseAt") or 0.0)
        if not force and gate > now:
            return 0
        due = _due_entries(now)
        if not due:
            return 0
        left = _budget_left()
        if len(due) > left:
            due = due[:max(0, left)]
        if force:
            # 手动放行：跳过随机间隔，但**不跳过并发上限** —— 用户点按钮是
            # "别等了，现在就放一批"，不是"绕过所有保护一次倒完"。
            take = due[:_concurrency_max()]
        else:
            # 单次只放 1 个：随机间隔由下一轮循环累积，避免瞬间成串
            take = due[:1]
        released = await _release_batch(take)
        if released:
            # 下一次放行至少隔一个随机间隔：这是"人类节奏"的唯一来源。
            # 记在预算状态里（而非队列条目上）—— 队列可能中途被清空/重建，
            # 而节奏应该跨这些事件连续，否则连续点击会让间隔不断重置为 0。
            _THROTTLE_STATE["nextReleaseAt"] = time.time() + _random_interval()
            _throttle_state_save()
            _throttle_queue_save()
        return released


def _throttle_state_resume_stamp() -> None:
    _THROTTLE_STATE["lastWakeAt"] = time.time()
    _throttle_state_save()


async def throttle_loop() -> None:
    """常驻后台循环：按随机间隔放行队列；预算耗尽时只做跨日巡检。"""
    log.info("TG 节流调度循环已启动（档位=%s 并发=%s~%s 间隔=%s~%ss 日预算=%s）",
             _THROTTLE_CONFIG.get("profile"), _THROTTLE_CONFIG.get("concurrencyMin"),
             _THROTTLE_CONFIG.get("concurrencyMax"), _THROTTLE_CONFIG.get("intervalMin"),
             _THROTTLE_CONFIG.get("intervalMax"), _THROTTLE_CONFIG.get("dailyBudget") or "不限")
    while True:
        try:
            if not _THROTTLE_LOOP_PAUSED:
                await throttle_dispatch_pending()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("TG 节流调度异常: %s", e)
        # 队列短时空转也无所谓：一个 5 秒的心跳成本远低于漏放行
        await asyncio.sleep(_THROTTLE_TICK)


def _ensure_loop() -> None:
    """惰性启动调度循环（无事件循环时静默跳过，由 bridge startup 兜底启动）。"""
    global _throttle_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _throttle_task is not None and not _throttle_task.done():
        return
    _throttle_task = loop.create_task(throttle_loop())


def throttle_startup() -> None:
    """bridge startup 调用：恢复队列后确保调度循环在跑。"""
    _throttle_config_load()
    _throttle_state_load()
    _throttle_queue_load()
    _rollover_if_needed()
    _ensure_loop()


def throttle_shutdown() -> None:
    global _throttle_task
    if _throttle_task is not None and not _throttle_task.done():
        _throttle_task.cancel()
    _throttle_task = None
