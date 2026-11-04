# -*- coding: utf-8 -*-
"""
core/state.py — 全局单例状态字典、线程/异步安全锁与磁盘持久化
=============================================================
本模块集中管理系统的全局运行时单例字典、队列状态与状态落盘逻辑。
"""
import os
import json
import time
import math
import posixpath
import asyncio
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Set, Tuple
import httpx
from core.config import (
    APP_ROOT_DIR, OPENLIST_URL, _WAITING_DISK_FILE, _ARCHIVE_FILE, _SUBS_FILE, _OPENLIST_FILE, _FLOOD_WAIT_FILE, _CUSTOM_NOTES_FILE, _CUSTOM_FILE_NOTES, _mask_token, _same_file_name
)
from core.logging import log

# ---------------------------------------------------------------------
# 1. 登录与风控状态
# ---------------------------------------------------------------------
_login_failures: Dict[str, List[float]] = {}

_FLOOD_WAIT_STATE: Dict[str, Any] = {
    "active": False,
    "account": "default",
    "wait_seconds": 0,
    "triggered_at": 0.0,
    "cooldown_until": 0.0,
    "reason": "",
    "suspended_tasks": {},
}
_FLOOD_WAIT_TIMER_TASK: Optional[asyncio.Task] = None

# ---------------------------------------------------------------------
# 2. 缓存与队列
# ---------------------------------------------------------------------
_TASKS_CACHE: Dict[str, Any] = {"expire": 0.0, "value": None}


def _tasks_cache_invalidate() -> None:
    """任务列表缓存统一失效入口（新任务入队/任务取消/状态变更后调用）。"""
    _TASKS_CACHE["expire"] = 0.0
    _TASKS_CACHE["value"] = None


_WAITING_DISK_TASKS: Dict[str, Dict[str, Any]] = {}
_WAITING_DISK_TASKS_MAX = 500  # 挂起队列上限：超过后拒绝新挂起入队（防无界增长）

# ---------------------------------------------------------------------
# 3. 归档与取回状态
# ---------------------------------------------------------------------
_ARCHIVE_JOBS: Dict[str, Dict[str, Any]] = {}
_ARCHIVE_MAX_JOBS = 5000
_DELETED_LOCAL_UIDS: Set[str] = set()
_QUICK_ARCHIVE_REGISTRY: Dict[str, Dict[str, Any]] = {}

_ARCHIVE_CONFIG_FILE = os.path.join(APP_ROOT_DIR, ".archive_config.json")
_ARCHIVE_CONFIG: Dict[str, Any] = {
    "autoArchive": True,
    "defaultDir": "",
    # 会话冷备在 OpenList 上的目标目录（空 = 自动推导；见 backup_service 解析优先级）
    "sessionBackupDir": "",
    # 本地会话冷备目录（空 = 自动推导：与 app 平级的 session-backups，回退到数据目录内）。
    # 用户可指定任意绝对路径，把本地快照落到独立磁盘/挂载卷上。
    "localBackupDir": "",
    "publicBaseUrl": "",
    "policy": "overwrite",
    "deleteLocal": False,
    "cleanFilename": True,
    "diskWatermarkGB": 5.0,
    "diskAutoClean": True,
    "diskHighWatermarkPercent": 85.0,
    "diskLowWatermarkPercent": 75.0,
    "stats": {
        "enqueued": 0,
        "done": 0,
        "failed": 0,
        "lastHitAt": 0.0,
    }
}

_RETRIEVE_JOBS: Dict[str, Dict[str, Any]] = {}
_RETRIEVE_FILE = os.path.join(APP_ROOT_DIR, ".retrieve_jobs.json")
_RETRIEVE_JOBS_MAX = 500

# ---------------------------------------------------------------------
# 4. 订阅规则
# ---------------------------------------------------------------------
_SUBS_MAX_RULES = 50
_SUB_RULES: Dict[str, Dict[str, Any]] = {}

# ---------------------------------------------------------------------
# 5. 通知配置
# ---------------------------------------------------------------------
_NOTIFY_CONFIG_FILE = os.path.join(APP_ROOT_DIR, ".notify_config.json")
_NOTIFY_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "channel": "both",  # "bot", "saved_messages", "both"
    "botToken": "",
    "chatId": "",
    "minFileSizeMB": 50,
    "events": {
        "downloadCompleted": True,
        "archiveSuccess": True,
        "archiveFailed": True,
        "diskWatermarkAlert": True,
    }
}
_NOTIFY_LAST_DISK_ALERT = 0.0

# ---------------------------------------------------------------------
# 6. OpenList 状态与客户端
# ---------------------------------------------------------------------
# 【必须 trust_env=False】OpenList 与 Java 后端都在本机回环地址上
# (OPENLIST_URL/TG_API_URL 默认 http://127.0.0.1:...)。httpx 默认会读环境变量里的
# ALL_PROXY/HTTP_PROXY，把这些回环请求也塞进代理：
#   1) 请求绕远路甚至失败；2) 若代理是 socks5h://（本机 clash 默认就是），
#   而 httpx 没装 socksio 依赖，会在建 client 时直接抛
#      ValueError: Unknown scheme for proxy URL URL('socks5h://127.0.0.1:7890')
#      —— 这是 import 期异常，整个应用起不来。
# 本机 /root/.bashrc 里 clashctl 的 watch_proxy 会在每个登录 shell 自动导出该变量，
# 所以「在交互 shell 里跑测试/服务」和「nohup/systemd 直接起」的行为会完全不同。
# 回环流量永远不该走代理，这里显式关掉环境变量解析。
_openlist_client = httpx.AsyncClient(
    base_url=OPENLIST_URL,
    timeout=httpx.Timeout(10.0),
    follow_redirects=False,
    trust_env=False,
)
_openlist_upload_client = httpx.AsyncClient(
    base_url=OPENLIST_URL,
    timeout=httpx.Timeout(connect=10.0, read=3600.0, write=3600.0, pool=10.0),
    follow_redirects=False,
    trust_env=False,
)
_OPENLIST: Dict[str, Any] = {}

# ---------------------------------------------------------------------
# 7. 会话备份与告警状态
# ---------------------------------------------------------------------
_SESSION_BACKUP_STATUS: Dict[str, Any] = {}

_ALERT_EVENT_LIMIT = 8
_ALERTS_MAX = 50
_alert_state: Dict[str, Any] = {
    "events": [],
    "read_ts": 0.0,
    "seen": {},
    "baselined": False,
}

# ---------------------------------------------------------------------
# 8. 异步并发锁与信号量
# ---------------------------------------------------------------------
_archive_lock = asyncio.Lock()
# 归档上传并发上限。
# 历史值硬编码为 2，但线上实测并发远不止 2：归档 sweep 每隔 30s 扫一轮，
# 每轮会把所有「已下载待归档」的文件一次性 _archive_enqueue 出去（实测单轮
# 35 个），每个 job 立刻 create_task 起协程，只是排队等这把信号量而已。
# 信号量本身确实限住了「同时真在传」的数量，但它掩盖了另一层问题：
# 排队中的 job 状态已经是 uploading（worker 在信号量外设置），前端看到的
# 「归档中」数量 = 已入队数量而非真实并发数，用户观感就是「并发过头」。
# 现在把并发放进配置（BRIDGE_ARCHIVE_CONCURRENCY，默认 2），并让排队中的
# job 保持 queued 状态（见 _archive_worker），不再冒充 uploading。
_ARCHIVE_CONCURRENCY_DEFAULT = 2
_ARCHIVE_CONCURRENCY = max(1, int(os.environ.get(
    "BRIDGE_ARCHIVE_CONCURRENCY", str(_ARCHIVE_CONCURRENCY_DEFAULT)) or _ARCHIVE_CONCURRENCY_DEFAULT))
_archive_sem = asyncio.Semaphore(_ARCHIVE_CONCURRENCY)
# 同一 remote_path 的上传串行锁（见 _archive_upload_lock）。
_ARCHIVE_UPLOAD_LOCKS: Dict[str, asyncio.Lock] = {}
_subs_lock = asyncio.Lock()
_disk_lock = asyncio.Lock()
_openlist_lock = asyncio.Lock()

# ---------------------------------------------------------------------
# 9. 状态持久化与管理函数
# ---------------------------------------------------------------------
def _flood_wait_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps(_FLOOD_WAIT_STATE, ensure_ascii=False, indent=2).encode("utf-8")
        fd = os.open(_FLOOD_WAIT_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("保存 flood_wait 状态失败: %s", e)


def _flood_wait_load() -> None:
    try:
        if os.path.exists(_FLOOD_WAIT_FILE):
            with open(_FLOOD_WAIT_FILE, "r", encoding="utf-8") as f:
                d = json.loads(f.read())
            if isinstance(d, dict):
                _FLOOD_WAIT_STATE.clear()
                _FLOOD_WAIT_STATE.update(d)
                now = time.time()
                until = float(_FLOOD_WAIT_STATE.get("cooldown_until") or 0.0)
                if now >= until:
                    _FLOOD_WAIT_STATE["active"] = False
                    _FLOOD_WAIT_STATE["wait_seconds"] = 0
                else:
                    _FLOOD_WAIT_STATE["active"] = True
                    log.info("已恢复 Telegram FloodWait 冷却状态: 剩余 %d 秒", int(until - now))
                    # 冷却中被重启时，必须把冷却定时器重新拉起来，否则 active=True
                    # 却没有任何循环在等待结束 → 调度永久挂起（要等下次显式触发才恢复）。
                    # 导入期调用此函数时还没有事件循环，_ensure_flood_wait_timer 会
                    # 静默跳过，由 bridge_server startup 拿到 loop 后再补一次。
                    _ensure_flood_wait_timer()
    except Exception as e:  # noqa: BLE001
        log.warning("恢复 flood_wait 状态异常: %s", e)


def _trigger_flood_wait(account: str = "default", wait_seconds: int = 30, reason: str = "") -> float:
    """触发或延长 FloodWait 风控冷却。并发多次触发时取最大到期时间，防击穿。"""
    now = time.time()
    try:
        w_val = int(wait_seconds if wait_seconds is not None else 30)
    except (ValueError, TypeError):
        w_val = 30
    wait_seconds = max(1, min(w_val, 86400))
    current_until = float(_FLOOD_WAIT_STATE.get("cooldown_until") or 0.0)
    new_until = max(current_until, now + wait_seconds)
    actual_wait = int(new_until - now)

    _FLOOD_WAIT_STATE["active"] = True
    _FLOOD_WAIT_STATE["account"] = account or "default"
    _FLOOD_WAIT_STATE["wait_seconds"] = actual_wait
    _FLOOD_WAIT_STATE["triggered_at"] = now
    _FLOOD_WAIT_STATE["cooldown_until"] = new_until
    _FLOOD_WAIT_STATE["reason"] = reason or f"FLOOD_WAIT_{actual_wait}"
    _flood_wait_save()

    log.warning("Telegram 账号 [%s] 触发风控冷却: %s, 冷却至 %s（共 %d 秒）",
                account, _FLOOD_WAIT_STATE["reason"],
                time.strftime("%H:%M:%S", time.localtime(new_until)), actual_wait)

    _ensure_flood_wait_timer()

    _tasks_cache_invalidate()
    return new_until


def _is_flood_wait_active(account: str = "default") -> bool:
    if not _FLOOD_WAIT_STATE.get("active"):
        return False
    now = time.time()
    until = float(_FLOOD_WAIT_STATE.get("cooldown_until") or 0.0)
    if now >= until:
        _FLOOD_WAIT_STATE["active"] = False
        _FLOOD_WAIT_STATE["wait_seconds"] = 0
        return False
    return True


def _get_flood_wait_status(account: str = "default") -> Dict[str, Any]:
    active = _is_flood_wait_active(account)
    now = time.time()
    until = float(_FLOOD_WAIT_STATE.get("cooldown_until") or 0.0)
    rem = max(0, int(math.ceil(until - now))) if active else 0
    total_wait = int(_FLOOD_WAIT_STATE.get("wait_seconds") or 0)
    reason = str(_FLOOD_WAIT_STATE.get("reason") or "")
    suspended = _FLOOD_WAIT_STATE.get("suspended_tasks") or {}
    msg = (f"Telegram 账号触发风控保护 ({reason or 'FLOOD_WAIT'})，已安全挂起调度，预计剩余 {rem} 秒自动恢复"
           if active else "Telegram 账号状态正常，无限流冷却")
    return {
        "isCooling": active,
        "account": _FLOOD_WAIT_STATE.get("account") or account,
        "remainingSeconds": rem,
        "totalWaitSeconds": total_wait,
        "cooldownUntil": until,
        "reason": reason,
        "suspendedTasksCount": len(suspended),
        "message": msg,
    }


def _reset_flood_wait(account: str = "default") -> None:
    """强制重置 FloodWait 状态"""
    _FLOOD_WAIT_STATE["active"] = False
    _FLOOD_WAIT_STATE["wait_seconds"] = 0
    _FLOOD_WAIT_STATE["cooldown_until"] = 0.0
    _FLOOD_WAIT_STATE["reason"] = ""
    _FLOOD_WAIT_STATE["suspended_tasks"] = {}
    _flood_wait_save()
    _tasks_cache_invalidate()
    log.info("管理员已强制清除 Telegram FloodWait 冷却状态")


# ---------------------------------------------------------------------
# 8b. 在线播放流量统计（累计字节）
# ---------------------------------------------------------------------
# 口径：云端在线播放经 bridge 代理的字节数 = 「从网盘下载」的流量；
# 按「上传下载对等」记账规则，同时计入累计下载与累计上传两张 KPI
# （VPS 视角：网盘 → VPS 是下载，VPS → 浏览器是上传，天然对等）。
# 持久化到 .stream_stats.json，bridge 重启不清零。
_STREAM_STATS_FILE = os.path.join(APP_ROOT_DIR, ".stream_stats.json")
_STREAM_STATS: Dict[str, Any] = {"total_bytes": 0, "updated_at": 0.0}


def _stream_stats_add(n_bytes: int) -> None:
    """累加本次在线播放经代理流出的字节数并落盘（节流：≥1MB 或首次写盘）。"""
    try:
        _STREAM_STATS["total_bytes"] = int(_STREAM_STATS.get("total_bytes") or 0) + int(n_bytes)
        _STREAM_STATS["updated_at"] = time.time()
        _stream_stats_save()
    except Exception as e:  # noqa: BLE001
        log.debug("在线播放流量记账失败: %s", e)


def _stream_stats_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps(_STREAM_STATS, ensure_ascii=False).encode("utf-8")
        fd = os.open(_STREAM_STATS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("保存在线播放流量统计失败: %s", e)


def _stream_stats_load() -> None:
    try:
        if os.path.exists(_STREAM_STATS_FILE):
            with open(_STREAM_STATS_FILE, "r", encoding="utf-8") as f:
                d = json.loads(f.read())
            if isinstance(d, dict):
                _STREAM_STATS.clear()
                _STREAM_STATS.update(d)
    except Exception as e:  # noqa: BLE001
        log.warning("恢复在线播放流量统计失败: %s", e)


# ---------------------------------------------------------------------
# 8c. 网盘浏览位置记忆（上次浏览的目录 + 常去网盘排行）
# ---------------------------------------------------------------------
# 用户诉求：刷新/重新进入「浏览」标签后，还要能回到上次点击的那个文件夹。
# 为什么存服务端而不是 localStorage：侧栏「浏览」链接要能**服务端重定向**到
# 上次的目录 —— 只有 bridge 知道这个位置，localStorage 做不到（首屏就会
# 先渲染根页再跳，且中键/新标签页/换浏览器全部失效）。
# recent 是访问计数排行（用于根页「常去网盘」chips 排序），上限 12 条防膨胀。
_BROWSE_MEMORY_FILE = os.path.join(APP_ROOT_DIR, ".browse_memory.json")
_BROWSE_MEMORY: Dict[str, Any] = {"last_path": "", "updated_at": 0.0, "recent": {}}
_BROWSE_MEMORY_MAX_RECENT = 12


def _browse_memory_remember(path: str) -> None:
    """记录一次网盘目录访问：更新 last_path 并累加该路径所属网盘的访问计数。"""
    try:
        norm = posixpath.normpath("/" + str(path or "").strip().lstrip("/"))
        if norm in ("", ".", "/"):
            return
        _BROWSE_MEMORY["last_path"] = norm
        _BROWSE_MEMORY["updated_at"] = time.time()
        drive = "/" + norm.strip("/").split("/")[0]
        recent = _BROWSE_MEMORY.setdefault("recent", {})
        if not isinstance(recent, dict):
            recent = {}
            _BROWSE_MEMORY["recent"] = recent
        recent[drive] = int(recent.get(drive) or 0) + 1
        # 排行上限：按访问次数保留前 N，避免网盘增删后残留无限增长
        if len(recent) > _BROWSE_MEMORY_MAX_RECENT:
            keep = sorted(recent.items(), key=lambda kv: -int(kv[1]))[:_BROWSE_MEMORY_MAX_RECENT]
            _BROWSE_MEMORY["recent"] = dict(keep)
        _browse_memory_save()
    except Exception as e:  # noqa: BLE001
        log.debug("记录网盘浏览位置失败: %s", e)


def _browse_memory_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps(_BROWSE_MEMORY, ensure_ascii=False).encode("utf-8")
        fd = os.open(_BROWSE_MEMORY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("保存网盘浏览记忆失败: %s", e)


def _browse_memory_load() -> None:
    try:
        if os.path.exists(_BROWSE_MEMORY_FILE):
            with open(_BROWSE_MEMORY_FILE, "r", encoding="utf-8") as f:
                d = json.loads(f.read())
            if isinstance(d, dict):
                _BROWSE_MEMORY.clear()
                _BROWSE_MEMORY.update({
                    "last_path": str(d.get("last_path") or ""),
                    "updated_at": float(d.get("updated_at") or 0.0),
                    "recent": d.get("recent") if isinstance(d.get("recent"), dict) else {},
                })
    except Exception as e:  # noqa: BLE001
        log.warning("恢复网盘浏览记忆失败: %s", e)


# ---------------------------------------------------------------------
# 8d. TG 请求节流（随机节奏 + 每日预算 + 跨日自动续跑）
# ---------------------------------------------------------------------
# 用户诉求：账号曾在批量下载时被 Telegram 风控，"限制了 5 个并发但它能一直下"。
# 根因不是并发数——后端 autoDownloadLimit 只限制同时在跑的任务数，第 1 个一结束
# 第 2 个立刻顶上，请求速率恒为 0 间隔的满负荷，这在 TG 反滥用看来与脚本无异。
# 真正要控的是【请求速率随时间的形状】：
#   1. 文件之间插入随机间隔（6~25 秒），不呈现机械等距；
#   2. 首批最多 concurrencyMax 个并发下，其余排队按间隔放行；
#   3. 每日预算封顶（平衡档 200 个），用完即挂起，次日零点自动续跑；
#   4. 缩略图补图是第二大 TG 请求源（翻云端归档每张卡片一次 GetMessage），
#      同样计入预算，超预算时直接跳过补图而不是阻塞页面。
# 配置/预算/队列三者分别落盘（.throttle_config / .throttle_state /
# .throttle_queue），与 waiting_disk 队列同一套"重启不丢"语义。
_THROTTLE_CONFIG_FILE = os.path.join(APP_ROOT_DIR, ".throttle_config.json")
_THROTTLE_STATE_FILE = os.path.join(APP_ROOT_DIR, ".throttle_state.json")
_THROTTLE_QUEUE_FILE = os.path.join(APP_ROOT_DIR, ".throttle_queue.json")

# 四档预设。用户拍板：默认用「平衡档」（并发 2~4、间隔 6~25 秒、日预算 200）。
# budget=0 表示不设上限（「只加随机间隔」档）。
_THROTTLE_PROFILES: Dict[str, Dict[str, Any]] = {
    "conservative": {"label": "保守档", "concurrencyMin": 1, "concurrencyMax": 2,
                     "intervalMin": 30.0, "intervalMax": 120.0, "dailyBudget": 40,
                     "desc": "并发 1~2、间隔 30~120 秒、日预算 40 个。适合新号或已被警告过的号"},
    "robust": {"label": "稳健档", "concurrencyMin": 1, "concurrencyMax": 3,
               "intervalMin": 15.0, "intervalMax": 60.0, "dailyBudget": 80,
               "desc": "并发 1~3、间隔 15~60 秒、日预算 80 个。最保守的安全节奏"},
    "balanced": {"label": "平衡档", "concurrencyMin": 2, "concurrencyMax": 4,
                 "intervalMin": 6.0, "intervalMax": 25.0, "dailyBudget": 200,
                 "desc": "并发 2~4、间隔 6~25 秒、日预算 200 个。速度快且保留人类节奏特征"},
    "unlimited": {"label": "只加随机间隔", "concurrencyMin": 2, "concurrencyMax": 4,
                  "intervalMin": 6.0, "intervalMax": 25.0, "dailyBudget": 0,
                  "desc": "不限制每日总量，只把零间隔改成随机间隔"},
}
_THROTTLE_DEFAULT_PROFILE = "balanced"

_THROTTLE_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "profile": _THROTTLE_DEFAULT_PROFILE,
    # 覆盖范围：默认四项全开（用户明确要求把「链接/直投/监听」「缩略图补图」都纳入）。
    # retrieve（网盘取回）走 OpenList 不碰 TG，恒不纳入。
    "scope": {
        "browse": True,        # 浏览页勾选下载
        "submit": True,        # /submit 链接提交
        "api": True,           # /api/tg/quick-download 一键直投
        "watch": True,         # 频道监听自动入队
        "thumb": True,         # 缩略图补图（GetMessage）
    },
    # 生效参数（切换档位时由服务层按预设回填，用户也可单独微调）
    "concurrencyMin": 2,
    "concurrencyMax": 4,
    "intervalMin": 6.0,
    "intervalMax": 25.0,
    "dailyBudget": 200,
    # 预算耗尽行为：挂起到次日自动续跑（用户指定）。仅支持该语义，
    # 字段留存以便未来扩展 "reject"/"slowdown"。
    "budgetBehavior": "suspend_next_day",
    # 次日续跑的起始钟点（本地时间，小时）。0 = 零点一过立刻续跑。
    "resumeHour": 0,
}

_THROTTLE_STATE: Dict[str, Any] = {
    "date": "",          # 预算归属日 YYYY-MM-DD
    "used": 0,           # 当日已放行的 TG 下载文件数（含缩略图补图次数单独计）
    "thumbUsed": 0,      # 当日缩略图补图消耗（计入 used）
    "submitted": 0,      # 当日累计受理文件数
    "lastSubmitAt": 0.0, # 上次放行时间戳（间隔控制基准）
    "lastWakeAt": 0.0,   # 上次跨日续跑时间
    "nextReleaseAt": 0.0,  # 下一次允许放行的时间戳（随机间隔闸门）
}

_THROTTLE_QUEUE: Dict[str, Dict[str, Any]] = {}
_THROTTLE_QUEUE_MAX = 2000


def _throttle_today() -> str:
    """预算归属日（本地时区 YYYY-MM-DD）。"""
    return time.strftime("%Y-%m-%d", time.localtime())


def _throttle_config_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps(_THROTTLE_CONFIG, ensure_ascii=False).encode("utf-8")
        tmp = f"{_THROTTLE_CONFIG_FILE}.tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.replace(tmp, _THROTTLE_CONFIG_FILE)
    except Exception as e:  # noqa: BLE001
        log.warning("保存 TG 节流配置失败: %s", e)


def _throttle_config_load() -> None:
    try:
        if not os.path.exists(_THROTTLE_CONFIG_FILE):
            return
        with open(_THROTTLE_CONFIG_FILE, "r", encoding="utf-8") as f:
            d = json.loads(f.read() or "{}")
        if not isinstance(d, dict):
            return
        if "enabled" in d:
            _THROTTLE_CONFIG["enabled"] = bool(d["enabled"])
        prof = str(d.get("profile") or "").strip()
        if prof in _THROTTLE_PROFILES:
            _THROTTLE_CONFIG["profile"] = prof
        scope = d.get("scope")
        if isinstance(scope, dict):
            for k in list(_THROTTLE_CONFIG["scope"].keys()):
                if k in scope:
                    _THROTTLE_CONFIG["scope"][k] = bool(scope[k])
        for k in ("concurrencyMin", "concurrencyMax", "dailyBudget"):
            if k in d:
                try:
                    _THROTTLE_CONFIG[k] = int(d[k])
                except (TypeError, ValueError):
                    pass
        for k in ("intervalMin", "intervalMax"):
            if k in d:
                try:
                    _THROTTLE_CONFIG[k] = float(d[k])
                except (TypeError, ValueError):
                    pass
        if d.get("budgetBehavior"):
            _THROTTLE_CONFIG["budgetBehavior"] = str(d["budgetBehavior"])
        if "resumeHour" in d:
            try:
                _THROTTLE_CONFIG["resumeHour"] = max(0, min(23, int(d["resumeHour"])))
            except (TypeError, ValueError):
                pass
    except Exception as e:  # noqa: BLE001
        log.warning("恢复 TG 节流配置失败: %s", e)


def _throttle_state_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps(_THROTTLE_STATE, ensure_ascii=False).encode("utf-8")
        fd = os.open(_THROTTLE_STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("保存 TG 节流预算失败: %s", e)


def _throttle_state_load() -> None:
    try:
        if not os.path.exists(_THROTTLE_STATE_FILE):
            return
        with open(_THROTTLE_STATE_FILE, "r", encoding="utf-8") as f:
            d = json.loads(f.read() or "{}")
        if isinstance(d, dict):
            _THROTTLE_STATE.clear()
            _THROTTLE_STATE.update({
                "date": str(d.get("date") or ""),
                "used": int(d.get("used") or 0),
                "thumbUsed": int(d.get("thumbUsed") or 0),
                "submitted": int(d.get("submitted") or 0),
                "lastSubmitAt": float(d.get("lastSubmitAt") or 0.0),
                "lastWakeAt": float(d.get("lastWakeAt") or 0.0),
                "nextReleaseAt": float(d.get("nextReleaseAt") or 0.0),
            })
    except Exception as e:  # noqa: BLE001
        log.warning("恢复 TG 节流预算失败: %s", e)


def _throttle_queue_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps({"tasks": _THROTTLE_QUEUE}, ensure_ascii=False).encode("utf-8")
        tmp = f"{_THROTTLE_QUEUE_FILE}.tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.replace(tmp, _THROTTLE_QUEUE_FILE)
    except Exception as e:  # noqa: BLE001
        log.warning("保存 TG 节流队列失败: %s", e)


def _throttle_queue_load() -> None:
    try:
        if not os.path.exists(_THROTTLE_QUEUE_FILE):
            return
        with open(_THROTTLE_QUEUE_FILE, "r", encoding="utf-8") as f:
            d = json.loads(f.read() or "{}")
        if isinstance(d, dict) and isinstance(d.get("tasks"), dict):
            _THROTTLE_QUEUE.clear()
            _THROTTLE_QUEUE.update(d["tasks"])
            if _THROTTLE_QUEUE:
                log.info("已恢复 TG 节流队列: %d 条待放行任务", len(_THROTTLE_QUEUE))
    except Exception as e:  # noqa: BLE001
        log.warning("恢复 TG 节流队列失败: %s", e)


def _format_throttle_task(w: Dict[str, Any], error_msg: str = "") -> Dict[str, Any]:
    """把节流队列条目渲染成任务列表行（status=waiting_throttle）。

    与 waiting_disk 同构：任务页可见、可取消、重启不丢。状态独立命名，
    不再借用 waiting_disk —— 否则用户看到「磁盘挂起」会去清磁盘，而真实
    原因是当天 TG 请求预算已用完。
    """
    now = float(w.get("created_at") or time.time())
    t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    return {
        "id": w["id"],
        "filename": w.get("filename", "未知文件"),
        "size": w.get("size_str", "未知大小"),
        "size_bytes": w.get("size", 0),
        "source": w.get("source", "TG 节流队列"),
        "source_url": "",
        "msg_id": w.get("msg_id", "—"),
        "status": "waiting_throttle",
        "progress": 0,
        "loaded": "0 B",
        "time": t_str,
        "local_path": "—（等待随机节奏放行，不占磁盘）",
        "error_msg": error_msg or w.get("error_msg") or "按随机节奏排队中，稍后自动放行",
        "stages": [
            {"name": "等待放行节奏", "done": "yes", "time": t_str, "dur": "排队中"},
            {"name": "下载媒体", "done": "no", "time": "—", "dur": "—"},
            {"name": "本地落盘", "done": "no", "time": "—", "dur": "—"},
        ],
        "_unique_id": w.get("uniqueId") or w.get("id", ""),
        "_telegram_id": None,
        "_file_id": None,
    }


async def _wake_flood_wait_tasks() -> int:
    suspended = dict(_FLOOD_WAIT_STATE.get("suspended_tasks", {}))
    _FLOOD_WAIT_STATE["suspended_tasks"].clear()
    _flood_wait_save()
    _tasks_cache_invalidate()
    woken = len(suspended)
    if woken > 0:
        log.info("已自动唤醒 %d 条 FloodWait 挂起任务", woken)
    return woken


async def _flood_wait_timer_loop() -> None:
    global _FLOOD_WAIT_TIMER_TASK
    try:
        while _is_flood_wait_active():
            await asyncio.sleep(1.0)
        _FLOOD_WAIT_STATE["active"] = False
        _FLOOD_WAIT_STATE["wait_seconds"] = 0
        _flood_wait_save()
        await _wake_flood_wait_tasks()
        log.info("FloodWait 冷却结束，已自动恢复调度并续跑")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.warning("FloodWait 定时器循环异常: %s", e)
    finally:
        _FLOOD_WAIT_TIMER_TASK = None


def _ensure_flood_wait_timer() -> None:
    """确保 FloodWait 冷却定时器在跑（幂等，可在导入期/startup 调用）。

    这是 FloodWait 定时器的**唯一入口**。历史上 bridge_server 也自己
    create_task(_flood_wait_timer_loop())，两个循环并发跑同一份状态：各自
    清空挂起队列、各自把 active 清零，shutdown 又只 cancel 前者 —— 挂起任务
    会被不确定地清空。现在只留 core.state 这一份，由 _trigger_flood_wait 与
    startup 共同保证存在。
    """
    global _FLOOD_WAIT_TIMER_TASK
    if _FLOOD_WAIT_STATE.get("active") and (_FLOOD_WAIT_TIMER_TASK is None or _FLOOD_WAIT_TIMER_TASK.done()):
        try:
            loop = asyncio.get_running_loop()
            _FLOOD_WAIT_TIMER_TASK = loop.create_task(_flood_wait_timer_loop())
        except RuntimeError:
            # 导入期还没有事件循环：由 bridge_server startup 再调一次补上
            pass


def _format_flood_wait_task(w: Dict[str, Any], rem_sec: int) -> Dict[str, Any]:
    now = float(w.get("created_at") or time.time())
    t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    reason = _FLOOD_WAIT_STATE.get("reason") or "FLOOD_WAIT"
    return {
        "id": w.get("id") or "flood-wait-task",
        "filename": w.get("filename", "未知文件"),
        "size": w.get("size_str", "未知大小"),
        "size_bytes": w.get("size", 0),
        "source": w.get("source", "风控冷却挂起队列"),
        "source_url": "",
        "msg_id": w.get("msg_id", "—"),
        "status": "waiting_disk",
        "progress": 0,
        "loaded": "0 B",
        "time": t_str,
        "local_path": "—（Telegram 风控保护中，自动倒计时续跑）",
        "error_msg": f"触发 Telegram 风控限流 ({reason})，安全冷却中（剩余 {rem_sec} 秒）",
        "stages": [
            {"name": "风控冷却", "done": "yes", "time": t_str, "dur": f"剩余 {rem_sec}s"},
            {"name": "下载媒体", "done": "no", "time": "—", "dur": "—"},
            {"name": "本地落盘", "done": "no", "time": "—", "dur": "—"},
        ],
        "_unique_id": w.get("uniqueId", ""),
        "_telegram_id": None,
        "_file_id": None,
    }


def _waiting_disk_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps({"tasks": _WAITING_DISK_TASKS}, ensure_ascii=False, indent=2).encode("utf-8")
        fd = os.open(_WAITING_DISK_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("保存 waiting_disk 挂起队列失败: %s", e)


def _waiting_disk_load() -> None:
    try:
        if os.path.exists(_WAITING_DISK_FILE):
            with open(_WAITING_DISK_FILE, "r", encoding="utf-8") as f:
                d = json.loads(f.read())
            if isinstance(d, dict) and isinstance(d.get("tasks"), dict):
                _WAITING_DISK_TASKS.clear()
                _WAITING_DISK_TASKS.update(d["tasks"])
                log.info("已恢复 waiting_disk 磁盘挂起队列: %d 条任务", len(_WAITING_DISK_TASKS))
    except Exception as e:  # noqa: BLE001
        log.warning("恢复 waiting_disk 队列异常: %s", e)


def _format_waiting_disk_task(w: Dict[str, Any]) -> Dict[str, Any]:
    now = float(w.get("created_at") or time.time())
    t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    return {
        "id": w["id"],
        "filename": w.get("filename", "未知文件"),
        "size": w.get("size_str", "未知大小"),
        "size_bytes": w.get("size", 0),
        "source": w.get("source", "磁盘挂起队列"),
        "source_url": "",
        "msg_id": w.get("msg_id", "—"),
        "status": "waiting_disk",
        "progress": 0,
        "loaded": "0 B",
        "time": t_str,
        "local_path": "—（磁盘高水位保护中，等待空间释放恢复）",
        "error_msg": w.get("error_msg", "本地磁盘占用率过高，已安全挂起排队"),
        "stages": [
            {"name": "等待磁盘空间", "done": "yes", "time": t_str, "dur": "挂起中"},
            {"name": "下载媒体", "done": "no", "time": "—", "dur": "—"},
            {"name": "本地落盘", "done": "no", "time": "—", "dur": "—"},
        ],
        "_unique_id": w.get("uniqueId", ""),
        "_telegram_id": None,
        "_file_id": None,
    }



def _archive_load() -> None:
    """启动时恢复归档日志；仍在 queued/uploading 的标记 failed 可重试。"""
    candidates = [_ARCHIVE_FILE]
    alt_file = os.path.join(APP_ROOT_DIR, "archive_jobs.json")
    if alt_file != _ARCHIVE_FILE and os.path.exists(alt_file):
        candidates.append(alt_file)

    for cf in candidates:
        try:
            with open(cf, "rb") as f:
                d = json.loads(f.read().decode("utf-8"))
            jobs = d.get("jobs") if isinstance(d, dict) else None
            if isinstance(jobs, dict):
                for k, v in jobs.items():
                    if isinstance(v, dict) and v.get("id"):
                        if str(k) not in _ARCHIVE_JOBS:
                            _ARCHIVE_JOBS[str(k)] = v
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("归档日志恢复失败 (%s): %s", cf, e)

    # 重启恢复要区分「真的传了一半」和「只是还在排队」：
    #   旧实现把 queued 与 uploading 一律写 failed，线上实测 53 条告警里 45 条
    #   全是 progress==0 的排队任务——它们一个字节都没传，却永久挂在「归档失败」
    #   列表里要求用户手动重试，而真正被中断的上传是 0 条。
    #   现在：queued(progress==0) 原样保留排队并打 resume_pending 标记，
    #   由 _startup 拿到事件循环后自动重新入队；只有真的在上传中的才判失败。
    queued_n = 0
    interrupted = 0
    for j in _ARCHIVE_JOBS.values():
        st = j.get("state")
        if st == "queued" and not int(j.get("progress") or 0):
            j["resume_pending"] = True
            queued_n += 1
        elif st in ("queued", "uploading"):
            j["state"] = "failed"
            j["error"] = "上传被中断（bridge 重启），可重试"
            j["updated_at"] = time.time()
            interrupted += 1
    if queued_n:
        log.info("已恢复 %d 条排队中的归档任务，将在启动后自动续跑", queued_n)
    if _ARCHIVE_JOBS:
        log.info("已恢复归档日志（%d 条任务，%d 条中断标记失败）",
                 len(_ARCHIVE_JOBS), interrupted)


def _archive_save() -> None:
    try:
        if len(_ARCHIVE_JOBS) > _ARCHIVE_MAX_JOBS:
            finished = sorted(
                (j for j in _ARCHIVE_JOBS.values()
                 if j.get("state") in ("done", "failed", "cancelled")),
                key=lambda j: j.get("created_at") or 0.0)
            drop = len(_ARCHIVE_JOBS) - _ARCHIVE_MAX_JOBS
            # 表键未必等于 job["id"]（跨进程恢复/外部写入时可能不一致）：
            # 老代码 pop(j.get("id")) 在两者不等时删不掉任何东西，实测上限设 10
            # 但写入 20 条 key≠id 时 20 条全部保留，内存/落盘上限完全失效。
            # 这里按「对象身份」回查真实表键，保证删得掉。
            doomed = finished[:drop]
            # 极端情况：待删数量超过终态任务数（例如全部仍是 queued/uploading）。
            # 老代码此时完全不裁剪，表可以无限增长。按 created_at 从最旧的补足。
            if len(doomed) < drop:
                chosen = {id(j) for j in doomed}
                for j in sorted(_ARCHIVE_JOBS.values(),
                                key=lambda x: x.get("created_at") or 0.0):
                    if len(doomed) >= drop:
                        break
                    if id(j) not in chosen:
                        doomed.append(j)
                        chosen.add(id(j))
            if doomed:
                doomed_ids = {id(j) for j in doomed}
                for k, j in list(_ARCHIVE_JOBS.items()):
                    if id(j) in doomed_ids:
                        _ARCHIVE_JOBS.pop(k, None)
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps({"jobs": _ARCHIVE_JOBS}).encode("utf-8")
        fd = os.open(_ARCHIVE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    except Exception as e:  # noqa: BLE001
        log.warning("归档日志持久化失败: %s", e)


def _archive_config_load() -> None:
    try:
        with open(_ARCHIVE_CONFIG_FILE, "rb") as f:
            d = json.loads(f.read().decode("utf-8"))
        if isinstance(d, dict):
            if "autoArchive" in d:
                _ARCHIVE_CONFIG["autoArchive"] = bool(d["autoArchive"])
            if "defaultDir" in d:
                _ARCHIVE_CONFIG["defaultDir"] = str(d["defaultDir"] or "")
            if "sessionBackupDir" in d:
                _ARCHIVE_CONFIG["sessionBackupDir"] = str(d["sessionBackupDir"] or "").strip()
            if "localBackupDir" in d:
                _ARCHIVE_CONFIG["localBackupDir"] = str(d["localBackupDir"] or "").strip()
            if "publicBaseUrl" in d or "public_base_url" in d:
                _ARCHIVE_CONFIG["publicBaseUrl"] = str(d.get("publicBaseUrl") or d.get("public_base_url") or "").strip().rstrip("/")
            if "policy" in d:
                _ARCHIVE_CONFIG["policy"] = "skip" if str(d["policy"]).lower() == "skip" else "overwrite"
            if "deleteLocal" in d:
                _ARCHIVE_CONFIG["deleteLocal"] = bool(d["deleteLocal"])
            if "cleanFilename" in d:
                _ARCHIVE_CONFIG["cleanFilename"] = bool(d["cleanFilename"])
            if "diskWatermarkGB" in d:
                _ARCHIVE_CONFIG["diskWatermarkGB"] = float(d["diskWatermarkGB"] or 5.0)
            if "diskAutoClean" in d:
                _ARCHIVE_CONFIG["diskAutoClean"] = bool(d["diskAutoClean"])
            if "diskHighWatermarkPercent" in d:
                _ARCHIVE_CONFIG["diskHighWatermarkPercent"] = float(d["diskHighWatermarkPercent"] or 85.0)
            if "diskLowWatermarkPercent" in d:
                _ARCHIVE_CONFIG["diskLowWatermarkPercent"] = float(d["diskLowWatermarkPercent"] or 75.0)
            if isinstance(d.get("stats"), dict):
                _ARCHIVE_CONFIG["stats"] = d["stats"]
        if _ARCHIVE_CONFIG.get("defaultDir"):
            log.info("已恢复默认归档配置: 目录=%s, 自动归档=%s",
                     _ARCHIVE_CONFIG["defaultDir"], _ARCHIVE_CONFIG["autoArchive"])
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("默认归档配置恢复失败: %s", e)


def _archive_config_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps(_ARCHIVE_CONFIG, ensure_ascii=False).encode("utf-8")
        # tmp + os.replace 原子写：旧代码 O_TRUNC 直写，写入中途崩溃会留下
        # 被截断的 JSON；且归档 worker 里高频调用，并发写会丢更新。
        tmp = f"{_ARCHIVE_CONFIG_FILE}.tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.replace(tmp, _ARCHIVE_CONFIG_FILE)
    except Exception as e:  # noqa: BLE001
        log.warning("默认归档配置持久化失败: %s", e)


def _archive_config_public() -> Dict[str, Any]:
    stats = _ARCHIVE_CONFIG.get("stats") if isinstance(_ARCHIVE_CONFIG.get("stats"), dict) else {}
    return {
        "autoArchive": bool(_ARCHIVE_CONFIG.get("autoArchive")),
        "defaultDir": str(_ARCHIVE_CONFIG.get("defaultDir") or ""),
        "sessionBackupDir": str(_ARCHIVE_CONFIG.get("sessionBackupDir") or ""),
        "localBackupDir": str(_ARCHIVE_CONFIG.get("localBackupDir") or ""),
        "publicBaseUrl": str(_ARCHIVE_CONFIG.get("publicBaseUrl") or ""),
        "policy": str(_ARCHIVE_CONFIG.get("policy") or "overwrite"),
        "deleteLocal": bool(_ARCHIVE_CONFIG.get("deleteLocal")),
        "cleanFilename": bool(_ARCHIVE_CONFIG.get("cleanFilename", True)),
        "diskWatermarkGB": float(_ARCHIVE_CONFIG.get("diskWatermarkGB") or 5.0),
        "diskAutoClean": bool(_ARCHIVE_CONFIG.get("diskAutoClean", True)),
        "diskHighWatermarkPercent": float(_ARCHIVE_CONFIG.get("diskHighWatermarkPercent") or 85.0),
        "diskLowWatermarkPercent": float(_ARCHIVE_CONFIG.get("diskLowWatermarkPercent") or 75.0),
        # 上传并发与单轮入队上限（只读展示：真实并发由进程启动时的信号量决定，
        # 改它需要设环境变量后重启，避免运行期改信号量导致正在上传的任务失去约束）。
        "concurrency": int(_ARCHIVE_CONCURRENCY),
        "sweepBatch": int(_ARCHIVE_CONFIG.get("sweepBatch") or 0) or None,
        "stats": {
            "enqueued": int(stats.get("enqueued") or 0),
            "done": int(stats.get("done") or 0),
            "failed": int(stats.get("failed") or 0),
            "lastHitAt": float(stats.get("lastHitAt") or 0.0),
        }
    }


def _subs_load() -> None:
    try:
        with open(_SUBS_FILE, "rb") as f:
            d = json.loads(f.read().decode("utf-8"))
        rules = d.get("rules") if isinstance(d, dict) else None
        if isinstance(rules, dict):
            for k, v in rules.items():
                if isinstance(v, dict) and v.get("id"):
                    _SUB_RULES[str(k)] = v
        if _SUB_RULES:
            log.info("已恢复订阅归档规则（%d 条）", len(_SUB_RULES))
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("订阅规则恢复失败: %s", e)


def _subs_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps({"rules": _SUB_RULES}, ensure_ascii=False).encode("utf-8")
        # tmp + os.replace 原子写：_sub_bump 在每个归档任务终态都会调用本函数
        # （批量归档 200 文件 = 200 次写入），O_TRUNC 直写在写入中途崩溃会留下
        # 截断 JSON，并发写还会丢更新。
        tmp = f"{_SUBS_FILE}.tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.replace(tmp, _SUBS_FILE)
    except Exception as e:  # noqa: BLE001
        log.warning("订阅规则持久化失败: %s", e)


# ---------------------------------------------------------------------
# 5b. 频道监听已见集合（watch）
#
# 记录每条监听规则在每个会话里「已经见过的 uniqueId」，用于判断哪些是新文件。
# 只存 uniqueId 列表（不存文件对象），并有条数上限 —— 否则上百个会话的
# 历史消息会把状态文件撑爆。
# ---------------------------------------------------------------------
_WATCH_SEEN_FILE = os.path.join(APP_ROOT_DIR, ".watch_seen.json")
_WATCH_SEEN: Dict[str, List[str]] = {}
_WATCH_SEEN_MAX = 500


def _watch_seen_load() -> None:
    try:
        with open(_WATCH_SEEN_FILE, "rb") as f:
            d = json.loads(f.read().decode("utf-8"))
        seen = d.get("seen") if isinstance(d, dict) else None
        if isinstance(seen, dict):
            for k, v in seen.items():
                if isinstance(v, list):
                    _WATCH_SEEN[str(k)] = [str(x) for x in v][-_WATCH_SEEN_MAX:]
        if _WATCH_SEEN:
            log.info("已恢复频道监听已见集合（%d 个会话）", len(_WATCH_SEEN))
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("频道监听状态恢复失败: %s", e)


def _watch_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps({"seen": _WATCH_SEEN}, ensure_ascii=False).encode("utf-8")
        # tmp + os.replace 原子写：轮询每轮都可能写入，直写崩溃会留下截断 JSON，
        # 整个已见集合就丢了 —— 丢了会导致历史消息被当成新文件重新下载。
        tmp = f"{_WATCH_SEEN_FILE}.tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.replace(tmp, _WATCH_SEEN_FILE)
    except Exception as e:  # noqa: BLE001
        log.warning("频道监听状态持久化失败: %s", e)


def _notify_config_load() -> None:
    try:
        if os.path.exists(_NOTIFY_CONFIG_FILE):
            try:
                os.chmod(_NOTIFY_CONFIG_FILE, 0o600)
            except Exception:
                pass
            with open(_NOTIFY_CONFIG_FILE, "r", encoding="utf-8") as f:
                d = json.loads(f.read())
            if isinstance(d, dict):
                if "enabled" in d:
                    _NOTIFY_CONFIG["enabled"] = bool(d["enabled"])
                if "channel" in d:
                    _NOTIFY_CONFIG["channel"] = str(d["channel"] or "both")
                if "botToken" in d:
                    _NOTIFY_CONFIG["botToken"] = str(d["botToken"] or "")
                if "chatId" in d:
                    _NOTIFY_CONFIG["chatId"] = str(d["chatId"] or "")
                if "minFileSizeMB" in d:
                    _NOTIFY_CONFIG["minFileSizeMB"] = int(d["minFileSizeMB"] or 50)
                if isinstance(d.get("events"), dict):
                    _NOTIFY_CONFIG["events"].update(d["events"])
            log.info("已恢复 Telegram 通知配置（启用: %s, 通道: %s）",
                     _NOTIFY_CONFIG["enabled"], _NOTIFY_CONFIG["channel"])
    except Exception as e:  # noqa: BLE001
        log.warning("读取 .notify_config.json 异常: %s", e)


def _notify_config_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps(_NOTIFY_CONFIG, indent=2, ensure_ascii=False).encode("utf-8")
        fd = os.open(_NOTIFY_CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        try:
            os.chmod(_NOTIFY_CONFIG_FILE, 0o600)
        except Exception:
            pass
        log.info("Telegram 通知配置已更新并落盘")
    except Exception as e:  # noqa: BLE001
        log.warning("保存 .notify_config.json 异常: %s", e)


_BATCH_RETRY_LOCK = asyncio.Lock()
_MAX_JOB_RETRIES = 20


def _get_notify_config_public() -> Dict[str, Any]:
    masked = _mask_token(_NOTIFY_CONFIG.get("botToken", ""))
    return {
        "enabled": bool(_NOTIFY_CONFIG.get("enabled", False)),
        "channel": str(_NOTIFY_CONFIG.get("channel", "both")),
        "botToken": masked,
        "botTokenMasked": masked,
        "hasBotToken": bool(_NOTIFY_CONFIG.get("botToken")),
        "chatId": str(_NOTIFY_CONFIG.get("chatId", "")),
        "minFileSizeMB": int(_NOTIFY_CONFIG.get("minFileSizeMB", 50)),
        "events": dict(_NOTIFY_CONFIG.get("events", {})),
    }



def _openlist_update_base_url(url: str) -> str:
    raw = str(url or "").strip().rstrip("/")
    if raw:
        try:
            parsed = urlparse(raw)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                u = raw
            else:
                u = OPENLIST_URL.rstrip("/")
        except Exception:
            u = OPENLIST_URL.rstrip("/")
    else:
        u = OPENLIST_URL.rstrip("/")
    _OPENLIST["baseUrl"] = u
    _openlist_client.base_url = httpx.URL(u)
    _openlist_upload_client.base_url = httpx.URL(u)
    return u


def _openlist_load() -> None:
    try:
        candidates = [_OPENLIST_FILE]
        alt_file = os.path.join(APP_ROOT_DIR, "openlist.json")
        if alt_file != _OPENLIST_FILE and os.path.exists(alt_file):
            candidates.append(alt_file)
        for cf in candidates:
            if os.path.exists(cf):
                with open(cf, "rb") as f:
                    d = json.loads(f.read().decode("utf-8"))
                for k in ("username", "password", "token", "baseUrl"):
                    v = str(d.get(k) or "")
                    if v and not _OPENLIST.get(k):
                        _OPENLIST[k] = v
                if not _OPENLIST.get("logged_at"):
                    _OPENLIST["logged_at"] = float(d.get("logged_at") or 0.0)
                if _OPENLIST.get("baseUrl"):
                    _openlist_update_base_url(_OPENLIST["baseUrl"])
        if _OPENLIST.get("username"):
            log.info("已恢复 OpenList 登录态（用户: %s, 地址: %s）",
                     _OPENLIST["username"], _OPENLIST.get("baseUrl") or OPENLIST_URL)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("OpenList 凭据恢复失败: %s", e)


def _openlist_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps({
            "baseUrl": _OPENLIST.get("baseUrl") or OPENLIST_URL,
            "username": _OPENLIST.get("username", ""),
            "password": _OPENLIST.get("password", ""),
            "token": _OPENLIST.get("token", ""),
            "logged_at": _OPENLIST.get("logged_at", 0.0),
        }).encode("utf-8")
        fd = os.open(_OPENLIST_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    except Exception as e:  # noqa: BLE001
        log.warning("OpenList 凭据持久化失败: %s", e)


def _openlist_clear() -> None:
    _OPENLIST.clear()
    try:
        os.remove(_OPENLIST_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("OpenList 凭据清除失败: %s", e)


_ARCH_PILL = {
    "queued": ("pending", "排队中"),
    "uploading": ("upload", "归档中"),
    # 上传已成功、但云端大小暂时确认不了（OpenList 单文件探测超时）。
    # 这既不是成功也不是失败：本地文件必须保留，稍后由后台复查自动转 done/failed。
    "unverified": ("upload", "待核验"),
    "done": ("archived", "已归档"),
    "failed": ("failed", "归档失败"),
    "cancelled": ("pending", "已取消"),
}


def _archive_public(job: Dict[str, Any]) -> Dict[str, Any]:
    """给前端展示的 job 视图（绝不携带本地路径）。"""
    state = str(job.get("state") or "queued")
    cls, label = _ARCH_PILL.get(state, ("pending", state))
    return {
        "id": job.get("id"),
        "uniqueId": job.get("unique_id"),
        "filename": job.get("filename"),
        "remoteDir": job.get("remote_dir"),
        "remotePath": job.get("remote_path"),
        "state": state,
        "progress": int(job.get("progress") or 0),
        "error": job.get("error") or "",
        "archivedAt": float(job.get("archived_at") or 0.0),
        "createdAt": float(job.get("created_at") or 0.0),
        "deleteLocal": bool(job.get("delete_local")),
        "localDeleted": bool(job.get("local_deleted")),
        "localDeleteError": str(job.get("local_delete_error") or ""),
        "verifyPending": bool(job.get("verify_pending")),
        "pillCls": cls,
        "pillLabel": label,
    }


def _archive_state_of(unique_id: Any) -> Optional[Dict[str, Any]]:
    """某文件的最新归档任务视图（library_local 渲染按钮/胶囊用）。"""
    if not unique_id:
        return None
    latest = None
    for j in _ARCHIVE_JOBS.values():
        if str(j.get("unique_id") or "") == str(unique_id):
            if latest is None or (j.get("created_at") or 0) > (latest.get("created_at") or 0):
                latest = j
    return _archive_public(latest) if latest else None


def _archive_path_owners() -> Dict[str, set]:
    """云端路径 → 曾使用它的 unique_id 集合（路径小写）。供归档入队时判「这个路径是否已被**别的文件**占用」。
    为什么按 unique_id 记账而不是简单「路径是否存在过」：
      - 同一文件重复归档（用户再点一次「重新归档」）应当允许复用原路径 ——
        它是同一份内容，覆盖自己是幂等的，不该被改名成 ` (1)` 越积越多。
      - 路径一旦被**另一个** unique_id 用过，就说明它承载了两个不同文件：
        再写进去就是静默覆盖（实测：同路径两次 PUT 都返回 200，远端只剩后写的那个）。
    只排除 cancelled：失败任务的云端残片无法确证，宁可让路。
    """
    owners: Dict[str, set] = {}
    for j in _ARCHIVE_JOBS.values():
        rp = str(j.get("remote_path") or "")
        uid = str(j.get("unique_id") or "")
        if not rp or not uid or j.get("state") == "cancelled":
            continue
        owners.setdefault(rp.lower(), set()).add(uid)
    return owners


def _archive_upload_lock(remote_path: str) -> "asyncio.Lock":
    """同一 remote_path 的串行上传锁（消灭同路径并发写的 eTag 冲突）。

    为什么不能只靠文件名唯一化：唯一化让**新任务**不再撞车，但历史遗留的、
    或经 `/archive/retry` 重放的任务仍可能指向同一路径。两个上传同时写同一路径
    时 OneDrive 报 `resourceModified`，多轮重试全灭（实测日志 30 次）。
    锁按路径懒创建、用完即弃，避免长期持有大量锁对象。
    """
    key = str(remote_path or "").lower()
    lk = _ARCHIVE_UPLOAD_LOCKS.get(key)
    if lk is None:
        lk = asyncio.Lock()
        _ARCHIVE_UPLOAD_LOCKS[key] = lk
    return lk


def _archive_active_of(unique_id: str) -> Optional[Dict[str, Any]]:
    """进行中（queued/uploading）的归档任务，幂等去重用。"""
    for j in _ARCHIVE_JOBS.values():
        if str(j.get("unique_id") or "") == unique_id and j.get("state") in ("queued", "uploading"):
            return j
    return None


def _archive_latest_raw_of(unique_id: str) -> Optional[Dict[str, Any]]:
    """某文件最新一条归档任务的原始 job。"""
    latest = None
    for j in _ARCHIVE_JOBS.values():
        if str(j.get("unique_id") or "") == str(unique_id):
            if latest is None or (j.get("created_at") or 0) > (latest.get("created_at") or 0):
                latest = j
    return latest


# ---------------------------------------------------------------------
# 归档查询的「单次遍历」加速器
# ---------------------------------------------------------------------
# 背景：_archive_latest_raw_of / _archive_registry_lookup 都是 O(n) 全表扫描，
# 而它们被放在**逐文件渲染循环**里调用（task_service 每任务一次、
# browse_service 每行一次）。归档到 5000 条时，单次查询实测 9.1 ms，
# 2000 个任务一次渲染就要跑十几秒。
#
# 设计取舍（重要）：这里刻意**不使用跨请求的持久缓存**。
# 曾尝试用模块级 dict 缓存 + len() 失效，但 _ARCHIVE_JOBS 存在「长度不变而内容变更」
# 的写入模式（测试/业务都会 clear() 后重新填充），导致脏读，已被回退。
# 改为「一次扫描，本次遍历内复用」：调用方在循环前 build 一次，
# 循环内 O(1) 查询，循环结束即丢弃 —— 天然无失效问题，语义与逐个全表扫描严格等价。
class _ArchiveIndex:
    """一次性归档索引：一次遍历建立，供同一批渲染复用。

    语义与逐个全表扫描严格等价：
    - latest_of(uid)：扫描语义「unique_id 相同且 created_at 最大」→ 直接映射到最新那条。
    - registry_lookup(uid, fn, sz)：反向扫描语义「谁先命中就返回谁」→
      为 uid 命中与文件名命中分别记录其在反序中的位置，取位置更早者。
    """

    __slots__ = ("by_uid", "_reg_uid", "_reg_fn")

    def __init__(self, jobs: Dict[str, Dict[str, Any]]) -> None:
        by_uid: Dict[str, Dict[str, Any]] = {}
        for j in jobs.values():
            # 与慢路径 str(j.get("unique_id") or "") 完全一致（含空串键）
            uid = str(j.get("unique_id") or "")
            cur = by_uid.get(uid)
            if cur is None or (j.get("created_at") or 0) > (cur.get("created_at") or 0):
                by_uid[uid] = j
        self.by_uid = by_uid


        # 反序一次性构建，同时记录位置（位置只统计可查询状态，与慢路径的
        # 「跳过不可查询者」一致，故相对先后关系保持）
        reg_uid: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        reg_fn: Dict[str, List[Tuple[int, Dict[str, Any]]]] = {}
        pos = 0
        for j in reversed(list(jobs.values())):
            if j.get("state") not in ("done", "uploading", "queued"):
                continue
            j_uid = str(j.get("unique_id") or "").strip()
            if j_uid and j_uid not in reg_uid:
                reg_uid[j_uid] = (pos, j)
            j_fn = str(j.get("filename") or j.get("raw_filename") or "").strip().lower()
            if j_fn:
                reg_fn.setdefault(j_fn, []).append((pos, j))
            pos += 1
        self._reg_uid = reg_uid
        self._reg_fn = reg_fn

    def latest_of(self, unique_id: Any) -> Optional[Dict[str, Any]]:
        # 与慢路径严格一致：不因 uid 为空而提前返回 None（原实现空串/None 也能匹配
        # 到 unique_id 为空的那批 job，取 created_at 最大者）
        return self.by_uid.get(str(unique_id or ""))

    def registry_lookup(self, unique_id: str = "", filename: str = "",
                        size_bytes: Any = None) -> Optional[Dict[str, Any]]:
        uid = str(unique_id or "").strip()
        norm_fn = str(filename or "").strip().lower()
        sz = None
        if isinstance(size_bytes, (int, float)) and size_bytes > 0:
            sz = int(size_bytes)

        uid_hit = self._reg_uid.get(uid) if uid else None
        fn_list = self._reg_fn.get(norm_fn) if norm_fn else None
        if fn_list is None and norm_fn:
            # 一方缺扩展名时 _same_file_name 退化为「主干相等」
            stem = norm_fn.rsplit(".", 1)[0] if "." in norm_fn else norm_fn
            fn_list = self._reg_fn.get(stem)
        fn_hit = None
        if fn_list:
            for _, j in fn_list:
                if sz is None or j.get("size_bytes") == sz:
                    fn_hit = (_, j)
                    break

        # 复刻「谁先命中返回谁」：取位置更早者
        if uid_hit is None and fn_hit is None:
            return None
        if uid_hit is None:
            return fn_hit[1]
        if fn_hit is None:
            return uid_hit[1]
        return uid_hit[1] if uid_hit[0] <= fn_hit[0] else fn_hit[1]



def _archive_index_snapshot() -> "_ArchiveIndex":
    """为一批只读渲染建立一次性索引（循环前调用，循环内 O(1) 查询）。"""
    return _ArchiveIndex(_ARCHIVE_JOBS)



def _archive_registry_lookup(unique_id: str = "", filename: str = "", size_bytes: Any = None) -> Optional[Dict[str, Any]]:
    if not _ARCHIVE_JOBS:
        return None
    uid = str(unique_id or "").strip()
    norm_fn = str(filename or "").strip().lower()
    sz = None
    if isinstance(size_bytes, (int, float)) and size_bytes > 0:
        sz = int(size_bytes)

    for j in reversed(list(_ARCHIVE_JOBS.values())):
        if j.get("state") not in ("done", "uploading", "queued"):
            continue
        if uid and str(j.get("unique_id") or "").strip() == uid:
            return j
        j_fn = str(j.get("filename") or j.get("raw_filename") or "").strip().lower()
        if norm_fn and j_fn and _same_file_name(norm_fn, j_fn):
            if sz is None or j.get("size_bytes") == sz:
                return j
    return None


def _custom_notes_load() -> None:
    try:
        with open(_CUSTOM_NOTES_FILE, "rb") as f:
            d = json.loads(f.read().decode("utf-8"))
        notes = d.get("notes") if isinstance(d, dict) else None
        if isinstance(notes, dict):
            _CUSTOM_FILE_NOTES.clear()
            for k, v in notes.items():
                if k and v and isinstance(v, str):
                    _CUSTOM_FILE_NOTES[str(k)] = v.strip()
        if _CUSTOM_FILE_NOTES:
            log.info("已恢复视频自定义备注命名映射（%d 条）", len(_CUSTOM_FILE_NOTES))
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("恢复视频自定义备注失败: %s", e)


def _custom_notes_save() -> None:
    try:
        if len(_CUSTOM_FILE_NOTES) > 4000:
            drop = len(_CUSTOM_FILE_NOTES) - 4000
            for k in list(_CUSTOM_FILE_NOTES.keys())[:drop]:
                _CUSTOM_FILE_NOTES.pop(k, None)
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps({"notes": _CUSTOM_FILE_NOTES}, ensure_ascii=False, indent=2).encode("utf-8")
        fd = os.open(_CUSTOM_NOTES_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("保存视频自定义备注失败: %s", e)


# 模块装载时从磁盘加载已有状态
_flood_wait_load()
_waiting_disk_load()
_archive_load()
_archive_config_load()
_subs_load()
_watch_seen_load()
_notify_config_load()
_openlist_load()
_custom_notes_load()
_stream_stats_load()
_browse_memory_load()
_throttle_config_load()
_throttle_state_load()
_throttle_queue_load()
