# -*- coding: utf-8 -*-
"""
services/watermark_service.py — VPS 本地磁盘动态水位保护与应急清理服务
======================================================================
负责 85% 高水位熔断拦截、75% 低水位自动唤醒以及已归档文件的 FIFO 应急被动清理。
"""
import os
import time
import shutil
import asyncio
from typing import Any, Dict, List, Optional, Tuple
from core.config import (
    APP_ROOT_DIR, BASE_DIR, _pick_id, _resolve_host_local_path
)
from core.state import (
    _ARCHIVE_CONFIG, _WAITING_DISK_TASKS, _WAITING_DISK_TASKS_MAX, _ARCHIVE_JOBS,
    _waiting_disk_save, _TASKS_CACHE, _tasks_cache_invalidate
)
from core.backend import BACKEND
from core.logging import log, LOG_STORE


def _get_disk_free_gb() -> float:
    """获取应用所在磁盘分区的可用空间（GB）。"""
    try:
        usage = shutil.disk_usage(APP_ROOT_DIR if os.path.isdir(APP_ROOT_DIR) else BASE_DIR)
        return usage.free / (1024 ** 3)
    except Exception:
        return 999.0


def _get_disk_usage_percent() -> float:
    """获取应用所在磁盘分区的实际使用率百分比（0.0 ~ 100.0）。"""
    try:
        usage = shutil.disk_usage(APP_ROOT_DIR if os.path.isdir(APP_ROOT_DIR) else BASE_DIR)
        if usage.total <= 0:
            return 0.0
        return (usage.used / usage.total) * 100.0
    except Exception:
        return 0.0


def _is_disk_high_watermark_exceeded() -> Tuple[bool, float, float]:
    """检查磁盘使用率是否超过高水位熔断阈值（默认 85%）。返回 (是否超限, 当前百分比, 阈值)"""
    high = float(_ARCHIVE_CONFIG.get("diskHighWatermarkPercent", 85.0) or 85.0)
    cur = _get_disk_usage_percent()
    return (cur >= high), cur, high


def _is_disk_low_watermark_reached() -> Tuple[bool, float, float]:
    """检查磁盘使用率是否回落至低水位唤醒阈值（默认 75%）。返回 (是否回落, 当前百分比, 阈值)"""
    low = float(_ARCHIVE_CONFIG.get("diskLowWatermarkPercent", 75.0) or 75.0)
    cur = _get_disk_usage_percent()
    return (cur < low), cur, low


async def _check_and_wake_waiting_disk_tasks() -> int:
    """巡检并自动唤醒处于 waiting_disk 挂起的下载任务"""
    if not _WAITING_DISK_TASKS:
        return 0

    is_low, cur_pct, low_threshold = _is_disk_low_watermark_reached()
    if not is_low:
        return 0

    high_threshold = float(_ARCHIVE_CONFIG.get("diskHighWatermarkPercent", 85.0) or 85.0)
    log.info("本地磁盘使用率回落至 %.1f%%（低于 %.1f%% 唤醒线），开始唤醒 waiting_disk 挂起任务",
             cur_pct, low_threshold)

    sorted_tasks = sorted(_WAITING_DISK_TASKS.values(), key=lambda x: float(x.get("created_at") or 0.0))
    woken_count = 0
    dropped_count = 0

    for task in sorted_tasks:
        cur = _get_disk_usage_percent()
        if cur >= high_threshold:
            log.warning("唤醒过程中磁盘占用再度升至 %.1f%%（达到高水位 %.1f%%），中止后续唤醒", cur, high_threshold)
            break

        tid = task["id"]
        payload = task.get("payload")
        # 占位挂起任务（payload 为空）不可能是真实下载请求：早期版本直接在
        # _waiting_disk_add 里塞了 {"id","created_at"} 空壳，唤醒时这里会跳过
        # 下载调用却仍然 pop + woken_count += 1 + 落 INFO「已成功唤醒」。
        # 实测：woken_count=1 而 BACKEND.start_download_multiple 调用 0 次，
        # 任务被静默删除、/api/disk/wake 返回假的成功数。
        if not payload:
            _WAITING_DISK_TASKS.pop(tid, None)
            dropped_count += 1
            log.warning("挂起任务 %s 无下载请求体（占位记录），无法恢复，已移除",
                        task.get("filename") or tid)
            continue
        try:
            if isinstance(payload, list):
                await BACKEND.start_download_multiple({"files": payload})
            elif isinstance(payload, dict):
                if "files" in payload:
                    await BACKEND.start_download_multiple(payload)
                else:
                    await BACKEND.start_download_multiple({"files": [payload]})
            _WAITING_DISK_TASKS.pop(tid, None)
            woken_count += 1
            log.info("已成功唤醒挂起任务: %s (%s)", task.get("filename"), tid)
        except Exception as e:
            log.warning("唤醒任务 %s 失败: %s", tid, e)

    if woken_count or dropped_count:
        # 即使一个都没唤醒成功，只要清掉了占位记录也必须落盘，否则重启后
        # 这些无法恢复的空壳会一直占据挂起队列、每次巡检重复报警。
        _waiting_disk_save()
        _tasks_cache_invalidate()
    if woken_count:
        LOG_STORE.append("INFO", f"磁盘回落至 {cur_pct:.1f}%，已自动唤醒 {woken_count} 个挂起下载任务")

    return woken_count


async def _disk_guard_or_enqueue(raw, payload_files, source_tag: str):
    """提交前统一水位门禁：高水位时挂起入队并失效任务缓存，返回响应 dict 或 None。

    raw: 原始请求体（链接列表或文件列表，供 waiting_disk 挂起还原）；
    payload_files: 已解析的下载 payload；source_tag: 日志定位用来源标签。
    返回 None 表示未熔断，调用方继续正常提交。
    """
    high_exceeded, cur_pct, high_threshold = _is_disk_high_watermark_exceeded()
    if high_exceeded:
        await _disk_guard_check()
        high_exceeded, cur_pct, high_threshold = _is_disk_high_watermark_exceeded()

    if not high_exceeded:
        return None
    low_threshold = float(_ARCHIVE_CONFIG.get("diskLowWatermarkPercent", 75.0) or 75.0)
    enqueued_count = _enqueue_waiting_disk_files(raw, payload_files, cur_pct, high_threshold, low_threshold)
    _tasks_cache_invalidate()
    msg = (f"本地磁盘占用率已达 {cur_pct:.1f}%（超过 {high_threshold:.1f}% 警戒线），"
           f"已将 {enqueued_count} 个任务安全置入 waiting_disk 挂起队列，降至 {low_threshold:.1f}% 自动恢复")
    log.warning("磁盘水位熔断拦截 [%s]：%s", source_tag, msg)
    return {
        "ok": False,
        "code": "DISK_WATERMARK_EXCEEDED",
        "state": "waiting_disk",
        "count": enqueued_count,
        "message": msg,
    }


async def _disk_guard_or_enqueue_links(links: List[str], source_tag: str):
    """链接提交版水位门禁：高水位时链接挂起入队并失效任务缓存，返回响应 dict 或 None。"""
    high_exceeded, cur_pct, high_threshold = _is_disk_high_watermark_exceeded()
    if high_exceeded:
        await _disk_guard_check()
        high_exceeded, cur_pct, high_threshold = _is_disk_high_watermark_exceeded()

    if not high_exceeded:
        return None
    low_threshold = float(_ARCHIVE_CONFIG.get("diskLowWatermarkPercent", 75.0) or 75.0)
    enqueued_count = await _enqueue_waiting_disk_links(links, cur_pct, high_threshold, low_threshold)
    _tasks_cache_invalidate()
    msg = (f"磁盘占用率已达 {cur_pct:.1f}%（超过 {high_threshold:.1f}% 警戒线），"
           f"已将 {enqueued_count} 条下载安全置入 waiting_disk 挂起队列，降至 {low_threshold:.1f}% 自动恢复")
    log.warning("磁盘水位熔断拦截 [%s]：%s", source_tag, msg)
    return {
        "ok": False,
        "code": "DISK_WATERMARK_EXCEEDED",
        "state": "waiting_disk",
        "count": enqueued_count,
        "message": msg,
    }


async def _disk_guard_check() -> int:
    """磁盘高低水位动态熔断保护与应急清理。"""
    if not _ARCHIVE_CONFIG.get("diskAutoClean", True):
        return 0

    high_exceeded, cur_pct, high_threshold = _is_disk_high_watermark_exceeded()
    low_watermark = float(_ARCHIVE_CONFIG.get("diskLowWatermarkPercent", 75.0) or 75.0)
    gb_threshold = float(_ARCHIVE_CONFIG.get("diskWatermarkGB", 5.0) or 5.0)
    free_gb = _get_disk_free_gb()

    if not high_exceeded and free_gb >= gb_threshold:
        return 0

    log.warning("VPS 磁盘触碰高水位警戒线（当前使用率 %.1f%% >= %.1f%%，剩余 %.2f GB），触发应急保护清理",
                cur_pct, high_threshold, free_gb)
    if high_exceeded:
        try:
            from services.notification_service import notify_disk_watermark_alert
            notify_disk_watermark_alert(cur_pct, high_threshold, free_gb, len(_WAITING_DISK_TASKS))
        except Exception as e:
            log.warning("派发磁盘告警通知异常: %s", e)

    done_jobs = [
        j for j in _ARCHIVE_JOBS.values()
        if j.get("state") == "done" and not j.get("local_deleted")
    ]
    done_jobs.sort(key=lambda x: float(x.get("updated_at") or x.get("created_at") or 0.0))

    cleaned = 0
    for j in done_jobs:
        lp = _resolve_host_local_path(str(j.get("local_path") or ""))
        if lp and os.path.exists(lp):
            ok, _ = await _delete_local_file_by_job(j)
            if ok:
                cleaned += 1
                new_pct = _get_disk_usage_percent()
                if new_pct < low_watermark and _get_disk_free_gb() >= gb_threshold + 1.0:
                    break

    if cleaned:
        LOG_STORE.append("WARN", f"磁盘高水位应急清理触发：释放 {cleaned} 个已归档本地文件，磁盘占用率降至 {_get_disk_usage_percent():.1f}%")
        log.info("磁盘高水位应急清理：已成功自动清理 %d 个已归档本地文件", cleaned)

    return cleaned


async def _safe_delete_local_path(lp: str) -> Tuple[bool, str]:
    if not lp or lp == "—":
        return False, "文件路径为空"
    if os.path.islink(lp):
        return False, "目标不是普通文件或为软链接，拒绝删除"
    real_p = os.path.realpath(lp)
    base = os.path.basename(real_p)
    if not base or base.startswith("."):
        return False, f"拒绝操作点号开头的隐藏/内部文件: {base}"
    if base.endswith((".py", ".json", ".jsonl", ".db", ".sqlite", ".sh", ".env", ".key", ".pem", ".yml", ".yaml", ".md", ".html", ".js", ".css")):
        return False, f"受保护的系统/代码/配置文件，拒绝删除: {base}"
    if not os.path.exists(real_p):
        return False, "文件在磁盘上不存在"
    if not os.path.isfile(real_p) or os.path.islink(real_p):
        return False, "目标不是普通文件或为软链接，拒绝删除"
    try:
        await asyncio.to_thread(os.remove, real_p)
        return True, ""
    except Exception as e:
        log.warning("删除本地文件失败 (%s): %s", real_p, e)
        return False, str(e)


async def _notify_backend_remove_uid(uid: str) -> None:
    if not uid:
        return
    try:
        from services.task_service import tasks_all
        tasks_list = await tasks_all()
        for t in tasks_list:
            if str(t.get("_unique_id") or "") == uid:
                tg = t.get("_telegram_id")
                fid = t.get("_file_id")
                if tg and fid:
                    try:
                        await BACKEND.remove_file(tg, {"fileId": fid})
                    except Exception as e:
                        log.debug("向后端通知删除记录失败 (tg=%s, fid=%s): %s", tg, fid, e)
                break
    except Exception as e:
        log.debug("_notify_backend_remove_uid 失败: %s", e)
    finally:
        BACKEND._cache.clear()
        _tasks_cache_invalidate()


async def _delete_local_file_by_job(job: Dict[str, Any]) -> Tuple[bool, str]:
    from core.state import _DELETED_LOCAL_UIDS, _archive_save
    lp = _resolve_host_local_path(str(job.get("local_path") or ""))
    uid = str(job.get("unique_id") or "")
    ok, err = await _safe_delete_local_path(lp)
    if ok:
        job["local_deleted"] = True
        if uid:
            _DELETED_LOCAL_UIDS.add(uid)
        _archive_save()
        log.info("归档成功后已自动删除本地文件: %s", lp)
        await _notify_backend_remove_uid(uid)
        return True, ""
    job["local_delete_error"] = err
    _archive_save()
    log.warning("归档后自动删除本地文件失败 (%s): %s", lp, err)
    return False, err


def _enqueue_waiting_disk_files(meta_files: Any, payload_files: List[Dict[str, Any]], cur_pct: float, high_thresh: float, low_thresh: float) -> int:
    enqueued = 0
    now = time.time()
    meta_dict = {}
    if isinstance(meta_files, list):
        for f in meta_files:
            if isinstance(f, dict):
                fid = str(f.get("fileId") or f.get("id") or "")
                uid = str(f.get("uniqueId") or "")
                if fid:
                    meta_dict[fid] = f
                if uid:
                    meta_dict[uid] = f

    for p in payload_files:
        fid = str(p.get("fileId") or "")
        match_meta = meta_dict.get(fid) or {}
        fn = str(match_meta.get("filename") or match_meta.get("name") or f"file_{fid}")
        sz = match_meta.get("size") or match_meta.get("_size") or "未知大小"
        sz_bytes = match_meta.get("_size_bytes") or match_meta.get("size_bytes") or 0
        uid = str(match_meta.get("uniqueId") or match_meta.get("_unique_id") or fid)
        task_id = f"wait_disk_{int(now * 1000)}_{enqueued + 1}"
        error_msg = f"本地磁盘占用率已达 {cur_pct:.1f}%（警戒线 {high_thresh:.1f}%），任务已安全挂起，等待降至 {low_thresh:.1f}% 自动恢复调度"

        _WAITING_DISK_TASKS[task_id] = {
            "id": task_id,
            "filename": fn,
            "size": sz_bytes,
            "size_str": str(sz),
            "source": str(match_meta.get("source") or match_meta.get("chatTitle") or "磁盘保护挂起队列"),
            "msg_id": str(p.get("messageId") or "—"),
            "payload": p,
            "status": "waiting_disk",
            "error_msg": error_msg,
            "created_at": now,
            "uniqueId": uid,
        }
        enqueued += 1

    if enqueued and len(_WAITING_DISK_TASKS) >= _WAITING_DISK_TASKS_MAX:
        log.warning("waiting_disk 挂起队列已达上限 %s，拒绝继续入队", _WAITING_DISK_TASKS_MAX)
        return 0
    _waiting_disk_save()
    return enqueued


async def _enqueue_waiting_disk_links(links: List[str], cur_pct: float, high_thresh: float, low_thresh: float) -> int:
    from services.browse_service import chat_sources
    def _parse_link(line: str) -> Optional[str]:
        s = line.strip()
        if not s:
            return None
        m = re.search(r"https?://t\.me/[^\s]+", s)
        return m.group(0) if m else None

    import re
    parsed = [p for p in (_parse_link(l) for l in links) if p]
    if not parsed:
        return 0
    now = time.time()
    enqueued = 0
    try:
        sources = await chat_sources()
        tg_id = sources[0].get("telegramId") if sources else 0
    except Exception:
        tg_id = 0

    for link_url in parsed:
        recs = []
        if tg_id:
            try:
                recs = await BACKEND.resolve_link(tg_id, link_url)
            except Exception:
                recs = []
        if recs:
            for rec in recs:
                fid = rec.get("fileId") or rec.get("id")
                cid = rec.get("chatId")
                mid = rec.get("messageId")
                if fid and cid and mid:
                    fn = str(rec.get("name") or rec.get("filename") or f"link_file_{fid}")
                    sz = rec.get("size") or "未知大小"
                    task_id = f"wait_disk_{int(now * 1000)}_{enqueued + 1}"
                    p = {"telegramId": int(rec.get("telegramId") or tg_id), "chatId": int(cid), "messageId": int(mid), "fileId": int(fid)}
                    _WAITING_DISK_TASKS[task_id] = {
                        "id": task_id,
                        "filename": fn,
                        "size": 0,
                        "size_str": str(sz),
                        "source": str(rec.get("chatTitle") or "链接提交"),
                        "msg_id": str(mid),
                        "payload": p,
                        "status": "waiting_disk",
                        "error_msg": f"磁盘使用率已达 {cur_pct:.1f}%（>= {high_thresh:.1f}%），已挂起排队，回落至 {low_thresh:.1f}% 自动恢复",
                        "created_at": now,
                        "uniqueId": str(_pick_id(rec) or fid),
                    }
                    enqueued += 1
        else:
            task_id = f"wait_disk_{int(now * 1000)}_{enqueued + 1}"
            _WAITING_DISK_TASKS[task_id] = {
                "id": task_id,
                "filename": link_url.split("/")[-1] or "telegram_link",
                "size": 0,
                "size_str": "—",
                "source": "链接提交",
                "msg_id": "—",
                "payload": None,
                "status": "waiting_disk",
                "error_msg": f"本地磁盘占用率达 {cur_pct:.1f}%，任务已安全挂起",
                "created_at": now,
                "uniqueId": "",
            }
            enqueued += 1

    _waiting_disk_save()
    return enqueued
