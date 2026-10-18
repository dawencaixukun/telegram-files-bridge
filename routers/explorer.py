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


def _current_drive_label(norm_path: str, storages: List[Dict[str, Any]]) -> str:
    """当前路径所属网盘的展示名（工具条下拉按钮回显用）。

    根目录返回空串 → 前端显示「全部网盘」。路径第二层以后（/onedrive/yello）
    同样回显 OneDrive，让用户始终知道自己在哪个盘里。
    """
    if not norm_path or norm_path == "/":
        return ""
    top = norm_path.strip("/").split("/")[0]
    for s in storages or []:
        if str(s.get("name") or "") == top or str(s.get("mount_path") or "").strip("/") == top:
            return str(s.get("display_name") or s.get("name") or top)
    return top

# 根目录网盘卡片统计缓存：{mount_path: (timestamp, stats)}，30s 内复用，避免每次进页面打 N 次 OpenList
_DRIVE_STATS_CACHE: Dict[str, Any] = {}
_DRIVE_STATS_TTL = 30.0


async def _drive_stats(mount_path: str):
    """统计单个网盘挂载点的一级项目数（失败返回 None，由上层渲染成「—」）。"""
    try:
        data = await _openlist_mod.openlist_fs_list_all(mount_path)
        if not data or not data.get("ok"):
            return None
        entries = data.get("entries") or []
        dirs = data.get("dirs_count")
        files = data.get("files_count")
        if dirs is None:
            dirs = sum(1 for e in entries if e.get("is_dir"))
        if files is None:
            files = sum(1 for e in entries if not e.get("is_dir"))
        size = data.get("total_size_human")
        if not size:
            total = data.get("total_size") or sum(
                (e.get("size") or 0) for e in entries if not e.get("is_dir")
            )
            size = _human_size(total)
        return {"dirs": int(dirs or 0), "files": int(files or 0), "size": size or "—"}
    except Exception as exc:  # 单个网盘列不出来不应影响整页
        log.warning("explorer 网盘统计失败 %s: %s", mount_path, exc)
        return None


def _human_size(num: float) -> str:
    try:
        n = float(num or 0)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return "—"


async def _attach_drive_stats(storages: List[Dict[str, Any]]) -> None:
    """并发补齐根目录每张网盘卡片的 stats（带 30s 内存缓存），原地写入 dict。"""
    now = asyncio.get_event_loop().time()
    pending = []
    for s in storages:
        mp = s.get("mount_path") or ""
        hit = _DRIVE_STATS_CACHE.get(mp)
        if hit and (now - hit[0]) < _DRIVE_STATS_TTL:
            s["stats"] = hit[1]
        else:
            s["stats"] = None
            pending.append(s)
    if not pending:
        return
    results = await asyncio.gather(
        *[_drive_stats(s.get("mount_path") or "") for s in pending],
        return_exceptions=True,
    )
    for s, res in zip(pending, results):
        stats = None if isinstance(res, BaseException) else res
        s["stats"] = stats
        _DRIVE_STATS_CACHE[s.get("mount_path") or ""] = (now, stats)


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

    # 根目录：并发补齐每个网盘的一级项目统计（30s 缓存，单个失败只显示「—」）
    if is_root and storages:
        await _attach_drive_stats(storages)

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
        # 工具条「全部网盘」下拉按钮的回显名：取当前路径的第一段去匹配挂载网盘
        "current_drive_label": _current_drive_label(norm_path, storages),
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


@router.get("/api/explorer/search")
async def api_explorer_search(request: Request, q: str = ""):
    """「在全部网盘查询」：跨网盘按文件名搜索（根页面搜索组件专用）。

    优先 OpenList 原生 /api/fs/search 全库索引；不可用（未配置索引/未登录）时
    服务层自动降级为遍历各挂载网盘一级目录过滤文件名。
    """
    data = await _openlist_mod.openlist_search_all(q, limit=40)
    return JSONResponse({
        "ok": bool(data.get("ok")),
        "query": str(data.get("query") or q or ""),
        "results": data.get("results") or [],
        "message": str(data.get("message") or ""),
    })


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
