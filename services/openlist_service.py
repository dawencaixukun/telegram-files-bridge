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
from urllib.parse import quote
from typing import Any, Dict, List, Optional, Tuple
import httpx
from fastapi.responses import JSONResponse
from core.config import OPENLIST_URL, _archive_norm_dir, _fmt_size
from core.state import (
    _OPENLIST, _openlist_client, _openlist_upload_client, _openlist_save, _archive_save, _ARCHIVE_CONFIG
)
from core.logging import log

_UPLOAD_CHUNK = 4 << 20  # 4MB 分块
# 历史值 1MB：流式写盘对每个分块做一次 await asyncio.to_thread(fh.write, chunk)，
# 10GB 文件 = 10240 次线程池往返。放大到 4MB 后降到约 1/4，内存峰值仍只有
# chunk × 并发数（_RETRIEVE_SEM 限 2）。上传侧读取同一常量，行为一致。

# 内存跨网盘复制/移动传输任务追踪器（供前端进度条轮询与 Telegram Bot 查询）
_CLOUD_TRANSFER_TASKS: Dict[str, Dict[str, Any]] = {}
_TRANSFER_TRACKER_TASKS: set = set()


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


_OPENLIST_VERIFY_CACHE: Dict[str, Any] = {"token": "", "expire": 0.0, "mock_id": None}


async def _openlist_verify_token(token: str) -> bool:
    """GET /api/me 验证 token（包络 code==200 即有效；Authorization 直传 JWT）。"""
    if not token:
        return False
    mock_get = _openlist_client.__dict__.get("get")
    now = time.monotonic()
    if (mock_get is None
            and _OPENLIST_VERIFY_CACHE.get("token") == token
            and _OPENLIST_VERIFY_CACHE.get("expire", 0.0) > now):
        return True
    try:
        resp = await _openlist_client.get("/api/me", headers={"Authorization": token})
    except Exception:  # noqa: BLE001
        return False
    try:
        ok = int(resp.json().get("code", 0)) == 200
        if ok and mock_get is None:
            _OPENLIST_VERIFY_CACHE["token"] = token
            _OPENLIST_VERIFY_CACHE["expire"] = now + 15.0
        return ok
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


async def _openlist_relocate_after_rename(token: str, remote_path: str,
                                          size_bytes: int = 0) -> Optional[str]:
    """按【同目录 + 相同文件大小】在云端重定位改名后的文件，返回新路径。

    场景：用户在 OpenList 网页里把文件改名 —— 归档记录里存的旧路径立刻失效
    （OpenList 按钮回落首页、取回失败）。改名不改内容，所以"同目录下唯一一个
    大小相同的文件"几乎必然就是它。

    返回：
      * 找到唯一匹配 -> 新路径（调用方应回写归档记录，实现自愈）
      * 找到 0 个或多个匹配 -> None（多匹配说明无法确定是哪个，宁可失败
        也不能静默改指向别的文件 —— 那是数据错乱）
    """
    raw = str(remote_path or "").strip().replace("\\", "/").lstrip("/")
    # raw 先 lstrip 再拼根：直接 normpath("/" + raw) 会让 POSIX 把开头的 "//"
    # 当成特殊双斜杠路径原样保留（实测 parent 变 '//onedrive/d'）。
    parent = posixpath.dirname(posixpath.normpath("/" + raw))
    if not parent:
        return None
    try:
        lst = await openlist_list_files(parent)
    except Exception as e:  # noqa: BLE001
        log.debug("改名重定位：列目录失败 %s: %s", parent, e)
        return None
    if not lst.get("ok"):
        return None
    size = int(size_bytes or 0)
    if size <= 0:
        return None
    cands = []
    for f in lst.get("files") or []:
        try:
            if int(f.get("size") or 0) == size:
                cands.append(str(f.get("name") or ""))
        except (TypeError, ValueError):
            continue
    old_name = posixpath.basename(norm_remote := posixpath.normpath("/" + raw))
    cands = [c for c in cands if c != old_name]
    if len(cands) == 1:
        new_path = posixpath.join(parent, cands[0])
        log.info("改名自愈：%s -> %s（按大小 %d 字节唯一匹配）", remote_path, new_path, size)
        return new_path
    if len(cands) > 1:
        log.info("改名重定位：同目录有 %d 个同大小文件，无法唯一确定，放弃", len(cands))
    return None


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


_ORIG_OPENLIST_DIRS = openlist_dirs
_OPENLIST_MOUNTS_CACHE: Dict[str, Any] = {"expire": 0.0, "value": None}


async def openlist_mounts() -> Dict[str, Any]:
    """列出 OpenList 根目录下的真实挂载网盘（云端归档页「网盘分类」的唯一数据源）。

    返回 {"ok": bool, "mounts": [name, ...], "message": str}。
    OpenList 未登录/不可达时 ok=False 且 mounts 为空，调用方应退化为
    仅按归档记录里真实出现过的网盘聚合，绝不回退到任何硬编码列表。
    """
    is_unmocked = (
        _openlist_client.__dict__.get("post") is None
        and openlist_dirs is _ORIG_OPENLIST_DIRS
    )
    now = time.monotonic()
    if is_unmocked and _OPENLIST_MOUNTS_CACHE["expire"] > now and _OPENLIST_MOUNTS_CACHE["value"]:
        return dict(_OPENLIST_MOUNTS_CACHE["value"])
    d = await openlist_dirs("/")
    if not d.get("ok"):
        return {"ok": False, "mounts": [], "message": str(d.get("message") or "")}
    mounts = [str(x.get("name") or "") for x in (d.get("dirs") or []) if x.get("name")]
    res = {"ok": True, "mounts": mounts, "message": ""}
    if is_unmocked and mounts:
        _OPENLIST_MOUNTS_CACHE["expire"] = now + 30.0
        _OPENLIST_MOUNTS_CACHE["value"] = res
    return res


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


DRIVER_LABELS: Dict[str, str] = {
    "onedrive": "OneDrive",
    "wopan": "沃家云盘",
    "googledrive": "Google Drive",
    "google": "Google Drive",
    "quark": "夸克网盘",
    "115": "115网盘",
    "115 open": "115网盘",
    "115 cloud": "115网盘",
    "123pan": "123云盘",
    "aliyundrive": "阿里云盘",
    "aliyundriveopen": "阿里云盘",
    "baidunetdisk": "百度网盘",
    "lanzou": "蓝奏云",
    "local": "本地存储",
    "webdav": "WebDAV",
    "sftp": "SFTP",
    "ftp": "FTP",
    "smb": "SMB",
}

VIDEO_EXTS = {"mp4", "mkv", "mov", "avi", "webm", "flv", "m4v", "ts", "wmv", "rmvb"}


def get_driver_label(driver_or_name: str) -> str:
    if not driver_or_name:
        return ""
    key = str(driver_or_name).strip().lower()
    return DRIVER_LABELS.get(key) or DRIVER_LABELS.get(key.replace(" ", "")) or str(driver_or_name).strip()


async def openlist_storages() -> List[Dict[str, Any]]:
    """获取 OpenList 挂载的存储列表（优先查 /api/admin/storage/list，降级到 openlist_mounts）。"""
    storages = []
    try:
        token = await _openlist_token()
        resp = await _openlist_client.get(
            "/api/admin/storage/list",
            headers={"Authorization": token}
        )
        code, data, _ = _openlist_env(resp)
        if resp.status_code == 401 or code in (401, 403):
            token = await _openlist_relogin()
            resp = await _openlist_client.get(
                "/api/admin/storage/list",
                headers={"Authorization": token}
            )
            code, data, _ = _openlist_env(resp)
        if code == 200 and isinstance(data, dict):
            for s in (data.get("content") or []):
                m_path = str(s.get("mount_path") or "")
                if not m_path.startswith("/"):
                    m_path = "/" + m_path
                clean_path = posixpath.normpath(m_path)
                m_name = clean_path.strip("/").split("/")[0] if clean_path.strip("/") else clean_path
                driver = str(s.get("driver") or "")
                status = str(s.get("status") or "work")
                remark = str(s.get("remark") or "")
                display = get_driver_label(driver) or get_driver_label(m_name) or m_name
                storages.append({
                    "mount_path": clean_path,
                    "name": m_name,
                    "driver": driver or m_name,
                    "display_name": display,
                    "status": status,
                    "status_label": "正常在线" if status == "work" else status,
                    "remark": remark,
                    "capacity_str": "就绪",
                    "openlist_url": _openlist_direct_url(clean_path),
                })
    except Exception as e:
        log.debug("读取 OpenList admin/storage/list 失败: %s", e)

    if not storages:
        m_res = await openlist_mounts()
        for m in (m_res.get("mounts") or []):
            clean_m = str(m or "").strip().lstrip("/")
            if not clean_m:
                continue
            display = get_driver_label(clean_m) or clean_m
            storages.append({
                "mount_path": f"/{clean_m}",
                "name": clean_m,
                "driver": clean_m.capitalize(),
                "display_name": display,
                "status": "work",
                "status_label": "正常在线",
                "remark": "",
                "capacity_str": "就绪",
                "openlist_url": _openlist_direct_url(f"/{clean_m}"),
            })
    return storages


async def openlist_fs_list_all(path: str = "/", refresh: bool = False) -> Dict[str, Any]:
    norm_path = posixpath.normpath("/" + str(path or "").strip().replace("\\", "/").lstrip("/"))
    if not norm_path or norm_path == ".":
        norm_path = "/"

    breadcrumbs = [{"name": "全部网盘", "path": "/", "is_last": norm_path == "/"}]
    if norm_path != "/":
        parts = [p for p in norm_path.strip("/").split("/") if p]
        curr = ""
        for i, p in enumerate(parts):
            curr += "/" + p
            breadcrumbs.append({
                "name": p,
                "path": curr,
                "is_last": (i == len(parts) - 1)
            })

    parent_path = "/"
    if norm_path != "/":
        parent_path = posixpath.dirname(norm_path)
        if not parent_path:
            parent_path = "/"

    try:
        token = await _openlist_token()
    except Exception as e:
        return {
            "ok": False,
            "path": norm_path,
            "parent_path": parent_path,
            "breadcrumbs": breadcrumbs,
            "entries": [],
            "dirs_count": 0,
            "files_count": 0,
            "total_size": 0,
            "total_size_human": "—",
            "message": str(e),
            "openlist_ready": False,
        }

    async def _list_call(tok: str):
        return await _openlist_client.post(
            "/api/fs/list",
            json={"path": norm_path, "password": "", "page": 1, "per_page": 0, "refresh": bool(refresh)},
            headers={"Authorization": tok}
        )

    try:
        resp = await _list_call(token)
        code, data, msg = _openlist_env(resp)
        if resp.status_code == 401 or code in (401, 403):
            token = await _openlist_relogin()
            resp = await _list_call(token)
            code, data, msg = _openlist_env(resp)
        if code != 200:
            return {
                "ok": False,
                "path": norm_path,
                "parent_path": parent_path,
                "breadcrumbs": breadcrumbs,
                "entries": [],
                "dirs_count": 0,
                "files_count": 0,
                "total_size": 0,
                "total_size_human": "—",
                "message": msg or f"OpenList 返回错误码 {code}",
                "openlist_ready": True,
            }

        content = data.get("content") or []
        dirs = []
        files = []
        total_size = 0

        top_drive = norm_path.strip("/").split("/")[0] if norm_path != "/" else ""
        top_drive_label = get_driver_label(top_drive) if top_drive else ""

        for item in content:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            is_dir = bool(item.get("is_dir"))
            size = int(item.get("size") or 0)
            item_path = posixpath.join(norm_path, name)
            item_thumb = str(item.get("thumb") or "")
            item_type = int(item.get("type") or (1 if is_dir else 0))
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            is_video = (item_type == 2) or (ext in VIDEO_EXTS)

            item_drive = top_drive or (name if norm_path == "/" else "")
            item_drive_label = top_drive_label or get_driver_label(item_drive)

            entry = {
                "name": name,
                "path": item_path,
                "is_dir": is_dir,
                "size": size,
                "size_human": "—" if is_dir else _fmt_size(size),
                "modified": str(item.get("modified") or item.get("created") or "")[:19].replace("T", " "),
                "thumb": item_thumb,
                "type": item_type,
                "is_video": is_video,
                "ext": ext.upper() if ext else ("DIR" if is_dir else "FILE"),
                "drive": item_drive,
                "drive_label": item_drive_label,
                "openlist_url": _openlist_direct_url(item_path),
            }
            if is_dir:
                dirs.append(entry)
            else:
                files.append(entry)
                total_size += size

        dirs.sort(key=lambda x: x["name"].lower())
        files.sort(key=lambda x: x["name"].lower())
        entries = dirs + files

        return {
            "ok": True,
            "path": norm_path,
            "parent_path": parent_path,
            "breadcrumbs": breadcrumbs,
            "entries": entries,
            "dirs_count": len(dirs),
            "files_count": len(files),
            "total_size": total_size,
            "total_size_human": _fmt_size(total_size),
            "write": bool(data.get("write", True)),
            "message": "",
            "openlist_ready": True,
        }
    except Exception as e:
        log.error("列出 OpenList 文件异常: %s", e)
        return {
            "ok": False,
            "path": norm_path,
            "parent_path": parent_path,
            "breadcrumbs": breadcrumbs,
            "entries": [],
            "dirs_count": 0,
            "files_count": 0,
            "total_size": 0,
            "total_size_human": "—",
            "message": f"连接 OpenList 失败: {e}",
            "openlist_ready": False,
        }


async def openlist_fs_rename_entry(path: str, new_name: str) -> Dict[str, Any]:
    norm_path = posixpath.normpath("/" + str(path or "").strip().replace("\\", "/").lstrip("/"))
    clean_new_name = str(new_name or "").strip()
    if not norm_path or norm_path == "/":
        return {"ok": False, "code": 400, "message": "无法重命名根目录"}
    if not clean_new_name:
        return {"ok": False, "code": 400, "message": "新名称不能为空"}
    if "/" in clean_new_name or "\\" in clean_new_name:
        return {"ok": False, "code": 400, "message": "名称中不得包含路径分隔符 / 或 \\"}

    old_name = posixpath.basename(norm_path)
    if old_name == clean_new_name:
        return {"ok": True, "code": 200, "message": "名称未变化"}

    try:
        token = await _openlist_token()
    except Exception as e:
        return {"ok": False, "code": 502, "message": f"OpenList 未就绪: {e}"}

    async def _rename_call(tok: str):
        return await _openlist_client.post(
            "/api/fs/rename",
            json={"path": norm_path, "name": clean_new_name},
            headers={"Authorization": tok}
        )

    try:
        resp = await _rename_call(token)
        code, data, msg = _openlist_env(resp)
        if resp.status_code == 401 or code in (401, 403):
            token = await _openlist_relogin()
            resp = await _rename_call(token)
            code, data, msg = _openlist_env(resp)
        if resp.status_code != 200 or code != 200:
            return {"ok": False, "code": code, "message": msg or f"重命名失败 (业务码 {code})"}

        new_path = posixpath.join(posixpath.dirname(norm_path), clean_new_name)
        try:
            from core.state import _ARCHIVE_JOBS, _archive_save
            updated = 0
            for jid, j in list(_ARCHIVE_JOBS.items()):
                if j.get("remote_path") == norm_path:
                    j["remote_path"] = new_path
                    j["filename"] = clean_new_name
                    updated += 1
            if updated:
                _archive_save()
        except Exception as e:
            log.debug("回写归档记录失败: %s", e)

        return {"ok": True, "code": 200, "message": f"已成功重命名为 {clean_new_name}", "new_path": new_path}
    except Exception as e:
        log.error("调用 OpenList fs/rename 异常: %s", e)
        return {"ok": False, "code": 500, "message": f"重命名异常: {e}"}


openlist_fs_rename = openlist_fs_rename_entry


async def _openlist_tasks_undone(kind: str = "copy") -> List[Dict[str, Any]]:
    """获取 OpenList 正在进行的后台任务列表（copy / move 等）。"""
    try:
        token = await _openlist_token()
    except Exception:
        return []
    url = f"/api/admin/task/{kind}/undone"
    try:
        resp = await _openlist_client.get(url, headers={"Authorization": token})
        if resp.status_code == 404:
            resp = await _openlist_client.get(f"/api/task/{kind}/undone", headers={"Authorization": token})
        code, data, _ = _openlist_env(resp)
        if code == 200 and isinstance(data, list):
            return data
    except Exception as e:
        log.debug("获取 OpenList %s 任务失败: %s", kind, e)
    return []


async def _openlist_tasks_done(kind: str = "copy") -> List[Dict[str, Any]]:
    """获取 OpenList 已完成的任务列表（copy / move 等）。"""
    try:
        token = await _openlist_token()
    except Exception:
        return []
    url = f"/api/admin/task/{kind}/done"
    try:
        resp = await _openlist_client.get(url, headers={"Authorization": token})
        if resp.status_code == 404:
            resp = await _openlist_client.get(f"/api/task/{kind}/done", headers={"Authorization": token})
        code, data, _ = _openlist_env(resp)
        if code == 200 and isinstance(data, list):
            return data
    except Exception as e:
        log.debug("获取 OpenList 已完成 %s 任务失败: %s", kind, e)
    return []


async def openlist_get_cloud_tasks() -> Dict[str, Any]:
    """汇总当前正在执行的跨网盘复制与移动任务（融合 OpenList 实时任务与本地追踪器）。"""
    copy_tasks = await _openlist_tasks_undone("copy")
    move_tasks = await _openlist_tasks_undone("move")
    active_local = [t for t in _CLOUD_TRANSFER_TASKS.values() if t.get("state") == "running"]
    return {
        "ok": True,
        "copy": copy_tasks,
        "move": move_tasks,
        "active_local": active_local,
        "total": len(copy_tasks) + len(move_tasks) + len(active_local),
    }


async def _track_cloud_task(action: str, src_dir: str, dst_dir: str, names: List[str]) -> None:
    """后台轮询 OpenList 任务队列，并在开始、完成或失败时向 Telegram Bot 派发通知。"""
    task_id = f"transfer_{int(time.time()*1000)}_{action}"
    task_record = {
        "id": task_id,
        "action": action,
        "src_dir": src_dir,
        "dst_dir": dst_dir,
        "names": names,
        "progress": 0.0,
        "state": "running",
        "started_at": time.time(),
        "updated_at": time.time(),
        "error": "",
    }
    _CLOUD_TRANSFER_TASKS[task_id] = task_record

    # 派发开始通知
    try:
        from services.notification_service import notify_cloud_transfer_start
        notify_cloud_transfer_start(action, src_dir, dst_dir, names)
    except Exception as e:
        log.debug("派发跨盘传输开始通知异常: %s", e)

    start_time = time.time()
    max_wait = 3600  # 1小时最长保护
    poll_interval = 1.5
    matched_openlist_task_id = None
    seen_running = False

    while (time.time() - start_time) < max_wait:
        await asyncio.sleep(poll_interval)
        try:
            undone = await _openlist_tasks_undone(action)
            found = None
            for t in undone:
                t_name = str(t.get("name") or "")
                for fname in names:
                    if fname in t_name or (matched_openlist_task_id and t.get("id") == matched_openlist_task_id):
                        found = t
                        matched_openlist_task_id = t.get("id")
                        break
                if found:
                    break

            if found:
                seen_running = True
                pct = float(found.get("progress") or 0.0)
                task_record["progress"] = pct
                task_record["updated_at"] = time.time()
                if pct >= 100.0:
                    break
            else:
                done_list = await _openlist_tasks_done(action)
                done_task = None
                for t in done_list:
                    t_name = str(t.get("name") or "")
                    for fname in names:
                        if fname in t_name or (matched_openlist_task_id and t.get("id") == matched_openlist_task_id):
                            done_task = t
                            break
                    if done_task:
                        break
                if done_task:
                    state = int(done_task.get("state") or 2)
                    err = str(done_task.get("error") or "")
                    if state == 2 or not err:
                        task_record["progress"] = 100.0
                        task_record["state"] = "succeeded"
                        break
                    else:
                        task_record["state"] = "failed"
                        task_record["error"] = err
                        break
                elif seen_running:
                    task_record["progress"] = 100.0
                    task_record["state"] = "succeeded"
                    break
                else:
                    if (time.time() - start_time) >= 6.0:
                        task_record["progress"] = 100.0
                        task_record["state"] = "succeeded"
                        break
        except Exception as e:
            log.debug("轮询 OpenList 传输任务异常: %s", e)

    elapsed = max(1, int(time.time() - start_time))
    task_record["elapsed"] = elapsed
    task_record["updated_at"] = time.time()

    # 派发完成或失败通知
    try:
        from services.notification_service import (
            notify_cloud_transfer_completed,
            notify_cloud_transfer_failed,
        )
        if task_record.get("state") == "failed":
            notify_cloud_transfer_failed(action, src_dir, dst_dir, names, task_record.get("error") or "任务执行失败")
        else:
            task_record["state"] = "succeeded"
            task_record["progress"] = 100.0
            notify_cloud_transfer_completed(action, src_dir, dst_dir, names, elapsed)
    except Exception as e:
        log.debug("派发跨盘传输结束通知异常: %s", e)

    # 内存留存 120 秒供前端展示 100% 完成
    await asyncio.sleep(120.0)
    _CLOUD_TRANSFER_TASKS.pop(task_id, None)


async def _openlist_fs_copy(src_dir: str, dst_dir: str, names: List[str]) -> Dict[str, Any]:
    """调用 OpenList /api/fs/copy 进行跨目录/跨网盘文件复制。

    OpenList 规范：POST /api/fs/copy {"src_dir": "...", "dst_dir": "...", "names": ["..."]}
    返回 {"ok": bool, "code": int, "data": Any, "message": str}。
    """
    s_dir = posixpath.normpath("/" + str(src_dir or "").strip().replace("\\", "/").lstrip("/"))
    d_dir = posixpath.normpath("/" + str(dst_dir or "").strip().replace("\\", "/").lstrip("/"))
    clean_names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not clean_names:
        return {"ok": False, "code": 400, "message": "缺少要复制的文件名"}
    if s_dir == d_dir:
        return {"ok": False, "code": 400, "message": "源目录与目标目录相同，无需复制"}
    try:
        token = await _openlist_token()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": 502, "message": f"OpenList 未就绪: {e}"}

    async def _copy_once(tok: str):
        return await _openlist_client.post(
            "/api/fs/copy",
            json={"src_dir": s_dir, "dst_dir": d_dir, "names": clean_names},
            headers={"Authorization": tok},
        )

    try:
        resp = await _copy_once(token)
        code, data, msg = _openlist_env(resp)
        if resp.status_code == 401 or code in (401, 403):
            token = await _openlist_relogin()
            resp = await _copy_once(token)
            code, data, msg = _openlist_env(resp)
        if resp.status_code != 200 or code != 200:
            return {"ok": False, "code": code, "message": msg or f"OpenList 返回业务码 {code}"}

        # 启动后台异步任务追踪与 Bot 通知
        trk_task = asyncio.create_task(_track_cloud_task("copy", s_dir, d_dir, clean_names))
        _TRANSFER_TRACKER_TASKS.add(trk_task)
        trk_task.add_done_callback(_TRANSFER_TRACKER_TASKS.discard)

        return {"ok": True, "code": 200, "data": data, "message": msg or "复制任务已提交"}
    except Exception as e:  # noqa: BLE001
        log.error("OpenList fs/copy 异常 (%s -> %s, %s): %s", s_dir, d_dir, clean_names, e)
        return {"ok": False, "code": 500, "message": f"复制失败: {e}"}


async def _openlist_fs_move(src_dir: str, dst_dir: str, names: List[str]) -> Dict[str, Any]:
    """调用 OpenList /api/fs/move 进行跨目录/跨网盘文件移动。

    OpenList 规范：POST /api/fs/move {"src_dir": "...", "dst_dir": "...", "names": ["..."]}
    返回 {"ok": bool, "code": int, "data": Any, "message": str}。
    """
    s_dir = posixpath.normpath("/" + str(src_dir or "").strip().replace("\\", "/").lstrip("/"))
    d_dir = posixpath.normpath("/" + str(dst_dir or "").strip().replace("\\", "/").lstrip("/"))
    clean_names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not clean_names:
        return {"ok": False, "code": 400, "message": "缺少要移动的文件名"}
    if s_dir == d_dir:
        return {"ok": False, "code": 400, "message": "源目录与目标目录相同，无需移动"}
    try:
        token = await _openlist_token()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": 502, "message": f"OpenList 未就绪: {e}"}

    async def _move_once(tok: str):
        return await _openlist_client.post(
            "/api/fs/move",
            json={"src_dir": s_dir, "dst_dir": d_dir, "names": clean_names},
            headers={"Authorization": tok},
        )

    try:
        resp = await _move_once(token)
        code, data, msg = _openlist_env(resp)
        if resp.status_code == 401 or code in (401, 403):
            token = await _openlist_relogin()
            resp = await _move_once(token)
            code, data, msg = _openlist_env(resp)
        if resp.status_code != 200 or code != 200:
            return {"ok": False, "code": code, "message": msg or f"OpenList 返回业务码 {code}"}

        # 启动后台异步任务追踪与 Bot 通知
        trk_task = asyncio.create_task(_track_cloud_task("move", s_dir, d_dir, clean_names))
        _TRANSFER_TRACKER_TASKS.add(trk_task)
        trk_task.add_done_callback(_TRANSFER_TRACKER_TASKS.discard)

        return {"ok": True, "code": 200, "data": data, "message": msg or "移动任务已提交"}
    except Exception as e:  # noqa: BLE001
        log.error("OpenList fs/move 异常 (%s -> %s, %s): %s", s_dir, d_dir, clean_names, e)
        return {"ok": False, "code": 500, "message": f"移动失败: {e}"}


openlist_fs_copy = _openlist_fs_copy
openlist_fs_move = _openlist_fs_move


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
