# -*- coding: utf-8 -*-

"""
routers/library.py — 表现层路由模块：本地/云端资产库浏览、删除、失效清理与云端取回
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

@router.get("/library/local", response_class=HTMLResponse)
async def library_local(request: Request, category: str = ""):
    tasks_list = await tasks_all()
    # 「立即归档」亮起总开关：OpenList 已挂载（登录且令牌验证通过）
    openlist_ready = await _openlist_ready()
    # 本地在存：所有文件记录经过严格在盘门禁过滤，已归档且本地已删除的文件自动隐藏
    _ai = _archive_index_snapshot()
    all_files = [_enrich_archive(f, openlist_ready, _ai) for f in _dedup_files([_to_local_file_from_task(t) for t in tasks_list])]
    in_stock_files = [f for f in all_files if _is_local_in_stock(f)]
    unarchived_count = sum(1 for f in in_stock_files if not (f.get("archive") and f.get("archive", {}).get("state") == "done"))
    archived_count = sum(1 for f in in_stock_files if f.get("archive") and f.get("archive", {}).get("state") == "done")
    files = _filter_local_files(in_stock_files, archive_status=category) if category else in_stock_files
    total_size = _sum_human(files)
    ctx = await _ctx(request, "library-local", "library", "local", extra={
        "files": files,
        "total_size": total_size,
        "filtered": bool(category),
        "selected_category": category,
        "unarchived_count": unarchived_count,
        "archived_count": archived_count,
        # 来源聊天筛选选项 = 真实账号聊天列表（与 /tasks、/submit 同源）
        "chat_sources": await chat_sources(),
    })
    return templates.TemplateResponse("library_local.html", ctx)


@router.get("/library/cloud", response_class=HTMLResponse)
async def library_cloud(request: Request):
    # 云端归档 = bridge 实际上传到 OpenList 的文件（归档日志为源，尽力经
    # OpenList fs/list 校验云端仍在）。Java 后端 transferStatus 与本页无关。
    cloud_files = await _cloud_archive_rows()
    # 「网盘分类」唯一数据源 = OpenList 根目录下的真实挂载；
    # 拉取失败时不回退硬编码，仅用归档记录里真实出现过的网盘兜底。
    mounts_res = await openlist_mounts()
    mounts = list(mounts_res.get("mounts") or [])
    if not mounts_res.get("ok"):
        log.warning("网盘分类：读取 OpenList 挂载失败，退化为按归档记录聚合: %s",
                    mounts_res.get("message"))

    # 按网盘分组：云端页全部条目都是已归档，所以「未归档/已归档」这个维度分不出来；
    # 真正有意义的分组维度是「落在哪个网盘」。每组先聚合数量与体积供文件夹卡片头部展示。
    drive_groups: List[Dict[str, Any]] = []
    by_drive: Dict[str, List[Dict[str, Any]]] = {}
    for f in cloud_files:
        by_drive.setdefault(str(f.get("drive") or "默认网盘"), []).append(f)
    # 组的顺序：先按真实挂载顺序，再补历史记录里出现过但当前未挂载的网盘
    ordered_keys = [d for d in mounts if d in by_drive]
    ordered_keys += [k for k in by_drive if k not in ordered_keys]
    for k in ordered_keys:
        rows = by_drive[k]
        total_bytes = sum(int(r.get("size_bytes") or 0) for r in rows)
        missing = sum(1 for r in rows if r.get("status") == "missing")
        drive_groups.append({
            "drive": k,
            "files": rows,
            "count": len(rows),
            "size_human": _fmt_size(total_bytes),
            "missing_count": missing,
        })

    ctx = await _ctx(request, "library-cloud", "library", "cloud", extra={
        "cloud_files": cloud_files,
        "drive_groups": drive_groups,
        "openlist_public_base": str(_ARCHIVE_CONFIG.get("publicBaseUrl") or ""),
        "openlist_mounts": mounts,
        "openlist_mounts_ok": bool(mounts_res.get("ok")),
    })
    return templates.TemplateResponse("library_cloud.html", ctx)


@router.get("/partials/local-files", response_class=HTMLResponse)
async def partial_local_files(request: Request, source: str = "", type: str = "", size: str = "", category: str = ""):
    """本地在存结果区局部刷新（htmx），与 /partials/tasks 同款模式。"""
    tasks_list = await tasks_all()
    openlist_ready = await _openlist_ready()
    _ai = _archive_index_snapshot()
    all_files = [_enrich_archive(f, openlist_ready, _ai) for f in _dedup_files([_to_local_file_from_task(t) for t in tasks_list])]
    files = [f for f in all_files if _is_local_in_stock(f)]
    filtered = _filter_local_files(files, source=source, ftype=type, fsize=size, archive_status=category)
    return templates.TemplateResponse("partials/_local_files.html", {
        "request": request,
        "files": filtered,
        "total_size": _sum_human(filtered),
        "filtered": bool(source or type or size or category),
        "selected_category": category,
    })


@router.post("/library/local/delete")
@router.post("/api/local/delete")
async def library_local_delete(request: Request):
    """本地文件单项及批量删除端点。
    请求体：{"uniqueIds": ["uid1", ...]} 或 {"uniqueId": "uid1"}
    校验权限与严格路径（只删除合法本地下载文件，防任意路径逃逸），同步通知后端清理。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "message": "请求体格式错误"}, status_code=400)

    uids_raw = body.get("uniqueIds")
    if not isinstance(uids_raw, list):
        single = body.get("uniqueId")
        uids_raw = [single] if single else []
    uids = [str(u).strip() for u in uids_raw if u and str(u).strip()]
    if not uids:
        return JSONResponse({"ok": False, "message": "未指定要删除的文件 (uniqueId)"}, status_code=400)
    if len(uids) > 100:
        return JSONResponse({"ok": False, "message": "单次最多删除 100 个文件"}, status_code=400)

    tasks_list = await tasks_all()
    by_uid = {str(t.get("_unique_id") or ""): t for t in tasks_list if t.get("_unique_id")}

    deleted = []
    errors = []
    for uid in uids:
        t = by_uid.get(uid)
        if not t:
            if uid in _DELETED_LOCAL_UIDS:
                deleted.append(uid)
                continue
            errors.append({"uniqueId": uid, "message": "找不到该文件的本地记录"})
            continue

        lp = _resolve_host_local_path(t.get("local_path"))
        if lp and os.path.exists(lp):
            ok, err = await _safe_delete_local_path(lp)
            if not ok:
                errors.append({"uniqueId": uid, "message": f"删除磁盘文件失败: {err}"})
                continue

        # 若该文件恰有进行中的归档任务，安全联动取消归档，防止脏上传
        active = _archive_active_of(uid)
        if active:
            act_task = _ARCHIVE_TASKS.get(active.get("id"))
            if act_task and not act_task.done():
                act_task.cancel()
            active["state"] = "cancelled"
            active["error"] = "本地文件已被删除，归档已取消"
            active["updated_at"] = time.time()
            _archive_save()

        _DELETED_LOCAL_UIDS.add(uid)
        deleted.append(uid)
        tg = t.get("_telegram_id")
        fid = t.get("_file_id")
        if tg and fid:
            try:
                await BACKEND.remove_file(tg, {"fileId": fid})
            except Exception as e:  # noqa: BLE001
                log.debug("通知后端删除记录失败: %s", e)

    if deleted:
        BACKEND._cache.clear()
        global _TASKS_CACHE
        _TASKS_CACHE["expire"] = 0.0
        _TASKS_CACHE["value"] = None

    if not deleted and errors:
        return {"ok": False, "deleted": 0, "errors": errors, "message": errors[0]["message"]}
    return {
        "ok": True,
        "deleted": len(deleted),
        "deletedUids": deleted,
        "errors": errors,
        "message": f"成功删除 {len(deleted)} 个本地文件" if not errors else f"已删除 {len(deleted)} 个文件，{len(errors)} 个失败"
    }


@router.post("/library/cloud/delete")
@router.post("/archive/cloud/delete")
async def library_cloud_delete(request: Request):
    """云端文件删除接口：调用 OpenList POST /api/fs/remove 并在本地归档日志清理记录。
    支持 {"remotePath": "..."}, {"remotePaths": [...]}, {"jobId": "..."}, {"jobIds": [...]}
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "message": "请求体格式错误"}, status_code=400)

    paths_to_delete: List[str] = []
    jids_to_delete: List[str] = []

    raw_paths = body.get("remotePaths")
    if not isinstance(raw_paths, list):
        p = body.get("remotePath") or body.get("cloudPath")
        raw_paths = [p] if p else []
    for p in raw_paths:
        norm = _norm_remote_path(str(p or ""))
        if norm and norm != "/":
            paths_to_delete.append(norm)

    raw_jids = body.get("jobIds")
    if not isinstance(raw_jids, list):
        jid = body.get("jobId") or body.get("id")
        raw_jids = [jid] if jid else []
    for jid in raw_jids:
        jid_str = str(jid or "").strip()
        if jid_str and jid_str in _ARCHIVE_JOBS:
            jids_to_delete.append(jid_str)
            job_path = _ARCHIVE_JOBS[jid_str].get("remote_path")
            norm = _norm_remote_path(str(job_path or ""))
            if norm and norm not in paths_to_delete:
                paths_to_delete.append(norm)

    if not paths_to_delete and not jids_to_delete:
        return JSONResponse({"ok": False, "message": "未指定要删除的云端文件或任务"}, status_code=400)

    grouped: Dict[str, List[str]] = {}
    for rp in paths_to_delete:
        parent_dir, fname = posixpath.split(rp)
        if not parent_dir:
            parent_dir = "/"
        grouped.setdefault(parent_dir, []).append(fname)

    errors = []
    deleted_paths = []

    token = ""
    try:
        token = await _openlist_token()
    except Exception as e:  # noqa: BLE001
        log.warning("获取 OpenList token 失败，将仅清理本地日志: %s", e)

    for pdir, names in grouped.items():
        if token:
            try:
                async def _rm_once(tok: str):
                    return await _openlist_client.post(
                        "/api/fs/remove",
                        json={"dir": pdir, "names": names},
                        headers={"Authorization": tok}
                    )
                resp = await _rm_once(token)
                code, data, msg = _openlist_env(resp)
                if resp.status_code == 401 or code in (401, 403):
                    token = await _openlist_relogin()
                    resp = await _rm_once(token)
                    code, data, msg = _openlist_env(resp)
                if code != 200 and "not found" not in msg.lower() and "no such file" not in msg.lower():
                    errors.append({"dir": pdir, "names": names, "message": msg or f"OpenList 业务码 {code}"})
                else:
                    for n in names:
                        deleted_paths.append(f"{'' if pdir == '/' else pdir}/{n}")
            except Exception as e:  # noqa: BLE001
                log.error("调用 OpenList fs/remove 异常: %s", e)
                errors.append({"dir": pdir, "names": names, "message": str(e)})
        else:
            for n in names:
                deleted_paths.append(f"{'' if pdir == '/' else pdir}/{n}")

    to_pop = set(jids_to_delete)
    for jid, j in list(_ARCHIVE_JOBS.items()):
        if str(j.get("remote_path") or "") in paths_to_delete:
            to_pop.add(jid)
    for jid in to_pop:
        _ARCHIVE_JOBS.pop(jid, None)
    if to_pop:
        _archive_save()

    return {
        "ok": len(deleted_paths) > 0 or len(to_pop) > 0,
        "deleted": len(deleted_paths) or len(to_pop),
        "cleanedJobs": len(to_pop),
        "errors": errors,
        "message": f"成功删除 {len(deleted_paths) or len(to_pop)} 个云端记录" if not errors else f"已处理删除，存在 {len(errors)} 个报错"
    }


@router.post("/library/cloud/clear-missing")
@router.post("/archive/cloud/clear-missing")
async def library_cloud_clear_missing():
    """清理已失效记录：扫描当前 _cloud_archive_rows() 中 status == 'missing' 的任务，从本地日志中移除。"""
    rows = await _cloud_archive_rows()
    missing_paths = {r["cloud_path"].lower() for r in rows if r.get("status") == "missing"}
    missing_ids = {r["id"] for r in rows if r.get("status") == "missing" and r.get("id")}

    to_remove = []
    for jid, j in list(_ARCHIVE_JOBS.items()):
        if jid in missing_ids or str(j.get("remote_path") or "").lower() in missing_paths:
            to_remove.append(jid)

    for jid in to_remove:
        _ARCHIVE_JOBS.pop(jid, None)

    if to_remove:
        _archive_save()

    return {
        "ok": True,
        "cleared": len(to_remove),
        "message": f"已清理 {len(to_remove)} 条云端失效记录"
    }


@router.post("/library/cloud/retrieve")
@router.post("/archive/cloud/retrieve")
async def library_cloud_retrieve(request: Request):
    """云端文件取回：从 OpenList 下载文件写回本地磁盘。
    请求体：{"remotePath": "...", "overwrite": false} 或 {"jobId": "..."}
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "message": "请求体格式错误"}, status_code=400)

    remote_path = _norm_remote_path(str(body.get("remotePath") or body.get("cloudPath") or ""))
    job_id = str(body.get("jobId") or body.get("id") or "").strip()
    overwrite = bool(body.get("overwrite"))

    arch_job = None
    if job_id and job_id in _ARCHIVE_JOBS:
        arch_job = _ARCHIVE_JOBS[job_id]
        if not remote_path:
            remote_path = _norm_remote_path(str(arch_job.get("remote_path") or ""))
    elif remote_path:
        for j in _ARCHIVE_JOBS.values():
            if str(j.get("remote_path") or "").lower() == remote_path.lower():
                arch_job = j
                break

    if not remote_path or remote_path == "/":
        return JSONResponse({"ok": False, "message": "请提供合法的云端文件路径 (remotePath)"}, status_code=400)

    filename = posixpath.basename(remote_path)
    if not filename:
        return JSONResponse({"ok": False, "message": "无法从路径中解析出文件名"}, status_code=400)

    target_path = ""
    if arch_job and arch_job.get("local_path"):
        target_path = _resolve_host_local_path(arch_job["local_path"])
    if not target_path:
        target_path = os.path.join(APP_ROOT_DIR, "downloads", filename)

    # 目标落盘路径安全校验：防止覆盖受保护的系统/代码/配置文件或写入软链接
    target_base = os.path.basename(target_path)
    if not target_base or target_base.startswith("."):
        return JSONResponse({"ok": False, "message": f"拒绝取回目标为隐藏/点号文件: {target_base}"}, status_code=400)
    if target_base.endswith((".py", ".json", ".jsonl", ".db", ".sqlite", ".sh", ".env", ".key", ".pem", ".yml", ".yaml", ".md", ".html", ".js", ".css")):
        return JSONResponse({"ok": False, "message": f"受保护的系统/代码/配置文件，拒绝取回写入: {target_base}"}, status_code=400)
    if os.path.islink(target_path):
        return JSONResponse({"ok": False, "message": "取回目标路径为软链接，拒绝写入"}, status_code=400)

    # 防重复机制 1：已有同目标路径或同远端路径的进行中取回任务
    for rj in _RETRIEVE_JOBS.values():
        if rj.get("state") in ("queued", "downloading"):
            if rj.get("remote_path") == remote_path or rj.get("target_path") == target_path:
                return {
                    "ok": True,
                    "alreadyRunning": True,
                    "job": _retrieve_public(rj),
                    "message": f"文件「{filename}」已在取回队列中"
                }

    # 防重复机制 2：本地文件已存在且未指定 overwrite
    if os.path.exists(target_path) and os.path.isfile(target_path) and os.path.getsize(target_path) > 0 and not overwrite:
        return {
            "ok": True,
            "alreadyExists": True,
            "filename": filename,
            "targetPath": target_path,
            "message": f"本地已存在同名文件 ({filename})，无需重复取回"
        }

    job = {
        "id": secrets.token_hex(6),
        "remote_path": remote_path,
        "filename": filename,
        "target_path": target_path,
        "state": "queued",
        "progress": 0,
        "size_bytes": arch_job.get("size_bytes") if arch_job else 0,
        "downloaded_bytes": 0,
        "error": "",
        "created_at": time.time(),
        "updated_at": time.time(),
        "finished_at": 0.0,
    }
    _RETRIEVE_JOBS[job["id"]] = job
    _RETRIEVE_TASKS[job["id"]] = asyncio.create_task(_retrieve_worker(job))

    return {
        "ok": True,
        "job": _retrieve_public(job),
        "message": f"已将「{filename}」加入取回队列"
    }


@router.get("/library/cloud/retrieve/status")
@router.get("/archive/cloud/retrieve/status")
async def library_cloud_retrieve_status():
    """查询最近的取回任务状态。"""
    jobs = sorted(_RETRIEVE_JOBS.values(), key=lambda j: j.get("created_at") or 0.0, reverse=True)
    return {"ok": True, "jobs": [_retrieve_public(j) for j in jobs[:50]]}


@router.post("/library/cloud/retrieve/cancel")
async def library_cloud_retrieve_cancel(request: Request):
    """取消进行中的取回任务。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    jid = str((body or {}).get("jobId") or "").strip()
    job = _RETRIEVE_JOBS.get(jid)
    if not job:
        return JSONResponse({"ok": False, "message": "任务不存在"}, status_code=404)
    if job.get("state") not in ("queued", "downloading"):
        return {"ok": True, "job": _retrieve_public(job)}
    task = _RETRIEVE_TASKS.get(jid)
    if task and not task.done():
        task.cancel()
    job["state"] = "cancelled"
    job["error"] = "用户已取消"
    job["updated_at"] = time.time()
    return {"ok": True, "job": _retrieve_public(job)}


@router.get("/library/cloud/files")
@router.get("/archive/cloud/files")
async def library_cloud_files():
    """返回当前云端归档文件列表（供前端搜索、筛选、动态加载）。"""
    files = await _cloud_archive_rows()
    return {"ok": True, "files": files}

