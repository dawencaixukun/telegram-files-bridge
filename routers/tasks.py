# -*- coding: utf-8 -*-

"""
routers/tasks.py — 表现层路由模块：任务列表、任务详情、链接直投提交与任务重试/取消
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

@router.get("/tasks", response_class=HTMLResponse)
async def tasks(request: Request):
    ctx = await _ctx(request, "tasks", "tasks", extra={
        "tasks": await tasks_all(),
        "chat_sources": await chat_sources(),
    })
    return templates.TemplateResponse("tasks.html", ctx)


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: str):
    tasks_list = await tasks_all()
    # 查无此 id 必须落到「任务不存在」占位，不能静默回退成第一条任务
    task = next((t for t in tasks_list if str(t["id"]) == str(task_id)), None)
    if task is None:
        task = {
            "id": task_id, "time": "—", "source": "—", "msg_id": "—", "filename": "任务不存在",
            "size": "—", "status": "failed", "loaded": "—", "progress": 0,
            "local_path": "—", "cloud_path": "—", "source_url": "",
            "error_msg": "后端未返回该任务", "stages": _stages("failed"),
            "_unique_id": None, "_telegram_id": None,
        }
    ctx = await _ctx(request, "task-detail", "tasks", extra={"task": task})
    return templates.TemplateResponse("task_detail.html", ctx)


@router.post("/tasks", response_class=HTMLResponse)
async def post_task(request: Request):
    """顶栏模态的 hx-post='/tasks' 与 fetch JSON /tasks 转发（提交 links 给后端批量下载）。"""
    ok = False
    message = ""
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.json()
            links = body.get("links", "") if isinstance(body, dict) else ""
        else:
            form = await request.form()
            links = str(form.get("links", ""))
        # links 可能是字符串（每行一条）也可能是数组（fetch JSON 路径）
        if isinstance(links, list):
            raw_lines = [str(l) for l in links]
        else:
            raw_lines = str(links).splitlines()
        lines = [l.strip() for l in raw_lines if l.strip()]
        if lines:
            ok, err = await _resolve_links_to_files(lines)
            if ok:
                message = "已提交 %d 条链接" % ok
            else:
                message = err or "提交失败"
        else:
            message = "没有可识别的链接"
    except Exception as e:  # noqa: BLE001
        log.error("批量下载(任务)提交失败: %s", e)
        message = "提交失败：" + _tg_err_public(e)
    # htmx 请求返回局部表格；fetch JSON 请求返回结果对象
    if "application/json" in request.headers.get("content-type", ""):
        return JSONResponse({"ok": ok, "message": message})
    table_ctx = await _ctx(request, "tasks", "tasks", extra={
        "tasks": await tasks_all(force=True),
        "submit_error": message if not ok else "",
        "submit_ok": ok if ok else 0,
    })
    return templates.TemplateResponse("partials/_tasks_table.html", table_ctx)


@router.get("/submit", response_class=HTMLResponse)
async def submit(request: Request):
    sources = await chat_sources()
    # 注入来源到模板上下文（模板本身是 Alpine 前端校验，这里只是让 /submit 可查）
    ctx = await _ctx(request, "submit", "submit", extra={"chat_sources": sources})
    return templates.TemplateResponse("submit.html", ctx)


@router.post("/submit", response_class=HTMLResponse)
async def submit_post(request: Request):
    """提交下载：解析 links（每行一条 t.me 链接），两跳转成批量下载。"""
    form = await request.form()
    links = str(form.get("links", ""))
    force = bool(str(form.get("force") or "").lower() in ("1", "true", "yes"))
    lines = [l.strip() for l in links.splitlines() if l.strip()]
    error = ""
    ok_count = 0
    if lines:
        high_exceeded, cur_pct, high_threshold = _is_disk_high_watermark_exceeded()
        if high_exceeded:
            await _disk_guard_check()
            high_exceeded, cur_pct, high_threshold = _is_disk_high_watermark_exceeded()

        if high_exceeded:
            low_threshold = float(_ARCHIVE_CONFIG.get("diskLowWatermarkPercent", 75.0) or 75.0)
            enqueued = await _enqueue_waiting_disk_links(lines, cur_pct, high_threshold, low_threshold)
            error = f"磁盘占用率已达 {cur_pct:.1f}%（超过 {high_threshold:.1f}% 警戒线），已将 {enqueued} 条下载安全置入 waiting_disk 挂起队列，降至 {low_threshold:.1f}% 自动恢复"
            _TASKS_CACHE["expire"] = 0.0
            log.warning("磁盘水位熔断拦截 [/submit]：%s", error)
        else:
            ok_count, error = await _resolve_links_to_files(lines, force=force)
            if error:
                log.warning("submit 提交失败: %s", error)
            elif ok_count:
                log.info("已提交下载：成功解析 %s/%s 条链接", ok_count, len(lines))
    else:
        error = "没有输入任何链接"
    table_ctx = await _ctx(request, "tasks", "tasks", extra={
        "tasks": await tasks_all(force=True),
        "submit_error": error,
        "submit_ok": ok_count,
    })
    return templates.TemplateResponse("partials/_tasks_table.html", table_ctx)


@router.post("/task/retry")
async def task_retry(request: Request):
    """重试任务。前端传 {uniqueId, telegramId}；后端契约要 {chatId, messageId, fileId}。

    bridge 负责把 uniqueId 解析成真实字段（任务缓存内查找），前端接口保持不变。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "请求体不是合法 JSON"}
    unique_id = (body or {}).get("uniqueId")
    if not unique_id:
        return {"ok": False, "message": "缺少 uniqueId"}
    rec = _find_task_by_uid(await tasks_all(force=True), unique_id)
    if rec is None:
        return {"ok": False, "message": "任务不存在或已被移除"}
    telegram_id = rec.get("_telegram_id")
    chat_id = rec.get("_chat_id")
    file_id = rec.get("_file_id")
    msg_id = rec.get("msg_id")
    if telegram_id is None or chat_id is None or file_id is None or msg_id is None:
        return {"ok": False, "message": "任务记录缺少 chatId/fileId/messageId，无法重试"}
    try:
        await BACKEND.start_download(telegram_id, {
            "chatId": int(chat_id),
            "messageId": int(msg_id),
            "fileId": int(file_id),
        })
        return {"ok": True, "message": "已重新加入下载队列"}
    except Exception as e:  # noqa: BLE001
        log.error("任务重试失败: %s", e)
        return {"ok": False, "message": "重试失败：" + _tg_err_public(e)}


@router.post("/task/cancel")
async def task_cancel(request: Request):
    """取消任务。前端传 {uniqueId}；后端契约要 {fileId}。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "请求体不是合法 JSON"}
    unique_id = str((body or {}).get("uniqueId") or "")
    if not unique_id:
        return {"ok": False, "message": "缺少 uniqueId"}

    # 挂起队列中的任务直接出队取消
    if unique_id in _WAITING_DISK_TASKS:
        _WAITING_DISK_TASKS.pop(unique_id, None)
        _waiting_disk_save()
        global _TASKS_CACHE
        _TASKS_CACHE["expire"] = 0.0
        _TASKS_CACHE["value"] = None
        return {"ok": True, "message": "已从磁盘挂起队列中取消该任务"}
    for tid, w in list(_WAITING_DISK_TASKS.items()):
        if str(w.get("uniqueId")) == unique_id or str(w.get("id")) == unique_id:
            _WAITING_DISK_TASKS.pop(tid, None)
            _waiting_disk_save()
            _TASKS_CACHE["expire"] = 0.0
            _TASKS_CACHE["value"] = None
            return {"ok": True, "message": "已从磁盘挂起队列中取消该任务"}

    rec = _find_task_by_uid(await tasks_all(force=True), unique_id)
    if rec is None:
        return {"ok": False, "message": "任务不存在或已被移除"}
    telegram_id = rec.get("_telegram_id")
    file_id = rec.get("_file_id")
    if telegram_id is None or file_id is None:
        return {"ok": False, "message": "任务记录缺少 fileId，无法取消"}
    try:
        await BACKEND.cancel_download(telegram_id, {"fileId": int(file_id)})
        return {"ok": True, "message": "已取消任务"}
    except Exception as e:  # noqa: BLE001
        log.error("任务取消失败: %s", e)
        return {"ok": False, "message": "取消失败：" + _tg_err_public(e)}


@router.get("/partials/tasks", response_class=HTMLResponse)
async def partial_tasks(request: Request, status: str = "", source: str = "",
                        date_from: str = "", date_to: str = "",
                        refresh: str = ""):
    force = bool(refresh)
    all_tasks = await tasks_all(force=force)
    filtered = list(all_tasks)
    if status:
        if status in ("active", "unarchived", "hide_archived"):
            filtered = [t for t in filtered if t.get("status") != "archived"]
        else:
            filtered = [t for t in filtered if t.get("status") == status]
    if source:
        filtered = [t for t in filtered if t["source"] == source]
    # 日期过滤（本地时区，按文件日期 _date_ts 比对）
    import datetime as _dt
    try:
        if date_from:
            ts_from = _dt.datetime.combine(_dt.date.fromisoformat(date_from), _dt.time.min).timestamp()
            filtered = [t for t in filtered if t.get("_date_ts") and t["_date_ts"] >= ts_from]
        if date_to:
            ts_to = _dt.datetime.combine(_dt.date.fromisoformat(date_to), _dt.time.max).timestamp()
            filtered = [t for t in filtered if t.get("_date_ts") and t["_date_ts"] <= ts_to]
    except ValueError:
        pass  # 非法日期串直接忽略该过滤条件
    return templates.TemplateResponse("partials/_tasks_table.html", {"request": request, "tasks": filtered})

