# -*- coding: utf-8 -*-

"""
routers/auth.py — 表现层路由模块：身份认证、登录表单、初始化向导与改密登出
"""
from core import *
from services import *
import os
import re
import asyncio
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse



router = APIRouter()

@router.get("/login", response_class=HTMLResponse)
async def login(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {
        "request": request, "variant": "login", "error": error,
        "initialized": _is_initialized(),
    })


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    ip = _client_ip(request)
    if _login_blocked(ip):
        return templates.TemplateResponse("login.html", {"request": request, "variant": "login", "error": "尝试过于频繁，请稍后再试"})
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    try:
        await BACKEND.auth_login(username, password)
        # 记住凭据供 401 自愈重登；登录成功后清空上一会话的缓存
        BACKEND.set_credentials(username, password)
        _save_backend_credentials(username, password)
        BACKEND._cache.clear()
        _tasks_cache_invalidate()
        CHAT_SOURCE_CACHE["key"] = None
        CHAT_SOURCE_CACHE["value"] = None
        _clear_login_failures(ip)
        # 登录成功后立即在后台异步预热任务数据，当客户端完成 303 跳转发起 GET / 时，数据已就绪（耗时降至毫秒级）
        try:
            asyncio.create_task(tasks_all(force=True))
        except Exception:
            pass
        resp = RedirectResponse(url="/", status_code=303)
        # 浏览器会话与后端会话分离：签发 bridge 自身的门禁 cookie
        _set_portal_cookie(resp)
        return resp
    except Exception as e:  # noqa: BLE001
        _record_login_failure(ip)
        log.error("登录失败: %s", e)
        # 区分限流（429）与凭据错误，避免诱导被限流者继续试密码
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status == 429:
            return templates.TemplateResponse("login.html", {"request": request, "variant": "login", "error": "尝试过于频繁，请稍后再试"})
        # 固定文案：避免把后端内网 URL 回显到登录页
        return templates.TemplateResponse("login.html", {"request": request, "variant": "login", "error": "用户名或密码错误，或后端不可达"})


@router.get("/init", response_class=HTMLResponse)
async def init(request: Request):
    if not await _init_gate_open(_is_initialized()):
        # 初始化是一次性动作：已完成时不再向全网开放表单
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "variant": "init", "error": ""})


@router.post("/init", response_class=HTMLResponse)
async def init_submit(request: Request):
    """首启初始化：把一次性码/账号透传给后端 bootstrap。"""
    ip = _client_ip(request)
    if _login_blocked(ip):
        return templates.TemplateResponse("login.html", {"request": request, "variant": "init", "error": "尝试过于频繁，请稍后再试"})
    if not await _init_gate_open(_is_initialized()):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    otp = str(form.get("otp", "")).strip()
    username = str(form.get("username", "")).strip().lower()
    password = str(form.get("password", ""))
    password2 = str(form.get("password2", ""))
    if password != password2:
        return templates.TemplateResponse("login.html", {"request": request, "variant": "init", "error": "两次输入的密码不一致"})
    if len(password) < 12 or len(password) > 256:
        return templates.TemplateResponse("login.html", {"request": request, "variant": "init", "error": "密码长度必须在 12 到 256 位之间"})
    if not otp:
        return templates.TemplateResponse("login.html", {"request": request, "variant": "init", "error": "请输入初始化一次性码"})
    # 后端用户名规则：[a-z0-9][a-z0-9._-]{2,63}
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", username):
        return templates.TemplateResponse("login.html", {"request": request, "variant": "init", "error": "用户名需 3-64 位小写字母/数字/._- 开头为字母或数字"})
    try:
        await BACKEND.auth_bootstrap(otp, username, password)
        # 初始化成功 = 管理员已创建；bridge 记住该凭据用于会话过期自愈，
        # 并落盘一次性标志防止 /init 被重放
        BACKEND.set_credentials(username, password)
        _save_backend_credentials(username, password)
        _mark_initialized()
        _clear_login_failures(ip)
        resp = RedirectResponse(url="/", status_code=303)
        _set_portal_cookie(resp)
        return resp
    except Exception as e:  # noqa: BLE001
        _record_login_failure(ip)
        log.error("bootstrap 失败: %s", e)
        # 只透出可操作的文案；区分码无效/过期 vs 其他
        detail = _tg_err(e)
        if "TOKEN" in detail.upper() or "码" in detail:
            friendly = "初始化码无效或已过期（一次性码 15 分钟有效），请重新获取"
        elif "NETWORK" in detail.upper() or "LOCAL" in detail.upper():
            friendly = "初始化请求来源受限：bootstrap 仅允许本机/内网发起"
        else:
            friendly = "初始化失败：" + _tg_err_public(e)
        return templates.TemplateResponse("login.html", {"request": request, "variant": "init", "error": friendly})


@router.post("/auth/password")
async def auth_password(request: Request):
    """修改管理台密码（透传后端 /auth/password）。

    后端契约：成功=204 且**立即吊销该账号全部会话**并清空 tf_admin/tf_csrf。
    bridge 必须：更新自愈凭据 → 清空 cookie jar → 立即用新密码重登，
    否则改密成功后全站数据请求 401 且自愈必然失败。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "请求体不是合法 JSON"}
    old_password = str((body or {}).get("oldPassword", "") or (body or {}).get("currentPassword", "") or (body or {}).get("old", ""))
    new_password = str((body or {}).get("newPassword", "") or (body or {}).get("new", ""))
    if not old_password or not new_password:
        return {"ok": False, "message": "请填写旧密码与新密码"}
    if len(new_password) < 12 or len(new_password) > 256:
        return {"ok": False, "message": "新密码长度必须在 12 到 256 位之间"}
    try:
        await BACKEND.auth_password(old_password, new_password)
        # 凭据先更新，再清失效会话，再自愈重登（顺序不可颠倒）
        BACKEND.set_credentials(username=BACKEND._username, password=new_password)
        _save_backend_credentials(BACKEND._username, new_password)
        try:
            BACKEND.client.cookies.clear()
        except Exception:  # noqa: BLE001
            pass
        relog = await BACKEND._relogin()
        # 轮换门禁密钥：所有已签发的 portal token / CSRF token 立即失效
        try:
            global _PORTAL_SECRET
            data = secrets.token_bytes(32)
            os.makedirs(APP_ROOT_DIR, exist_ok=True)
            fd = os.open(_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            _PORTAL_SECRET = data
        except Exception as e:  # noqa: BLE001
            log.error("轮换 portal 密钥失败: %s", e)
        return {"ok": True, "relogin": relog}
    except Exception as e:  # noqa: BLE001
        log.error("修改密码失败: %s", e)
        return {"ok": False, "message": "修改密码失败：" + _tg_err_public(e)}


@router.post("/auth/logout")
async def logout(request: Request):
    # 后端会话是 bridge 进程内共享的（自愈重登依赖它），浏览器登出
    # 只删自身门禁 cookie，不调用 /auth/logout（否则会杀掉共享后端
    # 会话、断开 WS 中继，影响所有在线访客）。
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(PORTAL_COOKIE, path="/")
    resp.delete_cookie(CSRF_COOKIE, path="/")
    return resp

