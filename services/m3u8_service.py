# -*- coding: utf-8 -*-
"""
services/m3u8_service.py — 浏览器插件 M3U8(HLS) 下载引擎
=========================================================
为 Chrome 插件提供 m3u8 资源解析/提交/下载能力：
  解析(master/AES-128/fMP4/BYTERANGE) → 并发分片下载(断点续传) → 解密 → 合并
  → 自动创建归档任务，复用现有 OpenList 上传管线（与 TG 文件同等对待）。

安全护栏（对齐既往安全审计口径）：
  * 仅 http/https；DNS 解析结果含私网/环回/链路本地/保留地址一律拒绝（SSRF）。
    测试可通过模块级 _ALLOW_PRIVATE_TARGETS 临时放行回环假源站。
  * 手动跟随重定向并逐跳复检 SSRF（httpx follow_redirects=False）。
  * 分片数 / 总字节数 / 活动任务数上限，防资源耗尽。
  * 插件 Token 鉴权（常量时间比较）在 routers/extension.py，本模块只管存取。
"""
import os
import re
import time
import hmac
import shutil
import socket
import hashlib
import secrets
import asyncio
import ipaddress
import subprocess
from urllib.parse import urljoin, urlsplit
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.config import (
    _EXT_TOKEN_FILE, M3U8_DOWNLOAD_DIR, M3U8_CONCURRENCY, M3U8_MAX_ACTIVE,
    M3U8_MAX_SEGMENTS, M3U8_MAX_TOTAL_BYTES, M3U8_SEG_RETRIES, M3U8_REMUX_MP4,
    M3U8_MAX_PLAYLIST_BYTES,
    _clean_archive_filename, _ensure_archive_ext, _archive_norm_dir, _archive_join,
    _is_safe_subpath,
)
from core.state import (
    _M3U8_TASKS, _m3u8_save, _ARCHIVE_CONFIG,
    _ARCHIVE_JOBS, _archive_save, _archive_active_of,
)
# _ARCHIVE_TASKS（归档 worker 的 asyncio.Task 注册表）定义在归档服务模块，
# 不在 core/state —— 归档管线自身的归属地。此处只借用同一字典对象。
from services.archive_service import _ARCHIVE_TASKS
from core.logging import log

# SSRF 测试钩子：仅测试进程在 setUp 里显式置 True（放行回环假源站）。
_ALLOW_PRIVATE_TARGETS = False

# ffmpeg 二进制缓存（None=未探测；False=探测过且不存在）
_FFMPEG_BIN: Optional[str] = None

# 下载运行器注册表：task_id -> asyncio.Task（用于取消）
_M3U8_RUNNERS: Dict[str, asyncio.Task] = {}

# 插件 Token 进程内缓存（None=尚未从磁盘加载）
_EXT_TOKEN_CACHE: Optional[str] = None

# 单分片体积上限（防止异常大分片击穿内存）
_M3U8_MAX_SEGMENT_BYTES = 256 * 1024 * 1024

# 下载器默认 UA（防盗链站点常校验 UA；插件可在提交时覆盖）
_M3U8_DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class M3u8Error(Exception):
    """携带用户可读信息的 m3u8 处理错误（直接透出到 API message）。"""


# =====================================================================
# 1. m3u8 解析（手写轻量解析器，纯函数，可单测）
# =====================================================================

_ATTR_RE = re.compile(r'([A-Z0-9\-]+)=("[^"]*"|[^\s,]+)')


def _parse_attrs(s: str) -> Dict[str, str]:
    """解析 HLS 属性表（KEY=VALUE，VALUE 可带引号，逗号分隔）。"""
    out: Dict[str, str] = {}
    for m in _ATTR_RE.finditer(s or ""):
        key = m.group(1).upper()
        val = m.group(2)
        if val.startswith('"') and val.endswith('"') and len(val) >= 2:
            val = val[1:-1]
        out[key] = val
    return out


def _parse_byterange(spec: Optional[str], uri: str,
                     offsets: Dict[str, int]) -> Optional[Tuple[int, int]]:
    """#EXT-X-BYTERANGE: <n>[@<o>] → (start, length)。

    @o 缺省时按 HLS 规则从同 URI 上一段的末尾续算（RFC 8216）。
    """
    if not spec:
        return None
    spec = spec.strip()
    try:
        if "@" in spec:
            n_s, o_s = spec.split("@", 1)
            start, length = int(o_s), int(n_s)
        else:
            length = int(spec)
            start = offsets.get(uri, 0)
        offsets[uri] = start + length
        return (start, length)
    except (ValueError, TypeError):
        raise M3u8Error("无法解析 EXT-X-BYTERANGE: %s" % spec)


def _parse_m3u8(text: str, base_url: str) -> Dict[str, Any]:
    """把 m3u8 文本解析为结构化描述。

    返回：
      {"kind": "master", "variants": [{url, bandwidth, resolution, name}, ...]}
      {"kind": "media", "segments": [{uri, duration, range, enc, seq}, ...],
       "init_url", "encrypted", "duration", "media_sequence"}

    直播流（无 #EXT-X-ENDLIST）与 SAMPLE-AES 加密直接抛 M3u8Error。
    """
    text = (text or "").lstrip("\ufeff").strip()
    if not text.startswith("#EXTM3U"):
        raise M3u8Error("不是有效的 m3u8（缺少 #EXTM3U 头）")

    variants: List[Dict[str, Any]] = []
    segments: List[Dict[str, Any]] = []
    enc: Optional[Dict[str, str]] = None
    init_url: Optional[str] = None
    has_endlist = False
    media_sequence = 0
    seg_duration = 0.0
    total_duration = 0.0
    pending_inf: Optional[Dict[str, str]] = None
    pending_range: Optional[str] = None
    range_offsets: Dict[str, int] = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending_inf = _parse_attrs(line.split(":", 1)[1])
            continue
        if pending_inf is not None and not line.startswith("#"):
            variants.append({
                "url": urljoin(base_url, line),
                "bandwidth": _attr_int(pending_inf.get("BANDWIDTH")),
                "resolution": pending_inf.get("RESOLUTION", ""),
                "name": pending_inf.get("NAME") or pending_inf.get("RESOLUTION", ""),
            })
            pending_inf = None
            continue
        if line.startswith("#EXT-X-KEY:") or line.startswith("#EXT-X-SESSION-KEY:"):
            attrs = _parse_attrs(line.split(":", 1)[1])
            method = (attrs.get("METHOD") or "NONE").upper()
            if method == "NONE":
                enc = None
            elif method == "AES-128":
                if not attrs.get("URI"):
                    raise M3u8Error("AES-128 KEY 缺少 URI 属性")
                enc = {"method": "AES-128",
                       "key_url": urljoin(base_url, attrs["URI"]),
                       "iv": (attrs.get("IV") or "").lower()}
            elif "SAMPLE" in method:
                raise M3u8Error("SAMPLE-AES 加密暂不支持（仅支持 AES-128）")
            else:
                raise M3u8Error("不支持的加密方式: %s" % method)
            continue
        if line.startswith("#EXT-X-MAP:"):
            attrs = _parse_attrs(line.split(":", 1)[1])
            if attrs.get("BYTERANGE"):
                raise M3u8Error("EXT-X-MAP BYTERANGE 暂不支持")
            if not attrs.get("URI"):
                raise M3u8Error("EXT-X-MAP 缺少 URI")
            init_url = urljoin(base_url, attrs["URI"])
            continue
        if line.startswith("#EXT-X-ENDLIST"):
            has_endlist = True
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1].strip())
            except ValueError:
                media_sequence = 0
            continue
        if line.startswith("#EXTINF:"):
            try:
                seg_duration = float(line.split(":", 1)[1].split(",")[0] or 0)
            except (ValueError, IndexError):
                seg_duration = 0.0
            continue
        if line.startswith("#EXT-X-BYTERANGE:"):
            pending_range = line.split(":", 1)[1]
            continue
        if line.startswith("#"):
            continue  # 其余标签对本下载器无意义
        # ---- URI 行 ----
        uri = urljoin(base_url, line)
        segments.append({
            "uri": uri,
            "duration": seg_duration,
            "range": _parse_byterange(pending_range, uri, range_offsets),
            "enc": dict(enc) if enc else None,
            "seq": media_sequence + len(segments),
        })
        total_duration += seg_duration
        seg_duration = 0.0
        pending_range = None

    if variants and not segments:
        return {"kind": "master", "variants": variants}
    if not segments:
        raise M3u8Error("m3u8 中没有可下载的分片")
    if not has_endlist:
        raise M3u8Error("直播流（无 #EXT-X-ENDLIST）暂不支持，仅支持点播回放")
    return {
        "kind": "media",
        "segments": segments,
        "init_url": init_url,
        "encrypted": any(s["enc"] for s in segments),
        "duration": round(total_duration, 2),
        "media_sequence": media_sequence,
    }


def _attr_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# =====================================================================
# 2. SSRF 防护（DNS 解析级）
# =====================================================================

def _assert_public_http_url(url: str) -> None:
    """仅放行解析到公网地址的 http/https URL，其余一律 M3u8Error。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise M3u8Error("URL 无法解析")
    if parts.scheme not in ("http", "https"):
        raise M3u8Error("仅支持 http/https 链接")
    host = parts.hostname
    if not host:
        raise M3u8Error("URL 缺少主机名")
    # parts.port 对非法端口（:99999 / :abc）会抛 ValueError，必须一并收敛成
    # M3u8Error —— 否则会冒泡成 500 / 任务失败原因是 "ValueError"。
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise M3u8Error("URL 端口非法")
    if _ALLOW_PRIVATE_TARGETS:
        return
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as e:
        raise M3u8Error("域名解析失败: %s" % host) from e
    for info in infos:
        ip_s = info[4][0]
        try:
            addr = ipaddress.ip_address(ip_s)
        except ValueError:
            continue
        # IPv4-mapped IPv6（::ffff:127.0.0.1）的 is_loopback 为 False，必须拆出 v4 复检
        mapped = addr.ipv4_mapped if addr.version == 6 else None
        candidates = (addr,) if mapped is None else (addr, mapped)
        for a in candidates:
            if (a.is_private or a.is_loopback or a.is_link_local
                    or a.is_reserved or a.is_multicast or a.is_unspecified):
                raise M3u8Error("拒绝访问内网/保留地址（SSRF 防护）: %s" % host)


# =====================================================================
# 3. 插件 Token 存取（设置页生成/重置，插件请求头 X-Ext-Token）
# =====================================================================

def _token_generate() -> str:
    return secrets.token_urlsafe(24)


def _token_write(token: str) -> None:
    parent = os.path.dirname(_EXT_TOKEN_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(_EXT_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)


def ext_token_get() -> str:
    """懒加载并缓存插件 Token；首次调用时自动生成。"""
    global _EXT_TOKEN_CACHE
    if _EXT_TOKEN_CACHE:
        return _EXT_TOKEN_CACHE
    try:
        with open(_EXT_TOKEN_FILE, "rb") as f:
            tok = f.read().decode("utf-8").strip()
        if not tok:
            raise FileNotFoundError(_EXT_TOKEN_FILE)
        _EXT_TOKEN_CACHE = tok
    except FileNotFoundError:
        _EXT_TOKEN_CACHE = _token_generate()
        _token_write(_EXT_TOKEN_CACHE)
    except Exception as e:  # noqa: BLE001
        log.error("插件 Token 读取失败（每次进程重启都会重新生成）: %s", e)
        _EXT_TOKEN_CACHE = _token_generate()
    return _EXT_TOKEN_CACHE


def ext_token_reset() -> str:
    """生成新 Token 并落盘（旧 Token 立即失效）。返回新 Token。"""
    global _EXT_TOKEN_CACHE
    _EXT_TOKEN_CACHE = _token_generate()
    try:
        _token_write(_EXT_TOKEN_CACHE)
    except Exception as e:  # noqa: BLE001
        log.error("插件 Token 写盘失败（仅本次进程内有效）: %s", e)
    return _EXT_TOKEN_CACHE


def ext_token_verify(token: str) -> bool:
    if not token:
        return False
    return hmac.compare_digest(str(token), ext_token_get())


# =====================================================================
# 4. 任务登记与视图
# =====================================================================

def _safe_title(title: str) -> str:
    """把 title 收敛为安全的文件名片段。

    关键：title 最终会拼成成品文件名并写入分片目录（out_path = join(seg_dir, name)）。
    若 title 含目录分隔符或盘符（`..\\..\\x`、`C:\\Windows\\x`、`/tmp/x`），
    os.path.join 会丢弃 seg_dir，把成品写到任意位置 —— 这是任意文件写入。
    这里只保留最后一段路径分量，并消解 `..`；扩展名由调用方按容器类型决定。
    """
    s = str(title or "").replace("\\", "/").strip()
    s = s.rsplit("/", 1)[-1]        # 丢弃全部目录分量（含盘符前缀）
    s = s.strip(" ._-—#")
    while ".." in s:
        s = s.replace("..", "__")
    s = s.strip(" ._-—#")
    return s[:120]


def _new_task(url: str, title: str, headers: Dict[str, str],
              remote_dir: str = "") -> Dict[str, Any]:
    now = time.time()
    return {
        "id": secrets.token_hex(6),
        "url": url,
        "title": _safe_title(title),
        "filename": "",
        "state": "queued",          # queued|running|done|failed|cancelled
        "error": "",
        "total_segments": 0,
        "done_segments": 0,
        "downloaded_bytes": 0,
        "speed_bps": 0.0,
        "encrypted": False,
        "duration_secs": 0.0,
        # headers 入库前先白名单化 + 截断：原始 dict 无上限落盘，会被
        # 塞入超长无用头把 .m3u8_tasks.json 撑大（每条最多 500 条 × N MB）。
        "headers": _client_headers(headers),
        "output_path": "",
        "output_size": 0,
        "archived_job_id": "",
        # 插件可在提交时指定归档目标目录（已归一化）；空 = 用设置页默认目录。
        "remote_dir": remote_dir,
        "created_at": now,
        "updated_at": now,
        "finished_at": 0.0,
        "_speed_mark_bytes": 0,
        "_speed_mark_ts": now,
    }


def _m3u8_public(t: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": t.get("id"),
        "url": t.get("url"),
        "title": t.get("title"),
        "filename": t.get("filename"),
        "state": t.get("state"),
        "error": t.get("error"),
        "total_segments": t.get("total_segments"),
        "done_segments": t.get("done_segments"),
        "downloaded_bytes": t.get("downloaded_bytes"),
        "speed_bps": round(float(t.get("speed_bps") or 0), 1),
        "encrypted": bool(t.get("encrypted")),
        "duration_secs": t.get("duration_secs"),
        "output_size": t.get("output_size"),
        "archived_job_id": t.get("archived_job_id"),
        "remote_dir": t.get("remote_dir") or "",
        "created_at": t.get("created_at"),
        "updated_at": t.get("updated_at"),
        "finished_at": t.get("finished_at"),
    }


def _m3u8_bump_speed(t: Dict[str, Any], nbytes: int) -> None:
    """分片完成时推进 EMA 速率（写入 task，随任务表持久化）。"""
    now = time.time()
    mark_t = float(t.get("_speed_mark_ts") or 0)
    mark_b = int(t.get("_speed_mark_bytes") or 0)
    dt = now - mark_t
    if dt >= 0.8:
        inst = max(0, int(t["downloaded_bytes"]) - mark_b) / dt
        t["speed_bps"] = round(0.7 * float(t.get("speed_bps") or 0) + 0.3 * inst, 1)
        t["_speed_mark_ts"] = now
        t["_speed_mark_bytes"] = int(t["downloaded_bytes"])


def _seg_dir_of(t: Dict[str, Any]) -> str:
    return os.path.join(M3U8_DOWNLOAD_DIR, str(t.get("id") or "unknown"))


# 分片文件命名（与下载端写入时一致）：仅这两个模式允许被回收。
# 分片与其原子写残片（_write_atomic 先写 .part 再 rename）。用 [0-9] 而非 \d：
# Python 的 \d 默认匹配 Unicode 十进制数字，会误命中 seg_١٢٣٤٥.bin 这类文件名。
_SEG_NAME_RE = re.compile(r"^(?:seg_[0-9]{5}\.bin|init\.bin)(?:\.part)?$")


def _seg_dir_ok(t: Dict[str, Any]) -> str:
    """校验并返回任务分片目录的绝对真实路径；不合格返回空串。

    红线：目录必须落在 M3U8_DOWNLOAD_DIR 内（且不是根目录本身），目录名必须
    等于 task id。任务表由磁盘恢复（setdefault 不校验 id），任何删除/回收动作
    都必须先过这一关，否则被篡改的 id 会让操作越出下载根目录。
    """
    root = os.path.realpath(M3U8_DOWNLOAD_DIR)
    real = os.path.realpath(_seg_dir_of(t))
    if (not real or real == root or not _is_safe_subpath(real, root)
            or os.path.basename(real) != str(t.get("id") or "")):
        return ""
    return real


def _cleanup_segments(t: Dict[str, Any], remove_dir: bool = False) -> int:
    """回收任务分片，返回释放的字节数。

    分片只在「任务未完成、可续传」时有价值：一旦合并出成品，seg_*.bin 就是
    纯占用（约 1× 成品体积）。此前从不回收，磁盘只涨不消，最终被高水位门禁
    挡住新任务。

    红线（任何一条不满足即整体放弃）：
      * seg_dir 的真实路径必须落在 M3U8_DOWNLOAD_DIR 内，且目录名 == task id；
      * 只删除 _SEG_NAME_RE 命中的普通文件，绝不碰任何其他文件；
      * 成品（output_path）永不在此删除——它由归档管线的 deleteLocal 负责。
    """
    real = _seg_dir_ok(t)
    if not real:
        return 0
    try:
        names = os.listdir(real)
    except OSError:
        return 0
    keep = os.path.realpath(str(t.get("output_path"))) if t.get("output_path") else ""
    freed = 0
    for nm in names:
        if not _SEG_NAME_RE.match(nm):
            continue
        p = os.path.join(real, nm)
        if os.path.islink(p) or not os.path.isfile(p):
            continue
        if keep and os.path.realpath(p) == keep:
            continue
        try:
            freed += int(os.path.getsize(p))
            os.remove(p)
        except OSError:
            continue
    if remove_dir:
        try:
            # 只删空目录：成品若仍在（如 deleteLocal 未生效），os.listdir 非空，
            # 这里就不会动手，绝不误删用户还需要的成品。无法删除的 .part 残片
            # 已被上面的正则一并清掉，不会把目录永久卡成非空。
            if not os.listdir(real):
                os.rmdir(real)
        except OSError:
            pass
    return freed


# 运行器登记表：task_id -> [generation, asyncio.Task]。
# generation 用于解决「旧协程收尾期间又发起重试」的竞态：新 runner 递增代数，
# 旧协程 finally 只在代数仍属自己时才清理表项，绝不误删新 runner。
def _start_runner(task_id: str) -> bool:
    t = _M3U8_TASKS.get(task_id)
    if t is None:
        return False
    old = _M3U8_RUNNERS.get(task_id)
    if old is not None and old[1] is not None and not old[1].done():
        # 旧协程仍在跑（可能正处于收尾的 await 中）：不能静默返回，
        # 否则调用方以为已重启，任务却永远停在 queued。
        return False
    gen = (old[0] + 1) if old else 1
    _M3U8_RUNNERS[task_id] = [gen, asyncio.create_task(_run_m3u8_task(task_id, gen))]
    return True


def _disk_watermark_check() -> None:
    """磁盘高水位门禁：超限则拒绝新的 m3u8 下载任务。

    与 TG 下载路径（_disk_guard_or_enqueue）同一口径取自
    _ARCHIVE_CONFIG 的高低水位阈值。TG 侧超限是「挂起排队」，而 m3u8
    的资源占用模式不同（合并期会双份占盘），因此直接拒绝并给出可读原因，
    让插件/用户择时重试，避免把宿主盘写满拖垮归档与冷备。
    """
    from services.watermark_service import _is_disk_high_watermark_exceeded
    exceeded, cur, high = _is_disk_high_watermark_exceeded()
    if exceeded:
        raise M3u8Error(
            "本地磁盘使用率 %.1f%% 已达高水位 %.0f%%，已暂停接收新的网页下载任务；"
            "请清理磁盘后再试" % (cur, high))


async def m3u8_submit(url: str, title: str = "",
                      headers: Optional[Dict[str, str]] = None,
                      remote_dir: str = "") -> Dict[str, Any]:
    """提交一个 m3u8 下载任务（活动任务数上限 + 同 URL 幂等）。

    remote_dir：插件可选的归档目标目录（OpenList 绝对路径）。必须归一化通
    过 _archive_norm_dir，否则直接拒绝——绝不把未校验的字符串写进归档 job
    的 remote_path（那是上传目标，路径穿越等于写到别的网盘目录）。
    """
    url = str(url or "").strip()
    if not url or len(url) > 2048:
        raise M3u8Error("URL 为空或超长")
    norm_dir = ""
    raw_dir = str(remote_dir or "").strip()
    if raw_dir:
        norm_dir = _archive_norm_dir(raw_dir) or ""
        if not norm_dir or norm_dir == "/":
            raise M3u8Error("归档目录格式无效（需以 / 开头且指向具体网盘子目录，不能为根目录 /）")
    _assert_public_http_url(url)
    _disk_watermark_check()
    active = [t for t in _M3U8_TASKS.values() if t.get("state") in ("queued", "running")]
    if len(active) >= M3U8_MAX_ACTIVE:
        raise M3u8Error("进行中的下载任务已达上限（%d），请稍后再试" % M3U8_MAX_ACTIVE)
    for t in active:
        if t.get("url") == url:
            return {"task": t, "duplicate": True}
    task = _new_task(url, title or "", headers or {}, norm_dir)
    _M3U8_TASKS[task["id"]] = task
    _m3u8_save()
    _start_runner(task["id"])
    log.info("M3U8 下载任务已提交: %s (%s)", title or url, task["id"])
    return {"task": task, "duplicate": False}

async def m3u8_cancel(task_id: str) -> bool:
    t = _M3U8_TASKS.get(str(task_id or ""))
    if not t or t.get("state") not in ("queued", "running"):
        return False
    # 状态先落定再取消协程：任务若尚未真正开始执行（仍 queued），
    # task.cancel() 会让协程体根本不运行 —— 它的 CancelledError 分支与
    # finally 都不会执行，状态会永久停在 queued。此处必须主动改状态。
    t["state"] = "cancelled"
    t["error"] = "用户已取消"
    t["updated_at"] = t["finished_at"] = time.time()
    t["speed_bps"] = 0.0
    _m3u8_save()
    # 注意：这里**不**摘除 _M3U8_RUNNERS —— cancel() 只在下一次 await 处投递
    # CancelledError，协程体（以及 asyncio.to_thread 的子线程）还会继续跑一段。
    # 若在此处 pop，则紧随其后的「删除 / 重试」会读到 None 而误判「协程已收尾」：
    # 删除可能在飞行写入中清目录（残留分片 + 孤儿目录），重试会与新 runner
    # 并发写同一批 seg_*.bin。登记表统一由 runner 的 finally 按代数自行清理。
    entry = _M3U8_RUNNERS.get(t["id"])
    if entry is not None and len(entry) > 1 and entry[1] is not None and not entry[1].done():
        entry[1].cancel()
    return True


async def m3u8_retry(task_id: str) -> bool:
    t = _M3U8_TASKS.get(str(task_id or ""))
    if not t or t.get("state") not in ("failed", "cancelled"):
        return False
    active = [x for x in _M3U8_TASKS.values() if x.get("state") in ("queued", "running")]
    if len(active) >= M3U8_MAX_ACTIVE:
        raise M3u8Error("进行中的下载任务已达上限（%d）" % M3U8_MAX_ACTIVE)
    # 旧协程可能仍未收尾（正卡在 client.aclose() 等 await 上）。此时若把状态
    # 改成 queued 却启动不了新 runner，任务会永久停在 queued 并吃掉一个活动名额。
    # 先确保旧 runner 已结束：仍存活则拒绝本次重试并给出明确原因。
    entry = _M3U8_RUNNERS.get(t["id"])
    if entry is not None and entry[1] is not None and not entry[1].done():
        entry[1].cancel()
        try:
            await asyncio.wait_for(asyncio.shield(entry[1]), timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass
        _M3U8_RUNNERS.pop(t["id"], None)
    t["state"] = "queued"
    t["error"] = ""
    t["updated_at"] = time.time()
    _m3u8_save()
    if not _start_runner(t["id"]):
        # 兜底：启动失败就回滚状态，绝不留下「queued 但无人执行」的僵尸任务
        t["state"] = "failed"
        t["error"] = "无法启动下载协程（上一次任务仍在收尾），请稍后重试"
        t["updated_at"] = time.time()
        _m3u8_save()
        raise M3u8Error("上一次任务仍在收尾，请稍后重试")
    return True


async def m3u8_delete(task_id: str) -> Dict[str, Any]:
    """删除一个终态任务：回收分片目录 + 从任务表移除记录。

    只允许终态（done/failed/cancelled）——进行中的任务必须先取消，否则会
    出现「记录没了、协程还在写 seg_*.bin」的孤儿写入。

    两道安全闸门（都可能导致不可恢复的数据丢失）：
      * runner 协程若仍未收尾，删除会与它的写入/`_m3u8_save()` 竞争，任务
        可能被复活或文件被写到一半 —— 一律拒绝，让用户稍后重试。
      * 若该任务的归档 job 仍在上传中，删掉本地成品会让上传中途失败、而云端
        又没有完整副本 —— 此时**只回收分片，保留成品**。

    返回 {"ok", "message", "freed"}。
    """
    tid = str(task_id or "").strip()
    t = _M3U8_TASKS.get(tid)
    if not t:
        return {"ok": False, "message": "任务不存在", "freed": 0}
    if t.get("state") not in ("done", "failed", "cancelled"):
        return {"ok": False, "message": "任务仍在进行中，请先取消再删除", "freed": 0}
    entry = _M3U8_RUNNERS.get(tid)
    if entry is not None and len(entry) > 1 and entry[1] is not None and not entry[1].done():
        return {"ok": False, "message": "任务协程仍在收尾，请稍后再试", "freed": 0}
    # 归档上传中：成品是云端唯一数据源，绝不能删；而「只删分片 + 摘掉记录」
    # 会让成品变成没有任何 UI 入口可回收的孤儿文件，却对用户谎报「已删除」。
    # 因此直接拒绝，等上传核验完成后用户再删。
    job_id = str(t.get("archived_job_id") or "")
    if job_id:
        j = _ARCHIVE_JOBS.get(job_id)
        if j is not None and str(j.get("state") or "") in ("queued", "uploading"):
            # 提示可在归档页取消：否则用户会以为「等多久都删不掉」。
            return {"ok": False, "freed": 0,
                    "message": "该任务正在归档上传中，请等上传完成后再删除（也可在归档页取消该上传）"}
    freed = await asyncio.to_thread(_cleanup_segments, t, True)
    out = str(t.get("output_path") or "")
    # 与 _cleanup_segments 复用同一红线校验（目录必须落在下载根目录内、目录名
    # 等于 task id）。任务表是从磁盘 .m3u8_tasks.json 恢复的（setdefault 不校验
    # id），只有通过这道校验才敢照着 output_path 删文件。
    seg_dir = _seg_dir_ok(t)
    if out and seg_dir:
        real_out = os.path.realpath(out)
        # real_out != seg_dir：成品绝不能是目录本身；islink/isfile 双检排除软链。
        if (_is_safe_subpath(real_out, seg_dir) and real_out != seg_dir
                and os.path.isfile(real_out) and not os.path.islink(real_out)):
            try:
                freed += int(os.path.getsize(real_out))
                os.remove(real_out)
            except OSError:
                pass
    if seg_dir:
        try:
            if os.path.isdir(seg_dir) and not os.listdir(seg_dir):
                os.rmdir(seg_dir)
        except OSError:
            pass
    _M3U8_TASKS.pop(tid, None)
    _m3u8_save()
    log.info("M3U8 任务已删除并回收 %d 字节: %s", freed, tid)
    return {"ok": True, "message": "已删除", "freed": freed}




async def m3u8_resolve(url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """探测 m3u8：master 返回清晰度列表；media 返回分片数/时长/加密信息。"""
    url = str(url or "").strip()
    if not url or len(url) > 2048:
        raise M3u8Error("URL 为空或超长")
    _assert_public_http_url(url)
    hdrs = _client_headers(headers)
    async with _make_client(hdrs) as client:
        text = await _fetch_text(client, url)
    parsed = _parse_m3u8(text, url)
    if parsed["kind"] == "master":
        return {"kind": "master", "variants": parsed["variants"]}
    return {
        "kind": "media",
        "segments": len(parsed["segments"]),
        "duration_secs": parsed["duration"],
        "encrypted": parsed["encrypted"],
        "init_segment": bool(parsed.get("init_url")),
        "message": "可直接提交下载" if not parsed["encrypted"] else "AES-128 加密流，将自动取密钥解密",
    }


# =====================================================================
# 5. HTTP 客户端与抓取（手动重定向 + 逐跳 SSRF 复检）
# =====================================================================

def _client_headers(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    """白名单透传插件头：referer / user_agent（带浏览器默认 UA 兜底）。"""
    h = {"user-agent": _M3U8_DEFAULT_UA, "accept": "*/*"}
    if headers:
        ref = str(headers.get("referer") or "").strip()[:2048]
        ua = str(headers.get("user_agent") or headers.get("user-agent") or "").strip()[:512]
        if ref:
            h["referer"] = ref
        if ua:
            h["user-agent"] = ua
    return h


def _make_client(extra_headers: Optional[Dict[str, str]] = None) -> httpx.AsyncClient:
    # trust_env=False：绝不走系统代理环境变量（与本仓库 BackendClient 同口径），
    # 同时保证 SSRF 防护不受代理转发绕过。
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
        trust_env=False,
        headers=extra_headers or {},
    )


async def _fetch_response(client: httpx.AsyncClient, method: str, url: str,
                          headers: Optional[Dict[str, str]] = None,
                          max_redirects: int = 5) -> httpx.Response:
    current = url
    for _ in range(max_redirects):
        _assert_public_http_url(current)
        resp = await client.request(method, current, headers=headers or None)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location", "")
            resp.close()
            if not loc:
                raise M3u8Error("重定向缺少 Location")
            current = urljoin(current, loc)
            continue
        return resp
    raise M3u8Error("重定向次数过多（>%d）" % max_redirects)


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str:
    """拉取播放列表文本（带长度上限，防止超大响应打爆内存）。"""
    raw = await _fetch_bytes_bounded(client, url, None, M3U8_MAX_PLAYLIST_BYTES, ok_codes=(200,))
    return raw.decode("utf-8", errors="replace")


async def _fetch_bytes_bounded(client: httpx.AsyncClient, url: str,
                               headers: Optional[Dict[str, str]],
                               limit: int, ok_codes: Tuple[int, ...] = (200, 206),
                               max_redirects: int = 5) -> bytes:
    """流式 GET：边读边计数，超过 limit 立即中止。

    httpx 的 resp.content 会把整包读进内存后才返回，届时再比对大小已经晚了
    （并发下载异常大分片 → 进程 OOM）。这里一律走 stream 逐块累积。
    重定向仍逐跳复检 SSRF（本仓库既有的防护口径）。
    """
    current = url
    for _ in range(max_redirects):
        _assert_public_http_url(current)
        async with client.stream("GET", current, headers=headers or None) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location", "")
                if not loc:
                    raise M3u8Error("重定向缺少 Location")
                current = urljoin(current, loc)
                continue
            if resp.status_code not in ok_codes:
                raise M3u8Error("HTTP %d" % resp.status_code)
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > limit:
                    raise M3u8Error("响应体超过上限（%d MB）" % (limit // 1024 // 1024))
            return bytes(buf)
    raise M3u8Error("重定向次数过多（>%d）" % max_redirects)


# =====================================================================
# 6. 下载 / 解密 / 合并
# =====================================================================

def _iv_bytes(enc: Dict[str, str], seq: int) -> bytes:
    """推导分片 IV（RFC 8216）。

    只有 KEY 未提供 IV 属性时才回落到 media sequence number。若提供了 IV
    却解析失败/长度不对，必须直接报错 —— 静默回落会用错 IV 解出垃圾数据，
    而任务仍会标成 done 并归档，属于「静默数据损坏」。
    """
    iv = str(enc.get("iv") or "").strip()
    if not iv:
        return seq.to_bytes(16, "big")
    hexpart = iv[2:] if iv.lower().startswith("0x") else iv
    try:
        raw = bytes.fromhex(hexpart)
    except ValueError:
        raise M3u8Error("EXT-X-KEY 的 IV 不是合法十六进制: %s" % iv[:40])
    if len(raw) != 16:
        raise M3u8Error("EXT-X-KEY 的 IV 长度必须为 16 字节（实际 %d）" % len(raw))
    return raw


def _write_atomic(path: str, data: bytes) -> None:
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _aes_unpad(data: bytes) -> bytes:
    """剥掉 HLS AES-128 分片的 PKCS7 填充（RFC 8216 规定分片必须 PKCS7 填充）。

    注意：cryptography 的 CipherContext.finalize() 只做「块对齐校验」，
    **不会**剥除 PKCS7 填充 —— 填充必须由调用方自己处理，否则每个分片尾部
    都会残留 1~16 字节填充，拼接进成品就是可见的损坏。
    填充不合法时按原样返回（容忍少数不填充的非标准流），绝不因填充异常丢数据。
    """
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and len(data) >= pad and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def _aes_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if len(key) != 16:
        raise M3u8Error("AES-128 密钥长度异常（%d 字节）" % len(key))
    if not data or len(data) % 16 != 0:
        raise M3u8Error("加密分片长度异常（%d 字节，非 16 倍数）" % len(data))
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return _aes_unpad(dec.update(data) + dec.finalize())


async def _download_one(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                        seg: Dict[str, Any], dest_path: str,
                        t: Dict[str, Any], key_cache: Dict[str, bytes]) -> None:
    async with sem:
        enc = seg.get("enc")
        rng = seg.get("range")
        last_err: Optional[Exception] = None
        # 断点续传：磁盘上已有非空分片则直接复用，不再请求源站
        # （重试 / bridge 重启恢复后，只补缺失分片）。
        try:
            if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                return
        except OSError:
            pass
        for attempt in range(1, max(1, M3U8_SEG_RETRIES) + 1):
            try:
                headers: Dict[str, str] = {}
                if rng:
                    headers["Range"] = "bytes=%d-%d" % (rng[0], rng[0] + rng[1] - 1)
                # 流式读取 + 边读边限长：绝不能让异常大分片先整包进内存
                # （6 路并发各持一份 → 进程 OOM，殃及所有用户）。
                data = await _fetch_bytes_bounded(
                    client, seg["uri"], headers, _M3U8_MAX_SEGMENT_BYTES)
                if rng:
                    # 带 Range 的请求必须得到 206（200 = 源站不支持 Range，
                    # 返回的是整个文件；按分片写入会让成品静默错位/重复）。
                    if len(data) != rng[1]:
                        raise M3u8Error(
                            "Range 响应长度不符（期望 %d，实际 %d）" % (rng[1], len(data)))
                if enc:
                    key = key_cache.get(enc["key_url"])
                    if not key:
                        raise M3u8Error("密钥缺失")
                    data = await asyncio.to_thread(_aes_decrypt, data, key, _iv_bytes(enc, seg["seq"]))
                await asyncio.to_thread(_write_atomic, dest_path, data)
                t["done_segments"] = int(t.get("done_segments") or 0) + 1
                t["downloaded_bytes"] = int(t.get("downloaded_bytes") or 0) + len(data)
                _m3u8_bump_speed(t, len(data))
                if int(t.get("downloaded_bytes") or 0) > M3U8_MAX_TOTAL_BYTES:
                    raise M3u8Error("超过单任务体积上限（%dGB），已中止" % (M3U8_MAX_TOTAL_BYTES // 1024**3))
                return
            except M3u8Error as e:
                if str(e).startswith("超过单任务体积上限"):
                    raise
                last_err = e
            except Exception as e:  # noqa: BLE001
                last_err = e
            if attempt >= max(1, M3U8_SEG_RETRIES):
                break
            await asyncio.sleep(0.5 * attempt)
        raise M3u8Error("分片下载失败(%s): %s" % (seg["uri"][:160], last_err))


def _merge_files(paths: List[str], out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as out:
        for p in paths:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out, 1024 * 1024)


def _find_ffmpeg() -> Optional[str]:
    global _FFMPEG_BIN
    if _FFMPEG_BIN is None:
        _FFMPEG_BIN = shutil.which("ffmpeg") or False or ""  # 探测一次并缓存
        if not _FFMPEG_BIN:
            _FFMPEG_BIN = ""
    return _FFMPEG_BIN or None


async def _remux_with_ffmpeg(src: str, dst: str, ffmpeg: str) -> bool:
    """TS → MP4 无损 remux（stream copy）。失败返回 False，调用方回退 .ts。"""
    def _run() -> int:
        try:
            return subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", src,
                 "-c", "copy", "-movflags", "+faststart", dst],
                timeout=1800,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode
        except Exception:  # noqa: BLE001
            return -1
    rc = await asyncio.to_thread(_run)
    return rc == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0


def _derive_title(t: Dict[str, Any]) -> str:
    title = str(t.get("title") or "").strip()
    if title:
        return title
    try:
        tail = urlsplit(t["url"]).path.rstrip("/").rpartition("/")[2]
    except Exception:  # noqa: BLE001
        tail = ""
    tail = tail.rsplit("?", 1)[0].rsplit(".", 1)[0] if tail else ""
    return tail or ("m3u8-" + hashlib.sha1(str(t.get("url")).encode()).hexdigest()[:8])


def _register_archive_job(t: Dict[str, Any]) -> Optional[str]:
    """下载完成 → 按归档 job 结构入队，复用现有 OpenList 上传管线。"""
    from services.archive_service import _archive_worker  # 局部导入避免环

    uid = "m3u8-" + hashlib.sha1(str(t.get("url")).encode()).hexdigest()[:16]
    lp = str(t.get("output_path") or "")
    # 复用已有在途 job 前必须比对本地路径：同一 URL 二次下载会是新 task、新
    # seg_dir（可能新 title），若只按 URL 哈希去重，第二个任务会被挂上别人的
    # job id，而它自己那份文件永远不会被上传。
    active = _archive_active_of(uid)
    if active is not None and str(active.get("local_path") or "") == lp:
        t["archived_job_id"] = str(active.get("id") or "")
        return t["archived_job_id"]
    # 目录优先级：插件提交时指定的 remote_dir > 设置页默认目录 > /m3u8。
    # 必须剔除 "/"：_archive_norm_dir("/") 会返回真值 "/"，若只靠 or 链，"根目录"
    # 会被当成合法目标而把成品上传到网盘根（submit/archive-config 都显式拒绝
    # "/"，但 _archive_config_load 从磁盘恢复配置时并不校验）。
    def _norm_dir_or_empty(x: Any) -> str:
        d = _archive_norm_dir(str(x or ""))
        return d if d and d != "/" else ""
    remote_dir = (_norm_dir_or_empty(t.get("remote_dir"))
                  or _norm_dir_or_empty(_ARCHIVE_CONFIG.get("defaultDir"))
                  or "/m3u8")
    # 文件名清洗同样要尊重设置页开关（与 archive_start 口径一致）
    clean_on = bool(_ARCHIVE_CONFIG.get("cleanFilename", True))
    name = _ensure_archive_ext(
        _clean_archive_filename(str(t.get("filename") or "未命名"), enabled=clean_on),
        local_path=lp)
    job = {
        "id": secrets.token_hex(6),
        "unique_id": uid,
        "filename": name,
        "raw_filename": name,
        "size_bytes": int(t.get("output_size") or 0) or None,
        "local_path": lp,
        "remote_dir": remote_dir,
        "remote_path": _archive_join(remote_dir, name),
        "policy": "overwrite",
        "delete_local": bool(_ARCHIVE_CONFIG.get("deleteLocal")),
        "state": "queued",
        "progress": 0,
        "error": "",
        "created_at": time.time(),
        "updated_at": time.time(),
        "archived_at": 0.0,
    }
    _ARCHIVE_JOBS[job["id"]] = job
    try:
        _ARCHIVE_TASKS[job["id"]] = asyncio.create_task(_archive_worker(job))
    except RuntimeError:
        # 无运行中事件循环（例如被同步代码直接调用）：撤销入队，避免留下
        # 「任务表里有、却没有 worker 在跑」的僵尸归档任务。
        _ARCHIVE_JOBS.pop(job["id"], None)
        raise
    _archive_save()
    t["archived_job_id"] = job["id"]
    log.info("M3U8 成品已自动入队归档: %s → %s", name, job["remote_path"])
    return job["id"]

async def _run_m3u8_task(task_id: str, gen: int = 0) -> None:
    """任务主流程：playlist → 密钥 → 并发分片（续传）→ 合并 → remux → 归档入队。"""
    t = _M3U8_TASKS.get(task_id)
    if not t:
        return
    t["state"] = "running"
    t["updated_at"] = time.time()
    _m3u8_save()
    client: Optional[httpx.AsyncClient] = None
    seg_dir = _seg_dir_of(t)
    try:
        os.makedirs(seg_dir, exist_ok=True)
        client = _make_client(_client_headers(t.get("headers")))

        # 1) playlist
        text = await _fetch_text(client, t["url"])
        parsed = _parse_m3u8(text, str(t["url"]))
        if parsed["kind"] == "master":
            raise M3u8Error("这是主播放列表（含多清晰度），请调用 resolve 选择具体清晰度后提交")
        segs: List[Dict[str, Any]] = parsed["segments"]
        if len(segs) > M3U8_MAX_SEGMENTS:
            raise M3u8Error("分片数 %d 超过上限 %d" % (len(segs), M3U8_MAX_SEGMENTS))
        t["total_segments"] = len(segs)
        t["encrypted"] = parsed["encrypted"]
        t["duration_secs"] = parsed["duration"]
        _m3u8_save()

        # 2) AES-128 密钥预取（去重）
        key_cache: Dict[str, bytes] = {}
        if parsed["encrypted"]:
            for ku in {s["enc"]["key_url"] for s in segs if s.get("enc")}:
                kresp = await _fetch_response(client, "GET", ku)
                if kresp.status_code != 200:
                    raise M3u8Error("密钥下载失败 HTTP %d" % kresp.status_code)
                if len(kresp.content) != 16:
                    raise M3u8Error("AES-128 密钥长度异常（%d 字节）" % len(kresp.content))
                key_cache[ku] = kresp.content

        # 3) fMP4 init 段
        # 注意：RFC 8216 中 EXT-X-KEY 同样作用于 EXT-X-MAP 的 init 段。
        # 若漏解密，产出的 mp4 头部是密文 → 播放器必然报错，而任务却是 done
        # 并自动归档（静默损坏）。因此这里按首个分片的加密上下文解密。
        init_path: Optional[str] = None
        if parsed.get("init_url"):
            idata = await _fetch_bytes_bounded(
                client, parsed["init_url"], None, _M3U8_MAX_SEGMENT_BYTES)
            first_enc = next((s["enc"] for s in segs if s.get("enc")), None)
            if first_enc:
                key = key_cache.get(first_enc["key_url"])
                if not key:
                    raise M3u8Error("init 段加密但密钥缺失")
                idata = await asyncio.to_thread(
                    _aes_decrypt, idata, key, _iv_bytes(first_enc, segs[0]["seq"]))
            init_path = os.path.join(seg_dir, "init.bin")
            await asyncio.to_thread(_write_atomic, init_path, idata)

        # 4) 并发分片下载（磁盘已有分片自动跳过 → 断点续传）
        # 复用分片不会经过 _download_one 的计数路径，进度必须在这里预扫描补上，
        # 否则重试后「已完成分片数」从 0 重新计数，进度条与总数对不上。
        reused = 0
        reused_bytes = 0
        for i in range(len(segs)):
            p = os.path.join(seg_dir, "seg_%05d.bin" % i)
            try:
                if os.path.exists(p) and os.path.getsize(p) > 0:
                    reused += 1
                    reused_bytes += os.path.getsize(p)
            except OSError:
                continue
        if reused:
            t["done_segments"] = reused
            t["downloaded_bytes"] = reused_bytes
            t["_speed_mark_bytes"] = reused_bytes
            t["_speed_mark_ts"] = time.time()
            log.info("M3U8 断点续传：复用磁盘上 %d/%d 个已下载分片", reused, len(segs))
            _m3u8_save()
        sem = asyncio.Semaphore(max(1, M3U8_CONCURRENCY))
        pending = [
            asyncio.create_task(
                _download_one(client, sem, s,
                              os.path.join(seg_dir, "seg_%05d.bin" % i), t, key_cache))
            for i, s in enumerate(segs)
        ]
        try:
            # gather 默认在首个异常处抛出但**不取消**其余协程：残留协程会继续
            # 重试/写入，而本函数 finally 已关闭 client，且重试时新旧协程会并发
            # 写同一 seg_*.bin。这里显式收尾，确保失败后没有散兵游勇。
            await asyncio.gather(*pending)
        except BaseException:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise

        # 5) 合并
        fmp4 = bool(init_path) or bool(
            segs and str(segs[0]["uri"]).lower().rsplit("?", 1)[0].endswith((".m4s", ".mp4")))
        title = _derive_title(t)
        clean_on = bool(_ARCHIVE_CONFIG.get("cleanFilename", True))
        raw_name = _clean_archive_filename(title + (".mp4" if fmp4 else ".ts"), enabled=clean_on)
        name = _ensure_archive_ext(raw_name, local_path="") or raw_name
        # 红线：成品必须落在本任务的分片目录内。`_safe_title` 已去掉目录分量，
        # 这里再做一次显式断言，杜绝任何路径穿越把成品写到目录外。
        out_path = os.path.join(seg_dir, os.path.basename(name))
        if os.path.dirname(os.path.abspath(out_path)) != os.path.abspath(seg_dir):
            raise M3u8Error("成品路径越出任务目录，已拒绝写入")
        merge_paths = ([init_path] if init_path else []) + \
            [os.path.join(seg_dir, "seg_%05d.bin" % i) for i in range(len(segs))]
        await asyncio.to_thread(_merge_files, merge_paths, out_path)

        # 6) TS 可选 ffmpeg remux → MP4（fMP4 本身就是 mp4，无需 remux）
        final_path = out_path
        if not fmp4 and M3U8_REMUX_MP4:
            ffmpeg = _find_ffmpeg()
            if ffmpeg:
                mp4_path = os.path.join(seg_dir, os.path.splitext(name)[0] + ".mp4")
                if await _remux_with_ffmpeg(out_path, mp4_path, ffmpeg):
                    final_path = mp4_path
                    try:
                        os.remove(out_path)
                    except OSError:
                        pass
        t["filename"] = os.path.basename(final_path)
        t["output_path"] = final_path
        t["output_size"] = os.path.getsize(final_path)
        t["state"] = "done"
        t["finished_at"] = time.time()
        t["updated_at"] = t["finished_at"]
        t["speed_bps"] = 0.0
        _m3u8_save()
        log.info("M3U8 下载完成: %s（%d 分片, %d 字节）",
                 t["filename"], len(segs), t["output_size"])

        # 7) 自动归档（失败不影响任务 done 状态，归档可在归档页重试）
        # 尊重设置页的「自动归档」总开关：关掉后 TG 下载不再自动上传，
        # m3u8 成品也应一视同仁，只留在本地等用户手动归档。锁定取消的任务除外。
        if t.get("state") == "done" and bool(_ARCHIVE_CONFIG.get("autoArchive")):
            try:
                _register_archive_job(t)
                _m3u8_save()
            except Exception as e:  # noqa: BLE001
                log.warning("M3U8 成品自动归档入队失败: %s", e)
        elif t.get("state") == "done":
            log.info("M3U8 成品已下载完成，但「自动归档」已关闭，留待手动归档: %s",
                     t.get("filename"))
        # 8) 回收分片：成品已成，seg_*.bin 再无价值（重试/续传只对未完成任务
        #    有意义）。此前从不回收，磁盘只涨不消。失败/取消的分支**不清理**，
        #    以便 retry 复用已下载分片。
        if t.get("state") == "done":
            try:
                freed = await asyncio.to_thread(_cleanup_segments, t)
                if freed:
                    log.info("M3U8 已回收分片缓存 %d 字节: %s", freed, t.get("id"))
            except Exception as e:  # noqa: BLE001
                log.warning("M3U8 分片回收失败(%s): %r", t.get("id"), e)
    except asyncio.CancelledError:
        t["state"] = "cancelled"
        t["error"] = "用户已取消"
        t["updated_at"] = t["finished_at"] = time.time()
        log.info("M3U8 下载已取消: %s", t.get("id"))
    except M3u8Error as e:
        t["state"] = "failed"
        t["error"] = str(e)
        t["updated_at"] = time.time()
        log.warning("M3U8 下载失败(%s): %s", t.get("id"), e)
    except Exception as e:  # noqa: BLE001
        t["state"] = "failed"
        t["error"] = "下载失败：" + e.__class__.__name__
        t["updated_at"] = time.time()
        log.error("M3U8 下载异常(%s): %r", t.get("id"), e)
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
        # 只在代数仍属自己时清理登记表：若期间已发起重试（新 runner 已登记），
        # 这里绝不能误删它，否则新任务会变成无人接管的僵尸任务。
        entry = _M3U8_RUNNERS.get(task_id)
        if entry is not None and entry[0] == gen:
            _M3U8_RUNNERS.pop(task_id, None)
        t["updated_at"] = time.time()
        _m3u8_save()
