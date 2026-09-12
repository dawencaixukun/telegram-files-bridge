"""
services/media_service.py — 缩略图自愈引擎
==========================================
负责全尺寸缩略图异步自愈（GetMessage 定位 / GetRemoteFile 兜底 + 同步补下载）。

注：原 HTTP 206 范围请求解析、文件分片流式读取与 /api/media/* 系列接口
（「边下边播 / 本地在线播放」）已按需求整体移除，本模块不再承担流式传输职责。
"""
import time
import asyncio
from typing import Any, Dict, List, Optional, Set

from core.backend import telegram_api_call
from core.logging import log

_THUMB_HEAL_TTL = 300.0
_THUMB_HEAL_MAX_BYTES = 10 * 1024 * 1024
_thumb_heal_failed: Dict[str, float] = {}
_thumb_heal_inflight: Set[str] = set()
_thumb_heal_lock = asyncio.Lock()
_THUMB_HEAL_SEM = asyncio.Semaphore(4)


def _collect_file_nodes(node: Any, out: List[Dict[str, Any]], depth: int = 0) -> None:
    if depth > 12 or len(out) >= 16:
        return
    if isinstance(node, dict):
        rid = node.get("id")
        remote = node.get("remote") if isinstance(node.get("remote"), dict) else {}
        if isinstance(rid, int) and remote:
            out.append({
                "id": rid,
                "size": node.get("size") or node.get("expectedSize") or 0,
                "uniqueId": str(remote.get("uniqueId") or ""),
                "remoteId": str(remote.get("id") or ""),
            })
        for v in node.values():
            _collect_file_nodes(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _collect_file_nodes(v, out, depth + 1)


async def _heal_thumbnail(unique_id: str, chat_id: Any = None, message_id: Any = None) -> Optional[str]:
    """补下载缩略图，返回可用于 /file 端点的当前 uniqueId；失败返回 None。"""
    if time.monotonic() - _thumb_heal_failed.get(unique_id, 0.0) < _THUMB_HEAL_TTL:
        return None
    async with _thumb_heal_lock:
        if unique_id in _thumb_heal_inflight:
            return None
        _thumb_heal_inflight.add(unique_id)
    try:
        async with _THUMB_HEAL_SEM:
            target: Optional[Dict[str, Any]] = None
            if chat_id is not None and message_id is not None:
                try:
                    msg = await telegram_api_call(
                        "GetMessage",
                        {"chatId": int(chat_id), "messageId": int(message_id)},
                        timeout=20.0,
                    )
                    nodes: List[Dict[str, Any]] = []
                    _collect_file_nodes(msg, nodes)
                    for n in nodes:
                        if unique_id in (n["uniqueId"], n["remoteId"]):
                            if 0 < int(n["size"] or 0) <= _THUMB_HEAL_MAX_BYTES:
                                target = n
                            break
                    if target is None:
                        sized = [n for n in nodes
                                 if n["uniqueId"] and 0 < int(n["size"] or 0) <= _THUMB_HEAL_MAX_BYTES]
                        if sized:
                            thumbs = [n for n in sized if n["uniqueId"].startswith("AQAD")]
                            target = min(thumbs or sized, key=lambda n: int(n["size"]))
                except Exception as e:  # noqa: BLE001
                    log.info("thumb heal: GetMessage 失败: %s", str(e)[:120])
            if target is None:
                remote = await telegram_api_call("GetRemoteFile", {"remoteFileId": unique_id}, 20.0)
                remote = remote if isinstance(remote, dict) else {}
                if remote.get("id"):
                    target = {"id": remote["id"], "uniqueId": unique_id}
            if target is None:
                _thumb_heal_failed[unique_id] = time.monotonic()
                log.info("thumb heal: 消息对象里没有可用的缩略图 File")
                return None
            dl = await telegram_api_call(
                "DownloadFile",
                {"fileId": target["id"], "priority": 16, "offset": 0, "limit": 0, "synchronous": True},
                timeout=90.0,
            )
            local = (dl if isinstance(dl, dict) else {}).get("local") or {}
            if local.get("isDownloadingCompleted") and local.get("path"):
                log.info("thumb heal: 补图成功 %s -> %s", unique_id[:24], (target["uniqueId"] or "?")[:24])
                return target["uniqueId"] or unique_id
            _thumb_heal_failed[unique_id] = time.monotonic()
            log.info("thumb heal: DownloadFile 未完成: %s", str(dl)[:160])
            return None
    except ValueError as e:
        log.warning("thumb heal: 拒绝 %s: %s", unique_id, e)
        _thumb_heal_failed[unique_id] = time.monotonic()
        return None
    except Exception as e:  # noqa: BLE001
        log.info("thumb heal: %s 失败: %s", unique_id, e)
        _thumb_heal_failed[unique_id] = time.monotonic()
        return None
    finally:
        _thumb_heal_inflight.discard(unique_id)
