# -*- coding: utf-8 -*-
"""
services/m3u8_service.py — OmniSnag 远端 M3U8 下载桥接服务
=========================================================
负责 M3U8 播放列表探测解析、任务队列调度、分片流式下载、
AES-128 解密、ffmpeg 转封装/拼接合并、断点续传与持久化。
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.config import APP_ROOT_DIR, BASE_DIR

log = logging.getLogger("m3u8_service")

# ---------------------------------------------------------------------
# 常量与路径配置
# ---------------------------------------------------------------------
DOWNLOADS_DIR = os.path.join(APP_ROOT_DIR, "downloads")
_M3U8_TASKS_FILE = os.path.join(APP_ROOT_DIR, ".m3u8_tasks.json")

_M3U8_MAX_CONCURRENT_DOWNLOADS = 3
_M3U8_MAX_QUEUE = 50
_M3U8_SEGMENT_CONCURRENCY = 6

_M3U8_TASKS: Dict[str, Dict[str, Any]] = {}
_M3U8_ACTIVE_TASKS: Dict[str, asyncio.Task] = {}
_M3U8_SUBPROCESSES: Dict[str, asyncio.subprocess.Process] = {}
_M3U8_CANCELLED_SET = set()
_M3U8_SEMAPHORE: Optional[asyncio.Semaphore] = None

# 限流记录：token -> [timestamps]
_RATE_LIMITS: Dict[str, List[float]] = {}


# ---------------------------------------------------------------------
# 1. Token 鉴权管理
# ---------------------------------------------------------------------
def _ext_token_path() -> str:
    if os.environ.get("EXT_TOKEN_FILE"):
        return os.environ["EXT_TOKEN_FILE"]
    base_token = os.path.join(BASE_DIR, ".ext_token")
    if os.path.exists(base_token):
        return base_token
    app_data_token = os.path.join(APP_ROOT_DIR, ".ext_token")
    if os.path.exists(app_data_token):
        return app_data_token
    return base_token


def get_or_create_ext_token() -> str:
    """读取或创建插件 Token（权限 0600）。"""
    path = _ext_token_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                t = f.read().strip()
                if t:
                    return t
        except Exception as e:
            log.warning("读取 .ext_token 失败: %s", e)

    token = secrets.token_urlsafe(32)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        # 兼容 app-data 目录
        alt_path = os.path.join(APP_ROOT_DIR, ".ext_token")
        if alt_path != path:
            try:
                fd_alt = os.open(alt_path, flags, 0o600)
                with os.fdopen(fd_alt, "w", encoding="utf-8") as f_alt:
                    f_alt.write(token + "\n")
            except Exception:
                pass
    except Exception as e:
        log.error("创建 .ext_token 失败: %s", e)
    return token


def verify_ext_token(provided_token: str) -> bool:
    """比对 X-Ext-Token（常数时间防时序攻击）。"""
    if not provided_token or not isinstance(provided_token, str):
        return False
    expected = get_or_create_ext_token()
    if not expected:
        return False
    return hmac.compare_digest(provided_token.strip(), expected.strip())


# ---------------------------------------------------------------------
# 2. SSRF 护栏与限流
# ---------------------------------------------------------------------
def _is_blocked_ip(ip: Any) -> bool:
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def is_ssrf_safe_url(url: str) -> Tuple[bool, str]:
    """SSRF 护栏：解析目标域名后拒绝私网/环回/链路本地/保留地址，仅允许 http(s)。"""
    if os.environ.get("EXT_M3U8_ALLOW_LOCAL"):
        return True, ""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as e:
        return False, f"无效的 URL 格式: {e}"

    if parsed.scheme.lower() not in ("http", "https"):
        return False, "只允许 http:// 或 https:// 协议"

    hostname = parsed.hostname
    if not hostname:
        return False, "缺少有效的主机名"

    try:
        ip = ipaddress.ip_address(hostname)
        if _is_blocked_ip(ip):
            return False, "目标地址被 SSRF 护栏拦截（禁止访问私网/环回/保留地址）"
    except ValueError:
        try:
            addr_infos = socket.getaddrinfo(hostname, None)
            if not addr_infos:
                return False, "域名解析失败（未获得有效 IP）"
            for addr in addr_infos:
                ip_str = addr[4][0]
                ip = ipaddress.ip_address(ip_str)
                if _is_blocked_ip(ip):
                    return False, "目标地址被 SSRF 护栏拦截（禁止访问私网/环回/保留地址）"
        except socket.gaierror as e:
            return False, f"域名解析失败: {e}"
        except Exception as e:
            return False, f"域名解析异常: {e}"
    return True, ""


def check_ext_rate_limit(token: str, max_requests: int = 120, window: float = 60.0) -> bool:
    """限流：每 Token 60 秒 120 次。"""
    now = time.time()
    times = _RATE_LIMITS.setdefault(token, [])
    cutoff = now - window
    times[:] = [t for t in times if t > cutoff]
    if len(times) >= max_requests:
        return False
    times.append(now)
    return True


# ---------------------------------------------------------------------
# 3. M3U8 解析逻辑
# ---------------------------------------------------------------------
def _parse_m3u8_attrs(attr_str: str) -> Dict[str, str]:
    res = {}
    for k, v1, v2 in re.findall(r"([A-Z0-9\-]+)=(?:\"([^\"]*)\"|([^,]*))", attr_str):
        res[k] = v1 if v1 != "" else v2
    return res


def parse_m3u8_content(content: str, base_url: str) -> Dict[str, Any]:
    """解析 M3U8 内容：区分 master 与 media 变体。"""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        variants = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("#EXT-X-STREAM-INF:"):
                clean_attrs = _parse_m3u8_attrs(line[len("#EXT-X-STREAM-INF:"):])
                uri = None
                j = i + 1
                while j < len(lines):
                    if not lines[j].startswith("#"):
                        uri = lines[j]
                        i = j
                        break
                    j += 1
                if uri:
                    full_url = urllib.parse.urljoin(base_url, uri)
                    bw = int(clean_attrs.get("BANDWIDTH", 0) or 0)
                    res = clean_attrs.get("RESOLUTION", "")
                    codecs = clean_attrs.get("CODECS", "")
                    name = clean_attrs.get("NAME") or res or (f"{bw // 1000}k" if bw else f"variant_{len(variants) + 1}")
                    variants.append({
                        "url": full_url,
                        "name": name,
                        "resolution": res,
                        "bandwidth": bw,
                        "codecs": codecs
                    })
            i += 1
        return {"kind": "master", "variants": variants}

    segments = []
    durations = []
    encrypted = False
    init_segment = False

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXT-X-KEY:"):
            clean_attrs = _parse_m3u8_attrs(line[len("#EXT-X-KEY:"):])
            method = clean_attrs.get("METHOD", "").upper()
            if method and method != "NONE":
                encrypted = True
        elif line.startswith("#EXT-X-MAP:"):
            init_segment = True
        elif line.startswith("#EXTINF:"):
            inf = line[len("#EXTINF:"):].split(",")[0].strip()
            try:
                durations.append(float(inf))
            except Exception:
                durations.append(0.0)
            j = i + 1
            while j < len(lines):
                if not lines[j].startswith("#"):
                    seg_uri = lines[j]
                    if ".m4s" in seg_uri.lower():
                        init_segment = True
                    segments.append(seg_uri)
                    i = j
                    break
                j += 1
        i += 1

    return {
        "kind": "media",
        "url": base_url,
        "segments": len(segments),
        "duration_secs": round(sum(durations), 2),
        "encrypted": encrypted,
        "init_segment": init_segment
    }


def _build_upstream_headers(headers: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    out = {}
    if headers and isinstance(headers, dict):
        ref = headers.get("referer") or headers.get("Referer")
        ua = headers.get("user_agent") or headers.get("User-Agent")
        if ref:
            out["Referer"] = str(ref)
        if ua:
            out["User-Agent"] = str(ua)
    if "User-Agent" not in out:
        out["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    return out


async def resolve_m3u8(url: str, headers: Optional[Dict[str, Any]] = None) -> Tuple[bool, int, Dict[str, Any]]:
    """探测解析 M3U8 目标地址。返回 (success, status_code, data_or_error_dict)。"""
    safe, reason = is_ssrf_safe_url(url)
    if not safe:
        return False, 400, {"ok": False, "message": reason}

    req_headers = _build_upstream_headers(headers)
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, trust_env=False) as client:
            resp = await client.get(url, headers=req_headers)
            if resp.status_code >= 400:
                return False, 502, {"ok": False, "message": f"上游服务器返回 HTTP {resp.status_code}"}
            content = resp.text
    except Exception as e:
        return False, 502, {"ok": False, "message": f"连接上游失败: {e}"}

    if not content or ("#EXTM3U" not in content and "#EXT-X-" not in content):
        return False, 502, {"ok": False, "message": "上游响应内容不是合法的 M3U8 播放列表"}

    try:
        parsed = parse_m3u8_content(content, url)
        res = {"ok": True}
        res.update(parsed)
        return True, 200, res
    except Exception as e:
        return False, 502, {"ok": False, "message": f"解析 M3U8 格式失败: {e}"}


# ---------------------------------------------------------------------
# 4. 任务状态持久化与管理
# ---------------------------------------------------------------------
def _m3u8_tasks_load() -> None:
    global _M3U8_TASKS
    if not os.path.exists(_M3U8_TASKS_FILE):
        _M3U8_TASKS = {}
        return
    try:
        with open(_M3U8_TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                _M3U8_TASKS = {t["id"]: t for t in data if isinstance(t, dict) and "id" in t}
            elif isinstance(data, dict):
                items = data.get("tasks", data)
                if isinstance(items, list):
                    _M3U8_TASKS = {t["id"]: t for t in items if isinstance(t, dict) and "id" in t}
                elif isinstance(items, dict):
                    _M3U8_TASKS = items
        # 重启后将未完成任务标记为中断
        changed = False
        for t in _M3U8_TASKS.values():
            if t.get("state") in ("queued", "running"):
                t["state"] = "failed"
                t["error"] = "服务重启，任务已中断"
                t["updated_at"] = time.time()
                changed = True
        if changed:
            _m3u8_tasks_save()
    except Exception as e:
        log.warning("读取 .m3u8_tasks.json 失败: %s", e)
        _M3U8_TASKS = {}


def _m3u8_tasks_save() -> None:
    try:
        os.makedirs(os.path.dirname(_M3U8_TASKS_FILE), exist_ok=True)
        tmp = f"{_M3U8_TASKS_FILE}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"tasks": list(_M3U8_TASKS.values())}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _M3U8_TASKS_FILE)
    except Exception as e:
        log.warning("保存 .m3u8_tasks.json 失败: %s", e)


def _clean_old_m3u8_tasks(keep_count: int = 100, max_age_days: int = 7) -> None:
    now = time.time()
    max_age_secs = max_age_days * 86400
    finished = [
        t for t in _M3U8_TASKS.values()
        if t.get("state") in ("done", "failed", "cancelled")
    ]
    finished.sort(key=lambda t: t.get("updated_at", 0), reverse=True)
    to_delete = set()
    for idx, t in enumerate(finished):
        age = now - t.get("updated_at", now)
        if idx >= keep_count or age > max_age_secs:
            to_delete.add(t["id"])
    if to_delete:
        for tid in to_delete:
            _M3U8_TASKS.pop(tid, None)
        _m3u8_tasks_save()


# ---------------------------------------------------------------------
# 5. 文件名清洗与查重
# ---------------------------------------------------------------------
def _clean_m3u8_filename(title: str, url: str) -> str:
    name = (title or "").strip()
    if not name:
        parsed = urllib.parse.urlparse(url)
        path_name = os.path.basename(parsed.path)
        if path_name and path_name.lower().endswith(".m3u8"):
            name = path_name[:-5]
        elif path_name:
            name = path_name
        else:
            name = "m3u8_video"
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(". ")
    name = re.sub(r"_+", "_", name)
    if not name:
        name = "m3u8_video"
    if not name.lower().endswith(".mp4"):
        name += ".mp4"
    return name


def _resolve_unique_filepath(downloads_dir: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    candidate = filename
    counter = 1
    while os.path.exists(os.path.join(downloads_dir, candidate)):
        candidate = f"{base}_{counter}{ext}"
        counter += 1
    return os.path.join(downloads_dir, candidate)


# ---------------------------------------------------------------------
# 6. AES-128 解密助手
# ---------------------------------------------------------------------
def _decrypt_aes_128(data: bytes, key: bytes, iv: bytes) -> bytes:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(data) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        return unpadder.update(decrypted) + unpadder.finalize()
    except Exception:
        return decrypted


# ---------------------------------------------------------------------
# 7. 任务下载与合并核心 Worker
# ---------------------------------------------------------------------
def _get_semaphore() -> asyncio.Semaphore:
    global _M3U8_SEMAPHORE
    if _M3U8_SEMAPHORE is None:
        _M3U8_SEMAPHORE = asyncio.Semaphore(_M3U8_MAX_CONCURRENT_DOWNLOADS)
    return _M3U8_SEMAPHORE


async def _download_worker(task_id: str) -> None:
    sem = _get_semaphore()
    async with sem:
        task = _M3U8_TASKS.get(task_id)
        if not task or task.get("state") == "cancelled" or task_id in _M3U8_CANCELLED_SET:
            return

        task["state"] = "running"
        task["updated_at"] = time.time()
        _m3u8_tasks_save()

        url = task["url"]
        headers = task.get("headers") or {}
        req_headers = _build_upstream_headers(headers)

        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        tmp_dir = os.path.join(DOWNLOADS_DIR, f".m3u8_tmp_{task_id}")
        os.makedirs(tmp_dir, exist_ok=True)

        try:
            # 1. 获取最新 playlist
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, trust_env=False) as client:
                resp = await client.get(url, headers=req_headers)
                if resp.status_code >= 400:
                    raise RuntimeError(f"拉取播放列表失败: HTTP {resp.status_code}")
                content = resp.text

            # 若为 master 播放列表，自动选择最高清晰度变体
            parsed = parse_m3u8_content(content, url)
            if parsed.get("kind") == "master":
                variants = parsed.get("variants") or []
                if not variants:
                    raise RuntimeError("主播放列表无有效清晰度变体")
                variants.sort(key=lambda v: (v.get("bandwidth") or 0), reverse=True)
                target_url = variants[0]["url"]
                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, trust_env=False) as client:
                    resp = await client.get(target_url, headers=req_headers)
                    if resp.status_code >= 400:
                        raise RuntimeError(f"拉取变体列表失败: HTTP {resp.status_code}")
                    content = resp.text
                url = target_url

            # 2. 逐行详细解析 segments 与 keys
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            segment_items = []
            init_item = None
            media_seq = 0
            current_key_raw = None
            durations = []

            i = 0
            while i < len(lines):
                line = lines[i]
                if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    try:
                        media_seq = int(line.split(":", 1)[1])
                    except Exception:
                        pass
                elif line.startswith("#EXT-X-KEY:"):
                    current_key_raw = _parse_m3u8_attrs(line[len("#EXT-X-KEY:"):])
                elif line.startswith("#EXT-X-MAP:"):
                    map_attrs = _parse_m3u8_attrs(line[len("#EXT-X-MAP:"):])
                    init_uri = map_attrs.get("URI")
                    if init_uri:
                        init_item = urllib.parse.urljoin(url, init_uri)
                elif line.startswith("#EXTINF:"):
                    inf = line[len("#EXTINF:"):].split(",")[0].strip()
                    try:
                        dur = float(inf)
                    except Exception:
                        dur = 0.0
                    durations.append(dur)
                    j = i + 1
                    while j < len(lines):
                        if not lines[j].startswith("#"):
                            seg_uri = lines[j]
                            seq_idx = media_seq + len(segment_items)
                            segment_items.append({
                                "idx": len(segment_items),
                                "url": urllib.parse.urljoin(url, seg_uri),
                                "seq": seq_idx,
                                "key_info": dict(current_key_raw) if current_key_raw else None,
                            })
                            i = j
                            break
                        j += 1
                i += 1

            if not segment_items:
                raise RuntimeError("播放列表未包含有效媒体分片")

            task["total_segments"] = len(segment_items)
            task["duration_secs"] = round(sum(durations), 2)
            task["encrypted"] = any(s.get("key_info") and s["key_info"].get("METHOD", "").upper() == "AES-128" for s in segment_items)
            task["init_segment"] = bool(init_item) or any(".m4s" in s["url"].lower() for s in segment_items)
            _m3u8_tasks_save()

            # 3. 密钥拉取与缓存
            key_cache: Dict[str, bytes] = {}
            for item in segment_items:
                kinfo = item.get("key_info")
                if kinfo and kinfo.get("METHOD", "").upper() == "AES-128":
                    kuri = kinfo.get("URI")
                    if kuri:
                        abs_kurl = urllib.parse.urljoin(url, kuri)
                        if abs_kurl not in key_cache:
                            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, trust_env=False) as client:
                                kresp = await client.get(abs_kurl, headers=req_headers)
                                if kresp.status_code != 200:
                                    raise RuntimeError(f"拉取解密密钥失败: HTTP {kresp.status_code}")
                                key_cache[abs_kurl] = kresp.content

            # 4. 下载 init 分片（若存在）
            init_file = None
            if init_item:
                init_file = os.path.join(tmp_dir, "init.mp4")
                if not (os.path.exists(init_file) and os.path.getsize(init_file) > 0):
                    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, trust_env=False) as client:
                        iresp = await client.get(init_item, headers=req_headers)
                        if iresp.status_code != 200:
                            raise RuntimeError(f"拉取 init 分片失败: HTTP {iresp.status_code}")
                        with open(init_file, "wb") as f:
                            f.write(iresp.content)

            # 5. 并发下载媒体分片（支持断点续传）
            seg_sem = asyncio.Semaphore(_M3U8_SEGMENT_CONCURRENCY)
            downloaded_bytes = 0
            done_segments = 0
            last_speed_time = time.time()
            last_speed_bytes = 0

            # 扫描已下载的分片（用于续传）
            for item in segment_items:
                sfile = os.path.join(tmp_dir, f"seg_{item['idx']:06d}.ts")
                if os.path.exists(sfile) and os.path.getsize(sfile) > 0:
                    done_segments += 1
                    downloaded_bytes += os.path.getsize(sfile)

            task["done_segments"] = done_segments
            task["downloaded_bytes"] = downloaded_bytes
            _m3u8_tasks_save()

            async def _fetch_segment(item: Dict[str, Any]):
                nonlocal downloaded_bytes, done_segments, last_speed_time, last_speed_bytes
                if task_id in _M3U8_CANCELLED_SET or task.get("state") == "cancelled":
                    raise asyncio.CancelledError()

                sfile = os.path.join(tmp_dir, f"seg_{item['idx']:06d}.ts")
                if os.path.exists(sfile) and os.path.getsize(sfile) > 0:
                    return

                async with seg_sem:
                    if task_id in _M3U8_CANCELLED_SET or task.get("state") == "cancelled":
                        raise asyncio.CancelledError()

                    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, trust_env=False) as client:
                        sresp = await client.get(item["url"], headers=req_headers)
                        if sresp.status_code != 200:
                            raise RuntimeError(f"分片 {item['idx']} 下载失败: HTTP {sresp.status_code}")
                        data = sresp.content

                    kinfo = item.get("key_info")
                    if kinfo and kinfo.get("METHOD", "").upper() == "AES-128":
                        abs_kurl = urllib.parse.urljoin(url, kinfo.get("URI", ""))
                        key = key_cache.get(abs_kurl)
                        if not key:
                            raise RuntimeError("未找到分片对应的解密密钥")
                        iv_raw = kinfo.get("IV")
                        if iv_raw:
                            iv_clean = iv_raw[2:] if iv_raw.lower().startswith("0x") else iv_raw
                            iv = bytes.fromhex(iv_clean)
                        else:
                            iv = item["seq"].to_bytes(16, "big")
                        data = _decrypt_aes_128(data, key, iv)

                    tmp_sfile = f"{sfile}.tmp.{os.getpid()}"
                    with open(tmp_sfile, "wb") as sf:
                        sf.write(data)
                    os.replace(tmp_sfile, sfile)

                    downloaded_bytes += len(data)
                    done_segments += 1
                    now = time.time()
                    if now - last_speed_time >= 1.0:
                        speed = int((downloaded_bytes - last_speed_bytes) / (now - last_speed_time) * 8)
                        task["speed_bps"] = max(0, speed)
                        last_speed_bytes = downloaded_bytes
                        last_speed_time = now

                    task["done_segments"] = done_segments
                    task["downloaded_bytes"] = downloaded_bytes
                    task["updated_at"] = now

            await asyncio.gather(*[_fetch_segment(s) for s in segment_items])

            # 6. 合并分片
            out_filename = task.get("filename") or _clean_m3u8_filename(task.get("title", ""), url)
            final_out_path = _resolve_unique_filepath(DOWNLOADS_DIR, out_filename)
            task["filename"] = os.path.basename(final_out_path)

            if task.get("init_segment"):
                # fMP4: 直接字节拼接 init + 分片
                with open(final_out_path, "wb") as out_f:
                    if init_file and os.path.exists(init_file):
                        with open(init_file, "rb") as init_f:
                            shutil.copyfileobj(init_f, out_f)
                    for s in segment_items:
                        sfile = os.path.join(tmp_dir, f"seg_{s['idx']:06d}.ts")
                        with open(sfile, "rb") as sf:
                            shutil.copyfileobj(sf, out_f)
            else:
                # TS 分片：通过 ffmpeg 拼接
                concat_path = os.path.join(tmp_dir, "concat.txt")
                with open(concat_path, "w", encoding="utf-8") as cf:
                    for s in segment_items:
                        sname = f"seg_{s['idx']:06d}.ts"
                        cf.write(f"file '{sname}'\n")

                ffmpeg_bin = "/usr/bin/ffmpeg" if os.path.exists("/usr/bin/ffmpeg") else "ffmpeg"
                cmd = [
                    ffmpeg_bin, "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", concat_path,
                    "-c", "copy",
                    "-movflags", "+faststart",
                    final_out_path
                ]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=tmp_dir
                )
                _M3U8_SUBPROCESSES[task_id] = proc
                stdout, stderr = await proc.communicate()
                _M3U8_SUBPROCESSES.pop(task_id, None)

                if proc.returncode != 0:
                    err_msg = stderr.decode(errors="replace")[:300]
                    log.warning(f"ffmpeg -c copy 退出码 {proc.returncode}，尝试重编码降级合并: {err_msg}")
                    cmd_fallback = [
                        ffmpeg_bin, "-y",
                        "-f", "concat",
                        "-safe", "0",
                        "-i", concat_path,
                        "-c:v", "libx264",
                        "-c:a", "aac",
                        "-movflags", "+faststart",
                        final_out_path
                    ]
                    proc_fb = await asyncio.create_subprocess_exec(
                        *cmd_fallback,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=tmp_dir
                    )
                    _M3U8_SUBPROCESSES[task_id] = proc_fb
                    stdout_fb, stderr_fb = await proc_fb.communicate()
                    _M3U8_SUBPROCESSES.pop(task_id, None)
                    if proc_fb.returncode != 0:
                        raise RuntimeError(f"ffmpeg 合并失败: {stderr_fb.decode(errors='replace')[:200]}")

            if not os.path.exists(final_out_path) or os.path.getsize(final_out_path) == 0:
                raise RuntimeError("合并输出文件生成失败或为空")

            task["state"] = "done"
            task["output_size"] = os.path.getsize(final_out_path)
            task["speed_bps"] = 0
            task["error"] = ""
            task["updated_at"] = time.time()
            shutil.rmtree(tmp_dir, ignore_errors=True)
            _clean_old_m3u8_tasks()
            _m3u8_tasks_save()

        except asyncio.CancelledError:
            task["state"] = "cancelled"
            task["error"] = "用户取消"
            task["speed_bps"] = 0
            task["updated_at"] = time.time()
            _m3u8_tasks_save()
        except Exception as e:
            task["state"] = "failed"
            task["error"] = str(e)
            task["speed_bps"] = 0
            task["updated_at"] = time.time()
            _m3u8_tasks_save()
        finally:
            _M3U8_ACTIVE_TASKS.pop(task_id, None)
            _M3U8_SUBPROCESSES.pop(task_id, None)
            _M3U8_CANCELLED_SET.discard(task_id)


# ---------------------------------------------------------------------
# 8. 对外业务接口封装
# ---------------------------------------------------------------------
async def submit_m3u8_task(url: str, headers: Optional[Dict[str, Any]] = None, title: str = "") -> Tuple[Dict[str, Any], bool]:
    """提交下载任务，返回 (task, is_duplicate)。"""
    norm_url = url.strip()
    # 查重：同一 url 已有活跃或成功任务则复用
    for t in _M3U8_TASKS.values():
        if t.get("url") == norm_url and t.get("state") in ("queued", "running", "done"):
            return t, True

    task_id = secrets.token_hex(6)
    filename = _clean_m3u8_filename(title, norm_url)

    task = {
        "id": task_id,
        "url": norm_url,
        "title": title or "",
        "filename": filename,
        "state": "queued",
        "total_segments": 0,
        "done_segments": 0,
        "downloaded_bytes": 0,
        "speed_bps": 0,
        "duration_secs": 0.0,
        "encrypted": False,
        "init_segment": False,
        "output_size": 0,
        "error": "",
        "headers": headers or {},
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    _M3U8_TASKS[task_id] = task
    _m3u8_tasks_save()

    t = asyncio.create_task(_download_worker(task_id))
    _M3U8_ACTIVE_TASKS[task_id] = t
    return task, False


def get_m3u8_tasks(limit: int = 50) -> Dict[str, Any]:
    """获取任务列表（进行中优先）。"""
    tasks = list(_M3U8_TASKS.values())
    active_count = sum(1 for t in tasks if t.get("state") in ("queued", "running"))

    def _sort_key(t):
        st = t.get("state", "")
        # running > queued > others
        prio = 0 if st == "running" else (1 if st == "queued" else 2)
        return (prio, -t.get("updated_at", 0))

    tasks.sort(key=_sort_key)
    paged = tasks[:limit]

    res_tasks = []
    for t in paged:
        res_tasks.append({
            "id": t.get("id"),
            "filename": t.get("filename") or "未命名任务",
            "state": t.get("state") or "queued",
            "total_segments": t.get("total_segments") or 0,
            "done_segments": t.get("done_segments") or 0,
            "downloaded_bytes": t.get("downloaded_bytes") or 0,
            "speed_bps": t.get("speed_bps") or 0,
            "duration_secs": t.get("duration_secs") or 0.0,
            "encrypted": bool(t.get("encrypted")),
            "output_size": t.get("output_size") or 0,
            "error": t.get("error") or ""
        })
    return {"ok": True, "active": active_count, "tasks": res_tasks}


async def cancel_m3u8_task(task_id: str) -> bool:
    """取消下载任务。"""
    task = _M3U8_TASKS.get(task_id)
    if not task:
        return False
    if task.get("state") not in ("queued", "running"):
        return False

    _M3U8_CANCELLED_SET.add(task_id)
    t = _M3U8_ACTIVE_TASKS.get(task_id)
    if t and not t.done():
        t.cancel()

    proc = _M3U8_SUBPROCESSES.get(task_id)
    if proc:
        try:
            proc.kill()
        except Exception:
            pass

    task["state"] = "cancelled"
    task["error"] = "用户取消"
    task["speed_bps"] = 0
    task["updated_at"] = time.time()
    _m3u8_tasks_save()
    return True


async def retry_m3u8_task(task_id: str) -> bool:
    """重试已失败或已取消的任务（支持断点续传）。"""
    task = _M3U8_TASKS.get(task_id)
    if not task:
        return False
    if task.get("state") not in ("failed", "cancelled"):
        return False

    _M3U8_CANCELLED_SET.discard(task_id)
    task["state"] = "queued"
    task["error"] = ""
    task["speed_bps"] = 0
    task["updated_at"] = time.time()
    _m3u8_tasks_save()

    t = asyncio.create_task(_download_worker(task_id))
    _M3U8_ACTIVE_TASKS[task_id] = t
    return True


# 模块导入时初始化
_m3u8_tasks_load()
get_or_create_ext_token()
