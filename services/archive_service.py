# -*- coding: utf-8 -*-
"""
services/archive_service.py — 网盘归档工作流调度引擎
====================================================
负责归档任务生命周期、流式上传调度、文件指纹去重查重、失败分类诊断与自动归档扫描。
"""
import os
import re
import time
import secrets
import asyncio
from typing import Any, Dict, List, Optional, Tuple
from core.config import (
    _fmt_size, _fmt_time, _clean_archive_filename,
    _resolve_host_local_path, _archive_norm_dir, _archive_join,
    _ensure_archive_ext, _same_file_name,
)
from core.state import (
    _ARCHIVE_JOBS, _ARCHIVE_CONFIG, _DELETED_LOCAL_UIDS,
    _QUICK_ARCHIVE_REGISTRY, _SUB_RULES, _archive_save,
    _archive_config_save, _archive_sem, _TASKS_CACHE,
    _archive_registry_lookup, _ARCH_PILL, _archive_public,
    _archive_state_of, _archive_active_of, _archive_latest_raw_of
)
from core.backend import BACKEND
from core.logging import log, LOG_STORE
from services.openlist_service import (
    _openlist_token, _openlist_relogin, _openlist_mkdir_tree,
    _openlist_exists, _openlist_put_once, _openlist_ready,
    _openlist_direct_url, _openlist_locate_url, _openlist_client, _openlist_env,
    _OpenListAuthErr, _openlist_stat
)
from services.notification_service import (
    notify_archive_success, notify_archive_failed
)
from services.bot_command_service import remember_archive_error as _remember_archive_error

_ARCHIVE_TASKS: Dict[str, Any] = {}
_SWEEP_LOCK = asyncio.Lock()
_AUTO_ARCHIVE_MAX_ATTEMPTS = 3

_OPENLIST_OFF_HINT = "OpenList 未挂载/未登录，请到设置页登录后归档"


def _arch_button_view(f: Dict[str, Any], openlist_ready: bool, local_exists: bool = True) -> Dict[str, Any]:
    arch = f.get("archive")
    if arch:
        st = arch.get("state")
        if st == "uploading":
            return {"enabled": False, "label": "归档中 %s%%" % (arch.get("progress") or 0),
                    "title": arch.get("remotePath") or ""}
        if st == "queued":
            return {"enabled": False, "label": "排队中…", "title": arch.get("remotePath") or ""}
        if st == "done":
            if not local_exists:
                return {"enabled": False, "label": "已归档",
                        "title": ("已归档到 " + (arch.get("remotePath") or "") + "（本地文件已清理）")}
            return {"enabled": openlist_ready, "label": "重新归档",
                    "title": ("已归档到 " + (arch.get("remotePath") or ""))
                             if openlist_ready else _OPENLIST_OFF_HINT}
        if st == "failed":
            if not local_exists:
                return {"enabled": False, "label": "归档失败 (本地已清理)",
                        "title": (arch.get("error") or "归档失败，且本地文件已不存在")}
            return {"enabled": openlist_ready, "label": "归档失败 · 重试",
                    "title": (arch.get("error") or "归档失败，点击重试")
                             if openlist_ready else _OPENLIST_OFF_HINT}
    if not local_exists and str(f.get("_download_status") or "") == "completed":
        return {"enabled": False, "label": "本地已清理", "title": "本地文件已被删除或清理"}
    if not openlist_ready:
        return {"enabled": False, "label": "立即归档", "title": _OPENLIST_OFF_HINT}
    if not f.get("unique_id"):
        return {"enabled": False, "label": "立即归档", "title": "该记录缺少 uniqueId，无法归档"}
    if str(f.get("_download_status") or "") != "completed":
        return {"enabled": False, "label": "立即归档",
                "title": "文件尚未下载完成，下载完成后可归档"}
    if not local_exists:
        return {"enabled": False, "label": "本地已清理", "title": "本地文件已不存在"}
    return {"enabled": True, "label": "立即归档", "title": "上传到 OpenList 云端归档"}


def _enrich_archive(f: Dict[str, Any], openlist_ready: bool,
                    arch_index: Optional[Any] = None) -> Dict[str, Any]:
    uid = f.get("_unique_id")
    uid_str = str(uid) if uid else ""
    f["unique_id"] = uid_str
    lp = _resolve_host_local_path(f.get("local_path"))
    local_exists = bool(lp and os.path.exists(lp) and uid_str not in _DELETED_LOCAL_UIDS)
    f["local_exists"] = local_exists
    f["can_delete_local"] = local_exists or (str(f.get("_download_status") or "") == "completed" and uid_str not in _DELETED_LOCAL_UIDS)
    f["archivable"] = bool(uid) and str(f.get("_download_status") or "") == "completed" and local_exists
    if arch_index is not None:
        latest_view = arch_index.latest_of(uid_str) if uid_str else None
        f["archive"] = _archive_public(latest_view) if latest_view else None
    else:
        f["archive"] = _archive_state_of(uid_str) if uid_str else None
    f["arch_btn"] = _arch_button_view(f, openlist_ready, local_exists)
    return f


from services.watermark_service import (
    _safe_delete_local_path, _notify_backend_remove_uid, _delete_local_file_by_job
)


async def _archive_worker(job: Dict[str, Any]) -> None:
    async with _archive_sem:
        if job.get("state") in ("done", "cancelled"):
            return
        job["state"] = "uploading"
        job["progress"] = 0
        job["error"] = ""
        job["updated_at"] = time.time()
        _archive_save()
        try:
            token = await _openlist_token()

            lp = _resolve_host_local_path(str(job.get("local_path") or ""))
            job["local_path"] = lp
            local_exists = await asyncio.to_thread(os.path.exists, lp)
            if not local_exists:
                # 本地已被删除时不能一律报失败：先核对云端是否已存在完整副本。
                # 典型场景：同一文件被重复入队，前一个任务已归档并删本地，
                # 后到的任务就会误报「本地文件不存在」并长期挂在失败列表里。
                expected = int(job.get("size_bytes") or 0)
                stat = await _openlist_stat(token, job["remote_path"])
                cloud_size = int((stat or {}).get("size") or 0)
                if stat is not None and cloud_size > 0 and (not expected or cloud_size == expected):
                    job["state"] = "done"
                    job["progress"] = 100
                    job["archived_at"] = time.time()
                    job["updated_at"] = job["archived_at"]
                    job["error"] = ""
                    job["remote_size"] = cloud_size
                    job["local_deleted"] = True
                    _archive_save()
                    log.info("本地文件已被删除，但云端已存在完整副本，判定为已归档: %s（%d 字节）",
                             job["remote_path"], cloud_size)
                    return
                raise RuntimeError(
                    "本地文件不存在或已被移动，且云端没有完整副本（云端 %s 字节）" % (cloud_size if stat is not None else "缺失"))
            size = await asyncio.to_thread(os.path.getsize, lp)
            if size <= 0:
                raise RuntimeError("本地文件为空（0 字节），无法归档")
            job["size_bytes"] = size

            if job.get("policy") == "skip":
                remote_stat = await _openlist_stat(token, job["remote_path"])
                if remote_stat is not None:
                    remote_size = int(remote_stat.get("size") or 0)
                    # 关键红线：只有云端文件**大小与本地一致且非 0** 才允许跳过并删本地。
                    # 历史事故：上传被取消/中断后云端残留 0 字节空壳，旧逻辑只判「存在」，
                    # 于是判 done 并执行 deleteLocal —— 本地被删、云端是空文件，真实数据丢失。
                    if remote_size == size and remote_size > 0:
                        job["state"] = "done"
                        job["progress"] = 100
                        job["archived_at"] = time.time()
                        job["error"] = ""
                        job["updated_at"] = job["archived_at"]
                        job["remote_size"] = remote_size
                        _archive_save()
                        log.info("归档跳过（云端已存在完整同名文件，大小一致 %d 字节）: %s",
                                 remote_size, job["remote_path"])
                        notify_archive_success(job)
                        if job.get("delete_local"):
                            await _delete_local_file_by_job(job)
                        return
                    # 云端存在但大小不符（0 字节或残缺）：绝不当成功，继续走覆盖上传修复它
                    log.warning(
                        "云端同名文件不完整（云端 %s 字节 / 本地 %s 字节），不跳过，改为重新上传覆盖: %s",
                        remote_size, size, job["remote_path"])
                    job["policy"] = "overwrite"

            await _openlist_mkdir_tree(token, str(job["remote_dir"]))

            try:
                await _openlist_put_once(token, job)
            except _OpenListAuthErr:
                token = await _openlist_relogin()
                await _openlist_put_once(token, job)

            # 上传返回 200 不等于落盘完整：必须回查云端真实大小再判成功。
            # 只有确认 size 与本地一致，才允许写 done 并按 deleteLocal 删本地文件。
            verify = await _openlist_stat(token, job["remote_path"])
            if verify is None:
                raise RuntimeError("上传后云端存在性核验失败，未确认归档完成")
            verify_size = int(verify.get("size") or 0)
            if verify_size != size:
                raise RuntimeError(
                    f"上传后云端大小核验不一致（云端 {verify_size} 字节 / 本地 {size} 字节），判定归档未完成")

            job["state"] = "done"
            job["progress"] = 100
            job["archived_at"] = time.time()
            job["updated_at"] = job["archived_at"]
            job["remote_size"] = verify_size
            _archive_save()
            log.info("归档完成: %s → %s（已核验 %d 字节）", job["filename"], job["remote_path"], verify_size)
            notify_archive_success(job)
            if job.get("delete_local"):
                await _delete_local_file_by_job(job)
        except asyncio.CancelledError:
            job["state"] = "cancelled"
            job["error"] = "用户已取消"
            job["updated_at"] = time.time()
            _archive_save()
            log.info("归档已取消: %s", job["filename"])
        except RuntimeError as e:
            job["state"] = "failed"
            job["error"] = str(e)
            job["updated_at"] = time.time()
            _archive_save()
            log.warning("归档失败（%s → %s）: %s", job["filename"], job["remote_path"], e)
            _remember_archive_error(job)
            notify_archive_failed(job)
        except Exception as e:  # noqa: BLE001
            job["state"] = "failed"
            job["error"] = "上传失败：" + e.__class__.__name__
            job["updated_at"] = time.time()
            _archive_save()
            log.error("归档异常（%s）: %s", job["filename"], e)
            _remember_archive_error(job)
            notify_archive_failed(job)
        finally:
            _ARCHIVE_TASKS.pop(job.get("id"), None)
            from services.subscription_service import _sub_on_job_finished
            _sub_on_job_finished(job)


async def _cloud_archive_rows(check_remote: bool = True) -> List[Dict[str, Any]]:
    done = [j for j in _ARCHIVE_JOBS.values() if j.get("state") == "done"]
    done.sort(key=lambda j: j.get("archived_at") or j.get("created_at") or 0.0, reverse=True)
    latest: Dict[str, Dict[str, Any]] = {}
    for j in done:
        key = str(j.get("remote_path") or "").lower()
        if key and key not in latest:
            latest[key] = j
    rows = list(latest.values())
    if not rows:
        return []

    remote_names: Dict[str, set] = {}
    if check_remote:
        try:
            token = await _openlist_token()
            dirs = sorted({str(j.get("remote_dir") or "") for j in rows if j.get("remote_dir")})
            for d in dirs:
                resp = await _openlist_client.post(
                    "/api/fs/list",
                    json={"path": d, "password": "", "page": 1, "per_page": 500, "refresh": False},
                    headers={"Authorization": token})
                code, data, _ = _openlist_env(resp)
                if code != 200:
                    continue
                remote_names[d] = {str(c.get("name") or "") for c in (data.get("content") or [])}
        except Exception as e:  # noqa: BLE001
            log.warning("云端归档存在性校验失败（按日志原样展示）: %s", e)

    out = []
    for j in rows:
        rp = str(j.get("remote_path") or "")
        rd = str(j.get("remote_dir") or "")
        fn = str(j.get("filename") or "")
        exists = True
        if check_remote and rd in remote_names:
            exists = fn in remote_names[rd]
        drive = (rp.strip("/").split("/")[0] if "/" in rp.strip("/") else "默认网盘") if rp else "默认网盘"
        status = "archived"
        if d_name := str(j.get("remote_dir") or ""):
            if d_name in remote_names:
                status = "archived" if fn in remote_names[d_name] else "missing"
        # 同一行内 _fmt_time / _openlist_direct_url 各被调用 2~3 次填同值别名键
        # （archived_time/archivedAt、openlist_url/directUrl/openlistUrl）。
        # 实测 50 行归档 → _openlist_direct_url 被调用 150 次（应为 50）。
        # _openlist_direct_url 内部有 rstrip + normpath + quote，循环里是纯浪费。
        _t = _fmt_time(j.get("archived_at") or j.get("created_at"))
        _url = _openlist_direct_url(rp)
        # 管理页定位 URL 必须指向父目录：OpenList 对文件路径调 fs/list 会 500
        # 「not a folder」，前端渲染失败即回落首页（用户症状：点了只开网盘首页）。
        _locate = _openlist_locate_url(rp)
        out.append({
            "id": j.get("id"),
            "unique_id": j.get("unique_id"),
            "uniqueId": j.get("unique_id"),
            "filename": fn,
            "raw_filename": j.get("raw_filename") or fn,
            "rawFilename": j.get("raw_filename") or fn,
            "size": _fmt_size(j.get("size_bytes")),
            "size_bytes": j.get("size_bytes") or 0,
            "sizeBytes": j.get("size_bytes") or 0,
            "remote_dir": rd,
            "remoteDir": rd,
            "remote_path": rp,
            "remotePath": rp,
            "cloud_path": rp,
            "cloudPath": rp,
            "drive": drive,
            "status": status,
            "archived_time": _t,
            "archivedAt": _t,
            "archivedTimestamp": j.get("archived_at") or j.get("created_at") or 0.0,
            "openlist_url": _url,
            "directUrl": _url,
            "openlistUrl": _url,
            "locateUrl": _locate,
            "locate_url": _locate,
            "exists": exists,
            "deleteLocal": bool(j.get("delete_local")),
            "localDeleted": bool(j.get("local_deleted")),
            "localPath": j.get("local_path") or "",
        })

    # 卡片化联查（历史缺陷：云端页旧表格只有文件维度字段，缩略图/消息定位
    # 存放在 TG 任务记录里，模板拿不到，卡片化后无图可显）。
    # 联查数据源一：bridge 任务列表（与旧版一致，含速率/状态富化）
    # 历史缺陷：已归档并删除本地文件的记录会被 Java 后端把 downloadStatus
    # 重置为 'idle'，而 tasks_all 只接受 downloading/paused/completed/error，
    # 整条被过滤 → VPS 实测 tasks_all 返回 0 条、56 张卡片联查全失败、
    # 全部落「无图占位」。缩略图字段（thumbnailUniqueId/thumbnail/telegramId/
    # chatId/messageId）与下载状态无关，因此联查数据源二：直接回源后端
    # 原始 /api/files 记录，任务列表命中优先、原始记录兜底。
    raw_map: Dict[str, Dict[str, Any]] = {}



    # 联查数据源二辅助：回源 Java 后端原始 /api/files 记录（不经
    # downloadStatus 过滤）。独立成闭包便于测试 patch 与复用。
    async def _raw_files():
        from core.backend import BACKEND
        _r = await BACKEND.list_all_files_page_info(force=False)
        return list(BACKEND._unwrap_files(_r) or [])

    try:
        for f in await _raw_files():
            u = str(f.get("uniqueId") or "")
            if u:
                raw_map[u] = f
    except Exception as e:  # noqa: BLE001
        log.debug("云端卡片联查后端原始记录失败（任务列表兜底）: %s", e)
    uid_map: Dict[str, Dict[str, Any]] = {}
    if any(r.get("uniqueId") for r in out):
        try:
            # 延迟导入防循环依赖：task_service 也在函数体内反向引用本模块
            from services.task_service import tasks_all
            for t in await tasks_all():
                tu = str(t.get("_unique_id") or "")
                if tu:
                    uid_map[tu] = t
        except Exception as e:  # noqa: BLE001
            log.warning("云端卡片联查缩略图失败（按无图占位渲染）: %s", e)
    for r in out:
        t = uid_map.get(str(r.get("uniqueId") or ""))
        raw = raw_map.get(str(r.get("uniqueId") or ""))
        if t is None and raw is not None:
            # 任务列表无此 uid（downloadStatus=idle 被过滤），用后端原始记录兜底。
            # 缩略图与消息定位字段两条链路同构，直接映射即可。
            t = {
                "_thumb_uid": str(raw.get("thumbnailUniqueId") or ""),
                "_thumb": str(raw.get("thumbnail") or ""),
                "_telegram_id": raw.get("telegramId"),
                "_chat_id": raw.get("chatId"),
                "msg_id": raw.get("messageId"),
            }
        if t:
            thumb_uid = str(t.get("_thumb_uid") or "")
            # 历史缺陷：早期归档任务只存了主文件 uniqueId，没有 thumbnailUniqueId，
            # 联查后 thumb_uid 为空 → 卡片只能落 minithumbnail/占位图。
            # 与本地在存页同款兜底：退回主文件 uid 并标记 heal=1，
            # 由 /preview 经 GetMessage 定位补下载真正的全尺寸缩略图。
            thumb_heal = False
            if not thumb_uid:
                main_uid = str(r.get("uniqueId") or (t.get("_unique_id") or ""))
                if main_uid:
                    thumb_uid = main_uid
                    thumb_heal = True
            r.update({
                "thumb": str(t.get("_thumb") or ""),
                "thumb_uid": thumb_uid,
                "thumb_heal": thumb_heal,
                "telegramId": t.get("_telegram_id"),
                "chatId": t.get("_chat_id"),
                "messageId": t.get("msg_id"),
            })
        else:
            # 无匹配任务（历史残留记录/预览件）：显式落空值，模板按占位图渲染
            r.setdefault("thumb", "")
            r.setdefault("thumb_uid", "")
            r.setdefault("thumb_heal", False)
            r.setdefault("telegramId", "")
            r.setdefault("chatId", "")
            r.setdefault("messageId", "")
    return out


def _classify_archive_error(raw_err: str) -> Tuple[str, str, str]:
    """把归档失败原因归类，用于前端分诊与「能否重试」判定。

    注意：分类顺序即优先级，必须把**永久性错误**放在前面判断，
    否则「本地文件已丢失」会被后面的宽泛规则（如 exist/连接）误判成可重试。

    返回值 (code, label, 建议动作)。
    """
    err = str(raw_err or "").strip()
    # 1) 永久性：本地源文件不存在，重试一万次也不会变好。
    #    历史缺陷：这类错误落到 unknown 且被前端当作可重试，用户每点一次
    #    「重试」就再跑一遍注定失败的归档（文件已删/已移动，重试无意义）。
    if re.search(r"本地文件不存在|本地文件为空|未在磁盘找到|本地已清理|本地文件已被移动", err, re.I):
        return "source_missing", "🗑️ 本地文件已丢失", "本地原文件已不存在，无法归档；请重新下载后再归档"
    # 2) 永久性：云端对象在会话期间被改动（eTag 失配），重试同样无用，
    #    必须重新走一次「上传」而不是重放旧的会话。
    if re.search(r"resourceModified|resource has changed|etag mismatch|eTag", err, re.I):
        return "remote_changed", "🔄 云端文件已变动", "云端同名文件已被其他操作改动，请重新下载后再归档"
    if re.search(r"itemNotFound|resource could not be found", err, re.I):
        return "remote_missing", "🔍 云端对象不存在", "云端目标已不存在，请确认网盘挂载与目标目录后重试"
    # 3) 临时性：鉴权、容量、冲突、网络抖动，重试有意义。
    if re.search(r"401|403|token|unauthorized|OpenListAuthErr|认证|登录失效|凭证", err, re.I):
        return "token_expired", "🔑 鉴权过期", "重新登录刷新 OpenList 令牌后重试"
    if re.search(r"507|quota|full|space|容量|空间|超限|insufficient", err, re.I):
        return "storage_full", "💾 容量超限", "清理网盘空间或更换归档目录"
    if re.search(r"409|already exists|conflict|同名|冲突|重名", err, re.I):
        return "conflict", "⚠️ 文件冲突", "启用覆盖策略后重试"
    if re.search(r"502|504|timeout|timed out|refused|connect|readtimeout|reset by peer|超时|断网|连接", err, re.I):
        return "timeout", "🌐 网络超时", "网络抖动，建议一键重新入队"
    return "unknown", "❓ 未知异常", "建议查看日志详情分析堆栈"


# 永久性失败分类：重试不会改变结果，必须禁止无限重试（否则形成死循环）。
_PERMANENT_ARCHIVE_ERRORS = frozenset({"source_missing", "remote_changed", "remote_missing"})


def _is_retryable_archive_error(raw_err: str) -> bool:
    """该失败是否值得重试。永久性错误（源文件丢失等）返回 False。"""
    code, _, _ = _classify_archive_error(raw_err)
    return code not in _PERMANENT_ARCHIVE_ERRORS


async def _check_file_dedup(unique_id: str = "", filename: str = "", size_bytes: Any = None, tasks_list: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """统一多维文件指纹检查函数：
    1. 优先查云端网盘已归档 (_ARCHIVE_JOBS, state=='done')
    2. 其次查本地中转区在存资产 (tasks_all, downloadStatus=='completed', 且磁盘文件存在且未标记删除)
    3. 再次查正在进行/排队的下载任务 (tasks_all, downloadStatus in ('downloading', 'queued', 'pending', 'waiting_disk'))
    """
    uid = str(unique_id or "").strip()
    norm_fn = str(filename or "").strip().lower()
    sz = None
    if isinstance(size_bytes, (int, float)) and size_bytes > 0:
        sz = int(size_bytes)

    # 1. 检查云端网盘已归档资产
    arch_job = _archive_registry_lookup(uid, norm_fn, sz)
    if arch_job and (arch_job.get("state") == "done" or arch_job.get("remote_path")):
        rp = str(arch_job.get("remote_path") or "")
        drive = (rp.strip("/").split("/")[0] if "/" in rp.strip("/") else "默认网盘") if rp else "默认网盘"
        arch_ts = arch_job.get("archived_at") or arch_job.get("created_at") or 0.0
        arch_date = _fmt_time(arch_ts) if arch_ts else "近期"
        openlist_url = _openlist_direct_url(rp) if rp else ""
        matched_by = "unique_id" if (uid and str(arch_job.get("unique_id") or "").strip() == uid) else "name_size"
        fn_display = str(arch_job.get("filename") or filename or "未知文件")
        size_display = arch_job.get("size_bytes") or sz

        return {
            "duplicate": True,
            "duplicateType": "cloud",
            "matchedBy": matched_by,
            "prompt": f"云端已于 {arch_date} 归档至 {drive}：{rp}",
            "message": f"云端已于 {arch_date} 归档至 {drive} 路径：{rp}",
            "asset": {
                "status": "archived",
                "uniqueId": str(arch_job.get("unique_id") or uid),
                "filename": fn_display,
                "size": size_display,
                "sizeHuman": _fmt_size(size_display) if size_display else "—",
                "cloudPath": rp,
                "drive": drive,
                "openlistUrl": openlist_url,
                "archivedAt": arch_ts,
                "archivedDate": arch_date,
                "localPath": None,
                "localExists": False,
                "taskId": None,
                "progress": None,
            },
            "actions": [
                {"type": "open_cloud", "label": "直达网盘查看", "url": openlist_url},
                {"type": "retrieve_cloud", "label": "直接取回至本地", "action": "retrieve", "path": rp},
                {"type": "force_download", "label": "强制重新下载", "action": "force"},
            ]
        }

    # 获取任务列表以比对本地在存与排队中任务
    if tasks_list is None:
        try:
            from services.task_service import tasks_all
            tasks_list = await tasks_all()
        except Exception:
            tasks_list = []

    # 2. 检查本地在存资产与下载任务
    for t in tasks_list:
        t_uid = str(t.get("_unique_id") or t.get("uniqueId") or "").strip()
        t_fn = str(t.get("filename") or t.get("name") or "").strip().lower()
        t_sz = t.get("_size_bytes")
        if not t_sz and isinstance(t.get("size"), (int, float)):
            t_sz = int(t.get("size"))

        match_uid = bool(uid and t_uid and uid == t_uid)
        match_ns = bool(norm_fn and t_fn and _same_file_name(norm_fn, t_fn) and (sz is None or t_sz == sz))

        if match_uid or match_ns:
            st = str(t.get("_download_status") or t.get("status") or "").lower()
            lp_raw = str(t.get("local_path") or t.get("localPath") or "")
            lp = _resolve_host_local_path(lp_raw) if (lp_raw and lp_raw != "—") else ""
            uid_cand = t_uid or uid

            # 2.1 本地已下载完成且磁盘上真实在存
            if st == "completed" and lp and os.path.exists(lp) and uid_cand not in _DELETED_LOCAL_UIDS:
                matched_by = "unique_id" if match_uid else "name_size"
                fn_display = str(t.get("filename") or filename or "未知文件")
                size_display = t_sz or sz
                return {
                    "duplicate": True,
                    "duplicateType": "local",
                    "matchedBy": matched_by,
                    "prompt": f"本地在存（路径：{lp}），无需重复下载",
                    "message": "本地在存：该文件已在本地中转区存在，支持直接查看或立即归档",
                    "asset": {
                        "status": "downloaded",
                        "uniqueId": uid_cand,
                        "filename": fn_display,
                        "size": size_display,
                        "sizeHuman": _fmt_size(size_display) if size_display else "—",
                        "cloudPath": None,
                        "drive": None,
                        "openlistUrl": None,
                        "archivedAt": None,
                        "archivedDate": None,
                        "localPath": lp,
                        "localExists": True,
                        "taskId": t.get("id"),
                        "progress": 100,
                    },
                    "actions": [
                        {"type": "open_local", "label": "查看本地在存", "url": "/library/local"},
                        {"type": "archive_now", "label": "立即归档", "action": "archive", "uniqueId": uid_cand},
                        {"type": "force_download", "label": "强制重新下载", "action": "force"},
                    ]
                }

            # 2.2 正在下载或排队中
            if st in ("downloading", "queued", "pending", "waiting_disk"):
                matched_by = "unique_id" if match_uid else "name_size"
                fn_display = str(t.get("filename") or filename or "未知文件")
                size_display = t_sz or sz
                prog = t.get("progress") or 0
                return {
                    "duplicate": True,
                    "duplicateType": "task",
                    "matchedBy": matched_by,
                    "prompt": f"该文件已在下载队列中（状态: {t.get('status')}，进度: {prog}%）",
                    "message": f"该文件已在下载队列中（状态: {t.get('status')}，进度: {prog}%），无需重复提交",
                    "asset": {
                        "status": t.get("status") or st,
                        "uniqueId": uid_cand,
                        "filename": fn_display,
                        "size": size_display,
                        "sizeHuman": _fmt_size(size_display) if size_display else "—",
                        "cloudPath": None,
                        "drive": None,
                        "openlistUrl": None,
                        "archivedAt": None,
                        "archivedDate": None,
                        "localPath": lp or None,
                        "localExists": False,
                        "taskId": t.get("id"),
                        "progress": prog,
                    },
                    "actions": [
                        {"type": "view_task", "label": "查看任务", "url": f"/tasks/{t.get('id')}"},
                        {"type": "force_download", "label": "强制重新下载", "action": "force"},
                    ]
                }

    return {
        "duplicate": False,
        "duplicateType": "none",
        "matchedBy": "none",
        "prompt": "",
        "message": "未发现重复资产",
        "asset": None,
        "actions": []
    }



async def _auto_archive_sweep() -> int:
    if _SWEEP_LOCK.locked():
        return 0
    async with _SWEEP_LOCK:
        return await _auto_archive_sweep_impl()


async def _auto_archive_sweep_impl() -> int:
    has_sub_rules = any(r.get("enabled") for r in _SUB_RULES.values())
    default_enabled = bool(_ARCHIVE_CONFIG.get("autoArchive"))
    default_dir = _archive_norm_dir(str(_ARCHIVE_CONFIG.get("defaultDir") or ""))
    has_default_rule = default_enabled and bool(default_dir and default_dir != "/")
    if _QUICK_ARCHIVE_REGISTRY:
        now_ts = time.time()
        for k in [k for k, v in _QUICK_ARCHIVE_REGISTRY.items() if now_ts - v.get("created_at", now_ts) > 172800]:
            _QUICK_ARCHIVE_REGISTRY.pop(k, None)
    has_quick_targets = bool(_QUICK_ARCHIVE_REGISTRY)

    if not has_sub_rules and not has_default_rule and not has_quick_targets:
        return 0
    if not await _openlist_ready():
        return 0
    try:
        from services.task_service import tasks_all
        tasks = await tasks_all(force=True)
    except Exception as e:  # noqa: BLE001
        log.warning("自动归档：任务聚合失败: %s", e)
        return 0
    enqueued = 0
    from services.subscription_service import (
        _sub_match_rule, _render_dir_template, _sub_bump, _sub_rules_sorted)

    # 规则集在单轮 sweep 内不会变化，预排序一次即可：_sub_rules_sorted() 每次
    # 都做完整 sorted() + 比较器（实测 100 条规则 138.9µs/次），逐任务重排会让
    # 单轮 2000 任务白耗 150ms+。
    _sweep_rules = [r for r in _sub_rules_sorted() if r.get("enabled")]

    for t in tasks:
        if str(t.get("_download_status") or "") != "completed":
            continue
        uid = str(t.get("_unique_id") or "")
        if not uid:
            continue

        rule = _sub_match_rule(t, _sweep_rules)
        remote_dir = None
        policy = "overwrite"
        delete_local = False
        rule_id = None
        rule_label = ""

        quick_target = _QUICK_ARCHIVE_REGISTRY.get(uid)
        if quick_target is not None:
            remote_dir = _archive_norm_dir(str(quick_target.get("remoteDir") or ""))
            policy = str(quick_target.get("policy") or "overwrite")
            delete_local = bool(quick_target.get("deleteLocal", False))
            rule_id = "quick"
            rule_label = "直投快捷归档"
        elif rule is not None:
            remote_dir = _render_dir_template(
                str(rule.get("dirTemplate") or ""),
                source=str(t.get("source") or rule.get("chatTitle") or ""),
                ftype=str(t.get("_type") or ""),
                ts=t.get("_date_ts") if isinstance(t.get("_date_ts"), (int, float)) else None,
                filename=str(t.get("filename") or ""),
                chat_title=str(rule.get("chatTitle") or t.get("source") or ""),
                width=t.get("width"),
                height=t.get("height"))
            policy = "skip" if str(rule.get("policy") or "") == "skip" else "overwrite"
            delete_local = bool(rule.get("deleteLocal", True))
            rule_id = rule.get("id")
            rule_label = f"规则「{rule.get('chatTitle') or ''}」"
        elif has_default_rule:
            remote_dir = default_dir
            policy = "skip" if str(_ARCHIVE_CONFIG.get("policy") or "").lower() == "skip" else "overwrite"
            delete_local = bool(_ARCHIVE_CONFIG.get("deleteLocal"))
            rule_id = "default"
            rule_label = "默认归档"
        else:
            continue

        if not remote_dir or remote_dir == "/":
            if rule is not None:
                log.warning("自动归档：规则 %s 目录模板渲染失败，跳过 %s", rule.get("id"), uid)
            continue

        latest = _archive_latest_raw_of(uid)
        attempts = 0
        if latest is not None:
            st = str(latest.get("state") or "")
            if st in ("queued", "uploading", "done", "cancelled"):
                continue
            attempts = int(latest.get("auto_attempts") or 0)
            # 永久性失败（本地源文件已丢失等）不再自动重试：重扫一轮也注定再失败一次，
            # 既白耗 I/O，又会让失败列表越来越长。用户重新下载后会产生新的 job。
            if not _is_retryable_archive_error(str(latest.get("error") or "")):
                continue
            if attempts >= _AUTO_ARCHIVE_MAX_ATTEMPTS:
                continue
        lp = _resolve_host_local_path(t.get("local_path"))
        if not lp or not os.path.exists(lp):
            continue
        raw_name = str(t.get("filename") or "未命名")
        # 后端偶发不返回 fileName，显示名会退化成「文件 <uniqueId>」（无后缀）。
        # 直接归档会在云端留下没有后缀的文件，播放器/刮削器全认不出来。
        # 这里先用本地路径/MIME 把真实后缀补回来，再做广告清洗。
        raw_name = _ensure_archive_ext(
            raw_name,
            local_path=str(t.get("local_path") or ""),
            mime=str(t.get("_mimeType") or ""),
            ftype=str(t.get("_type") or ""),
        )
        clean_name = _clean_archive_filename(raw_name, enabled=bool(_ARCHIVE_CONFIG.get("cleanFilename", True)))
        remote_path = _archive_join(remote_dir, clean_name)
        job = {
            "id": secrets.token_hex(6),
            "unique_id": uid,
            "filename": clean_name,
            "raw_filename": raw_name,
            "size_bytes": t.get("_size_bytes") if isinstance(t.get("_size_bytes"), (int, float)) else None,
            "local_path": lp,
            "remote_dir": remote_dir,
            "remote_path": remote_path,
            "policy": policy,
            "delete_local": delete_local,
            "state": "queued",
            "progress": 0,
            "error": "",
            "created_at": time.time(),
            "updated_at": time.time(),
            "archived_at": 0.0,
            "auto": True,
            "auto_attempts": attempts + 1,
            "rule_id": rule_id,
        }
        _ARCHIVE_JOBS[job["id"]] = job
        _ARCHIVE_TASKS[job["id"]] = asyncio.create_task(_archive_worker(job))
        if rule_id and rule_id not in ("default", "quick") and rule is not None:
            _sub_bump(rule.get("id"), "enqueued")
        elif rule_id == "quick":
            _QUICK_ARCHIVE_REGISTRY.pop(uid, None)
        elif rule_id == "default":
            stats = _ARCHIVE_CONFIG.setdefault("stats", {})
            stats["enqueued"] = int(stats.get("enqueued") or 0) + 1
            stats["lastHitAt"] = time.time()
            _archive_config_save()

        LOG_STORE.append("INFO", "自动归档已入队：%s → %s（%s，%s）" % (
            job["filename"], job["remote_path"], rule_label,
            ("第 %d 次尝试" % job["auto_attempts"]) if attempts else "首次"),
            task_id=str(t.get("id")))
        enqueued += 1

    if enqueued:
        _archive_save()

    try:
        from services.watermark_service import _disk_guard_check, _check_and_wake_waiting_disk_tasks
        await _disk_guard_check()
        await _check_and_wake_waiting_disk_tasks()
    except Exception as e:
        log.debug("自动归档后触发磁盘保护巡检异常: %s", e)

    return enqueued


async def _auto_archive_loop() -> None:
    """后台周期扫描（启动时由 _startup 挂起，进程生命周期内常驻）。"""
    from services.subscription_service import _AUTO_ARCHIVE_FIRST_DELAY, _AUTO_ARCHIVE_INTERVAL
    try:
        await asyncio.sleep(_AUTO_ARCHIVE_FIRST_DELAY)
        while True:
            try:
                from services.watermark_service import _disk_guard_check, _check_and_wake_waiting_disk_tasks
                n = await _auto_archive_sweep()
                if n:
                    log.info("自动归档：本轮新入队 %d 个文件", n)
                await _disk_guard_check()
                await _check_and_wake_waiting_disk_tasks()
            except Exception as e:  # noqa: BLE001
                log.warning("自动归档扫描异常: %s", e)
            await asyncio.sleep(_AUTO_ARCHIVE_INTERVAL)
    except asyncio.CancelledError:
        return
