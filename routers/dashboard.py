# -*- coding: utf-8 -*-

"""
routers/dashboard.py — 表现层路由模块：仪表盘主页与健康探针
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

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    ctx = await _ctx(request, "dashboard", "dashboard")
    return templates.TemplateResponse("dashboard.html", ctx)


@router.get("/health")
async def health():
    return {"ok": True}

