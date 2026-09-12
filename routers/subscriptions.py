# -*- coding: utf-8 -*-

"""
routers/subscriptions.py — 表现层路由模块：自动化订阅规则管理、模板预览与巡检执行
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

@router.get("/api/subscriptions")
async def api_subscriptions_list():
    return {"ok": True, "interval": int(_AUTO_ARCHIVE_INTERVAL),
            "maxAttempts": _AUTO_ARCHIVE_MAX_ATTEMPTS,
            "rules": [_sub_public(r) for r in _sub_rules_sorted()]}


@router.post("/api/subscriptions")
async def api_subscriptions_add(request: Request):
    body = await _subs_json_body(request)
    if body is None:
        return {"ok": False, "message": "请求体不是合法 JSON"}
    if len(_SUB_RULES) >= _SUBS_MAX_RULES:
        return {"ok": False, "message": "规则数量已达上限（%d 条）" % _SUBS_MAX_RULES}
    tg_id, chat_id = body.get("telegramId"), body.get("chatId")
    hit = _sub_validate_chat(tg_id, chat_id, await chat_sources())
    if hit is None:
        return {"ok": False, "message": "所选聊天不存在（请从下拉列表选择）"}
    for r in _SUB_RULES.values():
        if str(r.get("telegramId")) == str(tg_id) and str(r.get("chatId")) == str(chat_id):
            return {"ok": False, "message": "该聊天已有订阅规则，请直接编辑"}
    tpl = str(body.get("dirTemplate") or "").strip()
    if not tpl:
        return {"ok": False, "message": "请填写目标目录模板"}
    if _render_dir_template(tpl, source=str(hit.get("title") or ""), ftype="video", filename="Sample_1080p.mp4", chat_title=str(hit.get("title") or "")) in (None, "/"):
        return {"ok": False, "message": "目录模板无效：" + _TPL_HINT}
    priority = 0
    if "priority" in body:
        try:
            priority = int(body["priority"])
        except (ValueError, TypeError):
            priority = 0
    rule = {
        "id": secrets.token_hex(6),
        "telegramId": hit.get("telegramId"),
        "chatId": hit.get("chatId"),
        "chatTitle": str(hit.get("title") or "聊天"),
        "enabled": True,
        "priority": priority,
        "dirTemplate": tpl,
        "deleteLocal": bool(body.get("deleteLocal", True)),
        "policy": "overwrite" if str(body.get("policy") or "") == "overwrite" else "skip",
        "created_at": time.time(),
        "stats": {"enqueued": 0, "done": 0, "failed": 0, "last_hit_at": 0.0},
    }
    _SUB_RULES[rule["id"]] = rule
    _subs_save()
    asyncio.create_task(_auto_archive_sweep())
    LOG_STORE.append("INFO", "新增订阅归档规则：%s → %s (优先级: %d)" % (rule["chatTitle"], tpl, priority))
    return {"ok": True, "rule": _sub_public(rule)}


@router.post("/api/subscriptions/rule")
async def api_subscriptions_rule(request: Request):
    """创建或更新带优先级与高级变量的订阅规则。"""
    body = await _subs_json_body(request)
    if body is None:
        return {"ok": False, "message": "请求体不是合法 JSON"}
    rid = str(body.get("id") or "")
    if rid and rid in _SUB_RULES:
        return await api_subscriptions_update(request)
    return await api_subscriptions_add(request)


@router.post("/api/subscriptions/update")
async def api_subscriptions_update(request: Request):
    body = await _subs_json_body(request)
    if body is None:
        return {"ok": False, "message": "请求体不是合法 JSON"}
    rule = _SUB_RULES.get(str(body.get("id") or ""))
    if rule is None:
        return {"ok": False, "message": "规则不存在或已删除"}
    if "dirTemplate" in body:
        tpl = str(body.get("dirTemplate") or "").strip()
        if _render_dir_template(tpl, source=str(rule.get("chatTitle") or ""), ftype="video", filename="Sample_1080p.mp4", chat_title=str(rule.get("chatTitle") or "")) in (None, "/"):
            return {"ok": False, "message": "目录模板无效：" + _TPL_HINT}
        rule["dirTemplate"] = tpl
    if "priority" in body:
        try:
            rule["priority"] = int(body["priority"])
        except (ValueError, TypeError):
            pass
    if "deleteLocal" in body:
        rule["deleteLocal"] = bool(body.get("deleteLocal"))
    if "policy" in body:
        rule["policy"] = "overwrite" if str(body.get("policy") or "") == "overwrite" else "skip"
    if "enabled" in body:
        rule["enabled"] = bool(body.get("enabled"))
    _subs_save()
    if rule.get("enabled") and "enabled" in body:
        asyncio.create_task(_auto_archive_sweep())
    return {"ok": True, "rule": _sub_public(rule)}


@router.post("/api/subscriptions/reorder")
async def api_subscriptions_reorder(request: Request):
    """调整规则优先级排序。"""
    body = await _subs_json_body(request)
    if body is None:
        return {"ok": False, "message": "请求体不是合法 JSON"}
    orders = body.get("orders") or body.get("ruleIds") or []
    if isinstance(orders, list):
        for idx, item in enumerate(orders):
            if isinstance(item, dict):
                rid = str(item.get("id") or "")
                prio = int(item.get("priority", 0) or 0)
            else:
                rid = str(item)
                prio = (len(orders) - idx) * 10
            if rid in _SUB_RULES:
                _SUB_RULES[rid]["priority"] = prio
        _subs_save()
        return {"ok": True, "rules": [_sub_public(r) for r in _sub_rules_sorted()]}
    return {"ok": False, "message": "参数格式错误"}


@router.post("/api/subscriptions/preview-template")
async def api_subscriptions_preview_template(request: Request):
    """实时预览解析目录模板。"""
    body = await _subs_json_body(request)
    if body is None:
        return {"ok": False, "message": "请求体不是合法 JSON"}
    tpl = str(body.get("dirTemplate") or "").strip()
    sample = body.get("sample") if isinstance(body.get("sample"), dict) else {}
    src = str(sample.get("chatTitle") or sample.get("source") or "电影频道")
    fn = str(sample.get("filename") or "Sample_1080p.mp4")
    ftype = str(sample.get("type") or "video")
    res = str(sample.get("resolution") or "")
    ext = str(sample.get("ext") or "")
    w = sample.get("width")
    h = sample.get("height")
    preview = _render_dir_template(
        tpl,
        source=src,
        ftype=ftype,
        filename=fn,
        resolution=res,
        ext=ext,
        chat_title=src,
        width=w,
        height=h
    )
    if preview is None:
        return {"ok": False, "message": "目录模板包含未知变量或路径非法"}
    return {
        "ok": True,
        "previewDir": preview,
        "variables": {
            "source": src,
            "chat_title": src,
            "resolution": "1080p",
            "ext": "mp4",
            "type": ftype,
        }
    }


@router.post("/api/subscriptions/delete")
async def api_subscriptions_delete(request: Request):
    body = await _subs_json_body(request)
    if body is None:
        return {"ok": False, "message": "请求体不是合法 JSON"}
    rule = _SUB_RULES.pop(str(body.get("id") or ""), None)
    if rule is None:
        return {"ok": False, "message": "规则不存在或已删除"}
    _subs_save()
    LOG_STORE.append("INFO", "删除订阅归档规则：%s" % (rule.get("chatTitle") or ""))
    return {"ok": True}


@router.post("/api/subscriptions/run")
async def api_subscriptions_run():
    """手动触发一轮自动归档扫描（立即反馈，不必等周期）。"""
    n = await _auto_archive_sweep()
    return {"ok": True, "enqueued": n,
            "message": ("本轮新入队 %d 个文件" % n) if n else "本轮没有需要归档的文件"}


@router.get("/subscriptions", response_class=HTMLResponse)
async def subscriptions_page(request: Request):
    ctx = await _ctx(request, "subscriptions", "subscriptions", extra={
        "sub_rules": [_sub_public(r) for r in _sub_rules_sorted()],
        "sub_chat_sources": await chat_sources(),
        "auto_archive_interval": int(_AUTO_ARCHIVE_INTERVAL),
    })
    return templates.TemplateResponse("subscriptions.html", ctx)

