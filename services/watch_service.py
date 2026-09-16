# -*- coding: utf-8 -*-
"""
services/watch_service.py — 频道消息监听，实时入库（Watch Service）
==================================================================
订阅规则原本只在定时 sweep 里生效，而 sweep 只归档**已下载完成**的任务 ——
也就是说：新发布的媒体不会自动入库，必须手动去浏览页勾选下载。

本服务补上这一段：按「监听规则」盯住指定会话，新消息一出现就自动入队下载，
下载完成后由既有的自动归档 sweep 接续转存到网盘。

设计取舍（重要）：
* **不依赖 WS 上的新消息事件**。本项目现有 WS 事件只覆盖任务进度/日志，
  没有「频道新消息」事件；且事件格式未文档化。因此这里用**低频轮询**：
  只用既有的 `/telegram/{tg}/chat/{ch}/files` 接口，稳定可靠、不引入对
  未文档化后端行为的依赖。
* 间隔默认 15 秒。相比手动去页面点，已是"实时"；也不至于把 2 核 VPS 的
  后端压出问题（每次只是 1 个列表请求）。
* **入队必须复用既有的水位/风控熔断**：没熔断就直接提交，熔断中就把任务
  挂到 `_ARCHIVE_JOBS` 里等自动恢复（与 sweep 一致）。绝不能绕过熔断硬下。
"""
import asyncio
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from core.state import (
    _SUB_RULES, _ARCHIVE_JOBS, _WATCH_SEEN, _WATCH_SEEN_MAX, _watch_save,
    _is_flood_wait_active, _get_flood_wait_status,
)
from core.backend import BACKEND
from core.logging import log
from core.config import TG_WAIT_PHONE, TG_WAIT_CODE, TG_WAIT_PASSWORD, TG_WAIT_OTHER_DEVICE

# 轮询间隔：够"实时"，又不至于压后端
_WATCH_INTERVAL = float(os.environ.get("BRIDGE_WATCH_INTERVAL", "15"))
_WATCH_FIRST_DELAY = 8.0
# 每次轮询每会话最多取多少条（列表接口 limit 上限 500）
_WATCH_FETCH_LIMIT = int(os.environ.get("BRIDGE_WATCH_FETCH_LIMIT", "50"))
# 单个 watch 任务一次最多入队多少条，防止规则刚启用时把历史消息一次性全拉下来
_WATCH_MAX_ENQUEUE = int(os.environ.get("BRIDGE_WATCH_MAX_ENQUEUE", "20"))

_WATCH_TASK: Optional[asyncio.Task] = None

# TDLib 未就绪状态码：这些状态下不轮询（避免无意义的失败请求）
_NOT_READY_STATES = {TG_WAIT_PHONE, TG_WAIT_CODE, TG_WAIT_PASSWORD, TG_WAIT_OTHER_DEVICE}


def _watch_rules() -> List[Dict[str, Any]]:
    """取出启用了监听的规则（订阅规则上的 watch 开关）。"""
    out: List[Dict[str, Any]] = []
    for r in _SUB_RULES.values():
        if not r.get("enabled"):
            continue
        if not r.get("watch"):
            continue
        tg = str(r.get("telegramId") or "")
        ch = str(r.get("chatId") or "")
        # 双方都要具体值：'*' 通配在监听场景下含义不明（要盯所有会话？），拒绝
        if not tg or tg == "*" or not ch or ch == "*":
            continue
        out.append(r)
    return out


async def _tg_authorized() -> bool:
    """账号是否已授权可查。未授权时不轮询，避免刷错误日志。

    判定顺序：
    1. BACKEND.get_authorization_state（测试桩提供；生产 BackendClient 无此方法）
    2. 兜底：/telegrams 里任一账号 status == active（后端实况，34 VPS 实测有效）
    """
    getter = getattr(BACKEND, "get_authorization_state", None)
    if getter is not None:
        try:
            st = await getter()
        except Exception:  # noqa: BLE001
            return False
        if isinstance(st, dict):
            code = st.get("code") or st.get("status")
            try:
                if int(code) in _NOT_READY_STATES:
                    return False
            except (TypeError, ValueError):
                pass
            if st.get("authorized") is False:
                return False
        return True
    # 生产路径：后端 /telegrams 的 status 即授权实况（active=在线）
    try:
        tgs = await BACKEND.list_telegrams(force=True)
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(tgs, list) or not tgs:
        return False
    return any(str(t.get("status") or "") == "active" for t in tgs if isinstance(t, dict))


def _seen_key(rule_id: Any, chat_id: Any) -> str:
    return f"{rule_id}:{chat_id}"


def _seen_of(key: str) -> Set[str]:
    raw = _WATCH_SEEN.get(key)
    if not isinstance(raw, list):
        return set()
    return {str(x) for x in raw}


def _seen_put(key: str, uids: Set[str]) -> None:
    """写入已见集合：只保留最新 N 条，避免状态文件无界增长。"""
    merged = list(_seen_of(key))
    for u in uids:
        s = str(u)
        if s and s not in merged:
            merged.append(s)
    # 有界：保留末尾 _WATCH_SEEN_MAX 条（dict 保序，集合也按插入序近似）
    _WATCH_SEEN[key] = merged[-_WATCH_SEEN_MAX:]


async def _enqueue_new(rule: Dict[str, Any], files: List[Dict[str, Any]]) -> int:
    """把新文件入队下载。复用既有水位/风控熔断语义。

    返回入队条数。
    """
    from services.watermark_service import (
        _is_disk_high_watermark_exceeded, _is_disk_low_watermark_reached,
    )

    payload: List[Dict[str, Any]] = []
    for f in files:
        try:
            payload.append({
                "telegramId": int(f.get("telegramId") if f.get("telegramId") is not None
                                  else rule.get("telegramId")),
                "chatId": int(f.get("chatId") if f.get("chatId") is not None
                              else rule.get("chatId")),
                "messageId": int(f.get("messageId")),
                "fileId": int(f.get("fileId") or f.get("id") or 0),
            })
        except (TypeError, ValueError):
            continue
    if not payload:
        return 0
    payload = payload[:_WATCH_MAX_ENQUEUE]

    high_exceeded, cur_pct, high_threshold = _is_disk_high_watermark_exceeded()
    flood = _is_flood_wait_active()

    if high_exceeded or flood:
        # 熔断中：挂到归档任务表等恢复，与 sweep 的 waiting_disk / flood 语义一致，
        # 绝不绕过熔断硬下。
        reason = "磁盘高水位" if high_exceeded else "Telegram 风控"
        now = time.time()
        for pf in payload:
            jid = f"watch-{rule.get('id')}-{pf['messageId']}"
            _ARCHIVE_JOBS[jid] = {
                "id": jid,
                "unique_id": str(pf.get("fileId") or pf["messageId"]),
                "telegram_id": pf["telegramId"],
                "chat_id": pf["chatId"],
                "message_id": pf["messageId"],
                "state": "waiting_disk",
                "progress": 0,
                "created_at": now,
                "archived_at": 0.0,
                "local_path": "",
                "remote_path": "",
                "size_bytes": 0,
                "error": f"监听入队被{reason}熔断挂起",
                "rule_id": rule.get("id"),
                "source": "watch",
            }
        try:
            from core.state import _archive_save
            _archive_save()
        except Exception:  # noqa: BLE001
            pass
        log.warning("监听命中 %d 个新文件，因%s熔断已挂起等待恢复（rule=%s）",
                    len(payload), reason, rule.get("id"))
        return 0

    try:
        await BACKEND.start_download_multiple({"files": payload})
        log.info("监听自动入队 %d 个新文件（rule=%s, chat=%s）",
                 len(payload), rule.get("id"), rule.get("chatId"))
        return len(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("监听入队失败（rule=%s）: %s", rule.get("id"), e)
        return 0


async def _watch_tick() -> int:
    """单轮轮询：对每条监听规则查最新文件，把没见过的入队。"""
    rules = _watch_rules()
    if not rules:
        return 0
    if not await _tg_authorized():
        return 0

    total = 0
    for rule in rules:
        tg = rule.get("telegramId")
        ch = rule.get("chatId")
        key = _seen_key(rule.get("id"), ch)
        seen = _seen_of(key)
        try:
            files = await BACKEND.list_files(
                tg, ch, from_message_id=0, type="media", force=True)
        except Exception as e:  # noqa: BLE001
            log.warning("监听拉取失败（chat=%s）: %s", ch, e)
            continue
        if not isinstance(files, list) or not files:
            continue

        fresh: List[Dict[str, Any]] = []
        newly: Set[str] = set()
        for f in files[:_WATCH_FETCH_LIMIT]:
            uid = str(f.get("uniqueId") or "")
            if not uid:
                mid = f.get("messageId")
                uid = f"mid-{mid}" if mid is not None else ""
            if not uid:
                continue
            if uid not in seen:
                newly.add(uid)
                # watch 只自动下载视频：图片/音频/文档记入基线但跳过。
                # 与浏览页口径一致（browse_service 已把 photo 移出可下载类型）。
                ftype = str(f.get("type") or "").lower()
                if ftype != "video":
                    continue
                # 首次见到该会话：只记录基线，不入队。
                # 否则规则一启用就会把历史消息一次性全下载（几十上百个文件）。
                if seen:
                    fresh.append(f)

        if newly:
            _seen_put(key, newly)
            _watch_save()

        if fresh:
            # 最早的先入队，保持时间顺序
            fresh.reverse()
            n = await _enqueue_new(rule, fresh)
            total += n

    total += await _watch_text_links(rules)
    return total


# ---------------------------------------------------------------------------
# 文本链接监听：收藏里"复制链接发过来"的消息（纯文本）不会进文件库，这里
# 用 TDLib SearchChatMessages 直接搜会话消息，提取 t.me 链接并解析入队。
# ---------------------------------------------------------------------------

# 文本消息里的 t.me 链接（公开频道/私密频道/话题消息均覆盖，宽松提取后走严格解析）
_LINK_IN_TEXT = re.compile(
    r"https?://t(?:elegram)?\.(?:me|dog)/[A-Za-z0-9_/+\-]+",
    re.IGNORECASE)


def _extract_links(text: str) -> List[str]:
    out: List[str] = []
    for m in _LINK_IN_TEXT.finditer(text or ""):
        u = m.group(0).rstrip("/_")
        if u not in out:
            out.append(u)
    return out


def _link_msg_key(telegram_id: Any, chat_id: Any, message_id: Any) -> str:
    """文本链接消息的已见键：与媒体 uid 空间隔离（加前缀），避免互撞。"""
    return f"link-{telegram_id}-{chat_id}-{message_id}"


async def _search_text_messages(tg: Any, ch: Any, limit: int = 30) -> List[Dict[str, Any]]:
    """TDLib SearchChatMessages：搜该会话最近的文本消息（不过滤类型、无关键词 →
    TdLib 要求 query 非空时才全量检索；空 query 配 sender/filter 亦可，这里
    用 query='t.me' 直接命中含链接的消息，省掉逐条拉文本的开销）。"""
    from core.backend import telegram_api_call
    res = await telegram_api_call("SearchChatMessages", {
        "chatId": int(ch),
        "query": "t.me",
        "offsetMessageId": 0,
        "limit": limit,
    }, timeout=25.0)
    if not isinstance(res, dict):
        return []
    msgs = res.get("messages") or res.get("totalMessages") and [] or []
    if not isinstance(msgs, list):
        return []
    return [m for m in msgs if isinstance(m, dict)]


async def _watch_text_links(rules: List[Dict[str, Any]]) -> int:
    """对每条监听规则搜文本链接消息：新出现的 → resolve_link 解析入队。

    已见键带 link- 前缀与媒体 uid 隔离；基线规则与媒体监听一致（首次只记录）。
    """
    total = 0
    for rule in rules:
        tg = rule.get("telegramId")
        ch = rule.get("chatId")
        key = _seen_key(rule.get("id"), ch)
        seen = _seen_of(key)
        try:
            msgs = await _search_text_messages(tg, ch)
        except Exception as e:  # noqa: BLE001
            log.warning("监听文本消息失败（chat=%s）: %s", ch, e)
            continue
        if not msgs:
            continue

        fresh_links: List[str] = []
        newly: Set[str] = set()
        # 链接消息基线独立判定：只看该会话是否已有 link- 前缀键（媒体键与本键同存一个
        # 集合，媒体基线先写会让 seen 非空——不能拿它当"链接已建基线"的依据，
        # 否则首轮就会把全部历史链接消息解析入队（34 实测 37 条连环下载事故）。
        link_seen = {k for k in seen if k.startswith("link-")}
        for m in msgs:
            mid = m.get("id") or m.get("messageId")
            if mid is None:
                continue
            mkey = _link_msg_key(tg, ch, mid)
            if mkey in seen:
                continue
            newly.add(mkey)
            # 消息文本：TDLib MessageText.text.text；转发消息可能嵌套
            content = m.get("content") or {}
            text_obj = content.get("text") or {}
            text = str(text_obj.get("text") or "")
            if not text:
                # 引用/标题等其它文本载体也扫一眼（低成本）
                caption = content.get("caption") or {}
                text = str(caption.get("text") or "")
            if link_seen:  # 链接基线轮不入队
                fresh_links.extend(_extract_links(text))

        if newly:
            _seen_put(key, newly)
            _watch_save()

        if not fresh_links:
            continue

        # 链接解析 + 入队（复用任务服务的两跳解析，含去重与熔断语义）
        try:
            from services.task_service import _resolve_links_to_files
            n, err = await _resolve_links_to_files(fresh_links)
            if err:
                log.warning("监听链接解析失败（chat=%s）: %s", ch, err)
            total += n
            if n:
                log.info("监听自动解析 %d 条文本链接（chat=%s）", n, ch)
        except Exception as e:  # noqa: BLE001
            log.warning("监听链接入队异常（chat=%s）: %s", ch, e)
    return total


async def watch_loop() -> None:
    """后台常驻循环（由 bridge_server 启动时挂起）。"""
    try:
        await asyncio.sleep(_WATCH_FIRST_DELAY)
        while True:
            try:
                n = await _watch_tick()
                if n:
                    log.info("频道监听：本轮入队 %d 个文件", n)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("频道监听异常: %s", e)
            await asyncio.sleep(_WATCH_INTERVAL)
    except asyncio.CancelledError:
        log.info("频道监听已停止")
        return


def watch_state() -> Dict[str, Any]:
    """给设置页/诊断用的监听状态。"""
    rules = _watch_rules()
    out = []
    for r in rules:
        key = _seen_key(r.get("id"), r.get("chatId"))
        out.append({
            "ruleId": r.get("id"),
            "chatId": str(r.get("chatId") or ""),
            "chatTitle": str(r.get("chatTitle") or ""),
            "seen": len(_seen_of(key)),
        })
    return {
        "intervalSec": _WATCH_INTERVAL,
        "rules": out,
        "count": len(out),
    }
