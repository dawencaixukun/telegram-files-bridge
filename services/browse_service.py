# -*- coding: utf-8 -*-
"""
services/browse_service.py — 聊天资源浏览与流式分页去重服务
============================================================
负责账号聊天树构建、频道媒体按类检索、消息级转发副本折叠去重与资源浏览行渲染。
"""
import time
import asyncio
from typing import Any, Dict, List, Tuple

from core.config import _pick, _fmt_size, _fmt_time, _human_name
from core.state import _archive_registry_lookup
from core.backend import BACKEND
from core.logging import log
from services.openlist_service import _openlist_direct_url

_BROWSE_TYPES = (
    ("document", "全部"),
    ("media", "媒体"),
    ("video", "视频"),
    ("audio", "音频"),
)
# 历史变更：「图片」分类按用户要求整体移除（浏览栏不再提供图片入口）。
# photo 保留在 _BROWSE_TYPE_LABELS 仅为历史数据/类型标注兜底；
# 直接访问 type=photo 由各调用点回落 document（_BROWSE_TYPES 白名单校验）。
_BROWSE_FORBIDDEN_TYPES = {"photo"}
_BROWSE_TYPE_LABELS = {
    "video": "视频", "photo": "图片", "audio": "音频", "document": "文档",
    "file": "文件", "animation": "动图", "url": "链接", "media": "媒体",
    "unknown": "未知",
}

_BROWSE_SEEN: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
_BROWSE_SEEN_TTL = 1800.0
_BROWSE_SEEN_MAX = 64

CHAT_SOURCE_CACHE: Dict[str, Any] = {"key": None, "value": None}


def _browse_seen_key(tg_id: Any, chat_id: Any, type_: str) -> Tuple[str, str, str]:
    return (str(tg_id), str(chat_id), str(type_))


def _browse_seen_state(key: Tuple[str, str, str]) -> Dict[str, Any]:
    now = time.monotonic()
    for k in [k for k, v in _BROWSE_SEEN.items() if v["expire"] < now]:
        _BROWSE_SEEN.pop(k, None)
    state = _BROWSE_SEEN.get(key)
    if state is None:
        state = {"seen": set(), "collapsed": 0, "loaded": 0, "expire": now + _BROWSE_SEEN_TTL}
        _BROWSE_SEEN[key] = state
        if len(_BROWSE_SEEN) > _BROWSE_SEEN_MAX:
            oldest = min(_BROWSE_SEEN, key=lambda k: _BROWSE_SEEN[k]["expire"])
            _BROWSE_SEEN.pop(oldest, None)
    state["expire"] = now + _BROWSE_SEEN_TTL
    return state


def _browse_dedup_page(state: Dict[str, Any], files: List[Dict[str, Any]], cursor: int) -> List[Dict[str, Any]]:
    if not cursor:
        state["seen"] = set()
        state["collapsed"] = 0
        state["loaded"] = 0
    kept: List[Dict[str, Any]] = []
    for rec in files:
        uid = str(rec.get("uniqueId") or "")
        if uid:
            if uid in state["seen"]:
                state["collapsed"] += 1
                continue
            state["seen"].add(uid)
        kept.append(rec)
    state["loaded"] += len(kept)
    return kept


async def _chat_sources(force: bool = False) -> List[Dict[str, Any]]:
    sources: List[Dict[str, Any]] = []
    try:
        telegrams = await BACKEND.list_telegrams(force=force)
    except Exception as e:  # noqa: BLE001
        log.warning("list_telegrams 失败: %s", e)
        return sources
    if not isinstance(telegrams, list):
        return sources

    async def _gather_chat(tg: Dict[str, Any]):
        tg_id = _pick(tg, "telegramId", "telegram_id", "id")
        if tg_id is None:
            return []
        try:
            chats = await BACKEND.list_chats(tg_id, force=force)
        except Exception as e:  # noqa: BLE001
            log.warning("list_chats(%s) 失败: %s", tg_id, e)
            return []
        out = []
        if isinstance(chats, list):
            for ch in chats:
                out.append({
                    "chatId": _pick(ch, "chatId", "chat_id", "id"),
                    "title": str(_pick(ch, "title", "name", "channel", "chatName", default="聊天")),
                    "telegramId": tg_id,
                })
        return out

    results = await asyncio.gather(*[_gather_chat(t) for t in telegrams])
    for group in results:
        sources.extend(group)
    return sources


async def chat_sources(force: bool = False) -> List[Dict[str, Any]]:
    key = int(time.time()) // 30
    if not force and CHAT_SOURCE_CACHE["key"] == key:
        return CHAT_SOURCE_CACHE["value"]
    value = await _chat_sources(force=force)
    CHAT_SOURCE_CACHE["key"] = key
    CHAT_SOURCE_CACHE["value"] = value
    return value


async def _browse_tree(force: bool = False) -> List[Dict[str, Any]]:
    tree: List[Dict[str, Any]] = []
    try:
        telegrams = await BACKEND.list_telegrams(force=force)
    except Exception as e:  # noqa: BLE001
        log.warning("browse tree: list_telegrams 失败: %s", e)
        return tree
    if not isinstance(telegrams, list):
        return tree
    for tg in telegrams:
        tg_id = _pick(tg, "telegramId", "telegram_id", "id")
        if tg_id is None:
            continue
        try:
            chats = await BACKEND.list_chats(tg_id, force=force)
        except Exception as e:  # noqa: BLE001
            log.warning("browse tree: list_chats(%s) 失败: %s", tg_id, e)
            chats = []
        items: List[Dict[str, Any]] = []
        if isinstance(chats, list):
            for ch in chats:
                cid = _pick(ch, "chatId", "chat_id", "id")
                if cid is None:
                    continue
                title = str(_pick(ch, "title", "name", "channel", "chatName", default="聊天"))
                saved = str(cid) == str(tg_id)
                items.append({
                    "chatId": cid,
                    "title": "收藏 (Saved Messages)" if saved else title,
                    "saved": saved,
                })
        items.sort(key=lambda c: not c["saved"])
        tree.append({
            "telegramId": tg_id,
            "name": (str(_pick(tg, "name", "username", "phone", default="TG 账号")).strip() or "TG 账号"),
            "chats": items,
        })
    return tree


def _browse_rows(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for rec in files:
        try:
            date_str = ""
            d = rec.get("date")
            if d:
                date_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(d)))
        except (TypeError, ValueError):
            date_str = ""
        extra = rec.get("extra") if isinstance(rec.get("extra"), dict) else {}
        dur = extra.get("duration")
        dur_str = ""
        if isinstance(dur, int) and dur > 0:
            dur_str = f"{dur // 3600}:{dur % 3600 // 60:02d}:{dur % 60:02d}"
        name = _human_name(rec)
        ext = name.rsplit(".", 1)[-1][:4] if "." in name else "file"
        uid_str = str(rec.get("uniqueId") or "")
        arch_job = _archive_registry_lookup(uid_str, name, rec.get("size"))
        is_archived = bool(arch_job and arch_job.get("state") == "done")
        is_archiving = bool(arch_job and arch_job.get("state") in ("queued", "uploading"))
        cloud_path = str(arch_job.get("remote_path") or "") if arch_job else ""
        cloud_drive = (cloud_path.strip("/").split("/")[0] if "/" in cloud_path.strip("/") else "默认网盘") if cloud_path else ""
        openlist_url = _openlist_direct_url(cloud_path) if cloud_path else ""
        arch_ts = (arch_job.get("archived_at") or arch_job.get("created_at") or 0.0) if arch_job else 0.0
        archived_date = _fmt_time(arch_ts) if arch_ts else ""
        is_downloaded = (str(rec.get("downloadStatus") or "").lower() == "completed")
        local_path = str(rec.get("localPath") or "")

        rows.append({
            "fileId": rec.get("id"),
            "messageId": rec.get("messageId"),
            "chatId": rec.get("chatId"),
            "telegramId": rec.get("telegramId"),
            "uniqueId": uid_str,
            "name": name,
            "ext": ext.upper(),
            "size_str": _fmt_size(rec.get("size")),
            "date_str": date_str,
            "type": str(rec.get("type") or "unknown"),
            "type_label": _BROWSE_TYPE_LABELS.get(str(rec.get("type") or ""), "文件"),
            "thumb": str(rec.get("thumbnail") or ""),
            "thumb_uid": str(rec.get("thumbnailUniqueId") or ""),
            "dl": str(rec.get("downloadStatus") or "idle"),
            "tr": str(rec.get("transferStatus") or "idle"),
            "dur_str": dur_str,
            "is_archived": is_archived,
            "is_archiving": is_archiving,
            "cloud_path": cloud_path,
            "cloud_drive": cloud_drive,
            "openlist_url": openlist_url,
            "archived_date": archived_date,
            "is_downloaded": is_downloaded,
            "local_path": local_path,
        })
    return rows


async def _browse_files(tg_id: Any, chat_id: Any, type_: str = "document",
                        cursor: int = 0, limit: int = 30, hide_archived: bool = False) -> tuple:
    if not tg_id or not chat_id:
        return [], 0, 0, {"collapsed": 0, "loaded": 0}
    if type_ not in {t for t, _ in _BROWSE_TYPES}:
        type_ = "document"
    params: Dict[str, Any] = {"type": type_, "limit": limit}
    if cursor:
        params["fromMessageId"] = cursor
    try:
        tg = BACKEND._safe_id(tg_id)
        ch = BACKEND._safe_id(chat_id)
        raw = await BACKEND._request("GET", f"/telegram/{tg}/chat/{ch}/files", params=params)
    except Exception as e:  # noqa: BLE001
        log.warning("browse files(%s,%s) 失败: %s", tg_id, chat_id, e)
        return [], 0, 0, {"collapsed": 0, "loaded": 0}
    state = _browse_seen_state(_browse_seen_key(tg_id, chat_id, type_))
    # 过滤规则（用户要求，两次迭代）：
    # 1. 图片在浏览页全站屏蔽（包括「全部」分类，收藏里只见视频）；
    # 2. hide_archived 再滤掉已归档件。
    # 历史缺陷：后端每页 limit=30 条原始记录，过滤后可能只剩 0~1 条 ——
    # 表现为「一页只有一张卡，要狂点加载更多才能凑满一屏」。
    # 修复：过滤后数量不足一屏（< limit 的 60%）时，自动连续回源后端
    # 下一页补足，直到凑够 limit 条或后端没有更多（游标不再前进）。
    # 上限 8 轮防后端异常时死循环。
    _is_video = lambda f: (str(f.get("type") or "").lower() == "video"
                           or str(f.get("mimeType") or "").lower().startswith("video/"))
    collected: List[Dict[str, Any]] = []
    seen_uids: set = set()
    cur = cursor
    next_cursor = 0
    for _round in range(8):
        params: Dict[str, Any] = {"type": type_, "limit": limit}
        if cur:
            params["fromMessageId"] = cur
        try:
            tg = BACKEND._safe_id(tg_id)
            ch = BACKEND._safe_id(chat_id)
            raw = await BACKEND._request("GET", f"/telegram/{tg}/chat/{ch}/files", params=params)
        except Exception as e:  # noqa: BLE001
            log.warning("browse files(%s,%s) 失败: %s", tg_id, chat_id, e)
            break
        page = BACKEND._unwrap_files(raw)
        if not page and _round == 0:
            return [], 0, 0, {"collapsed": 0, "loaded": 0}
        if isinstance(raw, dict):
            try:
                next_cursor = int(raw.get("nextFromMessageId") or 0)
            except (TypeError, ValueError):
                next_cursor = 0
        if next_cursor and next_cursor == cur:
            next_cursor = 0
        page = _browse_dedup_page(state, page, cur)
        for f in page:
            uid = str(f.get("uniqueId") or "")
            if uid and uid in seen_uids:
                continue
            if uid:
                seen_uids.add(uid)
            if not _is_video(f):
                continue
            if hide_archived and _archive_registry_lookup(str(f.get("uniqueId") or ""), _human_name(f), f.get("size")):
                continue
            collected.append(f)
        # 凑够一屏，或后端没有更多（游标不前进/空页）→ 停止补页
        if len(collected) >= limit or not next_cursor or not page:
            break
        cur = next_cursor
    files = collected
    # 「已列 N / 共 M」口径：过滤后前端实际可见数（后端 count 是未过滤总数）
    count = len(files)
    return _browse_rows(files), count, next_cursor, {"collapsed": state["collapsed"], "loaded": state["loaded"]}
