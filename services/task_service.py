# -*- coding: utf-8 -*-
"""
services/task_service.py — 任务聚合引擎与状态监控服务
======================================================
负责跨账号/跨聊天任务聚合、FileRecord 状态转换、总览 KPI 统计与任务状态跃迁告警对账。
"""
import os
import time
import json
import shutil
import hashlib
import asyncio
import datetime as _dt
from urllib.parse import urlsplit
from typing import Any, Dict, List, Optional, Tuple

from core.config import (
    APP_ROOT_DIR, BASE_DIR, CACHE_TTL, TG_READY, _pick, _pick_id,
    _fmt_size, _fmt_time, _match_size_bucket, _classify_file_type,
    ALLOWED_TG_DOMAINS, RE_TG_PRIVATE, RE_TG_PUBLIC, _LINK_PATTERNS, _tg_err_public,
    _resolve_host_local_path
)
from core.templates import _stages, _spark_points, _speed_chart_points
from core.state import (
    _TASKS_CACHE, _WAITING_DISK_TASKS, _FLOOD_WAIT_STATE,
    _format_waiting_disk_task, _format_flood_wait_task,
    _is_flood_wait_active, _get_flood_wait_status,
    _DELETED_LOCAL_UIDS, _alert_state, _ALERT_EVENT_LIMIT, _ALERTS_MAX,
    _archive_latest_raw_of, _ARCHIVE_JOBS
)
from core.backend import BACKEND, chat_sources
from core.logging import log, LOG_STORE

_TASK_STATUSES = ("downloading", "paused", "completed", "error")
_CHAT_TITLE_CACHE: Dict[str, Any] = {"expire": 0.0, "value": None}

# 任务队列展示优先级：进行中的任务必须浮到最前，已归档的沉到最后。
# 此前 tasks_all() 直接沿用后端返回顺序（本质是数据库返回序，对用户无意义），
# 结果「下载中」的一条被几十条「已归档」历史记录淹没，用户得滚屏才能找到
# 真正在跑的任务。这里按状态分层，层内新的排前面。
_TASK_RANK = {
    "download": 0,       # 下载中 —— 最前
    "upload": 0,         # 归档上传中
    "verify": 0,         # 校验中
    "waiting_disk": 1,   # 磁盘/风控挂起（等资源，但仍是活跃任务）
    "pending": 1,        # 待处理
    "failed": 2,         # 失败待重试（需要用户注意）
    "downloaded": 3,     # 已下载待归档（可操作）
    "archived": 4,       # 已归档 —— 沉底
    "isolated": 5,       # 隔离
}


def _task_sort_key(t: Dict[str, Any]) -> Tuple[int, float, str]:
    """任务排序键：(状态优先级, 时间倒序, id 兜底)。

    时间用文件日期 _date_ts（秒），缺失记 0；末尾用 id 兜底是为了让
    同优先级、同时间的记录有确定顺序，避免每次刷新行序跳动。
    """
    rank = _TASK_RANK.get(str(t.get("status") or ""), 6)
    try:
        ts = float(t.get("_date_ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    return (rank, -ts, str(t.get("id") or ""))


def _local_stock_exists(task: Dict[str, Any]) -> bool:
    """该任务对应的本地文件是否真实在存（磁盘存在 + 未标记删除）。

    与 /library/local 页面的 _is_local_in_stock / _enrich_archive 门禁
    保持完全同一口径，供侧边栏「本地在存」徽标计数使用——两个数字
    必须指向同一批文件。口径：以「文件是否物理在盘」为准，
    不看下载状态（与页面一致：文件在盘就占着磁盘，就该显示）。
    """
    uid = str(task.get("_unique_id") or "")
    if uid and uid in _DELETED_LOCAL_UIDS:
        return False
    # 与 _enrich_archive 相同的路径解析（兼容 Docker 容器路径映射）
    lp = _resolve_host_local_path(str(task.get("local_path") or ""))
    if not lp or lp == "—":
        return False
    try:
        return os.path.exists(lp)
    except OSError:
        return False


def _stable_task_id(unique_id: Any, index: int) -> int:
    if unique_id:
        h = hashlib.sha1(str(unique_id).encode()).hexdigest()[:12]
        return int(h, 16) >> 4
    return index


def _human_name(rec: Dict[str, Any]) -> str:
    name = _pick(rec, "fileName", "filename", "title", "name", "message", default="")
    if name:
        return str(name)
    chat = _pick(rec, "chatTitle", "chat_title", "chatName", "channel", default="")
    return "文件 " + str(_pick_id(rec) or (chat or ""))


def _chat_title(rec: Dict[str, Any]) -> str:
    return str(_pick(rec, "chatTitle", "chat_title", "chatName", "channel", default=""))


def _to_task(rec: Dict[str, Any], index: int, chat_titles: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    dl_status = str(rec.get("downloadStatus") or "").strip().lower()
    unique_id = _pick_id(rec)
    uid_str = str(unique_id or "")

    arch_job = _archive_latest_raw_of(uid_str) if uid_str else None
    cloud_path = "—"
    if arch_job and arch_job.get("remote_path"):
        cloud_path = str(arch_job.get("remote_path"))

    if dl_status == "completed":
        if arch_job and arch_job.get("state") == "done":
            status = "archived"
        elif arch_job and arch_job.get("state") == "uploading":
            status = "upload"
        else:
            status = "downloaded"
    elif dl_status == "downloading":
        status = "download"
    elif dl_status == "error":
        status = "failed"
    else:
        status = "pending"

    size = rec.get("size")
    msg_id = rec.get("messageId")
    chat_id = rec.get("chatId")
    titles = chat_titles or {}
    chat = titles.get(str(chat_id)) or _chat_title(rec) or "未知频道"
    size_str = _fmt_size(size)

    # 下载进度与归档上传进度是两个完全不同的量，绝不能混用同一个 progress 字段：
    # 历史上 status=='upload' 时 progress 被直接覆盖成上传百分比（且常是 6% 这种很小的值），
    # 而模板始终按「已下载 x%」渲染，于是用户在归档阶段看到「已下载 6%」，
    # 误判成下载失败/卡住。这里分别落盘，并给出明确的口径标签供模板使用。
    dl = rec.get("downloadedSize")
    download_progress = None
    if dl is not None and size:
        try:
            download_progress = max(0, min(100, int(float(dl) / float(size) * 100)))
        except (TypeError, ValueError, ZeroDivisionError):
            download_progress = None

    # 字节级下载快照：卡片化/表格化渲染方都能拿到精确的已下载字节，
    # 速率计算（_enrich_download_speed）也依赖它做相邻两次采样的差值。
    dl_bytes = None
    try:
        if dl is not None:
            dl_bytes = max(0, int(float(dl)))
    except (TypeError, ValueError):
        dl_bytes = None

    upload_progress = None
    if status == "upload" and arch_job:
        try:
            upload_progress = max(0, min(100, int(arch_job.get("progress") or 0)))
        except (TypeError, ValueError):
            upload_progress = 0

    # 字节级上传快照：归档任务只有进度百分比，按 size × progress 估算已上传量，
    # 供 _enrich_download_speed 差分出上传速率。
    ul_bytes = None
    if status == "upload" and arch_job:
        try:
            j_size = int(arch_job.get("size_bytes") or 0)
            j_prog = float(arch_job.get("progress") or 0)
            if j_size > 0:
                ul_bytes = int(j_size * max(0.0, min(100.0, j_prog)) / 100.0)
        except (TypeError, ValueError):
            ul_bytes = None

    if status == "upload":
        progress = upload_progress if upload_progress is not None else 0
        progress_kind = "upload"
    else:
        progress = download_progress
        progress_kind = "download" if download_progress is not None else ""
    progress_label = {"upload": "已上传", "download": "已下载"}.get(progress_kind, "已下载")

    local_path = rec.get("localPath") or ""

    date_ts = None
    try:
        val = float(rec.get("date") or 0)
        if val > 0:
            date_ts = val if val < 1e12 else val / 1000.0
    except (TypeError, ValueError):
        pass

    return {
        "id": _stable_task_id(unique_id, index),
        "time": _fmt_time(rec.get("date")),
        "source": chat,
        "msg_id": msg_id,
        "filename": _human_name(rec),
        "size": size_str,
        "status": status,
        "loaded": f"{progress}%" if progress is not None else "—",
        "progress": progress if progress is not None else 0,
        # 进度口径：模板据此渲染「已下载 / 已上传」，避免把归档上传进度标成已下载。
        "progress_kind": progress_kind,
        "progress_label": progress_label,
        "download_progress": download_progress if download_progress is not None else 0,
        "upload_progress": upload_progress if upload_progress is not None else 0,
        "local_path": str(local_path) if local_path else "—",
        "cloud_path": cloud_path,
        "source_url": f"https://t.me/c/{chat_id}/{msg_id}" if (msg_id and chat_id) else "",
        "error_msg": "",
        "stages": _stages(status, rec, arch_job),
        "_telegram_id": rec.get("telegramId"),
        "_chat_id": chat_id,
        "_unique_id": unique_id,
        "_file_id": rec.get("id"),
        "_type": rec.get("type"),
        "_mimeType": rec.get("mimeType"),
        "_transfer_status": rec.get("transferStatus"),
        "_download_status": rec.get("downloadStatus"),
        "_thumb": str(rec.get("thumbnail") or ""),
        "_thumb_uid": str(rec.get("thumbnailUniqueId") or ""),
        "_archived_at": rec.get("completionDate"),
        "_archived_at_formatted": _fmt_time(rec.get("completionDate")),
        "_date_ts": date_ts,
        "_size_bytes": size if isinstance(size, (int, float)) else None,
        "_dl_bytes": dl_bytes,
        "_dl_sampled_at": time.time(),
        "_ul_bytes": ul_bytes,
        "_ul_sampled_at": time.time(),
    }


def _to_local_file_from_task(task: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "filename": task.get("filename", "—"),
        "size": task.get("size", "—"),
        "source": task.get("source", "—"),
        "time": task.get("time", "—"),
        "status": task.get("status", "pending"),
        "local_path": task.get("local_path"),
        "_file_id": task.get("_file_id"),
        "_download_status": task.get("_download_status"),
        "_transfer_status": task.get("_transfer_status"),
        "_type": task.get("_type"),
        "_mimeType": task.get("_mimeType"),
        "_size_bytes": task.get("_size_bytes"),
        "_unique_id": task.get("_unique_id"),
        "_chat_id": task.get("_chat_id"),
        "_telegram_id": task.get("_telegram_id"),
        "_msg_id": task.get("msg_id"),
        "_thumb": task.get("_thumb"),
        "_thumb_uid": task.get("_thumb_uid"),
    }


def _to_cloud_file_from_task(task: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "archived_time": task.get("_archived_at_formatted") or task.get("time", "—"),
        "filename": task.get("filename", "—"),
        "size": task.get("size", "—"),
        "cloud_path": "—",
        "status": task.get("status", "archived"),
    }


async def _chat_title_map() -> Dict[str, str]:
    if _CHAT_TITLE_CACHE["expire"] > time.monotonic():
        return _CHAT_TITLE_CACHE["value"] or {}
    out: Dict[str, str] = {}
    try:
        from core.backend import chat_sources
        sources = await chat_sources()
        for s in sources:
            if s.get("chatId") is not None:
                out[str(s["chatId"])] = s["title"]
    except Exception as e:  # noqa: BLE001
        log.warning("chat_title_map 失败: %s", e)
    _CHAT_TITLE_CACHE["expire"] = time.monotonic() + CACHE_TTL
    _CHAT_TITLE_CACHE["value"] = out
    return out


async def _build_tasks(force: bool = False) -> List[Dict[str, Any]]:
    tasks: List[Dict[str, Any]] = []
    have_all_files = False
    try:
        raw = await BACKEND.list_all_files_page_info(force=force)
        all_files = BACKEND._unwrap_files(raw)
        have_all_files = True
        records: List[Dict[str, Any]] = list(all_files)
        if isinstance(raw, dict):
            cursor = 0
            for _ in range(20):
                try:
                    nxt = int(raw.get("nextFromMessageId") or 0)
                except (TypeError, ValueError):
                    break
                if not nxt or nxt == cursor or len(all_files) < 500:
                    break
                cursor = nxt
                raw = await BACKEND._request("GET", "/files", params={"limit": 500, "fromMessageId": cursor})
                page = BACKEND._unwrap_files(raw)
                if not page:
                    break
                records.extend(page)
        titles = await _chat_title_map()
        for i, rec in enumerate(records):
            try:
                if rec.get("downloadStatus") not in _TASK_STATUSES:
                    continue
                tasks.append(_to_task(rec, i + 1, titles))
            except Exception as e:  # noqa: BLE001
                log.warning("to_task 跳过期 record: %s", e)
        return tasks
    except Exception as e:  # noqa: BLE001
        log.warning("list_all_files 失败，回退逐聊天: %s", e)
    if have_all_files:
        return tasks

    from core.backend import chat_sources
    try:
        sources = await chat_sources(force=force)
    except Exception as e:  # noqa: BLE001
        log.warning("chat_sources 失败: %s", e)
        return tasks
    idx = 0
    titles = {s.get("chatId") and str(s["chatId"]): s["title"] for s in sources}
    for s in sources:
        try:
            files = await BACKEND.list_files_all_pages(s["telegramId"], s["chatId"], force=force)
        except Exception as e:  # noqa: BLE001
            log.warning("list_files(%s,%s) 失败: %s", s["telegramId"], s["chatId"], e)
            continue
        if not files:
            continue
        for rec in files:
            try:
                if rec.get("downloadStatus") not in _TASK_STATUSES:
                    continue
                idx += 1
                t = _to_task(rec, idx, titles)
                if t.get("_chat_id") is None:
                    t["_chat_id"] = s["chatId"]
                if t.get("_telegram_id") is None:
                    t["_telegram_id"] = s["telegramId"]
                tasks.append(t)
            except Exception as e:  # noqa: BLE001
                log.warning("to_task 跳过期 record: %s", e)
    return tasks


# KPI 速率迷你趋势的滚动历史（最近 14 个采样点）：
# 每次仪表盘渲染（或 /api/speeds 轮询）追加一条 {dl, ul}，超出窗口弹出。
_SPEED_HISTORY: List[Dict[str, float]] = []
_SPEED_HISTORY_MAX = 14


def _record_speed_sample(dl_bps: float, ul_bps: float) -> None:
    """追加一次速率采样到滚动历史（供 KPI 迷你趋势使用）。"""
    _SPEED_HISTORY.append({"dl": float(dl_bps or 0), "ul": float(ul_bps or 0)})
    if len(_SPEED_HISTORY) > _SPEED_HISTORY_MAX:
        del _SPEED_HISTORY[:len(_SPEED_HISTORY) - _SPEED_HISTORY_MAX]


_DL_SPEED_STATE: Dict[str, Dict[str, float]] = {}
_UL_SPEED_STATE: Dict[str, Dict[str, float]] = {}


async def _running_count(tasks: List[Dict[str, Any]]) -> int:
    return sum(1 for t in tasks if t["status"] in ("download", "upload", "verify"))


async def _isolated_count(tasks: List[Dict[str, Any]]) -> int:
    return sum(1 for t in tasks if t["status"] == "isolated")




def _fmt_speed(bps: float) -> str:
    """字节速率 → 人类可读（MB/s 优先，小流量用 KB/s）。"""
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps:.0f} B/s"


def _fmt_total_bytes(n: int) -> Tuple[str, str]:
    """累计字节总量 → (数值, 单位) 二元组，供 KPI 卡片「值<small>单位</small>」结构。"""
    num = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024.0:
            return (f"{num:.0f}", unit) if unit == "B" else (f"{num:.1f}", unit)
        num /= 1024.0
    return (f"{num:.1f}", "PB")


def _sample_speed(state: Dict[str, Dict[str, float]], uid: str,
                  cur_bytes: Optional[float], sampled_at: float,
                  min_interval: float = 1.0) -> Optional[float]:
    """单任务速率采样：与上次快照做差值。返回 bytes/s，无有效基准时 None。

    历史缺陷：速率此前要靠用户手动刷新整页两次心算差值。这里把每次
    回源拉到的字节数快照存入 state，与上一次差分得到真实速率；
    字节倒退（任务重开）时不产生负速率，基准直接重置为当前值。
    """
    now = time.time()
    prev = state.get(uid)
    speed = None
    if (prev is not None and cur_bytes is not None
            and cur_bytes >= prev.get("bytes", 0)
            and now - prev.get("at", 0) > min_interval):
        dt = sampled_at - prev["at"]
        if dt > 0.5:
            speed = (cur_bytes - prev["bytes"]) / dt
    if cur_bytes is not None:
        state[uid] = {"bytes": cur_bytes, "at": sampled_at}
    return speed


def _enrich_download_speed(tasks: List[Dict[str, Any]]) -> None:
    """就地补算每个任务的下载/上传速率（bytes/s）。

    下载速率来自 Java 后端的 downloadedSize（状态 download），
    上传速率来自归档引擎 _ARCHIVE_JOBS 的 uploaded/progress
    （状态 upload）。缓存命中时 tasks_all() 直接返回旧列表不经过
    这里，速率最多滞后一个 CACHE_TTL(8s)，与进度条口径一致。
    """
    now = time.time()
    for t in tasks:
        st = str(t.get("status") or "")
        uid = str(t.get("_unique_id") or "")
        if st == "download":
            if not uid:
                continue
            # 状态切换到下载：清理上传快照，防止旧基准残留
            _UL_SPEED_STATE.pop(uid, None)
            speed = _sample_speed(_DL_SPEED_STATE, uid,
                                  t.get("_dl_bytes"),
                                  t.get("_dl_sampled_at") or now)
            if speed is not None:
                t["speed"] = max(0.0, speed)
                t["speed_label"] = _fmt_speed(t["speed"])
        elif st == "upload":
            if not uid:
                continue
            # 状态切换到上传：清理下载快照，防止旧基准残留
            _DL_SPEED_STATE.pop(uid, None)
            speed = _sample_speed(_UL_SPEED_STATE, uid,
                                  t.get("_ul_bytes"),
                                  t.get("_ul_sampled_at") or now)
            if speed is not None:
                t["speed"] = max(0.0, speed)
                t["speed_label"] = _fmt_speed(t["speed"])
        else:
            _DL_SPEED_STATE.pop(uid, None)
            _UL_SPEED_STATE.pop(uid, None)


async def _upload_speed_snapshot() -> float:
    """归档引擎当前总上传速率（bytes/s）。

    上传中的任务在 _ARCHIVE_JOBS 里有 progress 字段，但没有精确的
    uploaded 字节数；这里按 size_bytes × progress 估算当前已上传量，
    与上次快照差分。无上传任务时返回 0 并清理基准。
    """
    now = time.time()
    total = 0
    for j in _ARCHIVE_JOBS.values():
        if str(j.get("state") or "") != "uploading":
            continue
        size = int(j.get("size_bytes") or 0)
        prog = float(j.get("progress") or 0)
        total += int(size * max(0.0, min(100.0, prog)) / 100.0)
    jid = "__upload_total__"
    prev = _UL_SPEED_STATE.get(jid)
    speed = None
    if prev is not None and now - prev.get("at", 0) > 1.0:
        dt = now - prev["at"]
        if dt > 0.5:
            speed = max(0.0, (total - prev["bytes"]) / dt)
    _UL_SPEED_STATE[jid] = {"bytes": total, "at": now}
    return speed if speed is not None else 0.0


async def _download_speed_snapshot() -> float:
    """当前总下载速率（bytes/s）：对所有下载中任务的最新速率求和。

    每个任务的速率由 _enrich_download_speed 在 tasks_all 回源时算出
    并挂在 t["speed"] 上；这里直接求和（缓存值，最多滞后一个 TTL）。
    """
    tasks = await tasks_all()
    return sum(float(t.get("speed") or 0) for t in tasks
               if str(t.get("status") or "") == "download")


async def tasks_all(force: bool = False) -> List[Dict[str, Any]]:
    now = time.monotonic()
    if not force and _TASKS_CACHE["expire"] > now and _TASKS_CACHE["value"] is not None:
        return _TASKS_CACHE["value"]
    tasks = await _build_tasks(force=force)
    if _WAITING_DISK_TASKS:
        waiting_tasks = [_format_waiting_disk_task(w) for w in _WAITING_DISK_TASKS.values()]
        tasks = waiting_tasks + tasks
    if _is_flood_wait_active():
        st = _get_flood_wait_status()
        rem_sec = st.get("remainingSeconds", 0)
        suspended = list(_FLOOD_WAIT_STATE.get("suspended_tasks", {}).values())
        if suspended:
            flood_tasks = [_format_flood_wait_task(w, rem_sec) for w in suspended]
            tasks = flood_tasks + tasks
    # 统一按业务优先级排序：进行中(下载/上传/校验) → 挂起/待处理 → 失败 → 已下载 → 已归档。
    # 排序必须在挂上 waiting_disk / flood_wait 之后做，排在工作台、/tasks、
    # recent_tasks、聚合搜索等所有消费点之前，保证各处口径一致。
    # 下载速率富化：必须在写缓存前调用——缓存命中路径直接返回旧列表，
    # 速率快照依赖每次真实回源拉到的 downloadedSize 差值。
    tasks.sort(key=_task_sort_key)
    _enrich_download_speed(tasks)
    _observe_tasks(tasks)
    _TASKS_CACHE["expire"] = time.monotonic() + CACHE_TTL
    _TASKS_CACHE["value"] = tasks
    return tasks


async def _session_state() -> str:
    try:
        data = await BACKEND.auth_session()
        if not data:
            return "err"
        if data.get("authenticated") is False:
            return "warn"
        return "ok"
    except Exception as e:  # noqa: BLE001
        log.warning("session_state 判定异常: %s", e)
        return "err"


async def _tg_account_state() -> str:
    try:
        telegrams = await BACKEND.list_telegrams()
    except Exception as e:  # noqa: BLE001
        log.warning("tg_account_state 判定异常: %s", e)
        return "err"
    if not isinstance(telegrams, list) or not telegrams:
        return "none"
    for rec in telegrams:
        if not isinstance(rec, dict):
            continue
        if rec.get("authorized") is True:
            return "ok"
        state = rec.get("lastAuthorizationState")
        if isinstance(state, dict):
            try:
                constructor = int(state.get("constructor", state.get("@type")))
            except (TypeError, ValueError):
                constructor = None
            if constructor is not None and constructor != TG_READY:
                return "pending"
        if state is None:
            return "ok"
    return "pending"


async def _tg_accounts() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        telegrams = await BACKEND.list_telegrams()
        if isinstance(telegrams, list):
            for rec in telegrams:
                if not isinstance(rec, dict):
                    continue
                authorized = rec.get("authorized") is True or rec.get("lastAuthorizationState") is None
                if authorized:
                    out.append({
                        "id": _pick(rec, "id", "telegramId", "telegram_id"),
                        "name": _pick(rec, "name", "firstName", "username", default=""),
                        "phone": str(_pick(rec, "phoneNumber", "phone_number", "phone", default="—")),
                    })
    except Exception as e:  # noqa: BLE001
        log.warning("tg_accounts 读取失败: %s", e)
    return out


def _observe_tasks(tasks: List[Dict[str, Any]]) -> None:
    state = _alert_state
    seen: Dict[str, str] = state["seen"]
    current: Dict[str, str] = {}
    for t in tasks:
        tid = str(t.get("id"))
        st = str(t.get("status") or "")
        current[tid] = st
    if not state["baselined"]:
        if current:
            seen.update(current)
            state["baselined"] = True
        return

    events = state["events"]
    changed = False
    for tid, st in current.items():
        prev = seen.get(tid)
        seen[tid] = st
        if prev == st:
            continue
        color, title = None, ""
        if prev is None:
            color = "var(--wait)"
            title = f"新任务加入队列：{tid}"
        elif st == "downloaded" and prev != "downloaded":
            color = "var(--ok)"
            title = f"任务下载完成：{tid}"
        elif st == "archived" and prev != "archived":
            color = "var(--brand)"
            title = f"任务已归档至云端：{tid}"
        elif st == "failed" and prev != "failed":
            color = "var(--err)"
            title = f"任务失败：{tid}"
        elif st == "isolated" and prev != "isolated":
            color = "var(--iso)"
            title = f"文件已被安全隔离：{tid}"

        if color:
            events.insert(0, {
                "key": tid, "title": title,
                "time": time.strftime("%H:%M"), "ts": time.time(),
                "color": color,
            })
            changed = True
            _lvl = "ERROR" if color == "var(--err)" else ("WARN" if color == "var(--iso)" else "INFO")
            LOG_STORE.append(_lvl, title, task_id=tid)

    if changed:
        del events[_ALERTS_MAX:]
    if len(seen) > len(current) + 64:
        for k in [k for k in seen if k not in current]:
            del seen[k]


def alerts_snapshot() -> List[Dict[str, Any]]:
    read_ts = _alert_state["read_ts"]
    return [
        {"title": ev["title"], "time": ev["time"], "color": ev["color"],
         "unread": ev["ts"] > read_ts}
        for ev in _alert_state["events"][:_ALERT_EVENT_LIMIT]
    ]


def _disk_usage() -> Dict[str, Any]:
    try:
        usage = shutil.disk_usage(APP_ROOT_DIR if os.path.isdir(APP_ROOT_DIR) else BASE_DIR)
        total_gb = usage.total / (1 << 30)
        used_gb = usage.used / (1 << 30)
        return {
            "used_gb": round(used_gb, 1),
            "total_gb": round(total_gb, 1),
            "pct": int(used_gb / total_gb * 100) if total_gb else 0,
        }
    except Exception as e:  # noqa: BLE001
        log.warning("磁盘占用读取失败: %s", e)
        return {"used_gb": 0, "total_gb": 0, "pct": 0}


async def _disk_usage_async() -> Dict[str, Any]:
    return await asyncio.to_thread(_disk_usage)


_DISK_HISTORY_FILE = os.path.join(APP_ROOT_DIR, ".disk_usage_history.json")


def _record_disk_sample(used_gb: float) -> None:
    """记录当天的真实磁盘占用样本（每日一条，覆盖式），供 KPI 迷你趋势图使用。

    迷你趋势图必须来自真实采样；此前该曲线是用「错误数」「任务数」等
    无关序列顶替的，等于给用户看一根假线。没有历史时保持为空（平线）。
    """
    if used_gb <= 0:
        return
    try:
        key = _dt.date.today().isoformat()
        data = {}
        if os.path.exists(_DISK_HISTORY_FILE):
            try:
                with open(_DISK_HISTORY_FILE, "r", encoding="utf-8") as f:
                    data = json.loads(f.read() or "{}")
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        data[key] = round(float(used_gb), 2)
        # 只保留最近 60 天，避免文件无限增长
        for old in sorted(data.keys())[:-60]:
            data.pop(old, None)
        tmp = _DISK_HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False))
        os.replace(tmp, _DISK_HISTORY_FILE)
    except Exception as e:  # noqa: BLE001
        log.debug("磁盘占用样本记录失败: %s", e)


def _disk_trend(days: List[_dt.date], fallback: float) -> List[float]:
    """按天取真实磁盘占用样本；缺失的日期沿用最近一次有效样本，无样本则用当前值。"""
    samples: Dict[str, Any] = {}
    try:
        if os.path.exists(_DISK_HISTORY_FILE):
            with open(_DISK_HISTORY_FILE, "r", encoding="utf-8") as f:
                loaded = json.loads(f.read() or "{}")
            if isinstance(loaded, dict):
                samples = loaded
    except Exception as e:  # noqa: BLE001
        log.debug("磁盘占用历史读取失败: %s", e)

    out: List[float] = []
    # 起始值取窗口内最早的真实样本；窗口内完全没有样本时才退回当前值。
    # 这样不会在“没采过样的历史日期”上画出一条跌到 0 的假曲线。
    known = [samples.get(d.isoformat()) for d in days]
    first = next((v for v in known if isinstance(v, (int, float)) and v > 0), None)
    last = float(first) if first is not None else float(fallback or 0.0)
    for d in days:
        v = samples.get(d.isoformat())
        if isinstance(v, (int, float)) and v > 0:
            last = float(v)
        out.append(last)
    return out


async def _dashboard_stats(tasks: List[Dict[str, Any]], disk: Dict[str, Any]) -> Dict[str, Any]:
    """仪表盘 KPI/摘要统计。

    历史缺陷与本次变更：
    1. 仪表盘的「近 14 日任务量 & FloodWait」面积图已按用户要求整体移除，
       其 14 天窗口聚合循环与 GB 格式化一并清理，仪表盘不再消费趋势序列；
    2. 「下载流量/上传流量」（历史累计 GB）改为「下载速率/上传速率」
       （实时 bytes/s，来自速率引擎差分），前端轮询 /api/speeds 刷新。
    3. 历史回归缺陷：清理趋势字段时把 account.html（账号健康中心）仍在
       引用的 trend_labels/trend_errors/trend_tasks 一并删掉 —— Jinja 对
       「已存在但缺属性」的 dict 取属性会直接抛 UndefinedError，把账号
       健康中心整页打成 500。此处以**真实数据**重建近 7 日三序列：
       - trend_tasks：按任务完成时间（_archived_at，缺省回退 _date_ts）
         逐日聚合任务数；
       - trend_errors：按 LOG_STORE 中 ERROR 级运行日志的落盘时间逐日聚合；
       - trend_labels：近 7 日 "%m-%d" 标签。
       模板侧必须用 stats.get(...) 安全取值，任何一方缺字段都不再 500。
    """
    archived_total = 0
    failed_count = 0
    iso_count = 0
    total_bytes = 0
    days: List[_dt.date] = [(_dt.date.today() - _dt.timedelta(days=d)) for d in range(13, -1, -1)]
    # 近 7 日真实趋势序列（账号健康中心 account.html 的两张图）：
    # trend_tasks 逐日统计任务完成量（优先 _archived_at，老记录回退 _date_ts）；
    # trend_errors 逐日统计 ERROR 级运行日志条数（LOG_STORE 已持久化到 logs.jsonl，
    # 重启后仍可回放历史）。LOG_STORE 只存 "%m-%d %H:%M:%S"，年份靠与当前
    # 标签比对归类 —— 跨年老日志天然匹配不上，计入最近一天以外的桶即可忽略。
    days7: List[_dt.date] = [(_dt.date.today() - _dt.timedelta(days=d)) for d in range(6, -1, -1)]
    trend_labels = [d.strftime("%m-%d") for d in days7]
    day_index = {d.strftime("%m-%d"): i for i, d in enumerate(days7)}
    trend_tasks = [0] * 7
    trend_errors = [0] * 7
    for t in tasks:
        if str(t.get("status") or "") not in ("downloaded", "archived", "completed"):
            continue  # 只统计完成量：下载中/失败的记入只会扭曲「任务量」语义
        ts = t.get("_archived_at") or t.get("_date_ts") or 0
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            continue
        if ts > 1e12:  # 毫秒时间戳归一到秒
            ts = ts / 1000.0
        if ts <= 0:
            continue
        i = day_index.get(time.strftime("%m-%d", time.localtime(ts)))
        if i is not None:
            trend_tasks[i] += 1
    try:
        from core.logging import LOG_STORE as _LOG_STORE
        for line in _LOG_STORE.snapshot():
            if str(line.get("level") or "") != "ERROR":
                continue
            i = day_index.get(str(line.get("time") or "")[:5])
            if i is not None:
                trend_errors[i] += 1
    except Exception as e:  # noqa: BLE001
        log.debug("账号健康错误趋势聚合失败（按 0 处理）: %s", e)

    trend_archived = [0] * 14
    for t in tasks:
        st = t["status"]
        if st in ("completed", "archived"):
            archived_total += 1
        if st == "failed":
            failed_count += 1
        if st == "isolated":
            iso_count += 1
        sz = int(t.get("_size_bytes") or 0)
        if sz > 0:
            total_bytes += sz
    # 磁盘占用迷你趋势：先落一条当天的真实采样，再按天回放历史
    _record_disk_sample(float(disk.get("used_gb", 0) or 0))
    trend_disk = _disk_trend(days, float(disk.get("used_gb", 0) or 0))

    # KPI 卡片：累计下载总量 / 累计上传总量（服务端渲染的存量指标）。
    # 口径：累计下载 = 后端全部文件 downloadedSize 之和 + 归档 done 但后端
    # 已清零 downloadedSize 的记录按 size_bytes 补计（按 uid 去重防双计）；
    # 累计上传 = 归档引擎 state=done 任务 size_bytes 之和。
    dl_total_bytes = 0
    _dl_uids_with_size = set()
    _raw_files = []
    try:
        from core.backend import BACKEND as _BK
        _raw = await _BK.list_all_files_page_info(force=False)
        _raw_files = list(_BK._unwrap_files(_raw) or [])
        for f in _raw_files:
            v = f.get("downloadedSize")
            if not v and str(f.get("downloadStatus") or "") == "completed":
                v = f.get("size")
            try:
                if v and float(v) > 0:
                    _dl_uids_with_size.add(str(f.get("uniqueId") or ""))
                dl_total_bytes += max(0, int(float(v or 0)))
            except (TypeError, ValueError):
                continue
    except Exception as e:  # noqa: BLE001
        log.debug("累计下载统计失败（按 0 处理）: %s", e)
    ul_total_bytes = sum(int(j.get("size_bytes") or 0)
                         for j in _ARCHIVE_JOBS.values() if j.get("state") == "done")
    # 归档清零补偿：文件归档到云端后后端会把 downloadedSize 清零（VPS 实测
    # 下载总量被低估），已归档文件必然完整下载过 —— 按 uid 去重后补计。
    for j in _ARCHIVE_JOBS.values():
        if j.get("state") != "done":
            continue
        j_uid = str(j.get("unique_id") or "")
        j_size = int(j.get("size_bytes") or 0)
        if j_uid and j_uid not in _dl_uids_with_size and j_size > 0:
            dl_total_bytes += j_size
    dl_total = _fmt_total_bytes(dl_total_bytes)
    ul_total = _fmt_total_bytes(ul_total_bytes)
    kpis = [
        {"label": "累计下载", "icon": "ic-download", "value": dl_total[0],
         "unit": dl_total[1], "id": "kpi-dl-total",
         "delta": "", "d": "flat", "spark": "#22d3ee", "points": _spark_points([float(dl_total_bytes)] * 14)},
        {"label": "累计上传", "icon": "ic-upload", "value": ul_total[0],
         "unit": ul_total[1], "id": "kpi-ul-total",
         "delta": "", "d": "flat", "spark": "#60a5fa", "points": _spark_points([float(ul_total_bytes)] * 14)},
        {"label": "本地磁盘占用", "icon": "ic-hdd",
         "value": str(int(disk.get("used_gb", 0))), "unit": "GB",
         "delta": "", "d": "flat", "spark": "#8b5cf6", "points": _spark_points(trend_disk)},
        {"label": "云端归档累计", "icon": "ic-cloud", "value": f"{archived_total:,}", "unit": "个",
         "delta": "", "d": "flat", "spark": "#34d399", "points": _spark_points(trend_archived)},
    ]
    # 「实时速率」摘要卡（用户要求：两卡合一 —— 同一折线图内同时绘制
    # 上传（紫 #a78bfa）与下载（青 #22d3ee）两条曲线，图例显示各自实时值）。
    # 数据源：_SPEED_HISTORY 滚动窗口（14 点）；服务端渲染初始双曲线，
    # 前端轮询 /api/speeds 后实时追加点位重绘两条 polyline。
    up_now = await _upload_speed_snapshot()
    dl_now = sum(float(t.get("speed") or 0) for t in tasks
                 if str(t.get("status") or "") == "download")
    ul_pts, ul_raw, ul_path = _speed_chart_points([h.get("ul", 0.0) for h in _SPEED_HISTORY])
    dl_pts, dl_raw, dl_path = _speed_chart_points([h.get("dl", 0.0) for h in _SPEED_HISTORY])
    summary = [{
        "label": "实时速率",
        "href": "/tasks",
        "lines": [
            {"speed_key": "upload", "label": "上传", "value": _fmt_speed(up_now),
             "color": "#a78bfa", "icon": "ic-upload",
             "points": ul_pts, "raw": ul_raw, "path": ul_path},
            {"speed_key": "download", "label": "下载", "value": _fmt_speed(dl_now),
             "color": "#22d3ee", "icon": "ic-download",
             "points": dl_pts, "raw": dl_raw, "path": dl_path},
        ],
    }]
    return {
        "kpis": kpis,
        "summary": summary,
        "total_bytes": total_bytes,
        # 近 7 日真实趋势（账号健康中心 account.html 消费；模板用 .get 安全取值）
        "trend_labels": trend_labels,
        "trend_tasks": trend_tasks,
        "trend_errors": trend_errors,
    }


def _dedup_files(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for f in files:
        k = (f.get("filename"), f.get("source"))
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    return out


def _filter_local_files(files: List[Dict[str, Any]], source: str = "",
                        ftype: str = "", fsize: str = "",
                        archive_status: str = "") -> List[Dict[str, Any]]:
    out = files
    if source:
        out = [f for f in out if f.get("source") == source]
    if ftype:
        out = [f for f in out if _classify_file_type(f.get("_type"), f.get("_mimeType")) == ftype]
    if fsize:
        out = [f for f in out if _match_size_bucket(f.get("_size_bytes"), fsize)]
    if archive_status:
        st = archive_status.lower().strip()
        if st in ("unarchived", "pending", "not_archived"):
            out = [f for f in out if not (f.get("archive") and f.get("archive", {}).get("state") == "done")]
        elif st in ("archived", "done"):
            out = [f for f in out if f.get("archive") and f.get("archive", {}).get("state") == "done"]
    return out


def _is_local_in_stock(f: Dict[str, Any]) -> bool:
    if not f.get("local_exists"):
        return False
    arch = f.get("archive")
    if arch and (arch.get("localDeleted") or arch.get("deleteLocal")) and not f.get("local_exists"):
        return False
    uid = f.get("unique_id")
    if uid and str(uid) in _DELETED_LOCAL_UIDS:
        return False
    return True


def _find_task_by_uid(tasks_list: List[Dict[str, Any]], unique_id: Any) -> Optional[Dict[str, Any]]:
    if unique_id is None:
        return None
    for t in tasks_list:
        if str(t.get("_unique_id")) == str(unique_id):
            return t
    return None


_SESSION_STATE_LABEL = {"ok": "已连接", "warn": "未登录", "err": "session 失效"}
_SESSION_STATE_SUB = {"ok": "账号正常", "warn": "点击进入登录向导", "err": "session 失效，请重新登录"}

_TG_STATE_LABEL = {
    "ok": "TG 已登录",
    "pending": "TG 登录未完成",
    "none": "未绑定 TG 账号",
    "err": "后端不可达",
}
_TG_STATE_SUB = {
    "ok": "账号正常",
    "pending": "点击继续",
    "none": "点击绑定",
    "err": "点击重试",
}
_TG_STATE_DOT = {"ok": "ok", "pending": "warn", "none": "none", "err": "err"}


async def _ctx(request: Any, page_id="", active_nav="", active_page="", extra=None,
               force: bool = False) -> Dict[str, Any]:
    """公共上下文构造（与前端模板契约保持一致）。"""
    stale = {
        "session_state": "warn",
        "session_dot_class": "warn",
        "session_label": "未登录",
        "session_sub": "后端不可达",
        "tg_state": "err",
        "tg_dot_class": "err",
        "tg_label": "后端不可达",
        "tg_sub": "点击重试",
        "tg_accounts": [],
        "running_count": 0,
        "local_unarchived": 0,
        "isolated_count": 0,
        "alert_count": 0,
        "alerts": [],
        "done_today": 0,
        "now_fmt": time.strftime("%H:%M"),
    }
    try:
        # 并发并行拉取基础状态与任务，将原本串行的多个远端往返合并为单次并发（耗时减少 60%+）
        state, tg_state, tasks, disk, tg_accounts = await asyncio.gather(
            _session_state(),
            _tg_account_state(),
            tasks_all(force=force),
            _disk_usage_async(),
            _tg_accounts(),
        )
        alerts = alerts_snapshot()
        ctx = {
            "request": request,
            "page_id": page_id,
            "active_nav": active_nav,
            "active_page": active_page,
            "session_state": state,
            "session_dot_class": state,
            "session_label": _SESSION_STATE_LABEL.get(state, state),
            "session_sub": _SESSION_STATE_SUB.get(state, ""),
            "tg_state": tg_state,
            "tg_dot_class": _TG_STATE_DOT.get(tg_state, tg_state),
            "tg_label": _TG_STATE_LABEL.get(tg_state, tg_state),
            "tg_sub": _TG_STATE_SUB.get(tg_state, ""),
            "tg_accounts": tg_accounts,
            "running_count": await _running_count(tasks),
            # 侧边栏「本地在存」徽标：与 /library/local 页面同一口径
            # （下载完成 + 文件真实在磁盘 + uid 未标记删除）。
            # 旧口径 status not in (archived, isolated) 会把「下载中/失败/
            # 本地文件已删」全算进去，导致徽标 5、页面只有 2。
            "local_unarchived": sum(1 for t in tasks if _local_stock_exists(t)),
            "isolated_count": await _isolated_count(tasks),
            "alert_count": sum(1 for a in alerts if a["unread"]),
            "alerts": alerts,
            "done_today": sum(1 for t in tasks if t["status"] in ("downloaded", "archived")),
            "stats": await _dashboard_stats(tasks, disk),
            "recent_tasks": tasks[:8],
            "tasks": tasks,
            "now_fmt": time.strftime("%H:%M"),
        }
    except Exception as e:  # noqa: BLE001
        log.error("构造上下文失败: %s", e)
        # 降级上下文必须仍带 request，否则 TemplateResponse 会直接抛
        # ValueError('context must include a "request" key')，把「后端不可达」
        # 从可降级错误升级成整页 500。
        ctx = dict(stale)
        ctx["request"] = request
    if extra:
        ctx.update(extra)
    return ctx


def validate_and_parse_tg_link(link: str) -> Dict[str, Any]:
    """严格校验 Telegram 消息链接防 SSRF 与解析核心参数。
    支持格式：
      - https://t.me/c/<chat_id>/<message_id>
      - https://t.me/c/<chat_id>/<topic_id>/<message_id>
      - https://t.me/<username>/<message_id>
      - https://t.me/<username>/<topic_id>/<message_id>
      - telegram.me, telegram.dog 官方域名
    安全校验：
      - 限制最大长度 512 字符，防 ReDoS 与内存耗尽
      - 严格限定 Host 为 Telegram 官方域名白名单，阻断内网 SSRF 与伪造域名
      - 拦截携带 user:pass@ 凭证的伪造 URL
    """
    if not link or not isinstance(link, str):
        return {"ok": False, "code": "INVALID_LINK_FORMAT", "message": "请输入有效的 Telegram 链接"}
    raw = link.strip()
    if len(raw) > 512:
        return {"ok": False, "code": "INVALID_LINK_FORMAT", "message": "链接长度超出限制（最多512字符）"}

    target_url = raw if ("://" in raw) else f"https://{raw}"
    try:
        parsed = urlsplit(target_url)
    except Exception:
        return {"ok": False, "code": "INVALID_LINK_FORMAT", "message": "无法解析的 URL 格式"}

    if parsed.scheme.lower() not in ("http", "https"):
        return {"ok": False, "code": "INVALID_LINK_FORMAT", "message": "链接协议仅支持 HTTP/HTTPS"}

    if parsed.username or parsed.password:
        return {"ok": False, "code": "SSRF_BLOCKED", "message": "非法链接格式（包含用户凭证）"}

    host = (parsed.hostname or "").lower()
    if not host:
        return {"ok": False, "code": "INVALID_LINK_FORMAT", "message": "缺少主机名"}

    is_allowed = (host in ALLOWED_TG_DOMAINS) or any(host.endswith("." + d) for d in ALLOWED_TG_DOMAINS)
    if not is_allowed:
        return {"ok": False, "code": "SSRF_BLOCKED", "message": "域名不合法：仅支持 t.me / telegram.me / telegram.dog 官方链接"}

    path = parsed.path.strip("/")
    m_priv = RE_TG_PRIVATE.match(path)
    if m_priv:
        chat_id = m_priv.group(1)
        topic_id = int(m_priv.group(2)) if m_priv.group(2) else None
        msg_id = int(m_priv.group(3))
        canonical_url = f"https://t.me/c/{chat_id}/{msg_id}"
        return {
            "ok": True,
            "link_type": "private",
            "chat_identifier": chat_id,
            "topic_id": topic_id,
            "message_id": msg_id,
            "canonical_url": canonical_url,
        }

    m_pub = RE_TG_PUBLIC.match(path)
    if m_pub:
        username = m_pub.group(1)
        topic_id = int(m_pub.group(2)) if m_pub.group(2) else None
        msg_id = int(m_pub.group(3))
        canonical_url = f"https://t.me/{username}/{msg_id}"
        return {
            "ok": True,
            "link_type": "public",
            "chat_identifier": username,
            "topic_id": topic_id,
            "message_id": msg_id,
            "canonical_url": canonical_url,
        }

    return {"ok": False, "code": "INVALID_LINK_FORMAT", "message": "未识别到有效的频道消息路径（如 t.me/c/xxx/123 或 t.me/channel/123）"}


def _parse_link(line: str) -> Optional[str]:
    """把一条 t.me 链接解析为后端需要的引用串；不合法返回 None。"""
    res = validate_and_parse_tg_link(line)
    if res.get("ok"):
        return str(res.get("canonical_url"))
    for pat in _LINK_PATTERNS:
        m = pat.search(line)
        if m:
            chat, msg = m.group(1), m.group(2)
            if not chat.lstrip("-").isdigit():
                return f"https://t.me/{chat}/{msg}"
            return f"https://t.me/c/{chat}/{msg}"
    return None


async def _resolve_links_to_files(links: List[str], force: bool = False) -> tuple:
    """t.me 链接两跳解析：后端没有「按链接下载」端点。

    第一跳 GET /telegram/{tg}/chat/0/files?link=...（TDLib GetMessageLinkInfo）
    解析出文件记录；第二跳把 {telegramId, chatId, messageId, fileId} 数组
    交给 /files/start-download-multiple。返回 (成功数, 失败原因)。
    """
    from services.archive_service import _check_file_dedup
    parsed = [p for p in (_parse_link(l) for l in links) if p]
    if not parsed:
        return 0, "没有可识别的链接"
    try:
        sources = await chat_sources()
    except Exception as e:  # noqa: BLE001
        log.warning("chat_sources 失败: %s", e)
        return 0, "后端不可达"
    if not sources:
        return 0, "尚未绑定 Telegram 账号，请先完成 TG 登录"
    # 逐账号解析（链接归属哪个账号未知；第一个成功解析的账号负责下载）
    files: List[Dict[str, Any]] = []
    errors: List[str] = []
    for s in sources:
        tg_id = s.get("telegramId")
        if tg_id is None:
            continue
        got: List[Dict[str, Any]] = []
        for link in parsed:
            try:
                recs = await BACKEND.resolve_link(tg_id, link)
            except Exception as e:  # noqa: BLE001
                log.warning("resolve_link(%s) 失败: %s", link, e)
                continue
            for rec in recs:
                try:
                    if rec.get("fileId") or rec.get("id") or rec.get("messageId"):
                        got.append(rec)
                except Exception:  # noqa: BLE001
                    continue
        if got:
            tg_for_files = tg_id
            payload_files = []
            for rec in got:
                try:
                    payload_files.append({
                        "telegramId": int(rec.get("telegramId") or tg_for_files),
                        "chatId": int(rec.get("chatId")),
                        "messageId": int(rec.get("messageId")),
                        "fileId": int(rec.get("fileId") or rec.get("id")),
                    })
                except (TypeError, ValueError):
                    continue
            if payload_files:
                if not force:
                    clean_payload = []
                    last_dup = None
                    tasks_list = None
                    try:
                        tasks_list = await tasks_all()
                    except Exception:
                        tasks_list = []
                    for pf in payload_files:
                        rec_item = next((r for r in got if (r.get("fileId") or r.get("id")) == pf["fileId"]), None)
                        uid = str(_pick_id(rec_item) or "") if rec_item else ""
                        fn = _human_name(rec_item) if rec_item else ""
                        sz = rec_item.get("size") if rec_item else None
                        d_res = await _check_file_dedup(uid, fn, sz, tasks_list=tasks_list)
                        if d_res.get("duplicate"):
                            last_dup = d_res
                        else:
                            clean_payload.append(pf)
                    if not clean_payload and last_dup:
                        errors.append(last_dup.get("message") or "文件已存在，无需重复下载")
                        continue
                    payload_files = clean_payload
                if payload_files:
                    try:
                        await BACKEND.start_download_multiple({"files": payload_files})
                        files.extend(payload_files)
                        # 清后端数据缓存，否则 /files 旧数据让新任务不可见（任务表+告警都不触发）
                        BACKEND._cache.clear()
                    except Exception as e:  # noqa: BLE001
                        log.error("start-download-multiple 失败: %s", e)
                        errors.append(_tg_err_public(e))
            # 解析成功的账号已覆盖这批链接，不再换账号重试
            if files:
                break
    if files:
        return len(files), ""
    return 0, (errors[0] if errors else "链接解析不到可下载的文件")

