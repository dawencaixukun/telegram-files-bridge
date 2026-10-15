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
import posixpath
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from core.state import (
    _SUB_RULES, _ARCHIVE_JOBS, _WATCH_SEEN, _WATCH_SEEN_MAX, _watch_save,
    _is_flood_wait_active, _tasks_cache_invalidate, _custom_notes_save, _archive_save
)
from core.backend import BACKEND
from core.logging import log, LOG_STORE
from core.config import (
    TG_WAIT_PHONE, TG_WAIT_CODE, TG_WAIT_PASSWORD, TG_WAIT_OTHER_DEVICE,
    _CUSTOM_FILE_NOTES, _is_meaningless_filename, _sanitize_note_filename
)

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
    from services.watermark_service import _is_disk_high_watermark_exceeded

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
        if hasattr(BACKEND, "_cache") and isinstance(getattr(BACKEND, "_cache", None), dict):
            BACKEND._cache.clear()
        _tasks_cache_invalidate()
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
        await sync_chat_custom_notes(tg, ch)
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

# 记录「发送链接的文本消息 ID」到「解析出的视频 uniqueId」的映射
# 支持用户在收藏里对链接消息直接左滑「回复」发备注
_LINK_MSG_TO_UID: Dict[str, str] = {}


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
        notes_by_link: Dict[str, str] = {}
        link_to_msg: Dict[str, Tuple[Any, Any]] = {}
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
                extracted = _extract_links(text)
                if extracted:
                    fresh_links.extend(extracted)
                    # 提取链接同条消息附带的备注文字（剔除 http 链接部分）
                    rem = re.sub(r"https?://\S+", "", text).strip()
                    rem_note = _sanitize_note_filename(rem) if rem else ""
                    for lk in extracted:
                        if rem_note:
                            notes_by_link[lk] = rem_note
                        link_to_msg[lk] = (ch, mid)

        if newly:
            _seen_put(key, newly)
            _watch_save()

        if not fresh_links:
            continue

        # 链接解析 + 入队（复用任务服务的两跳解析，含去重与熔断语义）
        try:
            from services.task_service import _resolve_links_to_files
            n, err = await _resolve_links_to_files(
                fresh_links,
                notes_by_link=notes_by_link,
                link_to_msg=link_to_msg,
            )
            if err:
                log.warning("监听链接解析失败（chat=%s）: %s", ch, err)
            total += n
            if n:
                log.info("监听自动解析 %d 条文本链接（chat=%s）", n, ch)
        except Exception as e:  # noqa: BLE001
            log.warning("监听链接入队异常（chat=%s）: %s", ch, e)
    return total


# ---------------------------------------------------------------------------
# 收藏/监听会话备注自动命名：
# 支持 3 种习惯：
#   1) 转发视频到收藏时直接带说明文字（Telegram 会先发一条同秒纯文本、紧跟转发视频）
#   2) 先把视频转到收藏，随后（120 秒内）紧接着发一条纯文字备注
#   3) 对收藏里的任意视频「回复（Reply）」一条纯文字备注（随时改名，已归档云端同步改名）
# ---------------------------------------------------------------------------
def parse_chat_history_notes(messages: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """从 TDLib GetChatHistory 消息列表中解析视频的自定义备注名。

    返回 {uid_or_msgkey: {"uid": str, "mid": int, "note": str, "explicit": bool, "file_name": str}}。
    """
    if not isinstance(messages, list) or not messages:
        return {}

    asc = sorted(
        (m for m in messages if isinstance(m, dict) and m.get("id") is not None),
        key=lambda m: (int(m.get("date") or 0), int(m.get("id") or 0)),
    )

    items: List[Dict[str, Any]] = []
    videos_by_mid: Dict[int, Dict[str, Any]] = {}
    albums: Dict[int, List[Dict[str, Any]]] = {}

    for m in asc:
        mid = int(m.get("id") or 0)
        date = int(m.get("date") or 0)
        album_id = int(m.get("mediaAlbumId") or 0)
        is_fwd = bool(m.get("forwardInfo"))
        content = m.get("content") or {}
        if not isinstance(content, dict):
            continue

        video_obj = content.get("video")
        if isinstance(video_obj, dict):
            raw_v = video_obj.get("video")
            v_inner: Dict[str, Any] = raw_v if isinstance(raw_v, dict) else {}
            raw_rem = v_inner.get("remote")
            remote: Dict[str, Any] = raw_rem if isinstance(raw_rem, dict) else {}
            uid = str(remote.get("uniqueId") or "")
            fn = str(video_obj.get("fileName") or "")
            fsize = int(v_inner.get("size") or v_inner.get("expectedSize") or video_obj.get("size") or 0)
            v_item = {
                "kind": "video",
                "mid": mid,
                "date": date,
                "album_id": album_id,
                "is_fwd": is_fwd,
                "uid": uid,
                "file_name": fn,
                "size": fsize,
                "note": "",
                "explicit": False,
                "has_pre_note": False,
            }
            items.append(v_item)
            videos_by_mid[mid] = v_item
            if album_id:
                albums.setdefault(album_id, []).append(v_item)
            continue

        text_obj = content.get("text")
        if isinstance(text_obj, dict) and not is_fwd:
            raw_text = str(text_obj.get("text") or "").strip()
            if not raw_text or raw_text.startswith("/"):
                continue
            if _extract_links(raw_text) or re.search(r"https?://", raw_text, re.I):
                continue
            note_lines = [
                _sanitize_note_filename(ln)
                for ln in raw_text.splitlines()
                if _sanitize_note_filename(ln) and len(_sanitize_note_filename(ln)) <= 80
            ]
            clean_note = note_lines[0] if note_lines else _sanitize_note_filename(raw_text)
            if not clean_note or len(clean_note) > 80:
                continue
            reply_to = m.get("replyTo")
            reply_mid = int(
                (reply_to.get("messageId") if isinstance(reply_to, dict) else None)
                or m.get("replyToMessageId")
                or 0
            )
            items.append({
                "kind": "note",
                "mid": mid,
                "date": date,
                "note": clean_note,
                "note_lines": note_lines,
                "reply_mid": reply_mid,
                "consumed": False,
            })

    def _size_sort_key(v: Dict[str, Any]) -> Tuple[float, int, int]:
        sz = int(v.get("size") or 0)
        return (float(sz) if sz > 0 else float("inf"), int(v.get("date") or 0), int(v.get("mid") or 0))

    def _assign_batch(
        batch: List[Dict[str, Any]],
        base_note: str,
        explicit: bool = False,
        is_pre: bool = False,
        note_lines: Optional[List[str]] = None,
    ) -> None:
        if not batch or not base_note:
            return
        if len(batch) == 1:
            batch[0]["note"] = base_note
            if explicit:
                batch[0]["explicit"] = True
            if is_pre:
                batch[0]["has_pre_note"] = True
        elif note_lines and len(note_lines) == len(batch):
            # 多行备注且行数与视频数一致：一行对应一个视频名
            for v, ln in zip(batch, note_lines):
                v["note"] = ln
                if explicit:
                    v["explicit"] = True
                if is_pre:
                    v["has_pre_note"] = True
        else:
            # 多视频共用单条备注时：按文件大小从小到大排序（最小为 1，最大为 N）
            ordered = sorted(batch, key=_size_sort_key)
            for idx, v in enumerate(ordered, 1):
                v["note"] = f"{base_note} {idx}"
                if explicit:
                    v["explicit"] = True
                if is_pre:
                    v["has_pre_note"] = True

    # Pass 1: 转发时附带说明（纯文本紧邻在转发视频之前，间隔 -2s ~ +20s）
    n_items = len(items)
    for i, it in enumerate(items):
        if it["kind"] != "note" or it["reply_mid"] or it["consumed"]:
            continue
        if i + 1 >= n_items or items[i + 1]["kind"] != "video":
            continue
        v_first = items[i + 1]
        dt = v_first["date"] - it["date"]
        if not (-2 <= dt <= 20):
            continue
        batch = [v_first]
        first_album = v_first["album_id"]
        j = i + 2
        while j < n_items and items[j]["kind"] == "video":
            vj = items[j]
            if first_album and vj["album_id"] == first_album:
                batch.append(vj)
                j += 1
            elif not first_album and vj["album_id"] == 0 and abs(vj["date"] - v_first["date"]) <= 15:
                batch.append(vj)
                j += 1
            else:
                break
        it["consumed"] = True
        _assign_batch(batch, it["note"], explicit=True, is_pre=True, note_lines=it.get("note_lines"))

    # Pass 2: 先转发视频到收藏，随后（120 秒内）紧跟一条纯文本备注
    for i, it in enumerate(items):
        if it["kind"] != "note" or it["reply_mid"] or it["consumed"]:
            continue
        if i - 1 < 0 or items[i - 1]["kind"] != "video":
            continue
        v_prev = items[i - 1]
        if v_prev["has_pre_note"] or v_prev["note"]:
            continue
        dt = it["date"] - v_prev["date"]
        if not (0 <= dt <= 120):
            continue
        if v_prev["album_id"] and v_prev["album_id"] in albums:
            batch = [v for v in albums[v_prev["album_id"]] if not v["has_pre_note"] and not v["note"]]
        else:
            # 支持连续转发多条独立视频（间隔 <= 15s）后紧跟一条备注
            batch = [v_prev]
            k = i - 2
            while k >= 0 and items[k]["kind"] == "video":
                vk = items[k]
                if vk["has_pre_note"] or vk["note"]:
                    break
                if abs(batch[0]["date"] - vk["date"]) <= 15:
                    batch.insert(0, vk)
                    k -= 1
                else:
                    break
        if batch:
            it["consumed"] = True
            _assign_batch(batch, it["note"], explicit=True, is_pre=False, note_lines=it.get("note_lines"))

    # Pass 3: 显式「回复（Reply）」某条视频或合集改名（最高优先级，可随时改名）
    for it in items:
        if it["kind"] != "note" or not it["reply_mid"]:
            continue
        target_v = videos_by_mid.get(it["reply_mid"])
        if not target_v:
            # 检查被回复的消息是否是一条链接消息（通过 _LINK_MSG_TO_UID 找到解析出的视频 uniqueId）
            link_uid = _LINK_MSG_TO_UID.get(f"msg:{it['reply_mid']}") or next(
                (uid for k, uid in _LINK_MSG_TO_UID.items() if k.endswith(f":{it['reply_mid']}")),
                None
            )
            if link_uid:
                target_v = next((v for v in items if v.get("kind") == "video" and v.get("uid") == link_uid), None)
                if not target_v:
                    _CUSTOM_FILE_NOTES[link_uid] = it["note"]
                    it["consumed"] = True
                    continue
        if not target_v:
            continue
        it["consumed"] = True
        alb_id = target_v.get("album_id") or 0
        alb_list = albums.get(alb_id) if alb_id else None
        if alb_list and len(alb_list) > 1:
            # 支持对合集左滑回复：
            # 1) 若写 "2 专属名"（序号+空格+名字），仅改合集中按大小排第 2 的视频
            m_idx = re.match(r"^(\d{1,2})\s+(.+)$", it["note"])
            ordered_alb = sorted(alb_list, key=_size_sort_key)
            if m_idx and (not it.get("note_lines") or len(it["note_lines"]) == 1):
                pick_i = int(m_idx.group(1))
                sub_name = _sanitize_note_filename(m_idx.group(2))
                if 1 <= pick_i <= len(ordered_alb) and sub_name:
                    _assign_batch([ordered_alb[pick_i - 1]], sub_name, explicit=True, is_pre=False)
                    continue
            # 2) 若写多行名字或写 1 个总名字，自动作用于整个合集全部视频（按文件大小 1, 2, ...）
            _assign_batch(ordered_alb, it["note"], explicit=True, is_pre=False, note_lines=it.get("note_lines"))
        else:
            _assign_batch([target_v], it["note"], explicit=True, is_pre=False)

    # 若同一视频（相同 uid）被重新转发并附带了新备注，以最新一条有备注的消息为准
    latest_note_by_uid: Dict[str, Tuple[str, bool]] = {}
    for it in items:
        if it["kind"] == "video" and it["uid"] and it["note"]:
            latest_note_by_uid[it["uid"]] = (it["note"], bool(it["explicit"]))
    for it in items:
        if it["kind"] == "video" and it["uid"] in latest_note_by_uid:
            it["note"], it["explicit"] = latest_note_by_uid[it["uid"]]

    # Pass 4: 全局去重——若不同视频（不同 uid）赋了相同的备注名，自动递增 "备注 2", "备注 3"
    used_notes: Dict[str, str] = {}
    for it in items:
        if it["kind"] != "video" or not it["note"]:
            continue
        key = it["uid"] or f"mid:{it['mid']}"
        base = it["note"]
        candidate = base
        idx = 2
        while candidate.lower() in used_notes and used_notes[candidate.lower()] != key:
            candidate = f"{base} {idx}"
            idx += 1
        it["note"] = candidate
        used_notes[candidate.lower()] = key

    out: Dict[str, Dict[str, Any]] = {}
    for it in items:
        if it["kind"] == "video" and it["note"]:
            key = it["uid"] or f"mid:{it['mid']}"
            out[key] = {
                "uid": it["uid"],
                "mid": it["mid"],
                "note": it["note"],
                "explicit": it["explicit"],
                "file_name": it["file_name"],
            }
    return out


async def _auto_rename_archived_job(job: Dict[str, Any], new_stem: str) -> bool:
    """当已归档上云的视频收到新备注时，自动调用 OpenList /api/fs/rename 同步重命名云端文件。"""
    from services.openlist_service import _openlist_token, _openlist_relogin, _openlist_env
    from core.state import _openlist_client

    old_path = str(job.get("remote_path") or "").strip().replace("\\", "/")
    if not old_path or old_path == "/":
        return False
    old_path_norm = posixpath.normpath("/" + old_path.lstrip("/"))
    old_name = posixpath.basename(old_path_norm)
    parent_dir = posixpath.dirname(old_path_norm)
    if not old_name or not parent_dir or parent_dir == "/":
        return False
    _, ext = posixpath.splitext(old_name)
    if not ext:
        ext = ".mp4"
    clean_stem = _sanitize_note_filename(new_stem)
    if not clean_stem:
        return False
    if clean_stem.lower().endswith(ext.lower()):
        target_name = clean_stem
        clean_stem = posixpath.splitext(clean_stem)[0]
    else:
        target_name = f"{clean_stem}{ext}"

    if target_name == old_name:
        job["applied_note"] = new_stem
        return False

    try:
        token = await _openlist_token()
    except Exception:  # noqa: BLE001
        return False

    # 检查同目录下是否已有同名文件，若冲突则自动追加 (2), (3)...
    try:
        resp = await _openlist_client.post(
            "/api/fs/list",
            json={"path": parent_dir, "password": "", "page": 1, "per_page": 500, "refresh": False},
            headers={"Authorization": token},
        )
        code, data, _ = _openlist_env(resp)
        if resp.status_code == 401 or code in (401, 403):
            token = await _openlist_relogin()
            resp = await _openlist_client.post(
                "/api/fs/list",
                json={"path": parent_dir, "password": "", "page": 1, "per_page": 500, "refresh": False},
                headers={"Authorization": token},
            )
            code, data, _ = _openlist_env(resp)
        if code == 200:
            existing = {str(c.get("name") or "") for c in (data.get("content") or [])}
            idx = 2
            while target_name in existing and target_name != old_name:
                target_name = f"{clean_stem} ({idx}){ext}"
                idx += 1
    except Exception:  # noqa: BLE001
        pass

    try:
        resp = await _openlist_client.post(
            "/api/fs/rename",
            json={"path": old_path_norm, "name": target_name},
            headers={"Authorization": token},
        )
        code, _, msg = _openlist_env(resp)
        if resp.status_code == 401 or code in (401, 403):
            token = await _openlist_relogin()
            resp = await _openlist_client.post(
                "/api/fs/rename",
                json={"path": old_path_norm, "name": target_name},
                headers={"Authorization": token},
            )
            code, _, msg = _openlist_env(resp)
        if resp.status_code != 200 or code != 200:
            log.warning("收藏备注自动改名失败 (%s -> %s): %s", old_name, target_name, msg or code)
            return False
    except Exception as e:  # noqa: BLE001
        log.warning("收藏备注自动改名异常 (%s -> %s): %s", old_name, target_name, e)
        return False

    new_path = posixpath.join(parent_dir, target_name)
    old_lc = old_path_norm.lower()
    for j in _ARCHIVE_JOBS.values():
        if str(j.get("remote_path") or "").strip().replace("\\", "/").lower() == old_lc:
            j["remote_path"] = new_path
            j["filename"] = target_name
            j["applied_note"] = new_stem
    _archive_save()
    try:
        import services.archive_service as _arch_mod
        getattr(_arch_mod, "_OPENLIST_DIR_LIST_CACHE", {}).clear()
    except Exception:  # noqa: BLE001
        pass
    LOG_STORE.append("INFO", f"收藏备注自动重命名云端文件：{old_name} → {target_name}", task_id=str(job.get("id") or ""))
    log.info("收藏备注自动重命名云端文件：%s -> %s", old_path_norm, new_path)
    return True


async def sync_chat_custom_notes(tg_id: Any, chat_id: Any, limit: int = 40) -> int:
    """拉取指定会话最近消息，识别用户备注并写入 _CUSTOM_FILE_NOTES；对已归档无名文件自动重命名。"""
    if not hasattr(BACKEND, "telegram_api"):
        return 0
    from core.backend import telegram_api_call
    try:
        res = await telegram_api_call("GetChatHistory", {
            "chatId": int(chat_id),
            "fromMessageId": 0,
            "offset": 0,
            "limit": limit,
            "onlyLocal": False,
        }, timeout=12.0)
    except Exception:  # noqa: BLE001
        return 0
    if not isinstance(res, dict):
        return 0
    msgs = res.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return 0

    # 若存在左滑「回复」较早历史视频的情况（被回复的 messageId 不在最近 limit 条内），按需通过 GetMessage 补齐
    seen_mids = {int(m.get("id") or 0) for m in msgs if isinstance(m, dict)}
    missing_reply_mids: Set[int] = set()
    for m in msgs:
        if not isinstance(m, dict):
            continue
        reply_to = m.get("replyTo")
        rmid = int(
            (reply_to.get("messageId") if isinstance(reply_to, dict) else None)
            or m.get("replyToMessageId")
            or 0
        )
        if rmid and rmid not in seen_mids:
            missing_reply_mids.add(rmid)
    for rmid in list(missing_reply_mids)[:5]:
        try:
            rmsg = await telegram_api_call("GetMessage", {
                "chatId": int(chat_id),
                "messageId": rmid,
            }, timeout=6.0)
            if isinstance(rmsg, dict) and rmsg.get("id"):
                msgs.append(rmsg)
                seen_mids.add(int(rmsg.get("id") or 0))
        except Exception:  # noqa: BLE001
            pass

    parsed = parse_chat_history_notes(msgs)
    if not parsed:
        return 0

    changed = 0
    for item in parsed.values():
        uid = item["uid"]
        mid = item["mid"]
        note = item["note"]
        explicit = item["explicit"]
        if uid:
            if _CUSTOM_FILE_NOTES.get(uid) != note:
                _CUSTOM_FILE_NOTES[uid] = note
                changed += 1
        if mid:
            mkey = f"msg:{chat_id}:{mid}"
            if _CUSTOM_FILE_NOTES.get(mkey) != note:
                _CUSTOM_FILE_NOTES[mkey] = note
                changed += 1

        # 检查是否已有归档任务需要同步改名
        if uid:
            for j in list(_ARCHIVE_JOBS.values()):
                if str(j.get("unique_id") or "") != uid:
                    continue
                st = str(j.get("state") or "")
                cur_fn = str(j.get("filename") or "")
                if st == "done" and j.get("applied_note") != note:
                    if explicit or _is_meaningless_filename(cur_fn):
                        await _auto_rename_archived_job(j, note)

    if changed:
        _custom_notes_save()
        if hasattr(BACKEND, "_cache") and isinstance(getattr(BACKEND, "_cache", None), dict):
            BACKEND._cache.clear()
        _tasks_cache_invalidate()
    return changed


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


