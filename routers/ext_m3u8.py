# -*- coding: utf-8 -*-
"""
routers/ext_m3u8.py — 表现层路由模块：OmniSnag 扩展端 M3U8 远端下载契约
========================================================================
实现 5 大标准契约端点：
  POST /api/ext/m3u8/resolve  — 探测播放列表
  POST /api/ext/m3u8/submit   — 提交下载任务
  GET  /api/ext/m3u8/tasks    — 获取任务列表
  POST /api/ext/m3u8/cancel   — 取消下载任务
  POST /api/ext/m3u8/retry    — 重试失败/取消任务
"""

from typing import Any, Dict, Optional
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core import *
from services import (
    verify_ext_token,
    check_ext_rate_limit,
    is_ssrf_safe_url,
    resolve_m3u8,
    submit_m3u8_task,
    get_m3u8_tasks,
    cancel_m3u8_task,
    retry_m3u8_task,
    _M3U8_TASKS,
    _M3U8_MAX_QUEUE,
)

router = APIRouter()


def _authenticate_ext_request(request: Request) -> Optional[JSONResponse]:
    token = request.headers.get("X-Ext-Token", "").strip()
    if not token or not verify_ext_token(token):
        return JSONResponse(
            {
                "ok": False,
                "message": "Token 无效或缺失。请到「设置 → VPS 远程下载推送」核对服务端 .ext_token 里的值。",
            },
            status_code=401,
        )
    if not check_ext_rate_limit(token):
        return JSONResponse(
            {
                "ok": False,
                "message": "触发服务端限流（60 秒 / 120 次），请稍后再试。",
            },
            status_code=429,
        )
    return None


@router.post("/api/ext/m3u8/resolve")
async def ext_m3u8_resolve(request: Request):
    """探测播放列表，返回 master 变体列表或 media 分片摘要信息。"""
    auth_err = _authenticate_ext_request(request)
    if auth_err:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "message": "请求体格式错误，必须为 JSON 对象"}, status_code=400)

    url = str(body.get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "message": "缺少 m3u8 地址"}, status_code=400)

    headers = body.get("headers")
    ok, status, data = await resolve_m3u8(url, headers)
    return JSONResponse(data, status_code=status)


@router.post("/api/ext/m3u8/submit")
async def ext_m3u8_submit(request: Request):
    """提交 M3U8 下载任务（自动去重）。"""
    auth_err = _authenticate_ext_request(request)
    if auth_err:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "message": "请求体格式错误，必须为 JSON 对象"}, status_code=400)

    url = str(body.get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "message": "缺少 m3u8 地址"}, status_code=400)

    safe, reason = is_ssrf_safe_url(url)
    if not safe:
        return JSONResponse({"ok": False, "message": reason}, status_code=400)

    active_count = sum(1 for t in _M3U8_TASKS.values() if t.get("state") in ("queued", "running"))
    if active_count >= _M3U8_MAX_QUEUE:
        return JSONResponse({"ok": False, "message": "排队任务已达上限，请稍后再试"}, status_code=429)

    title = str(body.get("title") or "").strip()
    headers = body.get("headers")
    task, duplicate = await submit_m3u8_task(url, headers=headers, title=title)

    return JSONResponse({
        "ok": True,
        "task_id": task["id"],
        "duplicate": duplicate,
        "state": task["state"],
        "message": "服务端已有相同任务（未重复下载）" if duplicate else "已成功提交下载任务",
    })


@router.get("/api/ext/m3u8/tasks")
async def ext_m3u8_tasks(request: Request):
    """获取下载任务列表（进行中优先）。"""
    auth_err = _authenticate_ext_request(request)
    if auth_err:
        return auth_err

    try:
        limit = int(request.query_params.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50
    limit = min(200, max(1, limit))

    res = get_m3u8_tasks(limit=limit)
    return JSONResponse(res)


@router.post("/api/ext/m3u8/cancel")
async def ext_m3u8_cancel(request: Request):
    """取消下载任务（中断网络流与 ffmpeg 子进程）。"""
    auth_err = _authenticate_ext_request(request)
    if auth_err:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "message": "请求体格式错误"}, status_code=400)

    task_id = str(body.get("task_id") or "").strip()
    if not task_id:
        return JSONResponse({"ok": False, "message": "缺少 task_id 参数"}, status_code=400)

    success = await cancel_m3u8_task(task_id)
    if not success:
        return JSONResponse({"ok": False, "message": "任务不存在，或当前状态不允许取消"}, status_code=404)

    return JSONResponse({"ok": True, "message": "任务已成功取消"})


@router.post("/api/ext/m3u8/retry")
async def ext_m3u8_retry(request: Request):
    """重试已失败或已取消的任务（支持断点续传）。"""
    auth_err = _authenticate_ext_request(request)
    if auth_err:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "message": "请求体格式错误"}, status_code=400)

    task_id = str(body.get("task_id") or "").strip()
    if not task_id:
        return JSONResponse({"ok": False, "message": "缺少 task_id 参数"}, status_code=400)

    success = await retry_m3u8_task(task_id)
    if not success:
        return JSONResponse({"ok": False, "message": "任务不存在，或当前状态不允许重试"}, status_code=404)

    return JSONResponse({
        "ok": True,
        "task_id": task_id,
        "state": "queued",
        "message": "任务已重新入队",
    })
