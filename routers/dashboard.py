# -*- coding: utf-8 -*-

"""
routers/dashboard.py — 表现层路由模块：仪表盘主页与健康探针
"""
from core import *
from services import *
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse



router = APIRouter()

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    ctx = await _ctx(request, "dashboard", "dashboard")
    return templates.TemplateResponse("dashboard.html", ctx)


@router.get("/health")
async def health():
    return {"ok": True}

