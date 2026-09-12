# -*- coding: utf-8 -*-
"""
services/retrieve_service.py — 云端文件取回服务 (Retrieve Workflow)
================================================================
负责从 OpenList 云端存储以流式分片断点续传写回本地 VPS 磁盘。
"""
import os
import time
import asyncio
import posixpath
from urllib.parse import urljoin
from typing import Any, Dict, List, Optional

from core.config import (
    APP_ROOT_DIR, OPENLIST_URL, _resolve_host_local_path,
    _norm_remote_path
)
from core.state import (
    _RETRIEVE_JOBS, _ARCHIVE_JOBS, _RETRIEVE_JOBS_MAX
)
from core.logging import log
from services.openlist_service import (
    _openlist_client, _openlist_upload_client, _openlist_token,
    _openlist_relogin, _openlist_env, _UPLOAD_CHUNK
)

_RETRIEVE_TASKS: Dict[str, Any] = {}
_RETRIEVE_SEM = asyncio.Semaphore(2)


def _retrieve_public(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": job.get("id"),
        "remotePath": job.get("remote_path"),
        "filename": job.get("filename"),
        "targetPath": job.get("target_path"),
        "state": job.get("state"),
        "progress": int(job.get("progress") or 0),
        "sizeBytes": int(job.get("size_bytes") or 0),
        "downloadedBytes": int(job.get("downloaded_bytes") or 0),
        "error": job.get("error") or "",
        "createdAt": float(job.get("created_at") or 0.0),
        "updatedAt": float(job.get("updated_at") or 0.0),
        "finishedAt": float(job.get("finished_at") or 0.0),
    }


async def _retrieve_worker(job: Dict[str, Any]) -> None:
    async with _RETRIEVE_SEM:
        if job.get("state") in ("done", "cancelled"):
            return
        job["state"] = "downloading"
        job["updated_at"] = time.time()
        temp_path = job["target_path"] + f".part.{job['id']}"
        try:
            token = await _openlist_token()
            raw_url = ""
            dl_headers = {}
            try:
                resp = await _openlist_client.post(
                    "/api/fs/link",
                    json={"path": job["remote_path"]},
                    headers={"Authorization": token}
                )
                code, data, msg = _openlist_env(resp)
                if resp.status_code == 401 or code in (401, 403):
                    token = await _openlist_relogin()
                    resp = await _openlist_client.post(
                        "/api/fs/link",
                        json={"path": job["remote_path"]},
                        headers={"Authorization": token}
                    )
                    code, data, msg = _openlist_env(resp)
                if code == 200 and isinstance(data, dict) and data.get("url"):
                    raw_url = str(data["url"])
                    if isinstance(data.get("header"), dict):
                        dl_headers.update(data["header"])
            except Exception as e:  # noqa: BLE001
                log.debug("fs/link 尝试失败，转向 fs/get: %s", e)

            if not raw_url:
                resp = await _openlist_client.post(
                    "/api/fs/get",
                    json={"path": job["remote_path"]},
                    headers={"Authorization": token}
                )
                code, data, msg = _openlist_env(resp)
                if resp.status_code == 401 or code in (401, 403):
                    token = await _openlist_relogin()
                    resp = await _openlist_client.post(
                        "/api/fs/get",
                        json={"path": job["remote_path"]},
                        headers={"Authorization": token}
                    )
                    code, data, msg = _openlist_env(resp)
                if code != 200:
                    raise RuntimeError(msg or f"OpenList 获取文件信息失败 (code={code})")
                raw_url = str(data.get("raw_url") or "")
                if not job.get("size_bytes") and data.get("size"):
                    job["size_bytes"] = int(data["size"])

            if not raw_url:
                raise RuntimeError("OpenList 未返回文件下载直链")

            if raw_url.startswith("/"):
                raw_url = urljoin(OPENLIST_URL, raw_url)

            os.makedirs(os.path.dirname(job["target_path"]), exist_ok=True)
            async with _openlist_upload_client.stream("GET", raw_url, headers=dl_headers) as dl_resp:
                if dl_resp.status_code >= 400:
                    raise RuntimeError(f"从 OpenList 下载流失败: HTTP {dl_resp.status_code}")
                total = int(dl_resp.headers.get("content-length") or job.get("size_bytes") or 0)
                if total > 0:
                    job["size_bytes"] = total
                downloaded = 0
                fh = await asyncio.to_thread(open, temp_path, "wb")
                try:
                    async for chunk in dl_resp.aiter_bytes(chunk_size=_UPLOAD_CHUNK):
                        if not chunk:
                            continue
                        await asyncio.to_thread(fh.write, chunk)
                        downloaded += len(chunk)
                        job["downloaded_bytes"] = downloaded
                        if total > 0:
                            job["progress"] = min(99, int(downloaded * 100 / total))
                finally:
                    await asyncio.to_thread(fh.close)

            await asyncio.to_thread(os.replace, temp_path, job["target_path"])
            job["state"] = "done"
            job["progress"] = 100
            job["finished_at"] = time.time()
            job["updated_at"] = job["finished_at"]
            log.info("云端取回完成: %s → %s", job["remote_path"], job["target_path"])
        except asyncio.CancelledError:
            job["state"] = "cancelled"
            job["error"] = "取回已取消"
            job["updated_at"] = time.time()
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            job["state"] = "failed"
            job["error"] = str(e)
            job["updated_at"] = time.time()
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:  # noqa: BLE001
                    pass
            log.warning("云端取回失败 (%s): %s", job["remote_path"], e)
        finally:
            _RETRIEVE_TASKS.pop(job.get("id"), None)


def retrieve_status() -> List[Dict[str, Any]]:
    jobs = sorted(_RETRIEVE_JOBS.values(), key=lambda j: j.get("created_at") or 0.0, reverse=True)
    return [_retrieve_public(j) for j in jobs[:50]]


def retrieve_cancel(job_id: str) -> Optional[Dict[str, Any]]:
    job = _RETRIEVE_JOBS.get(job_id)
    if not job:
        return None
    if job.get("state") not in ("queued", "downloading"):
        return _retrieve_public(job)
    task = _RETRIEVE_TASKS.get(job_id)
    if task and not task.done():
        task.cancel()
    job["state"] = "cancelled"
    job["error"] = "用户已取消"
    job["updated_at"] = time.time()
    return _retrieve_public(job)
