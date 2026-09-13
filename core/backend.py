# -*- coding: utf-8 -*-
"""
core/backend.py — Java 后端通信客户端、凭据持久化与 WebSocket 实时中继
=======================================================================
封装与真实 Java 后端 (:8123) 的 HTTP 请求、CSRF/Session 会话维护与 WS 事件中继。
"""
import os
import re
import json
import time
import hashlib
import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
import websockets

from core.config import (
    APP_ROOT_DIR, TG_API_URL, WS_BASE_URL, CACHE_TTL, WS_RECONNECT_DELAY,
    CSRF_WHITELIST, TG_API_METHOD_WHITELIST, _CREDS_FILE,
    _pick, _extract_flood_wait_seconds
)
from core.state import (
    _trigger_flood_wait, _TASKS_CACHE
)
from core.logging import log, HUB, LOG_STORE, _log_line_from_event

_API_PENDING: Dict[str, asyncio.Future] = {}


async def telegram_api_call(method: str, params: Optional[Dict[str, Any]],
                            timeout: float = 30.0) -> Any:
    """发起 /telegram/api/{method} 并等待 WS 上的真实 TDLib 结果。"""
    resp = await BACKEND.telegram_api(method, params)
    handle = resp.get("code") if isinstance(resp, dict) else None
    if not handle or not isinstance(handle, str):
        return resp
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _API_PENDING[handle] = fut
    try:
        return await asyncio.wait_for(fut, timeout)
    finally:
        _API_PENDING.pop(handle, None)


class BackendClient:
    """封装对真实后端的所有 HTTP 调用，自动处理鉴权头与缓存。"""

    def __init__(self, base_url: str, ttl: float = CACHE_TTL):
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
        )
        self.ttl = ttl
        self._cache: Dict[str, tuple] = {}
        self._login_lock = asyncio.Lock()
        self._username: str = ""
        self._password: str = ""
        self._bs_cache: Optional[Dict[str, Any]] = None
        self._bs_cache_expire: float = 0.0

    def _csrf_token(self) -> Optional[str]:
        return self.client.cookies.get("tf_csrf")

    def set_credentials(self, username: str, password: str) -> None:
        self._username = username
        self._password = password

    async def _relogin(self) -> bool:
        if not self._username:
            return False
        async with self._login_lock:
            try:
                resp = await self.client.post(
                    "/auth/login",
                    json={"username": self._username, "password": self._password},
                )
                if resp.status_code < 400:
                    for c in self.client.cookies.jar:
                        c.secure = False
                    log.info("后端会话已自动重登")
                    self._cache.clear()
                    return True
            except Exception as e:  # noqa: BLE001
                log.warning("自动重登失败: %s", e)
        return False

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        raw: bool = False,
        retry_on_401: bool = True,
    ) -> Any:
        headers = {}
        # 显式透传已认证的后端 Cookie，防止因后端设置 Secure 属性而导致 httpx 在 http 链路上默认过滤
        cookie_header = "; ".join(f"{k}={v}" for k, v in self.client.cookies.items() if v)
        if cookie_header:
            headers["Cookie"] = cookie_header
        m = method.upper()
        is_state_changing = m not in ("GET", "HEAD", "OPTIONS")
        if is_state_changing and path not in CSRF_WHITELIST:
            csrf = self._csrf_token()
            if csrf:
                headers["X-CSRF-Token"] = csrf
        resp = await self.client.request(m, path, json=json, params=params, headers=headers)
        auth_lost = resp.status_code == 401 or (300 <= resp.status_code < 400 and resp.status_code != 304)
        if auth_lost and retry_on_401:
            self._cache.clear()
            if await self._relogin():
                csrf = self._csrf_token()
                if is_state_changing and path not in CSRF_WHITELIST and csrf:
                    headers["X-CSRF-Token"] = csrf
                resp = await self.client.request(m, path, json=json, params=params, headers=headers)
        if raw:
            return resp
        if resp.status_code in (420, 429):
            w_sec = _extract_flood_wait_seconds(resp.text)
            if w_sec:
                _trigger_flood_wait("default", w_sec, reason=f"HTTP_{resp.status_code}_FLOOD_WAIT_{w_sec}")
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except Exception:
            return resp.text

    async def _cached(
        self,
        key: str,
        producer: Callable[[], Awaitable[Any]],
        force: bool = False,
    ) -> Any:
        if not force and key in self._cache:
            expire_at, value = self._cache[key]
            if expire_at > time.monotonic():
                return value
        value = await producer()
        self._cache[key] = (time.monotonic() + self.ttl, value)
        return value

    async def close(self):
        await self.client.aclose()

    @staticmethod
    def _safe_id(value: Any) -> str:
        s = str(value)
        if not re.fullmatch(r"[0-9A-Za-z_-]+", s):
            raise ValueError(f"非法 id: {value!r}")
        return s

    async def auth_session(self) -> Dict[str, Any]:
        try:
            data = await self._request("GET", "/auth/session", retry_on_401=False)
            return data if isinstance(data, dict) else {}
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 401:
                return {"authenticated": False}
            log.warning("auth/session 失败: %s", e)
            return {}
        except Exception as e:  # noqa: BLE001
            log.warning("auth/session 失败: %s", e)
            return {}

    async def auth_login(self, username: str, password: str) -> Dict[str, Any]:
        async with self._login_lock:
            data = await self._request("POST", "/auth/login", json={"username": username, "password": password}, retry_on_401=False)
        return data if isinstance(data, dict) else {}

    async def auth_bootstrap(self, code: str, username: str, password: str) -> Dict[str, Any]:
        body: Dict[str, Any] = {"bootstrapToken": code, "username": username, "password": password}
        async with self._login_lock:
            data = await self._request("POST", "/auth/bootstrap", json=body, retry_on_401=False)
        return data if isinstance(data, dict) else {}

    async def bootstrap_status(self) -> Dict[str, Any]:
        try:
            data = await self._request("GET", "/auth/bootstrap/status")
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _bootstrap_status_cache(self) -> Optional[Dict[str, Any]]:
        now = time.monotonic()
        if self._bs_cache_expire > now:
            return self._bs_cache
        return None

    async def ensure_bootstrap_status(self) -> Optional[Dict[str, Any]]:
        cached = self._bootstrap_status_cache()
        if cached is not None:
            return cached
        await self.refresh_bootstrap_status()
        return self._bs_cache

    async def refresh_bootstrap_status(self) -> None:
        self._bs_cache = await self.bootstrap_status()
        self._bs_cache_expire = time.monotonic() + 60

    async def auth_logout(self) -> None:
        try:
            await self._request("POST", "/auth/logout")
        except Exception:  # noqa: BLE001
            log.warning("后端 logout 调用失败（本地清理照常进行）", exc_info=True)
        finally:
            self._cache.clear()

    async def list_telegrams(self, force: bool = False) -> List[Dict[str, Any]]:
        return await self._cached("telegrams", lambda: self._request("GET", "/telegrams"), force)

    async def list_telegrams_raw(self, authorized: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"authorized": authorized} if authorized is not None else None
        data = await self._request("GET", "/telegrams", params=params)
        return data if isinstance(data, list) else []

    async def list_chats(self, telegram_id: Any, force: bool = False) -> List[Dict[str, Any]]:
        tg = self._safe_id(telegram_id)
        data = await self._cached(f"chats:{tg}", lambda: self._request("GET", f"/telegram/{tg}/chats"), force)
        if not data and f"chats:{tg}" in self._cache:
            self._cache.pop(f"chats:{tg}", None)
        return data if isinstance(data, list) else []

    @staticmethod
    def _unwrap_files(data: Any) -> List[Dict[str, Any]]:
        if isinstance(data, dict):
            files = data.get("files")
            return files if isinstance(files, list) else []
        if isinstance(data, list):
            return data
        return []

    async def list_files(
        self,
        telegram_id: Any,
        chat_id: Any,
        from_message_id: int = 0,
        type: str = "media",
        download_status: str = "",
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        tg = self._safe_id(telegram_id)
        ch = self._safe_id(chat_id)
        params: Dict[str, Any] = {"type": type, "limit": 500}
        if from_message_id:
            params["fromMessageId"] = from_message_id
        if download_status:
            params["downloadStatus"] = download_status
        key = f"files:{tg}:{ch}:{from_message_id}:{type}:{download_status}"
        raw = await self._cached(key, lambda: self._request("GET", f"/telegram/{tg}/chat/{ch}/files", params=params), force)
        return self._unwrap_files(raw)

    async def list_files_all_pages(
        self,
        telegram_id: Any,
        chat_id: Any,
        type: str = "media",
        download_status: str = "",
        force: bool = False,
        max_pages: int = 20,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        cursor = 0
        for _ in range(max_pages):
            page = await self.list_files(telegram_id, chat_id, from_message_id=cursor,
                                         type=type, download_status=download_status, force=force)
            if not page:
                break
            out.extend(page)
            last_msg = page[-1].get("messageId")
            try:
                nxt = int(last_msg) if last_msg is not None else 0
            except (TypeError, ValueError):
                break
            if not nxt or len(page) < 500:
                break
            cursor = nxt
        return out

    async def list_all_files(self, force: bool = False) -> List[Dict[str, Any]]:
        raw = await self._cached("all_files", lambda: self._request("GET", "/files", params={"limit": 500}), force)
        return self._unwrap_files(raw)

    async def list_all_files_page_info(self, force: bool = False) -> Any:
        return await self._cached("all_files_raw", lambda: self._request("GET", "/files", params={"limit": 500}), force)

    async def resolve_link(self, telegram_id: Any, link: str) -> List[Dict[str, Any]]:
        tg = self._safe_id(telegram_id)
        data = await self._request("GET", f"/telegram/{tg}/chat/0/files",
                                   params={"link": link, "limit": 50})
        return self._unwrap_files(data)

    async def count_files(self, telegram_id: Any, chat_id: Any) -> Dict[str, Any]:
        tg = self._safe_id(telegram_id)
        ch = self._safe_id(chat_id)
        data = await self._request("GET", f"/telegram/{tg}/chat/{ch}/files/count")
        return data if isinstance(data, dict) else {}

    async def delete_telegram(self, telegram_id: Any) -> Dict[str, Any]:
        tg = self._safe_id(telegram_id)
        data = await self._request("POST", f"/telegram/{tg}/delete", json={})
        return data if isinstance(data, dict) else {}

    async def start_download(self, telegram_id: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        tg = self._safe_id(telegram_id)
        data = await self._request("POST", f"/{tg}/file/start-download", json=payload)
        return data if isinstance(data, dict) else {}

    async def start_download_multiple(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = await self._request("POST", "/files/start-download-multiple", json=payload)
        return data if isinstance(data, dict) else {}

    async def cancel_download(self, telegram_id: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        tg = self._safe_id(telegram_id)
        data = await self._request("POST", f"/{tg}/file/cancel-download", json=payload)
        return data if isinstance(data, dict) else {}

    async def toggle_pause_download(self, telegram_id: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        tg = self._safe_id(telegram_id)
        data = await self._request("POST", f"/{tg}/file/toggle-pause-download", json=payload)
        return data if isinstance(data, dict) else {}

    async def remove_file(self, telegram_id: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        tg = self._safe_id(telegram_id)
        data = await self._request("POST", f"/{tg}/file/remove", json=payload)
        return data if isinstance(data, dict) else {}

    async def auth_password(self, old_password: str, new_password: str) -> Dict[str, Any]:
        body = {"currentPassword": old_password, "newPassword": new_password}
        async with self._login_lock:
            data = await self._request("POST", "/auth/password", json=body, retry_on_401=False)
        return data if isinstance(data, dict) else {}

    async def get_file_preview_url(self, telegram_id: Any, unique_id: Any) -> str:
        from urllib.parse import quote
        tg = self._safe_id(telegram_id)
        return f"{TG_API_URL}/{tg}/file/{quote(str(unique_id))}"

    async def fetch_file_bytes(self, telegram_id: Any, unique_id: Any) -> tuple:
        from urllib.parse import quote
        tg = self._safe_id(telegram_id)
        uid = str(unique_id)
        if not re.fullmatch(r"[A-Za-z0-9_=\-]{4,160}", uid):
            raise ValueError("非法 uniqueId: %r" % uid[:24])
        resp = await self._request("GET", f"/{tg}/file/{quote(uid, safe='')}", raw=True)
        if resp.status_code >= 400:
            raise RuntimeError("后端文件端点返回 %s" % resp.status_code)
        ctype = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
        return resp.content, ctype

    async def get_setting(self, keys: str) -> Dict[str, Any]:
        data = await self._request("GET", "/settings", params={"keys": keys})
        return data if isinstance(data, dict) else {}

    async def create_setting(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = await self._request("POST", "/settings/create", json=payload)
        return data if isinstance(data, dict) else {}

    async def create_telegram(self, proxy_name: str = "") -> Dict[str, Any]:
        body = {"proxyName": proxy_name} if proxy_name else {}
        data = await self._request("POST", "/telegram/create", json=body)
        return data if isinstance(data, dict) else {}

    async def telegram_api(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if method not in TG_API_METHOD_WHITELIST or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", method):
            log.warning("拒绝未授权 TDLib 方法: %r", method)
            raise ValueError(f"不支持的 TDLib 方法: {method!r}")
        try:
            data = await self._request("POST", f"/telegram/api/{method}", json=params or {})
        except Exception as e:
            w_sec = _extract_flood_wait_seconds(e)
            if w_sec:
                _trigger_flood_wait("default", w_sec, reason=f"FLOOD_WAIT_{w_sec}")
            raise
        if isinstance(data, dict):
            w_sec = _extract_flood_wait_seconds(data)
            if w_sec:
                _trigger_flood_wait("default", w_sec, reason=str(data.get("message") or f"FLOOD_WAIT_{w_sec}"))
        return data if isinstance(data, dict) else {}


BACKEND = BackendClient(TG_API_URL)


async def _chat_sources(force: bool = False) -> List[Dict[str, Any]]:
    sources: List[Dict[str, Any]] = []
    try:
        telegrams = await BACKEND.list_telegrams(force=force)
    except Exception as e:  # noqa: BLE001
        log.warning("list_telegrams 失败: %s", e)
        return sources
    if not isinstance(telegrams, list):
        return sources

    async def _gather_chat(tg: Dict[str, Any]):
        tg_id = _pick(tg, "telegramId", "telegram_id", "id")
        if tg_id is None:
            return []
        try:
            chats = await BACKEND.list_chats(tg_id, force=force)
        except Exception as e:  # noqa: BLE001
            log.warning("list_chats(%s) 失败: %s", tg_id, e)
            return []
        out = []
        if isinstance(chats, list):
            for ch in chats:
                out.append({
                    "chatId": _pick(ch, "chatId", "chat_id", "id"),
                    "title": str(_pick(ch, "title", "name", "channel", "chatName", default="聊天")),
                    "telegramId": tg_id,
                })
        return out

    results = await asyncio.gather(*[_gather_chat(t) for t in telegrams])
    for group in results:
        sources.extend(group)
    return sources


CHAT_SOURCE_CACHE: Dict[str, Any] = {"key": None, "value": None}


async def chat_sources(force: bool = False) -> List[Dict[str, Any]]:
    key = int(time.time()) // 30
    if not force and CHAT_SOURCE_CACHE["key"] == key:
        return CHAT_SOURCE_CACHE["value"]
    value = await _chat_sources(force=force)
    CHAT_SOURCE_CACHE["key"] = key
    CHAT_SOURCE_CACHE["value"] = value
    return value


def _tg_err(e: Exception) -> str:
    """从 httpx.HTTPError / 后端错误体里提取可读信息。"""
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            data = resp.json()
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err.get("code") or resp.status_code)
            if isinstance(err, str):
                return err
        except Exception:  # noqa: BLE001
            pass
        return f"后端返回 {resp.status_code}"
    msg = str(e).strip()
    return msg if msg else e.__class__.__name__


def _tg_err_public(e: Exception) -> str:
    """客户端安全版错误文案：不回显后端错误结构/内网 URL/部署拓扑。"""
    resp = getattr(e, "response", None)
    if resp is not None:
        code = resp.status_code
        if code == 429:
            return "操作过于频繁，请稍后重试"
        if code in (401, 403):
            return "没有权限执行该操作，请重新登录后重试"
        if code >= 500:
            return "后端服务暂时不可用，请稍后重试"
    return "操作失败，请重试"


def _save_backend_credentials(username: str, password: str) -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps({"username": username, "password": password}).encode("utf-8")
        fd = os.open(_CREDS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        log.info("后端凭据已持久化（.backend_creds, 0600）")
    except Exception as e:  # noqa: BLE001
        log.warning("后端凭据持久化失败: %s", e)


def _load_backend_credentials() -> None:
    try:
        with open(_CREDS_FILE, "rb") as f:
            d = json.loads(f.read().decode("utf-8"))
        u, p = str(d.get("username") or ""), str(d.get("password") or "")
        if u and p:
            BACKEND.set_credentials(u, p)
            log.info("已从磁盘恢复后端凭据（用户: %s），401 时可自动重登", u)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("后端凭据恢复失败: %s", e)


_load_backend_credentials()


def _task_event_from_ws(ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    typ = ev.get("type")
    data = ev.get("data")
    if not isinstance(data, dict):
        return None
    if typ == 4:
        return None
    if typ == 5:
        unique_id = data.get("uniqueId")
        st = str(data.get("downloadStatus") or "").strip().lower()
        status = "downloaded" if st == "completed" else ("failed" if st == "error" else ("download" if st == "downloading" else "pending"))
        if not unique_id and data.get("fileId") is None:
            return None
        task_id = data.get("fileId")
        if unique_id:
            h = hashlib.sha1(str(unique_id).encode()).hexdigest()[:12]
            task_id = int(h, 16) >> 4
        return {
            "id": task_id,
            "status": status,
            "progress": 0,
            "filename": str(data.get("localPath") or unique_id or ""),
        }
    if typ == 3:
        local = data.get("local") if isinstance(data.get("local"), dict) else {}
        dl = local.get("downloadedSize")
        size = data.get("size") or data.get("expectedSize")
        remote = data.get("remote") if isinstance(data.get("remote"), dict) else {}
        unique_id = remote.get("uniqueId")
        progress = 0
        if dl and size:
            try:
                progress = int(float(dl) / float(size) * 100)
            except (TypeError, ValueError, ZeroDivisionError):
                progress = 0
        task_id = data.get("id")
        if unique_id:
            h = hashlib.sha1(str(unique_id).encode()).hexdigest()[:12]
            task_id = int(h, 16) >> 4
        return {
            "id": task_id,
            "status": "download",
            "progress": progress,
            "filename": "",
        }
    return None


async def _ws_cookie_header() -> dict:
    cookies = BACKEND.client.cookies
    pairs = []
    try:
        for name, value in cookies.items():
            if value:
                pairs.append(f"{name}={value}")
    except Exception as e:  # noqa: BLE001
        log.warning("读取 WS cookie 失败: %s", e)
    if not pairs:
        for name in ("tf_admin", "tf_csrf", "tf"):
            val = cookies.get(name)
            if val:
                pairs.append(f"{name}={val}")
    return {"Cookie": "; ".join(pairs)} if pairs else {}


async def _pick_ws_telegram_ids() -> List[Any]:
    out: List[Any] = []
    try:
        telegrams = await BACKEND.list_telegrams(force=True)
        if isinstance(telegrams, list):
            for rec in telegrams:
                if isinstance(rec, dict):
                    tg_id = _pick(rec, "telegramId", "telegram_id", "id")
                    if tg_id is not None:
                        out.append(tg_id)
    except Exception as e:  # noqa: BLE001
        log.warning("选取 WS telegramId 失败: %s", e)
    return out


async def _ws_consume(telegram_id: Any) -> None:
    ws_path = f"{WS_BASE_URL}/ws?telegramId={telegram_id}&_r={int(time.time()*1000)}"
    log.info("连接后端 WS: %s", ws_path)
    cookie_header = await _ws_cookie_header()
    async with websockets.connect(
        ws_path,
        ping_interval=20,
        ping_timeout=20,
        extra_headers=cookie_header,
    ) as ws:
        while True:
            raw = await ws.recv()
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                ev = {"type": 0, "code": 0, "data": raw, "timestamp": time.time()}
            if not isinstance(ev, dict):
                continue
            handle = ev.get("code")
            if isinstance(handle, str) and handle in _API_PENDING:
                fut = _API_PENDING.pop(handle, None)
                if fut is not None and not fut.done():
                    fut.set_result(ev.get("data"))
            try:
                task_ev = _task_event_from_ws(ev)
                if task_ev is not None:
                    await HUB.publish("tasks", task_ev)
                log_line = _log_line_from_event(ev, task_ev.get("id") if task_ev else None)
                stored = LOG_STORE.append(log_line["level"], log_line["msg"],
                                          task_id=str(log_line.get("taskId") or ""),
                                          time_str=str(log_line.get("time") or ""))
                await HUB.publish("logs", stored)
            except Exception as e:  # noqa: BLE001
                log.warning("WS 事件处理失败（已跳过）: %s", e)


async def _ws_reconnect_loop(telegram_id: Any) -> None:
    delay = WS_RECONNECT_DELAY
    while True:
        try:
            await _ws_consume(telegram_id)
            log.info("WS 连接正常关闭（telegram %s）", telegram_id)
            delay = WS_RECONNECT_DELAY
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("WS 断开（telegram %s），%.0fs 后重连: %s", telegram_id, delay, e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def ws_relay_loop():
    delay = WS_RECONNECT_DELAY
    while True:
        try:
            tg_ids = await _pick_ws_telegram_ids()
            if not tg_ids:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            delay = WS_RECONNECT_DELAY
            await asyncio.gather(*[_ws_reconnect_loop(t) for t in tg_ids])
        except asyncio.CancelledError:
            log.info("WS 中继停止")
            return
        except Exception as e:  # noqa: BLE001
            log.warning("WS 中继异常，%.0fs 后重连: %s", delay, e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def _tg_ensure_client() -> Dict[str, Any]:
    """确保当前会话上有一个 TDLib 客户端（已创建则复用）。"""
    return await BACKEND.telegram_create()


async def _tg_state_of(telegram_id: Any) -> Optional[int]:
    """查询指定客户端的当前授权状态构造器码。

    后端把 TdApi 对象序列化为 {"constructor": <int>}（本项目用整数构造器码，
    而非 TDLib 官方 JSON 的 "@type" 字符串），两者都兼容读取。

    关键：客户端**完成授权后会从未授权列表移除**，只查 authorized=false 会让
    「登录成功」被误判为超时——查不到时必须再到全量列表确认。
    """
    if telegram_id is None:
        return None
    rows = await BACKEND.list_telegrams_raw(authorized="false")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("id")) != str(telegram_id):
                continue
            state = row.get("lastAuthorizationState")
            if isinstance(state, dict):
                t = state.get("constructor", state.get("@type"))
                try:
                    return int(t)
                except (TypeError, ValueError):
                    return None
            # 未授权列表里但没有状态字段 → 视为已就绪
            return TG_READY
    # 未授权列表里没有：可能已完成授权，查全量列表确认
    all_rows = await BACKEND.list_telegrams_raw()
    if isinstance(all_rows, list):
        for row in all_rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("id")) != str(telegram_id):
                continue
            if row.get("authorized") is True or row.get("lastAuthorizationState") is None:
                return TG_READY
            state = row.get("lastAuthorizationState")
            if isinstance(state, dict):
                try:
                    return int(state.get("constructor", state.get("@type")))
                except (TypeError, ValueError):
                    return None
    return None


async def _tg_wait_state(telegram_id: Any, accept: set, seconds: float = 12.0) -> Optional[int]:
    """轮询直到状态进入 accept 集合或终态(CLOSED/CLOSING)，超时返回 None。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = await _tg_state_of(telegram_id)
        if state is not None:
            if state in accept:
                return state
            if state in (TG_CLOSED, TG_CLOSING):
                return state
        await asyncio.sleep(1.2)
    return None

