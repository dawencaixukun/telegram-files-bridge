# -*- coding: utf-8 -*-

"""
routers/browse.py — 表现层路由模块：Telegram 频道资源浏览与文件批量下载
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

@router.get("/browse", response_class=HTMLResponse)
async def browse_page(request: Request, tg: str = "", chat: str = "", type: str = "document", hide_archived: str = ""):
    """聊天浏览页：选聊天 → 浏览文件 → 勾选下载（收藏 Saved Messages 置顶）。"""
    tree = await _browse_tree()
    if (not tg or not chat) and tree:
        tg = str(tree[0]["telegramId"])
        chats0 = tree[0]["chats"]
        if chats0:
            chat = str(chats0[0]["chatId"])
    if type not in {t for t, _ in _BROWSE_TYPES}:
        type = "document"
    cur_title = "—"
    if tree and tg and chat:
        for acc in tree:
            if str(acc.get("telegramId")) == str(tg):
                for c in acc.get("chats", []):
                    if str(c.get("chatId")) == str(chat):
                        cur_title = str(c.get("title") or "聊天")
                        break
                break
    hide_arch = bool(hide_archived and hide_archived not in ("0", "false", "False"))
    rows, count, cursor, dedup = await _browse_files(tg, chat, type, hide_archived=hide_arch)
    ctx = await _ctx(request, "browse", "browse", extra={
        "tree": tree,
        "sel_tg": str(tg or ""),
        "sel_chat": str(chat or ""),
        "sel_type": type,
        "cur_title": cur_title,
        "browse_rows": rows,
        "browse_count": count,
        "browse_cursor": cursor,
        "browse_collapsed": dedup["collapsed"],
        "browse_loaded": dedup["loaded"],
        "browse_types": _BROWSE_TYPES,
        "hide_archived": hide_arch,
        # OpenList 外部访问域名：卡片上的「OpenList」直达按钮要用它拼公网地址
        # （内网 127.0.0.1 在用户浏览器里打不开）。
        "openlist_public_base": str(_ARCHIVE_CONFIG.get("publicBaseUrl") or ""),
        # 侧栏黑名单：模板据此显示「已隐藏 N 个会话 / 全部显示」提示条
        "browse_pins": _browse_pins_all(),
    })
    return templates.TemplateResponse("browse.html", ctx)


@router.get("/browse/account-tree", response_class=JSONResponse)
async def browse_account_tree(tg: str = ""):
    """返回**完整**会话树（不做黑名单隐藏），供浏览页「管理会话」面板列出候选。

    必须用 full=True：面板要能列出全部会话。hidden 字段 = 当前被隐藏的 chatId 列表
    （同时保留 pins 旧字段名兼容）。
    """
    tree = await _browse_tree(full=True)
    hidden = _browse_pins_all()
    return JSONResponse({"ok": True, "tree": tree, "hidden": hidden, "pins": hidden})


@router.post("/browse/pins")
async def browse_set_pins(request: Request):
    """设置单个会话的显示态（黑名单模型：关 = 隐藏）。

    体：{"tg": "<telegramId>", "chat": "<chatId>", "pinned": true|false}
    pinned=True  => 在侧栏显示；pinned=False => 从侧栏隐藏。
    返回的 shown 字段是该会话当前是否在侧栏显示。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    chat = str(body.get("chat") or "").strip()
    if not chat:
        return JSONResponse({"ok": False, "message": "缺少会话 ID"}, status_code=400)
    pinned = bool(body.get("pinned"))
    now_shown = _browse_pin_apply(chat, pinned)
    return JSONResponse({"ok": True, "chat": chat, "pinned": pinned,
                         "shown": now_shown, "hidden": _browse_pins_all()})


@router.post("/browse/pins/bulk")
async def browse_set_pins_bulk(request: Request):
    """批量设置显示态。体：{"chats": [...], "pinned": true|false}

    pinned=True => 显示；pinned=False => 隐藏（黑名单）。
    一次请求、一次落盘：前端「全选/取消全选」一次提交几十上百个 chatId。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    chats = body.get("chats")
    if not isinstance(chats, list):
        return JSONResponse({"ok": False, "message": "chats 必须是数组"}, status_code=400)
    changed = _browse_pins_apply_many(chats, bool(body.get("pinned")))
    return JSONResponse({"ok": True, "changed": changed, "pins": _browse_pins_all()})


@router.post("/browse/pins/clear")
async def browse_clear_pins(request: Request):
    """一键清空黑名单：全部隐藏的会话立即恢复显示。

    能一次做完就一次做完 —— 逐个 POST /browse/pins 取消会有 N 次磁盘写入与 N 次
    网络往返，且中途失败会留下「关了一半」的状态。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    removed = _browse_pins_clear()
    return JSONResponse({"ok": True, "removed": removed, "pins": _browse_pins_all()})


@router.get("/partials/browse-files", response_class=HTMLResponse)
async def partial_browse_files(request: Request, tg: str = "", chat: str = "",
                               type: str = "document", cursor: str = "0", hide_archived: str = ""):
    """文件列表 htmx 片段（含"加载更多"游标行）。/partials/* 已由门禁返回 401 JSON。"""
    try:
        cur = int(cursor or 0)
    except ValueError:
        cur = 0
    if type not in {t for t, _ in _BROWSE_TYPES}:
        type = "document"
    hide_arch = bool(hide_archived and hide_archived not in ("0", "false", "False"))
    rows, count, next_cursor, dedup = await _browse_files(tg, chat, type, cur, hide_archived=hide_arch)
    return templates.TemplateResponse("partials/_browse_files.html", {
        "request": request,
        "browse_rows": rows,
        "browse_count": count,
        "browse_cursor": next_cursor,
        "browse_collapsed": dedup["collapsed"],
        "browse_loaded": dedup["loaded"],
        "sel_tg": str(tg or ""),
        "sel_chat": str(chat or ""),
        "sel_type": type,
        "hide_archived": hide_arch,
    })


@router.post("/browse/download")
async def browse_download(request: Request):
    """浏览页勾选下载：{files:[{telegramId,chatId,messageId,fileId}]} → 批量下载。
    与 /submit 的两跳链接解析不同，浏览页手里已是完整 FileRecord，
    直接走 /files/start-download-multiple（后端契约见 start_download_multiple）。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    # 请求体可能是合法 JSON 但不是对象（列表/字符串/数字）。下方多处按 dict 取值，
    # 不归一化就会 AttributeError → HTTP 500。归一化为 dict 后再处理。
    if not isinstance(body, dict):
        body = {}
    raw = body.get("files")
    seen = set()
    payload_files: List[Dict[str, Any]] = []
    skipped_dup = 0
    skipped_local = 0
    last_cloud_res = None
    last_local_res = None
    force = bool(body.get("force"))

    tasks_list = None
    if not force:
        try:
            tasks_list = await tasks_all()
        except Exception:
            tasks_list = []

    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict):
            continue
        try:
            item = {
                "telegramId": int(f.get("telegramId")),
                "chatId": int(f.get("chatId")),
                "messageId": int(f.get("messageId")),
                "fileId": int(f.get("fileId")),
            }
        except (TypeError, ValueError):
            continue
        uid = str(f.get("uniqueId") or "")
        fn = str(f.get("filename") or f.get("name") or "")
        sz = f.get("size")

        # 查重指纹与智能关联（非 force 时）：已在云端网盘或本地在存均进行智能拦截与提示
        if not force:
            d_res = await _check_file_dedup(uid, fn, sz, tasks_list=tasks_list)
            if d_res.get("duplicate"):
                if d_res.get("duplicateType") == "cloud":
                    skipped_dup += 1
                    last_cloud_res = d_res
                    continue
                elif d_res.get("duplicateType") == "local":
                    skipped_local += 1
                    last_local_res = d_res
                    continue

        key = (item["telegramId"], item["chatId"], uid) if uid else \
              (item["telegramId"], item["chatId"], item["messageId"], item["fileId"])
        if key in seen:
            skipped_dup += 1
            continue
        seen.add(key)
        payload_files.append(item)

    if not payload_files:
        if last_cloud_res:
            arch_date = last_cloud_res["asset"]["archivedDate"]
            drive = last_cloud_res["asset"]["drive"]
            rp = last_cloud_res["asset"]["cloudPath"]
            msg = f"云端已于 {arch_date} 归档至 {drive} 路径：{rp}"
            return JSONResponse({
                "ok": False,
                "code": "DUPLICATE_ASSET",
                "duplicateType": "cloud",
                "message": msg,
                "skippedDup": skipped_dup,
                "asset": last_cloud_res["asset"],
                "actions": last_cloud_res["actions"]
            })
        elif last_local_res:
            msg = "本地在存：该文件已在本地中转区存在，无需重复下载"
            return JSONResponse({
                "ok": False,
                "code": "DUPLICATE_ASSET",
                "duplicateType": "local",
                "message": msg,
                "skippedDup": skipped_local,
                "asset": last_local_res["asset"],
                "actions": last_local_res["actions"]
            })
        msg = f"选中的 {skipped_dup} 个文件均已在网盘归档中，已自动跳过重复下载" if skipped_dup else "没有选中可下载的文件"
        return JSONResponse({"ok": False, "message": msg, "skippedDup": skipped_dup})

    # 磁盘高低水位熔断保护：85% 熔断挂起 / 75% 唤醒
    high_exceeded, cur_pct, high_threshold = _is_disk_high_watermark_exceeded()
    if high_exceeded:
        await _disk_guard_check()
        high_exceeded, cur_pct, high_threshold = _is_disk_high_watermark_exceeded()

    if high_exceeded:
        low_threshold = float(_ARCHIVE_CONFIG.get("diskLowWatermarkPercent", 75.0) or 75.0)
        enqueued_count = _enqueue_waiting_disk_files(raw, payload_files, cur_pct, high_threshold, low_threshold)
        global _TASKS_CACHE
        _TASKS_CACHE["expire"] = 0.0
        _TASKS_CACHE["value"] = None
        msg = f"本地磁盘占用率已达 {cur_pct:.1f}%（超过 {high_threshold:.1f}% 警戒线），新提交的 {enqueued_count} 个任务已安全置入 waiting_disk 挂起队列，等待磁盘回落至 {low_threshold:.1f}% 以下自动恢复调度"
        log.warning("磁盘水位熔断拦截 [/browse/download]：%s", msg)
        return JSONResponse({
            "ok": False,
            "code": "DISK_WATERMARK_EXCEEDED",
            "state": "waiting_disk",
            "count": enqueued_count,
            "message": msg,
            "skippedDuplicates": skipped_dup
        })

    try:
        await BACKEND.start_download_multiple({"files": payload_files})
        _TASKS_CACHE["expire"] = 0.0
        _TASKS_CACHE["value"] = None  # 任务页/角标立即可见新任务
        # 关键：/files 在 BackendClient._cached 还有独立 TTL 缓存，不清的话
        # 任务重建拿到的是提交前的旧数据 —— 新任务既进不了任务列表，也触发不了告警
        BACKEND._cache.clear()
        return JSONResponse({"ok": True, "count": len(payload_files), "skippedDuplicates": skipped_dup})
    except Exception as e:  # noqa: BLE001
        log.error("browse 批量下载失败: %s", e)
        return JSONResponse({"ok": False, "message": _tg_err_public(e)})

