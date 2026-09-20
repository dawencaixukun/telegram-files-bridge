# -*- coding: utf-8 -*-

"""
routers/extension.py — 浏览器插件接入域路由（第 10 大领域）
=============================================================
对外提供 Chrome 插件调用的 m3u8 下载 API：

  * /api/ext/m3u8/*      —— 插件端点。豁免 Portal Cookie 门禁与 CSRF（插件无
    Cookie），改为自校验请求头 `X-Ext-Token`（常量时间比较）+ 每 IP 限流。
  * /api/ext-token/*     —— Token 管理端点。**留在门户门禁之后**（需登录 + CSRF），
    供设置页展示与重置。

安全要点：
  * Token 校验失败统一返回 401，不区分「未携带」与「错误」，不泄漏 Token 长度。
  * /api/ext/m3u8/ 的前缀在 core/config.PUBLIC_PREFIXES 中豁免；本模块必须自行
    完成鉴权，绝不能依赖上层门禁。
  * 目标 URL 的 SSRF 校验在 services/m3u8_service 内（DNS 解析级）执行。
"""
from core import *
from services import *
import time
from typing import Any, Dict, List
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


router = APIRouter()

# 插件端点请求头名（Token 传递方式）
_EXT_TOKEN_HEADER = "x-ext-token"

# 插件端点限流：60s 窗口 120 次/IP（下载提交相对重，但仍允许批量提交）
_EXT_RATE_WINDOW = 60.0
_EXT_RATE_LIMIT = 120
_EXT_REQUESTS: Dict[str, List[float]] = {}
_EXT_REQ_MAX = 1000


def _ext_rate_limited(ip: str) -> bool:
    """插件端点每 IP 限流（滑动窗口，字典有界）。"""
    now = time.time()
    window = [t for t in _EXT_REQUESTS.get(ip, []) if now - t < _EXT_RATE_WINDOW]
    _EXT_REQUESTS[ip] = window
    if len(_EXT_REQUESTS) > _EXT_REQ_MAX:
        stale = [k for k, ts in _EXT_REQUESTS.items()
                 if not any(now - t < _EXT_RATE_WINDOW for t in ts)]
        for k in stale:
            _EXT_REQUESTS.pop(k, None)
    if len(window) >= _EXT_RATE_LIMIT:
        return True
    _EXT_REQUESTS.setdefault(ip, []).append(now)
    return False


def _ext_guard(request: Request):
    """插件端点统一门禁：返回 None 表示放行，否则返回可直接返回的 JSONResponse。"""
    ip = _client_ip(request)
    if _ext_rate_limited(ip):
        return JSONResponse({"ok": False, "code": "RATE_LIMITED",
                             "message": "请求过于频繁，请稍后再试"}, status_code=429)
    token = request.headers.get(_EXT_TOKEN_HEADER, "")
    if not ext_token_verify(token):
        log.warning("插件端点鉴权失败（IP %s，路径 %s）", ip, request.url.path)
        return JSONResponse({"ok": False, "code": "UNAUTHORIZED",
                             "message": "插件 Token 无效，请在管理台「设置 → 浏览器插件接入」重新获取"},
                            status_code=401)
    return None


async def _ext_json(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _headers_of(body: Dict[str, Any]) -> Dict[str, str]:
    h = body.get("headers")
    return h if isinstance(h, dict) else {}


# ---------------------------------------------------------------------
# 插件端点：/api/ext/m3u8/*
# ---------------------------------------------------------------------

@router.post("/api/ext/m3u8/resolve")
async def ext_m3u8_resolve(request: Request):
    """探测 m3u8：master 返回清晰度列表；media 返回分片数/时长/加密信息。

    请求体：{"url": "...", "headers": {"referer": "...", "user_agent": "..."}}
    """
    blocked = _ext_guard(request)
    if blocked is not None:
        return blocked
    body = await _ext_json(request)
    url = str(body.get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "message": "缺少 url 参数"}, status_code=400)
    try:
        info = await m3u8_resolve(url, _headers_of(body))
        return {"ok": True, **info}
    except M3u8Error as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        log.warning("m3u8 resolve 异常: %r", e)
        return JSONResponse({"ok": False, "message": "探测失败：" + e.__class__.__name__},
                            status_code=502)


@router.post("/api/ext/m3u8/submit")
async def ext_m3u8_submit(request: Request):
    """提交 m3u8 下载任务。

    请求体：{"url": "...", "title": "...", "headers": {...},
             "remote_dir": "/onedrive/剧集"}
    返回：{"ok": true, "task_id": "...", "duplicate": false, "remote_dir": "..."}

    remote_dir 可选：插件指定成品归档到 OpenList 的哪个目录。留空则用设置页
    的默认归档目录；服务端会用 _archive_norm_dir 归一化校验，非法直接 400。
    """
    blocked = _ext_guard(request)
    if blocked is not None:
        return blocked
    body = await _ext_json(request)
    url = str(body.get("url") or "").strip()
    title = str(body.get("title") or "").strip()
    remote_dir = str(body.get("remote_dir") or body.get("archive_dir") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "message": "缺少 url 参数"}, status_code=400)
    try:
        res = await m3u8_submit(url, title, _headers_of(body), remote_dir)
    except M3u8Error as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=400)
    task = res["task"]
    return {
        "ok": True,
        "task_id": task["id"],
        "duplicate": bool(res.get("duplicate")),
        "state": task.get("state"),
        "remote_dir": task.get("remote_dir") or "",
        "message": "该链接已在下载队列中" if res.get("duplicate") else "已加入下载队列",
    }


@router.get("/api/ext/m3u8/tasks")
async def ext_m3u8_tasks(request: Request, limit: int = 50):
    """任务列表（插件与前端共用）：进行中优先，其余按时间倒序。"""
    blocked = _ext_guard(request)
    if blocked is not None:
        return blocked
    limit = max(1, min(200, int(limit or 50)))
    items = sorted(_M3U8_TASKS.values(), key=lambda t: t.get("created_at") or 0.0, reverse=True)
    active = [t for t in items if t.get("state") in ("queued", "running")]
    inactive = [t for t in items if t.get("state") not in ("queued", "running")]
    shown = (active + inactive[:max(0, limit - len(active))])[:limit]
    return {
        "ok": True,
        "active": len(active),
        "tasks": [_m3u8_public(t) for t in shown],
    }


@router.post("/api/ext/m3u8/cancel")
async def ext_m3u8_cancel(request: Request):
    blocked = _ext_guard(request)
    if blocked is not None:
        return blocked
    body = await _ext_json(request)
    tid = str(body.get("task_id") or body.get("id") or "").strip()
    if not tid:
        return JSONResponse({"ok": False, "message": "缺少 task_id"}, status_code=400)
    ok = await m3u8_cancel(tid)
    return ({"ok": True, "message": "已取消"} if ok else
            JSONResponse({"ok": False, "message": "任务不存在或无法取消"}, status_code=404))


@router.post("/api/ext/m3u8/retry")
async def ext_m3u8_retry(request: Request):
    blocked = _ext_guard(request)
    if blocked is not None:
        return blocked
    body = await _ext_json(request)
    tid = str(body.get("task_id") or body.get("id") or "").strip()
    if not tid:
        return JSONResponse({"ok": False, "message": "缺少 task_id"}, status_code=400)
    try:
        ok = await m3u8_retry(tid)
    except M3u8Error as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=429)
    return ({"ok": True, "message": "已重新加入下载队列"} if ok else
            JSONResponse({"ok": False, "message": "任务不存在或状态不允许重试"}, status_code=404))


@router.post("/api/ext/m3u8/delete")
async def ext_m3u8_delete(request: Request):
    """删除终态任务并回收其分片/本地成品（进行中任务需先取消）。"""
    blocked = _ext_guard(request)
    if blocked is not None:
        return blocked
    body = await _ext_json(request)
    tid = str(body.get("task_id") or body.get("id") or "").strip()
    if not tid:
        return JSONResponse({"ok": False, "message": "缺少 task_id"}, status_code=400)
    res = await m3u8_delete(tid)
    return JSONResponse(res, status_code=200 if res.get("ok") else 404)


@router.get("/api/ext/m3u8/dirs")
async def ext_m3u8_dirs(request: Request, path: str = "/"):
    """归档目录浏览（插件端）：复用 OpenList 目录列表，供插件选择归档位置。

    与门户端 /openlist/dirs 同一实现，但走 X-Ext-Token 鉴权，因为插件拿不到
    网页 Cookie。
    """
    blocked = _ext_guard(request)
    if blocked is not None:
        return blocked
    return await openlist_dirs(path)



# ---------------------------------------------------------------------
# 门户鉴权版任务端点：/api/m3u8/*（网页 UI 用，走 Cookie + CSRF）
# ---------------------------------------------------------------------
# 为什么需要这一组：/api/ext/m3u8/* 只认 X-Ext-Token，浏览器页面拿不到
# （Token 不该下发到页面 JS）。网页端要展示 m3u8 下载进度就必须有一套
# 走 Portal Cookie 门禁的同源端点。这些路径不以 /api/ext/m3u8/ 开头，
# 因此仍受 portal_auth_gate 保护（未登录 401 JSON、POST 需 CSRF）。

@router.get("/api/m3u8/tasks")
async def m3u8_tasks_portal(limit: int = 50):
    """网页端任务列表（与插件端同一份数据、同一视图）。"""
    limit = max(1, min(200, int(limit or 50)))
    items = sorted(_M3U8_TASKS.values(), key=lambda t: t.get("created_at") or 0.0, reverse=True)
    active = [t for t in items if t.get("state") in ("queued", "running")]
    inactive = [t for t in items if t.get("state") not in ("queued", "running")]
    shown = (active + inactive[:max(0, limit - len(active))])[:limit]
    return {
        "ok": True,
        "active": len(active),
        "tasks": [_m3u8_public(t) for t in shown],
    }


@router.post("/api/m3u8/cancel")
async def m3u8_cancel_portal(request: Request):
    body = await _ext_json(request)
    tid = str(body.get("task_id") or body.get("id") or "").strip()
    if not tid:
        return JSONResponse({"ok": False, "message": "缺少 task_id"}, status_code=400)
    ok = await m3u8_cancel(tid)
    return ({"ok": True, "message": "已取消"} if ok else
            JSONResponse({"ok": False, "message": "任务不存在或无法取消"}, status_code=404))


@router.post("/api/m3u8/retry")
async def m3u8_retry_portal(request: Request):
    body = await _ext_json(request)
    tid = str(body.get("task_id") or body.get("id") or "").strip()
    if not tid:
        return JSONResponse({"ok": False, "message": "缺少 task_id"}, status_code=400)
    try:
        ok = await m3u8_retry(tid)
    except M3u8Error as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=429)
    return ({"ok": True, "message": "已重新加入下载队列"} if ok else
            JSONResponse({"ok": False, "message": "任务不存在或状态不允许重试"}, status_code=404))

@router.post("/api/m3u8/delete")
async def m3u8_delete_portal(request: Request):
    """网页端删除终态任务并回收其分片/本地成品。"""
    body = await _ext_json(request)
    tid = str(body.get("task_id") or body.get("id") or "").strip()
    if not tid:
        return JSONResponse({"ok": False, "message": "缺少 task_id"}, status_code=400)
    res = await m3u8_delete(tid)
    return JSONResponse(res, status_code=200 if res.get("ok") else 404)

# ---------------------------------------------------------------------
# Token 管理端点：/api/ext-token/*（门户门禁之后，需登录 + CSRF）
# ---------------------------------------------------------------------

@router.get("/api/ext-token")
async def ext_token_show():
    """读取当前插件 Token（设置页展示用）。"""
    return {"ok": True, "token": ext_token_get()}


@router.post("/api/ext-token/reset")
async def ext_token_reset_route():
    """重置插件 Token：旧 Token 立即失效，需在插件里重新填写。"""
    tok = ext_token_reset()
    log.info("浏览器插件 Token 已重置（旧 Token 已失效）")
    return {"ok": True, "token": tok, "message": "Token 已重置，请更新插件配置"}
