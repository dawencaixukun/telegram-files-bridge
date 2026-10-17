# -*- coding: utf-8 -*-
"""
routers/explorer.py — 表现层路由模块：直接对接 OpenList 真实网盘的文件浏览器
========================================================================
支持层级目录、网盘卡片、视频缩略图与在线播放、跨盘复制/移动与进度条、重命名、OpenList 跳转。
"""
import posixpath
import asyncio
from typing import Any, Dict, List
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core import *
from services import *
import services.openlist_service as _openlist_mod
from services.task_service import _ctx
from core.state import _ARCHIVE_CONFIG
from core.templates import templates
from core.logging import log

router = APIRouter()


@router.get("/explorer", response_class=HTMLResponse)
@router.get("/library/browse", response_class=HTMLResponse)
async def explorer_page(request: Request, path: str = "/", refresh: bool = False, q: str = ""):
    """网盘直连浏览核心页面。默认 path="/" 显示全部挂载网盘大卡片，非根路径进入层级目录与文件网格。"""
    norm_path = posixpath.normpath("/" + str(path or "").strip().replace("\\", "/").lstrip("/"))
    if not norm_path or norm_path == ".":
        norm_path = "/"

    openlist_ready = await _openlist_mod._openlist_ready()
    storages = await _openlist_mod.openlist_storages()
    mounts_res = await _openlist_mod.openlist_mounts()
    mount_names = [f"/{m.lstrip('/')}" for m in (mounts_res.get("mounts") or [])]

    # 顶部常用网盘快速切换胶囊（根据当前浏览目录判定 active 状态）
    quick_drives = []
    for s in storages:
        m_path = s.get("mount_path", "")
        is_active = (norm_path == m_path) or (norm_path.startswith(m_path + "/") if m_path != "/" else False)
        quick_drives.append({
            "name": s.get("name", ""),
            "path": m_path,
            "display_name": s.get("display_name", ""),
            "driver": s.get("driver", ""),
            "is_active": is_active,
        })

    is_root = (norm_path == "/")

    if is_root:
        list_data = {
            "ok": True,
            "path": "/",
            "parent_path": "/",
            "breadcrumbs": [{"name": "全部网盘", "path": "/", "is_last": True}],
            "entries": [],
            "dirs_count": len(storages),
            "files_count": 0,
            "total_size": 0,
            "total_size_human": "—",
            "write": True,
            "message": "",
            "openlist_ready": openlist_ready,
        }
    else:
        list_data = await _openlist_mod.openlist_fs_list_all(norm_path, refresh=refresh)

    entries = list_data.get("entries") or []
    if q:
        q_lower = q.lower()
        entries = [e for e in entries if q_lower in e["name"].lower()]

    active_tasks = [t for t in _openlist_mod._CLOUD_TRANSFER_TASKS.values() if t.get("state") == "running"]

    ctx = await _ctx(request, "library-explorer", "library", "explorer")
    ctx.update({
        "current_path": norm_path,
        "is_root": is_root,
        "openlist_ready": openlist_ready,
        "storages": storages,
        "mounts": mount_names,
        "quick_drives": quick_drives,
        "breadcrumbs": list_data.get("breadcrumbs") or [],
        "parent_path": list_data.get("parent_path") or "/",
        "entries": entries,
        "all_entries_count": len(list_data.get("entries") or []),
        "dirs_count": list_data.get("dirs_count", 0),
        "files_count": list_data.get("files_count", 0),
        "total_size_human": list_data.get("total_size_human", "—"),
        "q": q,
        "openlist_public_base": str(_ARCHIVE_CONFIG.get("publicBaseUrl") or ""),
        "openlist_url": _openlist_mod._openlist_direct_url(norm_path),
        "active_tasks": active_tasks,
        "error_message": list_data.get("message", ""),
    })
    return templates.TemplateResponse("explorer.html", ctx)


@router.get("/api/explorer/list")
async def api_explorer_list(request: Request, path: str = "/", refresh: bool = False, q: str = ""):
    """获取指定网盘路径下的文件与子目录列表 JSON。"""
    data = await _openlist_mod.openlist_fs_list_all(path, refresh=refresh)
    if q and data.get("ok"):
        q_lower = q.lower()
        data["entries"] = [e for e in data.get("entries", []) if q_lower in e["name"].lower()]
    return data


@router.post("/api/explorer/rename")
async def api_explorer_rename(request: Request):
    """网盘文件或目录原地重命名。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "请求体不是有效 JSON"}, status_code=400)
    path = str(body.get("path") or "").strip()
    name = str(body.get("name") or "").strip()
    if not path or not name:
        return JSONResponse({"ok": False, "message": "缺少 path 或 name 参数"}, status_code=400)
    res = await _openlist_mod.openlist_fs_rename_entry(path, name)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/explorer/copy")
async def api_explorer_copy(request: Request):
    """跨盘或跨目录复制。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "请求体不是有效 JSON"}, status_code=400)

    src_dir = str(body.get("src_dir") or "").strip().replace("\\", "/")
    dst_dir = str(body.get("dst_dir") or "").strip().replace("\\", "/")
    names = body.get("names")
    if not names:
        single_name = str(body.get("name") or "").strip()
        if single_name:
            names = [single_name]
    remote_path = str(body.get("remotePath") or body.get("src_path") or body.get("cloudPath") or "").strip().replace("\\", "/")

    if remote_path and not src_dir:
        norm_rp = posixpath.normpath("/" + remote_path.lstrip("/"))
        src_dir = posixpath.dirname(norm_rp)
        if not names:
            names = [posixpath.basename(norm_rp)]

    if not src_dir or src_dir == "/":
        src_dir = "/"
    else:
        src_dir = posixpath.normpath("/" + src_dir.lstrip("/"))

    if not dst_dir:
        return JSONResponse({"ok": False, "message": "缺少目标目录 dst_dir"}, status_code=400)
    dst_dir = posixpath.normpath("/" + dst_dir.lstrip("/"))

    if not names or not isinstance(names, list):
        return JSONResponse({"ok": False, "message": "缺少要复制的文件名 names 列表"}, status_code=400)

    clean_names = [posixpath.basename(str(n).strip()) for n in names if str(n).strip()]
    if not clean_names:
        return JSONResponse({"ok": False, "message": "缺少有效的文件名"}, status_code=400)

    if src_dir == dst_dir:
        return JSONResponse({"ok": False, "message": "源目录与目标目录相同，无需复制"}, status_code=400)

    res = await _openlist_mod._openlist_fs_copy(src_dir, dst_dir, clean_names)
    status_code = 200 if res.get("ok") else (502 if "就绪" in str(res.get("message")) else 400)
    return JSONResponse(res, status_code=status_code)


@router.post("/api/explorer/move")
async def api_explorer_move(request: Request):
    """跨盘或跨目录移动。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "请求体不是有效 JSON"}, status_code=400)

    src_dir = str(body.get("src_dir") or "").strip().replace("\\", "/")
    dst_dir = str(body.get("dst_dir") or "").strip().replace("\\", "/")
    names = body.get("names")
    if not names:
        single_name = str(body.get("name") or "").strip()
        if single_name:
            names = [single_name]
    remote_path = str(body.get("remotePath") or body.get("src_path") or body.get("cloudPath") or "").strip().replace("\\", "/")

    if remote_path and not src_dir:
        norm_rp = posixpath.normpath("/" + remote_path.lstrip("/"))
        src_dir = posixpath.dirname(norm_rp)
        if not names:
            names = [posixpath.basename(norm_rp)]

    if not src_dir or src_dir == "/":
        src_dir = "/"
    else:
        src_dir = posixpath.normpath("/" + src_dir.lstrip("/"))

    if not dst_dir:
        return JSONResponse({"ok": False, "message": "缺少目标目录 dst_dir"}, status_code=400)
    dst_dir = posixpath.normpath("/" + dst_dir.lstrip("/"))

    if not names or not isinstance(names, list):
        return JSONResponse({"ok": False, "message": "缺少要移动的文件名 names 列表"}, status_code=400)

    clean_names = [posixpath.basename(str(n).strip()) for n in names if str(n).strip()]
    if not clean_names:
        return JSONResponse({"ok": False, "message": "缺少有效的文件名"}, status_code=400)

    if src_dir == dst_dir:
        return JSONResponse({"ok": False, "message": "源目录与目标目录相同，无需移动"}, status_code=400)

    res = await _openlist_mod._openlist_fs_move(src_dir, dst_dir, clean_names)
    status_code = 200 if res.get("ok") else (502 if "就绪" in str(res.get("message")) else 400)
    return JSONResponse(res, status_code=status_code)


@router.get("/api/explorer/stream-url")
async def api_explorer_stream_url(request: Request, path: str = ""):
    """获取视频串流直链（支持 PotPlayer / VLC / IINA / Web 播放器）。"""
    res = await _openlist_mod.openlist_stream_url(path)
    return res


@router.get("/api/explorer/download")
async def api_explorer_download(request: Request, path: str = ""):
    """直链下载端点（重定向至真实 CDN 下载地址）。"""
    st = await _openlist_mod.openlist_stream_url(path)
    if isinstance(st, dict) and st.get("ok") and st.get("url"):
        return RedirectResponse(st["url"], status_code=302)
    return JSONResponse({"ok": False, "message": "无法获取文件下载直链"}, status_code=400)


@router.get("/api/explorer/tasks")
async def api_explorer_tasks():
    """实时跨盘传输任务进度。"""
    return await _openlist_mod.openlist_get_cloud_tasks()
