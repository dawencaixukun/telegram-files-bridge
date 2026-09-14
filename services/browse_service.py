# -*- coding: utf-8 -*-
"""
services/browse_service.py — 聊天资源浏览与流式分页去重服务
============================================================
负责账号聊天树构建、频道媒体按类检索、消息级转发副本折叠去重与资源浏览行渲染。
"""
import json
import os
import time
import asyncio
from typing import Any, Dict, List, Tuple
from core.config import _pick, _fmt_size, _fmt_time, _human_name
from core import config as _config
from core.state import _archive_index_snapshot
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
_BROWSE_TYPE_LABELS = {
    "video": "视频", "photo": "图片", "audio": "音频", "document": "文档",
    "file": "文件", "animation": "动图", "url": "链接", "media": "媒体",
    "unknown": "未知",
}

_BROWSE_SEEN: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
_BROWSE_SEEN_TTL = 1800.0
_BROWSE_SEEN_MAX = 64

CHAT_SOURCE_CACHE: Dict[str, Any] = {"key": None, "value": None}

# ---------------------------------------------------------------------------
# 侧栏会话置顶（用户手动「只留想要的会话」）
#
# 侧栏会话来自账号的 dialog 列表（可能上百个，且绝大多数与归档无关）。用置顶
# 列表做**过滤收窄**：一旦用户置顶过任意会话，侧栏只展示置顶会话，其余收起。
# 值为 **chatId 字符串**，与 account-tree 端点返回的 chatId 类型一致，无需关心
# 是数字 ID 还是 @username。
# 每个进程各自持有内存集合 + 落盘 JSON；进程外改文件不会即时生效（与
# .subscriptions.json 等既有状态文件同一取舍）。
# ---------------------------------------------------------------------------
_BROWSE_PIN_FILE = ".browse_pins.json"
_BROWSE_PINS: set = set()
_BROWSE_PINS_LOADED = False


def _browse_pin_path() -> str:
    # 每次按当前 APP_ROOT_DIR 解析：测试会切 TG_DATA_DIR，不能模块加载时固化。
    return os.path.join(_config.APP_ROOT_DIR, _BROWSE_PIN_FILE)


def _browse_pins_ensure_loaded() -> None:
    """首次访问时懒加载。

    不在模块导入期加载：core.state 已经把「状态文件统一在状态层加载」定为单点，
    这里再插一个导入期加载会重新引入多条加载路径；而懒加载对 TG_DATA_DIR 的
    切换（测试环境）天然正确。
    """
    global _BROWSE_PINS_LOADED
    if not _BROWSE_PINS_LOADED:
        _BROWSE_PINS_LOADED = True
        _browse_pins_load()


def _browse_pins_load() -> None:
    """启动/首次访问时从磁盘恢复置顶列表。文件缺失或损坏都不应影响页面。"""
    global _BROWSE_PINS
    try:
        with open(_browse_pin_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("pins") if isinstance(data, dict) else data
        _BROWSE_PINS = {str(x) for x in raw} if isinstance(raw, list) else set()
        if _BROWSE_PINS:
            log.info("已恢复侧栏置顶会话: %d 个", len(_BROWSE_PINS))
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("侧栏置顶会话恢复失败: %s", e)


def _browse_pins_save() -> None:
    path = _browse_pin_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = json.dumps({"pins": sorted(_BROWSE_PINS)}, ensure_ascii=False).encode("utf-8")
        # tmp + os.replace 原子写：直接 O_TRUNC 直写时崩溃会留下被截断的 JSON，
        # 下次启动整个置顶列表就丢了（与 _archive_config_save 同一处理）。
        tmp = f"{path}.tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        log.warning("侧栏置顶会话持久化失败: %s", e)


def _browse_pins_all() -> List[str]:
    _browse_pins_ensure_loaded()
    return sorted(_BROWSE_PINS)


def _browse_pins_clear() -> int:
    """一键清空全部置顶。返回被清除的条数。

    单次落盘而不是逐个删除：逐条调用会产生 N 次磁盘写入与 N 个中间状态。
    """
    _browse_pins_ensure_loaded()
    n = len(_BROWSE_PINS)
    if n:
        _BROWSE_PINS.clear()
        _browse_pins_save()
        log.info("已一键清空侧栏置顶会话: %d 个", n)
    return n


def _browse_pins_apply_many(chat_ids: Any, pinned: bool) -> int:
    """批量置顶/取消置顶。返回实际变更的条数。

    单次落盘：逐个 _browse_pin_apply 会有 N 次磁盘写入，且中途异常会留下
    「改了一半」的状态。这里先在内存里改完再写一次。
    """
    _browse_pins_ensure_loaded()
    # 必须显式拒绝字符串：迭代 str 会逐字符产出，把 "notalist" 变成
    # a/i/l/n/o/s/t 七个"会话 ID"写进置顶列表（实测复现）。路由层虽已校验
    # 是 list，但服务函数自身也要挡住 —— 它会被其它调用方直接使用。
    if not isinstance(chat_ids, (list, tuple, set)):
        return 0
    ids = [str(c).strip() for c in chat_ids if str(c).strip()]
    if not ids:
        return 0
    changed = 0
    for cid in ids:
        if pinned:
            if cid not in _BROWSE_PINS:
                _BROWSE_PINS.add(cid)
                changed += 1
        else:
            if cid in _BROWSE_PINS:
                _BROWSE_PINS.discard(cid)
                changed += 1
    if changed:
        _browse_pins_save()
        log.info("批量%s侧栏置顶会话: %d 个", "设置" if pinned else "取消", changed)
    return changed


def _browse_pin_apply(chat_id: Any, pinned: bool) -> bool:
    """置顶/取消置顶。返回操作后该会话是否处于置顶态。"""
    _browse_pins_ensure_loaded()
    cid = str(chat_id or "").strip()
    if not cid:
        return False
    if pinned:
        _BROWSE_PINS.add(cid)
    else:
        _BROWSE_PINS.discard(cid)
    _browse_pins_save()
    return cid in _BROWSE_PINS


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


async def _browse_tree(force: bool = False, full: bool = False) -> List[Dict[str, Any]]:
    """构建账号→会话树。

    full=False（默认）：应用置顶收窄 —— 置顶过任意会话时，侧栏只保留置顶项
    （收藏始终保留），避免把账号里上百个无关会话全罗列出来。
    full=True：返回**完整**列表，不做收窄。供「管理会话」面板列候选使用 ——
    面板必须能看到全部会话，否则用户一旦置顶过，就再也无法把其它会话加回侧栏。
    """
    _browse_pins_ensure_loaded()
    tree: List[Dict[str, Any]] = []
    try:
        telegrams = await BACKEND.list_telegrams(force=force)
    except Exception as e:  # noqa: BLE001
        log.warning("browse tree: list_telegrams 失败: %s", e)
        return tree
    if not isinstance(telegrams, list):
        return tree
    # 置顶收窄：一旦用户置顶过任意会话，侧栏只保留置顶项（收藏始终保留），
    # 避免把账号里上百个无关会话全罗列出来。full=True 时跳过收窄。
    pins = _BROWSE_PINS
    filtering = bool(pins) and not full
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
                saved = str(cid) == str(tg_id)
                pinned = str(cid) in pins
                if filtering and not pinned and not saved:
                    continue
                title = str(_pick(ch, "title", "name", "channel", "chatName", default="聊天"))
                items.append({
                    "chatId": cid,
                    "title": "收藏 (Saved Messages)" if saved else title,
                    "saved": saved,
                    "pinned": pinned,
                })
        # 排序：收藏恒第 1（用户要求：收藏是默认展示的硬性标准，必须置顶），
        # 其余按置顶优先。saved 与 pinned 互斥（saved 即置顶）。
        items.sort(key=lambda c: (0 if c["saved"] else (1 if c["pinned"] else 2)))
        tree.append({
            "telegramId": tg_id,
            "name": (str(_pick(tg, "name", "username", "phone", default="TG 账号")).strip() or "TG 账号"),
            "chats": items,
        })
    return tree


def _browse_rows(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    # 一次性归档索引：下面逐条记录都要查归档表，逐个全表扫描在归档量大时是主要开销
    _ai = _archive_index_snapshot()
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
        arch_job = _ai.registry_lookup(uid_str, name, rec.get("size"))
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
    # 一次性归档索引：补页循环里逐文件查归档表（hide_archived 时）
    _ai = _archive_index_snapshot()
    if type_ not in {t for t, _ in _BROWSE_TYPES}:
        type_ = "document"
    params: Dict[str, Any] = {"type": type_, "limit": limit}
    if cursor:
        params["fromMessageId"] = cursor
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
            if hide_archived and _ai.registry_lookup(str(f.get("uniqueId") or ""), _human_name(f), f.get("size")):
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
