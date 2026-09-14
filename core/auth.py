# -*- coding: utf-8 -*-
"""
core/auth.py — 会话凭据签名、CSRF 双提交校验、IP 限流风控与安全中间件
===================================================================
负责门户认证 Token 生成与校验、全局中间件、限流防护与初始化门禁。
"""
import os
import time
import base64
import hashlib
import hmac as _hmac
import secrets
import ipaddress
from typing import Any, Dict, List, Optional
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from core.config import (
    APP_ROOT_DIR, PORTAL_COOKIE, CSRF_COOKIE, CSRF_HEADER,
    _SECRET_FILE, _INIT_FLAG_FILE, PORTAL_TTL, PUBLIC_PATHS,
    PUBLIC_PREFIXES, SECURE_COOKIE, TRUSTED_PROXIES,
    LOGIN_RATE_LIMIT, LOGIN_RATE_WINDOW, _LOGIN_FAILURE_MAX
)
from core.state import _login_failures
from core.logging import log

# ---------------------------------------------------------------------
# 1. 签名密钥与令牌机制
# ---------------------------------------------------------------------
# 磁盘不可写时的进程级回退密钥：只生成一次，保证同一进程内签发与校验一致
# （旧代码每次调用都新生成 → 全体 401 循环；见 _portal_secret 的注释）。
_FALLBACK_PORTAL_SECRET = secrets.token_bytes(32)

# 单 IP 失败时间戳上限，防止无界增长
_LOGIN_FAILURE_MAX_PER_IP = 100


def _portal_secret() -> bytes:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        if os.path.exists(_SECRET_FILE):
            with open(_SECRET_FILE, "rb") as f:
                data = f.read()
            if len(data) >= 32:
                return data
        data = secrets.token_bytes(32)
        fd = os.open(_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return data
    except Exception as e:  # noqa: BLE001
        # 回退密钥必须是「进程内唯一」的常量，绝不能每次调用都新生成：
        # 多 worker / 热重载 / 磁盘瞬时不可写时，签发与校验会用不同密钥，
        # 表现为全体用户莫名 401 循环（旧代码 return secrets.token_bytes(32) 就是此坑）。
        log.error("portal 密钥读取/写入失败（%s），本次运行使用进程级临时密钥", e)
        return _FALLBACK_PORTAL_SECRET


_PORTAL_SECRET = _portal_secret()


def _sign(value: str) -> str:
    mac = _hmac.new(_PORTAL_SECRET, value.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def _make_portal_token() -> str:
    exp = str(int(time.time()) + PORTAL_TTL)
    nonce = secrets.token_hex(8)
    return exp + "." + nonce + "." + _sign(exp + "." + nonce)


def _verify_portal_token(token: str) -> bool:
    try:
        exp_s, nonce, sig = token.split(".", 2)
        if not _hmac.compare_digest(_sign(exp_s + "." + nonce), sig):
            return False
        return int(exp_s) > int(time.time())
    except Exception:  # noqa: BLE001
        return False


def _is_initialized() -> bool:
    return os.path.exists(_INIT_FLAG_FILE)


def _mark_initialized() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        with open(_INIT_FLAG_FILE, "wb") as f:
            f.write(str(int(time.time())).encode())
    except Exception as e:  # noqa: BLE001
        log.error("写入初始化标志失败: %s", e)


def _is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


async def _init_gate_open(local_flag: bool) -> bool:
    """/init 是否开放：以后端 bootstrap/status 为准，本地标志仅作辅助。

    注意 fail-closed：后端不可达时旧代码 return not local_flag（本地无标志 →
    放行）。而 PUBLIC_PATHS 含 /init，等于「后端宕机窗口可匿名访问初始化页」。
    安全策略下必须收窄：状态未知一律视为「不开放」。
    """
    try:
        from core.backend import BACKEND
        status = await BACKEND.ensure_bootstrap_status()
    except Exception:  # noqa: BLE001
        status = None
    if status is None:
        return False
    return bool(status.get("required"))


# ---------------------------------------------------------------------
# 2. IP 解析与防暴破限流
# ---------------------------------------------------------------------
def _ip_in_networks(ip: str, networks: List[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        for net in networks:
            try:
                if "/" in net:
                    if addr in ipaddress.ip_network(net, strict=False):
                        return True
                elif addr == ipaddress.ip_address(net):
                    return True
            except ValueError:
                continue
    except ValueError:
        return False
    return False


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "?"
    if TRUSTED_PROXIES and _ip_in_networks(peer, TRUSTED_PROXIES):
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return peer


def _prune_login_failures() -> None:
    now = time.time()
    # 必须用 list(...) 快照：本函数会在 _record_login_failure / _login_blocked
    # 里被调用，同进程并发写 key 时直接遍历 items() 会 RuntimeError:
    # dictionary changed size during iteration（限流器 500）。
    stale = [ip for ip, ts_list in list(_login_failures.items())
             if not any(now - t < LOGIN_RATE_WINDOW for t in ts_list)]
    for ip in stale:
        _login_failures.pop(ip, None)


def _login_blocked(ip: str) -> bool:
    """只读判定：已过窗口的时间戳不落盘，避免只读路径也写字典（并发放大 + 无界增长）。"""
    now = time.time()
    window = [t for t in list(_login_failures.get(ip, [])) if now - t < LOGIN_RATE_WINDOW]
    return len(window) >= LOGIN_RATE_LIMIT


def _record_login_failure(ip: str) -> None:
    if len(_login_failures) > _LOGIN_FAILURE_MAX:
        _prune_login_failures()
    ts_list = _login_failures.setdefault(ip, [])
    ts_list.append(time.time())
    # 单 IP 时间戳也要封顶，否则长期大量失败会让列表无界增长
    if len(ts_list) > _LOGIN_FAILURE_MAX_PER_IP:
        del ts_list[:-_LOGIN_FAILURE_MAX_PER_IP]


def _clear_login_failures(ip: str) -> None:
    _login_failures.pop(ip, None)


# ---------------------------------------------------------------------
# 3. 搜索与重试限流
# ---------------------------------------------------------------------
_SEARCH_RATE_WINDOW = 5.0
_SEARCH_RATE_LIMIT = 50
_SEARCH_REQUESTS: Dict[str, List[float]] = {}
_SEARCH_REQ_MAX = 1000


def _search_rate_limited(ip: str) -> bool:
    now = time.time()
    window = [t for t in _SEARCH_REQUESTS.get(ip, []) if now - t < _SEARCH_RATE_WINDOW]
    _SEARCH_REQUESTS[ip] = window
    if len(_SEARCH_REQUESTS) > _SEARCH_REQ_MAX:
        stale = [k for k, ts in _SEARCH_REQUESTS.items() if not any(now - t < _SEARCH_RATE_WINDOW for t in ts)]
        for k in stale:
            _SEARCH_REQUESTS.pop(k, None)
    if len(window) >= _SEARCH_RATE_LIMIT:
        return True
    _SEARCH_REQUESTS.setdefault(ip, []).append(now)
    return False


_BATCH_RETRY_WINDOW = 10.0
_BATCH_RETRY_LIMIT = 25
_BATCH_RETRY_REQUESTS: Dict[str, List[float]] = {}


def _batch_retry_rate_limited(ip: str) -> bool:
    now = time.time()
    window = [t for t in _BATCH_RETRY_REQUESTS.get(ip, []) if now - t < _BATCH_RETRY_WINDOW]
    _BATCH_RETRY_REQUESTS[ip] = window
    if len(_BATCH_RETRY_REQUESTS) > 500:
        stale = [k for k, ts in _BATCH_RETRY_REQUESTS.items() if not any(now - t < _BATCH_RETRY_WINDOW for t in ts)]
        for k in stale:
            _BATCH_RETRY_REQUESTS.pop(k, None)
    if len(window) >= _BATCH_RETRY_LIMIT:
        return True
    _BATCH_RETRY_REQUESTS.setdefault(ip, []).append(now)
    return False


# ---------------------------------------------------------------------
# 4. Cookie 与中间件
# ---------------------------------------------------------------------
def _set_portal_cookie(resp, max_age: int = PORTAL_TTL) -> None:
    resp.set_cookie(
        PORTAL_COOKIE, _make_portal_token(),
        max_age=max_age, httponly=True, samesite="lax", path="/",
        secure=SECURE_COOKIE,
    )
    resp.set_cookie(
        CSRF_COOKIE, secrets.token_hex(16),
        max_age=max_age, httponly=False, samesite="lax", path="/",
        secure=SECURE_COOKIE,
    )


async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; connect-src 'self'",
    )
    return resp


async def portal_auth_gate(request: Request, call_next):
    """浏览器登录门禁与 CSRF 双提交校验。"""
    path = request.url.path
    if _is_public(path):
        return await call_next(request)
    token = request.cookies.get(PORTAL_COOKIE, "") or request.query_params.get("token", "")
    if _verify_portal_token(token):
        if request.method not in ("GET", "HEAD", "OPTIONS") and path not in ("/login", "/init", "/auth/logout"):
            header = request.headers.get(CSRF_HEADER, "")
            cookie = request.cookies.get(CSRF_COOKIE, "")
            if not header or not cookie or not _hmac.compare_digest(header, cookie):
                return JSONResponse({"ok": False, "message": "CSRF 校验失败，请刷新页面重试"}, status_code=403)
        return await call_next(request)

    if (path.startswith("/sse/") or path.startswith("/partials/") or path.startswith("/task/")
            or path.startswith("/settings/") or path.startswith("/tg-login/")
            or path.startswith("/alerts/") or path.startswith("/preview/")
            or path.startswith("/openlist/") or path.startswith("/archive/")
            or path.startswith("/api/")
            or path.startswith("/library/local/delete") or path.startswith("/library/cloud/")
            or path in ("/tasks", "/auth/password", "/submit")):
        return JSONResponse({"ok": False, "message": "未登录"}, status_code=401)
    return RedirectResponse(url="/login", status_code=302)
