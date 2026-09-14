# -*- coding: utf-8 -*-
"""
services/openlist_service.py — OpenList 云存储网关客户端与文件流式传输服务
========================================================================
负责与本地/远程 OpenList (端口 5244) 通信：令牌管理、挂载探针、分片上传与直链生成。
"""
import os
import time
import asyncio
import posixpath
from urllib.parse import quote, urlparse
from typing import Any, Dict, List, Optional, Tuple
import httpx
from fastapi.responses import JSONResponse
from core.config import (
    OPENLIST_URL, _archive_norm_dir
)
from core.state import (
    _OPENLIST, _openlist_client, _openlist_upload_client,
    _openlist_save, _openlist_update_base_url, _archive_save, _ARCHIVE_CONFIG
)
from core.logging import log

_UPLOAD_CHUNK = 4 << 20  # 4MB 分块
# 历史值 1MB：流式写盘对每个分块做一次 await asyncio.to_thread(fh.write, chunk)，
# 10GB 文件 = 10240 次线程池往返。放大到 4MB 后降到约 1/4，内存峰值仍只有
# chunk × 并发数（_RETRIEVE_SEM 限 2）。上传侧读取同一常量，行为一致。


async def _openlist_api_login(username: str, password: str) -> Tuple[str, str]:
    """调 OpenList POST /api/auth/login（唯一认证路径）。返回 (token, message)。"""
    try:
        resp = await _openlist_client.post(
            "/api/auth/login", json={"username": username, "password": password})
    except Exception as e:  # noqa: BLE001
        return "", "OpenList 服务不可达（%s）" % e.__class__.__name__
    if resp.status_code >= 400:
        return "", "OpenList 返回 HTTP %d" % resp.status_code
    try:
        env = resp.json()
    except Exception:  # noqa: BLE001
        return "", "OpenList 返回了无法解析的响应"
    try:
        code = int(env.get("code", 0))
    except (TypeError, ValueError):
        code = 0
    if code == 200:
        token = str((env.get("data") or {}).get("token") or "")
        if token:
            return token, ""
        return "", "OpenList 未返回令牌"
    return "", str(env.get("message") or "用户名或密码错误")


async def _openlist_verify_token(token: str) -> bool:
    """GET /api/me 验证 token（包络 code==200 即有效；Authorization 直传 JWT）。"""
    if not token:
        return False
    try:
        resp = await _openlist_client.get("/api/me", headers={"Authorization": token})
    except Exception:  # noqa: BLE001
        return False
    try:
        return int(resp.json().get("code", 0)) == 200
    except Exception:  # noqa: BLE001
        return False


async def _openlist_status() -> Dict[str, Any]:
    """当前 OpenList 登录态；token 失效且存有密码时静默重走 API 登录。"""
    username = _OPENLIST.get("username", "")
    base_url = _OPENLIST.get("baseUrl") or OPENLIST_URL
    if not username:
        return {"loggedIn": False, "username": "", "baseUrl": base_url,
                "verified": False, "loggedAt": 0.0, "message": "尚未登录"}
    verified = await _openlist_verify_token(_OPENLIST.get("token", ""))
    if not verified and _OPENLIST.get("password"):
        token, msg = await _openlist_api_login(username, _OPENLIST["password"])
        if token:
            _OPENLIST["token"] = token
            _OPENLIST["logged_at"] = time.time()
            _openlist_save()
            verified = True
            log.info("OpenList token 失效，已静默重登（用户: %s）", username)
        else:
            log.warning("OpenList token 失效且自动重登失败: %s", msg)
    return {
        "loggedIn": True,
        "username": username,
        "baseUrl": base_url,
        "verified": verified,
        "loggedAt": _OPENLIST.get("logged_at", 0.0),
        "message": "" if verified else "令牌已失效且自动重登失败，请重新登录",
    }


async def _openlist_ready() -> bool:
    """OpenList 是否已挂载可用（登录且令牌验证通过）——「立即归档」亮起的总开关。"""
    try:
        return bool((await _openlist_status()).get("verified"))
    except Exception as e:  # noqa: BLE001
        log.warning("openlist_ready 判定失败: %s", e)
        return False


async def _openlist_token() -> str:
    """取可用 token（失效时 _openlist_status 已静默重登）；不可用抛 RuntimeError。"""
    st = await _openlist_status()
    if not st.get("verified"):
        raise RuntimeError(st.get("message") or "OpenList 未登录或令牌失效，请到设置页登录")
    return str(_OPENLIST.get("token") or "")


async def _openlist_relogin() -> str:
    """上传中途令牌失效的兜底：用存量密码重登一次。"""
    if not _OPENLIST.get("password"):
        raise RuntimeError("OpenList 令牌已失效，请到设置页重新登录")
    token, msg = await _openlist_api_login(
        str(_OPENLIST.get("username") or ""), _OPENLIST["password"])
    if not token:
        raise RuntimeError("OpenList 令牌失效且自动重登失败：" + msg)
    _OPENLIST["token"] = token
    _OPENLIST["logged_at"] = time.time()
    _openlist_save()
    log.info("OpenList 令牌上传中途失效，已用存量密码重登（用户: %s）",
             _OPENLIST.get("username"))
    return token


def _openlist_env(resp: httpx.Response) -> Tuple[int, Dict[str, Any], str]:
    """OpenList /api 包络 → (code, data, message)。错误也是 HTTP 200，必须看包络码。

    注意 3xx：共享单例 _openlist_client 设了 follow_redirects=False。若 OpenList
    前面挂了反代、把请求 302 到 /login，这里只会拿到一段 HTML，json() 解析失败
    后返回 code=0，调用方一律当「业务失败」处理且**永不重登** —— 表现为「凭据
    明明是好的，却一直报未登录」。这里显式把 3xx 映射为未认证码 401，让调用方
    已有的 `code in (401, 403)` 重登逻辑生效。
    """
    if 300 <= resp.status_code < 400:
        return 401, {}, f"OpenList 返回重定向 HTTP {resp.status_code}（疑似反代跳登录）"
    try:
        env = resp.json()
    except Exception:  # noqa: BLE001
        return 0, {}, "OpenList 返回了无法解析的响应"
    try:
        code = int(env.get("code", 0))
    except (TypeError, ValueError):
        code = 0
    return code, (env.get("data") or {}), str(env.get("message") or "")


async def _openlist_mkdir_tree(token: str, remote_dir: str) -> None:
    """逐级建目录；「已存在」类错误视为成功（多网盘/mount 各层都覆盖）。"""
    cur = ""
    for seg in [s for s in remote_dir.split("/") if s]:
        cur += "/" + seg
        try:
            resp = await _openlist_client.post(
                "/api/fs/mkdir", json={"path": cur}, headers={"Authorization": token})
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"创建目录 {cur} 失败：OpenList 服务不可达") from e
        if resp.status_code >= 400:
            raise RuntimeError(f"创建目录 {cur} 失败：HTTP {resp.status_code}")
        code, _, msg = _openlist_env(resp)
        if code != 200 and "exist" not in msg.lower():
            raise RuntimeError(f"创建目录 {cur} 失败：{msg or code}")


async def _openlist_stat(token: str, remote_path: str) -> Optional[Dict[str, Any]]:
    """探测远端文件元数据（含 size）。不存在或探测失败返回 None。

    与 _openlist_exists 的区别：必须拿到 **size**。历史事故：上传被取消后
    OneDrive 会留下 0 字节残片，只判「存在与否」会把它当成已归档，
    接着按 deleteLocal 删掉本地文件 —— 本地没了、云端是空壳，真实数据丢失。
    """
    try:
        resp = await _openlist_client.post(
            "/api/fs/get", json={"path": remote_path}, headers={"Authorization": token})
    except Exception:  # noqa: BLE001
        return None
    code, data, _ = _openlist_env(resp)
    if code != 200 or not isinstance(data, dict):
        return None
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return {"size": size, "raw": data}


async def _openlist_exists(token: str, remote_path: str) -> bool:
    """/api/fs/get 探测远端文件是否已存在（探测失败按不存在处理，交给上传层报错）。"""
    return (await _openlist_stat(token, remote_path)) is not None


class _OpenListAuthErr(Exception):
    """鉴权失效信号：worker/目录接口捕获后重登重试一次。"""


async def _openlist_put_once(token: str, job: Dict[str, Any]) -> None:
    """单次流式 PUT。成功返回；业务失败抛 RuntimeError；鉴权失败抛 _OpenListAuthErr。"""
    remote_path = str(job["remote_path"])
    size = int(job.get("size_bytes") or 0)
    headers = {
        "Authorization": token,
        "File-Path": quote(remote_path, safe=""),
        "Content-Type": "application/octet-stream",
        "Content-Length": str(size),
        "As-Task": "false",
    }
    sent = 0
    last_flush = 0.0

    async def gen():
        nonlocal sent, last_flush
        fh = await asyncio.to_thread(open, job["local_path"], "rb")
        try:
            while True:
                chunk = await asyncio.to_thread(fh.read, _UPLOAD_CHUNK)
                if not chunk:
                    break
                sent += len(chunk)
                job["progress"] = min(99, int(sent * 100 / max(size, 1)))
                now = time.monotonic()
                if now - last_flush >= 2.0:
                    last_flush = now
                    _archive_save()
                yield chunk
        finally:
            try:
                await asyncio.to_thread(fh.close)
            except Exception:  # noqa: BLE001
                pass

    resp = await _openlist_upload_client.put(
        "/api/fs/put", content=gen(), headers=headers)
    if resp.status_code == 401:
        raise _OpenListAuthErr()
    code, _, msg = _openlist_env(resp)
    if code in (401, 403):
        raise _OpenListAuthErr()
    if code != 200:
        raise RuntimeError(msg or f"OpenList 返回业务码 {code}")


def _openlist_direct_url(remote_path: str) -> str:
    """生成在 OpenList Web 界面中定位到该文件的 URL（与“浏览下载”页一致）。

    注意：这里保留**文件完整路径**。实测“浏览下载”页的 OpenList 按钮正是用它
    并成功定位到文件所在位置，故两页保持同一语义，不再另造“父目录”版本。
    """
    if not remote_path or remote_path == "—":
        return ""
    base = (_ARCHIVE_CONFIG.get("publicBaseUrl") or _OPENLIST.get("baseUrl") or OPENLIST_URL).rstrip("/")
    raw = str(remote_path or "").strip().replace("\\", "/")
    norm = posixpath.normpath("/" + raw).lstrip("/")
    if not norm or norm.startswith("..") or norm == ".":
        return ""
    clean_path = quote(norm, safe="/")
    return f"{base}/{clean_path}"


async def openlist_dirs(path: str = "/") -> Dict[str, Any]:
    """归档弹窗的目录选择器：逐级列出 OpenList 挂载的目录（多网盘浏览）。"""
    d = _archive_norm_dir(path)
    if d is None:
        return {"ok": False, "message": "非法路径"}
    try:
        token = await _openlist_token()
    except RuntimeError as e:
        return {"ok": False, "message": str(e)}
    try:
        async def _list_once(tok: str):
            return await _openlist_client.post(
                "/api/fs/list",
                json={"path": d, "password": "", "page": 1, "per_page": 500, "refresh": False},
                headers={"Authorization": tok})

        resp = await _list_once(token)
        code, data, msg = _openlist_env(resp)
        if resp.status_code == 401 or code in (401, 403):
            token = await _openlist_relogin()
            resp = await _list_once(token)
            code, data, msg = _openlist_env(resp)
        if code != 200:
            return {"ok": False, "message": msg or f"OpenList 返回业务码 {code}"}
        base = "" if d == "/" else d
        dirs = []
        for c in (data.get("content") or []):
            if c.get("is_dir"):
                name = str(c.get("name") or "")
                if name:
                    dirs.append({"name": name, "path": f"{base}/{name}"})
        return {"ok": True, "path": d, "dirs": dirs}
    except RuntimeError as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:  # noqa: BLE001
        log.error("读取 OpenList 目录失败（%s）: %s", d, e)
        return {"ok": False, "message": "读取 OpenList 目录失败，请稍后重试"}


async def openlist_mounts() -> Dict[str, Any]:
    """列出 OpenList 根目录下的真实挂载网盘（云端归档页「网盘分类」的唯一数据源）。

    返回 {"ok": bool, "mounts": [name, ...], "message": str}。
    OpenList 未登录/不可达时 ok=False 且 mounts 为空，调用方应退化为
    仅按归档记录里真实出现过的网盘聚合，绝不回退到任何硬编码列表。
    """
    d = await openlist_dirs("/")
    if not d.get("ok"):
        return {"ok": False, "mounts": [], "message": str(d.get("message") or "")}
    mounts = [str(x.get("name") or "") for x in (d.get("dirs") or []) if x.get("name")]
    return {"ok": True, "mounts": mounts, "message": ""}


async def openlist_stream_url(path: str = "") -> Any:
    """获取视频在线播放直链（OpenList 302直链/WebDAV直链）。"""
    if not path:
        return JSONResponse({"ok": False, "message": "缺少 path 参数"}, status_code=400)
    norm = posixpath.normpath("/" + str(path).strip().replace("\\", "/"))
    filename = posixpath.basename(norm)
    direct_url = _openlist_direct_url(norm)

    raw_url = ""
    try:
        token = await _openlist_token()
        async def _get_link(tok: str):
            return await _openlist_client.post(
                "/api/fs/link",
                json={"path": norm},
                headers={"Authorization": tok}
            )
        resp = await _get_link(token)
        code, data, _ = _openlist_env(resp)
        if resp.status_code == 401 or code in (401, 403):
            token = await _openlist_relogin()
            resp = await _get_link(token)
            code, data, _ = _openlist_env(resp)
        if code == 200 and isinstance(data, dict) and data.get("url"):
            raw_url = str(data["url"])
    except Exception as e:
        log.debug("获取 OpenList 视频串流直链失败，回退 Web 界面直达: %s", e)

    if not raw_url:
        raw_url = direct_url

    return {
        "ok": True,
        "url": raw_url,
        "directUrl": direct_url,
        "path": norm,
        "filename": filename,
    }


async def openlist_list_files(path: str = "/") -> Dict[str, Any]:
    """列出 OpenList 目录下的**文件**（含大小与修改时间），用于会话冷备列表。

    与 openlist_dirs 的区别：这里要的是文件而非子目录。
    返回 {"ok": bool, "files": [{name, path, size, modified}], "message": str}。
    OpenList 未登录/不可达时 ok=False，调用方必须退化为「仅本地快照」，
    绝不允许用任何硬编码列表冒充云端真实内容。
    """
    d = _archive_norm_dir(path)
    if d is None:
        return {"ok": False, "files": [], "message": "非法路径"}
    try:
        token = await _openlist_token()
    except RuntimeError as e:
        return {"ok": False, "files": [], "message": str(e)}
    try:
        async def _list_once(tok: str):
            return await _openlist_client.post(
                "/api/fs/list",
                json={"path": d, "password": "", "page": 1, "per_page": 500, "refresh": False},
                headers={"Authorization": tok})

        resp = await _list_once(token)
        code, data, msg = _openlist_env(resp)
        if resp.status_code == 401 or code in (401, 403):
            token = await _openlist_relogin()
            resp = await _list_once(token)
            code, data, msg = _openlist_env(resp)
        if code != 200:
            return {"ok": False, "files": [], "message": msg or f"OpenList 返回业务码 {code}"}
        base = "" if d == "/" else d
        files = []
        for c in (data.get("content") or []):
            if not isinstance(c, dict) or c.get("is_dir"):
                continue
            name = str(c.get("name") or "")
            if not name:
                continue
            files.append({
                "name": name,
                "path": f"{base}/{name}",
                "size": int(c.get("size") or 0),
                "modified": str(c.get("modified") or ""),
            })
        return {"ok": True, "files": files, "message": ""}
    except RuntimeError as e:
        return {"ok": False, "files": [], "message": str(e)}
    except Exception as e:  # noqa: BLE001
        log.error("读取 OpenList 文件列表失败（%s）: %s", d, e)
        return {"ok": False, "files": [], "message": "读取 OpenList 目录失败，请稍后重试"}


async def openlist_download_to_file(remote_path: str, local_path: str, max_bytes: int = 0) -> Tuple[bool, str]:
    """把 OpenList 上的文件流式下载到本地（云端冷备取回还原时使用）。

    实现要点（线上实测结论）：
    - 必须先用 /api/fs/link 取真实直链；OpenList 的 /d/、/p/ 代理路径对
      API token 不认（返回 401 HTML），不能作为下载通道。
    - 直链是 OneDrive 临时地址，需要 follow_redirects=True 且读超时放宽到小时级。
    返回 (ok, message)。
    """
    if not remote_path or not remote_path.startswith("/"):
        return False, "非法远端路径"
    try:
        token = await _openlist_token()
    except RuntimeError as e:
        return False, str(e)
    try:
        resp = await _openlist_client.post(
            "/api/fs/link", json={"path": remote_path}, headers={"Authorization": token})
        code, data, msg = _openlist_env(resp)
        if resp.status_code == 401 or code in (401, 403):
            token = await _openlist_relogin()
            resp = await _openlist_client.post(
                "/api/fs/link", json={"path": remote_path}, headers={"Authorization": token})
            code, data, msg = _openlist_env(resp)
        if code != 200:
            return False, msg or f"获取云端直链失败（业务码 {code}）"
        url = str((data or {}).get("url") or "")
        if not url:
            return False, "云端未返回可用下载直链"

        timeout = httpx.Timeout(connect=15.0, read=3600.0, write=3600.0, pool=15.0)
        written = 0
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as c:
                async with c.stream("GET", url) as r:
                    if r.status_code >= 400:
                        return False, f"云端下载返回 HTTP {r.status_code}"
                    fd = os.open(local_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "wb") as f:
                        async for chunk in r.aiter_bytes(1 << 20):
                            if not chunk:
                                continue
                            written += len(chunk)
                            if max_bytes and written > max_bytes:
                                return False, f"云端文件超过允许大小上限（>{max_bytes} 字节），已中止"
                            await asyncio.to_thread(f.write, chunk)
        except Exception:
            # 中途断流不得留下半截文件冒充完整备份包
            try:
                os.remove(local_path)
            except OSError:
                pass
            raise
        if written == 0:
            try:
                os.remove(local_path)
            except OSError:
                pass
            return False, "云端下载得到 0 字节内容"
        return True, f"已下载 {written} 字节"
    except Exception as e:  # noqa: BLE001
        log.warning("从 OpenList 下载文件失败（%s）: %s", remote_path, e)
        return False, f"云端下载失败: {e}"
