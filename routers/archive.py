# -*- coding: utf-8 -*-

"""
routers/archive.py — 表现层路由模块：网盘归档启动、进度查询、失败诊断与智能重试
"""
import os
import re
import time
import json
import asyncio
from typing import Any, Dict, List, Optional, Tuple, Union
from fastapi import APIRouter, Request, Response, Form, Query, Header, Cookie, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse, PlainTextResponse
from core import *
from services import *



router = APIRouter()

@router.post("/archive/start")
@router.post("/archive/batch")
async def archive_start(request: Request):
    """一键归档 / 批量归档：
    {uniqueIds:[...], remoteDir:"/网盘/目录", policy:"overwrite"|"skip", deleteLocal:bool}。

    以 uniqueId 从任务缓存回查 FileRecord（localPath/下载状态/大小），
    仅下载完成且本地路径存在的文件可归档；同一文件已有进行中任务则幂等返回。
    deleteLocal 显式给出时以请求为准；未给出时回落设置页全局开关
    「归档成功后自动删除本地文件」。为真则归档成功后自动清理本地磁盘原文件。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "请求体不是合法 JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "message": "请求体格式错误"}

    remote_dir = _archive_norm_dir(str(body.get("remoteDir") or ""))
    if remote_dir is None or remote_dir == "/":
        return {"ok": False, "message": "请选择有效的目标目录（OpenList 根目录下）"}
    policy = "skip" if str(body.get("policy") or "").lower() == "skip" else "overwrite"
    # deleteLocal 未显式给出时，回落到设置页全局开关「归档成功后自动删除本地文件」
    # （与 /api/tg/quick-download 的取值口径保持一致，避免手动归档静默保留本地文件）
    if "deleteLocal" in body or "delete_local" in body:
        delete_local = bool(body.get("deleteLocal") or body.get("delete_local"))
    else:
        delete_local = bool(_ARCHIVE_CONFIG.get("deleteLocal"))

    uids_raw = body.get("uniqueIds")
    if not isinstance(uids_raw, list):
        uids_raw = [body.get("uniqueId")]
    seen = set()
    uids: List[str] = []
    for u in uids_raw:
        s = str(u or "").strip()
        if s and s not in seen:
            seen.add(s)
            uids.append(s)
    if not uids:
        return {"ok": False, "message": "没有可归档的文件"}

    tasks_list = await tasks_all()
    by_uid = {str(t.get("_unique_id") or ""): t for t in tasks_list if t.get("_unique_id")}

    started: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    seen_paths: set = set()
    for uid in uids:
        t = by_uid.get(uid)
        if t is None:
            errors.append({"uniqueId": uid, "message": "找不到该文件的本地记录"})
            continue
        if str(t.get("_download_status") or "") != "completed":
            errors.append({"uniqueId": uid, "message": "文件尚未下载完成，无法归档"})
            continue
        lp = _resolve_host_local_path(t.get("local_path"))
        if not lp or not os.path.exists(lp):
            errors.append({"uniqueId": uid, "message": "未在磁盘找到该本地文件，无法归档"})
            continue
        active = _archive_active_of(uid)
        if active is not None:
            started.append(_archive_public(active))
            continue

        raw_name = str(t.get("filename") or "未命名")
        # 显示名缺后缀时（后端未返回 fileName，退化成「文件 <uniqueId>」），
        # 从本地路径 / MIME 推断真实后缀补回，避免归档出无后缀文件。
        raw_name = _ensure_archive_ext(
            raw_name,
            local_path=str(t.get("local_path") or ""),
            mime=str(t.get("_mimeType") or ""),
            ftype=str(t.get("_type") or ""),
        )
        clean_name = _clean_archive_filename(raw_name, enabled=bool(_ARCHIVE_CONFIG.get("cleanFilename", True)))
        remote_path = _archive_join(remote_dir, clean_name)
        if remote_path.lower() in seen_paths:
            errors.append({"uniqueId": uid, "message": "目标目录已有同名文件排入本批，请分开归档"})
            continue
        seen_paths.add(remote_path.lower())

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
        }
        _ARCHIVE_JOBS[job["id"]] = job
        _ARCHIVE_TASKS[job["id"]] = asyncio.create_task(_archive_worker(job))
        started.append(_archive_public(job))
        log.info("归档任务已入队: %s → %s（策略 %s, 归档后删本地: %s）", job["filename"], job["remote_path"], policy, delete_local)

    if started:
        _archive_save()

    if not started and errors:
        return {"ok": False, "message": errors[0]["message"], "errors": errors}
    return {"ok": True, "started": len(started), "jobs": started, "errors": errors}


@router.get("/archive/status")
async def archive_status():
    """前端轮询归档进度（按 unique_id 汇聚最新状态，创建时间倒序）。"""
    jobs = sorted(_ARCHIVE_JOBS.values(),
                  key=lambda j: j.get("created_at") or 0.0, reverse=True)
    latest_by_uid: Dict[str, Dict[str, Any]] = {}
    for j in jobs:
        uid = str(j.get("unique_id") or "")
        if uid and uid not in latest_by_uid:
            latest_by_uid[uid] = j
        elif not uid:
            latest_by_uid[str(j.get("id") or secrets.token_hex(4))] = j
    all_latest = list(latest_by_uid.values())
    active = [j for j in all_latest if j.get("state") in ("queued", "uploading")]
    inactive = [j for j in all_latest if j.get("state") not in ("queued", "uploading")]
    res_jobs = active + inactive[:500]
    return {"ok": True, "jobs": [_archive_public(j) for j in res_jobs]}


@router.post("/archive/cancel")
async def archive_cancel(request: Request):
    """取消进行中的归档任务（尽力而为：停止取流，已上传部分不回滚）。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    jid = str((body or {}).get("jobId") or "")
    job = _ARCHIVE_JOBS.get(jid)
    if job is None:
        return {"ok": False, "message": "任务不存在或已被清理"}
    if job.get("state") not in ("queued", "uploading"):
        return {"ok": True, "job": _archive_public(job)}
    task = _ARCHIVE_TASKS.get(jid)
    if task is not None and not task.done():
        task.cancel()
    job["state"] = "cancelled"
    job["error"] = "用户已取消"
    job["updated_at"] = time.time()
    _archive_save()
    return {"ok": True, "job": _archive_public(job)}


@router.get("/api/archive/failed")
@router.get("/api/archive/failed-summary")
async def api_archive_failed_summary():
    """获取所有处于 failed 状态的归档任务聚合分类与明细。"""
    failed_jobs = [j for j in _ARCHIVE_JOBS.values() if j.get("state") == "failed"]
    failed_jobs.sort(key=lambda x: float(x.get("updated_at") or x.get("created_at") or 0.0), reverse=True)

    categories_count = {
        "token_expired": 0,
        "storage_full": 0,
        "conflict": 0,
        "timeout": 0,
        "unknown": 0,
    }

    diagnostics = []
    for j in failed_jobs:
        raw_err = str(j.get("error") or "")
        cat_code, cat_label, fix = _classify_archive_error(raw_err)
        categories_count[cat_code] = categories_count.get(cat_code, 0) + 1
        diagnostics.append({
            "id": j.get("id"),
            "uniqueId": str(j.get("unique_id") or ""),
            "filename": str(j.get("filename") or ""),
            "size": _fmt_size(j.get("size_bytes")),
            "sizeBytes": j.get("size_bytes") or 0,
            "remotePath": str(j.get("remote_path") or ""),
            "remoteDir": str(j.get("remote_dir") or ""),
            "error": raw_err,
            "category": cat_code,
            "categoryLabel": cat_label,
            "suggestedFix": fix,
            "retryCount": int(j.get("retry_count") or 0),
            "failedAt": float(j.get("updated_at") or j.get("created_at") or 0.0),
            "failedTime": _fmt_time(j.get("updated_at") or j.get("created_at")),
            "policy": str(j.get("policy") or "overwrite"),
            # 让前端知道这个失败到底能不能重试：永久性错误（本地文件已丢
            # 失等）必须把「重试」按钮置灰，而不是让用户点了又点。
            "retryable": cat_code not in _PERMANENT_ARCHIVE_ERRORS,
        })

    return {
        "ok": True,
        "summary": {
            "total": len(failed_jobs),
            "categories": categories_count,
        },
        "failedJobs": diagnostics,
        "diagnostics": diagnostics,
    }


@router.post("/api/archive/retry-failed")
@router.post("/api/archive/batch-retry")
async def api_archive_retry_failed(request: Request):
    """一键批量重试失败的归档任务（具备防高并发竞态锁与防刷限流机制）。"""
    ip = _client_ip(request)
    if _batch_retry_rate_limited(ip):
        return JSONResponse({"ok": False, "code": "RATE_LIMITED", "message": "批量重试请求过于频繁，请稍后再试"}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    target_category = str(body.get("category") or "all").lower()
    target_ids = body.get("jobIds") or body.get("ids") or []
    if isinstance(target_ids, (str, int)):
        target_ids = [str(target_ids)]
    if len(target_ids) > 100:
        return JSONResponse({"ok": False, "message": "单次批量重试任务数不可超过 100 个"}, status_code=400)
    target_ids_set = {str(i).strip() for i in target_ids if str(i).strip()} if target_ids else set()
    force_overwrite = bool(body.get("forceOverwrite", False))

    async with _BATCH_RETRY_LOCK:
        failed_jobs = [j for j in _ARCHIVE_JOBS.values() if j.get("state") == "failed"]
        if not failed_jobs:
            return {"ok": True, "message": "当前暂无失败的归档任务需要重试", "retriedCount": 0, "retriedJobIds": []}

        candidates = []
        blocked: List[Dict[str, str]] = []
        has_token_expired = False
        for j in failed_jobs:
            jid = str(j.get("id"))
            if target_ids_set and jid not in target_ids_set:
                continue
            raw_err = str(j.get("error") or "")
            cat_code, cat_label, fix = _classify_archive_error(raw_err)
            if target_category != "all" and cat_code != target_category:
                continue
            # 永久性错误熔断：本地源文件已丢失 / 云端对象已变动，重试一万次
            # 结果都不会变。历史上只有「未指定 jobIds」时才熔断，而前端的
            # 单项「重试」按钮恰好总是带 jobIds，于是死循环完全绕开了熔断——
            # 用户每点一次就再跑一遍注定失败的归档。这里对永久性错误一律拒绝，
            # 无论是否显式指定了 ID。
            if cat_code in _PERMANENT_ARCHIVE_ERRORS:
                blocked.append({"id": jid, "reason": cat_label, "suggestedFix": fix})
                continue
            # 次数熔断：临时性错误也不能无限重试。
            if int(j.get("retry_count") or 0) >= _MAX_JOB_RETRIES:
                blocked.append({"id": jid, "reason": f"已达最大重试次数 {_MAX_JOB_RETRIES}",
                                "suggestedFix": "请先排查根因，或删除该失败记录后重新归档"})
                continue
            if cat_code == "token_expired":
                has_token_expired = True
            candidates.append((j, cat_code))

        if not candidates:
            if blocked:
                # 如实告知为什么没重试，而不是含糊地说「未找到符合条件的任务」——
                # 否则用户以为按钮坏了，会反复点击。
                first = blocked[0]
                return {
                    "ok": False,
                    "code": "NOT_RETRYABLE",
                    "message": f"该任务无法重试：{first['reason']}。{first['suggestedFix']}",
                    "retriedCount": 0, "retriedJobIds": [],
                    "blocked": blocked, "blockedCount": len(blocked),
                }
            return {"ok": True, "message": "未找到符合条件的失败任务（或已达最大连续重试上限）",
                    "retriedCount": 0, "retriedJobIds": [], "blocked": [], "blockedCount": 0}

        if has_token_expired:
            try:
                await _openlist_relogin()
            except Exception as e:
                log.warning("重试前自动刷新 OpenList 凭据失败: %s", e)

        retried_ids = []
        for j, cat_code in candidates:
            jid = str(j.get("id"))
            j["state"] = "queued"
            j["progress"] = 0
            j["error"] = ""
            j["updated_at"] = time.time()
            j["retry_count"] = int(j.get("retry_count") or 0) + 1
            if force_overwrite and cat_code == "conflict":
                j["policy"] = "overwrite"
            retried_ids.append(jid)
            if jid not in _ARCHIVE_TASKS or _ARCHIVE_TASKS[jid].done():
                task = asyncio.create_task(_archive_worker(j))
                _ARCHIVE_TASKS[jid] = task

        _archive_save()
        log.info("已成功一键批量重试 %d 个失败归档任务: %s", len(retried_ids), retried_ids)

        return {
            "ok": True,
            "message": f"已成功重新调度 {len(retried_ids)} 个失败归档任务",
            "retriedCount": len(retried_ids),
            "retriedJobIds": retried_ids,
            "skippedCount": 0,
        }


@router.delete("/api/archive/failed")
@router.post("/api/archive/failed/dismiss")
async def api_archive_failed_dismiss(request: Request):
    """删除失败归档记录（支持单个/批量，供告警中心的胶囊删除按钮调用）。

    背景：永久性失败（本地文件已丢失等）被熔断后既修不好也删不掉，
    永远挂在告警中心。死记录没有出口，列表只会越积越长。

    Body: {"jobId": "..."} / {"jobIds": ["a","b"]} / {"allFailed": true}
    只删 state=='failed' 的记录；其它状态跳过并如实说明。
    删除的只是「失败记录」本身，不触碰云端/本地任何文件本体。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}

    ids: List[str] = []
    if body.get("jobIds") is not None:
        raw = body.get("jobIds")
        if isinstance(raw, (str, int)):
            raw = [raw]
        if not isinstance(raw, list):
            return JSONResponse({"ok": False, "message": "jobIds 必须是数组"}, status_code=400)
        ids = [str(i).strip() for i in raw if str(i).strip()]
    elif body.get("jobId") is not None:
        ids = [str(body["jobId"]).strip()]
    elif body.get("allFailed") is True:
        ids = [str(j.get("id")) for j in _ARCHIVE_JOBS.values() if j.get("state") == "failed"]

    ids = [i for i in ids if i]
    if not ids:
        return JSONResponse({"ok": False, "message": "缺少 jobId / jobIds / allFailed"}, status_code=400)
    if len(ids) > 100:
        return JSONResponse({"ok": False, "message": "单次最多删除 100 条失败记录"}, status_code=400)

    deleted: List[str] = []
    skipped: List[Dict[str, str]] = []
    for jid in ids:
        job = _ARCHIVE_JOBS.get(jid)
        if job is None:
            skipped.append({"id": jid, "reason": "记录不存在或已被清理"})
            continue
        if job.get("state") != "failed":
            skipped.append({"id": jid,
                            "reason": f"状态为 {job.get('state')}，仅 failed 记录可删除（进行中的请先取消）"})
            continue
        _ARCHIVE_JOBS.pop(jid, None)
        deleted.append(jid)

    if deleted:
        _archive_save()
        LOG_STORE.append("INFO", "已删除 %d 条失败归档记录（用户手动清理）" % len(deleted))

    return {
        "ok": True,
        "deletedCount": len(deleted),
        "deletedJobIds": deleted,
        "skipped": skipped,
        "skippedCount": len(skipped),
        "message": (f"已删除 {len(deleted)} 条失败记录"
                    + (f"，另有 {len(skipped)} 条跳过" if skipped else "")),
    }


@router.get("/archive/config")
async def archive_config_get():
    """获取当前归档配置（默认网盘目录、自动归档开关、冲突策略等）。"""
    return {"ok": True, "config": _archive_config_public()}


@router.post("/archive/config")
async def archive_config_post(request: Request):
    """保存默认归档配置。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "message": "请求体格式错误"}, status_code=400)

    raw_dir = str(body.get("defaultDir") or "").strip()
    norm_dir = ""
    if raw_dir:
        norm_dir = _archive_norm_dir(raw_dir)
        if norm_dir is None or norm_dir == "/":
            return JSONResponse({"ok": False, "message": "默认归档目录格式无效（需以 / 开头且指向具体网盘子目录，不能为根目录 /）"}, status_code=400)

    # 会话冷备目录：留空 = 自动推导；填写则必须是 / 开头的具体网盘子目录
    raw_bk = str(body.get("sessionBackupDir") or "").strip()
    norm_bk = ""
    if raw_bk:
        norm_bk = _archive_norm_dir(raw_bk)
        if norm_bk is None or norm_bk == "/":
            return JSONResponse({"ok": False, "message": "会话冷备目录格式无效（需以 / 开头且不能为根目录 /，例如 /onedrive/TG-Backups）"}, status_code=400)

    _ARCHIVE_CONFIG["autoArchive"] = bool(body.get("autoArchive"))
    _ARCHIVE_CONFIG["defaultDir"] = norm_dir
    if "sessionBackupDir" in body:
        _ARCHIVE_CONFIG["sessionBackupDir"] = norm_bk
    if "publicBaseUrl" in body or "public_base_url" in body:
        _ARCHIVE_CONFIG["publicBaseUrl"] = str(body.get("publicBaseUrl") or body.get("public_base_url") or "").strip().rstrip("/")
    _ARCHIVE_CONFIG["policy"] = "skip" if str(body.get("policy") or "").lower() == "skip" else "overwrite"
    _ARCHIVE_CONFIG["deleteLocal"] = bool(body.get("deleteLocal"))
    if "cleanFilename" in body:
        _ARCHIVE_CONFIG["cleanFilename"] = bool(body.get("cleanFilename"))
    if "diskWatermarkGB" in body:
        try:
            _ARCHIVE_CONFIG["diskWatermarkGB"] = max(1.0, min(100.0, float(body.get("diskWatermarkGB") or 5)))
        except (TypeError, ValueError):
            pass
    if "diskAutoClean" in body:
        _ARCHIVE_CONFIG["diskAutoClean"] = bool(body.get("diskAutoClean"))
    if "diskHighWatermarkPercent" in body:
        try:
            _ARCHIVE_CONFIG["diskHighWatermarkPercent"] = max(50.0, min(99.0, float(body.get("diskHighWatermarkPercent") or 85.0)))
        except (TypeError, ValueError):
            pass
    if "diskLowWatermarkPercent" in body:
        try:
            _ARCHIVE_CONFIG["diskLowWatermarkPercent"] = max(30.0, min(95.0, float(body.get("diskLowWatermarkPercent") or 75.0)))
        except (TypeError, ValueError):
            pass
    _archive_config_save()
    log.info("默认归档配置已更新: 自动归档=%s, 目录=%s, 策略=%s, 删本地=%s, 洗文件名=%s, 高水位=%s%%, 低水位=%s%%, 低水位GB=%sGB",
             _ARCHIVE_CONFIG["autoArchive"], _ARCHIVE_CONFIG["defaultDir"],
             _ARCHIVE_CONFIG["policy"], _ARCHIVE_CONFIG["deleteLocal"],
             _ARCHIVE_CONFIG["cleanFilename"], _ARCHIVE_CONFIG.get("diskHighWatermarkPercent", 85.0),
             _ARCHIVE_CONFIG.get("diskLowWatermarkPercent", 75.0), _ARCHIVE_CONFIG["diskWatermarkGB"])

    if _ARCHIVE_CONFIG["autoArchive"] and _ARCHIVE_CONFIG["defaultDir"]:
        asyncio.create_task(_auto_archive_sweep())

    return {"ok": True, "config": _archive_config_public()}


@router.post("/archive/sweep")
async def archive_sweep():
    """手动触发一轮全局归档扫描（扫描全部已完成下载的文件）。"""
    if not await _openlist_ready():
        return JSONResponse({"ok": False, "message": "OpenList 未登录或未连接，请先到设置页登录 OpenList"}, status_code=400)
    try:
        n = await _auto_archive_sweep()
        return {"ok": True, "enqueued": n, "message": f"扫描完成，新入队 {n} 个归档任务" if n else "扫描完成，没有新的待归档任务"}
    except Exception as e:  # noqa: BLE001
        log.error("手动触发归档扫描失败: %s", e)
        return JSONResponse({"ok": False, "message": f"扫描失败: {e}"}, status_code=500)

