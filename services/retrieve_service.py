# -*- coding: utf-8 -*-
"""
services/retrieve_service.py — 云端文件取回服务 (Retrieve Workflow)
================================================================
负责从 OpenList 云端存储以流式分片断点续传写回本地 VPS 磁盘。
"""
import os
import time
import asyncio
from urllib.parse import urljoin
from typing import Any, Dict, List, Optional
from core.config import APP_ROOT_DIR, OPENLIST_URL
from core.state import _RETRIEVE_JOBS
from core.logging import log
from services.openlist_service import (
    _openlist_client, _openlist_upload_client, _openlist_token,
    _openlist_relogin, _openlist_env, _UPLOAD_CHUNK,
    _openlist_relocate_after_rename,
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
                    # 改名自愈：路径失效（用户在 OpenList 里改过名）时，
                    # 按"同目录 + 相同文件大小"重定位。唯一匹配则回写记录并继续。
                    relocated = await _openlist_relocate_after_rename(
                        token, job["remote_path"], int(job.get("size_bytes") or 0))
                    if relocated:
                        job["remote_path"] = relocated
                        resp = await _openlist_client.post(
                            "/api/fs/get",
                            json={"path": relocated},
                            headers={"Authorization": token}
                        )
                        code, data, msg = _openlist_env(resp)
                        if code != 200:
                            raise RuntimeError(msg or "OpenList 获取文件信息失败（重定位后仍失败）")
                    else:
                        raise RuntimeError(msg or f"OpenList 获取文件信息失败 (code={code})")
                raw_url = str(data.get("raw_url") or "")
                if not job.get("size_bytes") and data.get("size"):
                    job["size_bytes"] = int(data["size"])

            if not raw_url:
                raise RuntimeError("OpenList 未返回文件下载直链")

            if raw_url.startswith("/"):
                raw_url = urljoin(OPENLIST_URL, raw_url)

            os.makedirs(os.path.dirname(job["target_path"]), exist_ok=True)

            # ---------------- 断点续传 ----------------
            # 上一次中断/失败会留下 .part 残片（下方异常分支不再删它），
            # 这里带上 Range 从断点继续。
            #
            # 关键陷阱：Range 可能被服务端**忽略**（返回 200 + 全量 body）。
            # 那时若仍以追加("ab")打开，新数据会拼在旧残片后面 —— 得到一个
            # 前段旧、后段新的损坏文件。所以必须按响应码决定追加还是重写：
            #   206 = 接受续传 -> 追加；  200 = 忽略 Range -> 从头重写。
            resume_from = 0
            try:
                if os.path.isfile(temp_path):
                    resume_from = int(os.path.getsize(temp_path))
            except OSError:
                resume_from = 0
            # 残片比记录的总大小还大 => 云端文件变小了或残片来自另一次下载，
            # 续传没有意义，重头来。
            known_size = int(job.get("size_bytes") or 0)
            if known_size and resume_from > known_size:
                resume_from = 0

            req_headers = dict(dl_headers)
            if resume_from > 0:
                req_headers["Range"] = f"bytes={resume_from}-"

            async with _openlist_upload_client.stream("GET", raw_url, headers=req_headers) as dl_resp:
                if dl_resp.status_code >= 400:
                    raise RuntimeError(f"从 OpenList 下载流失败: HTTP {dl_resp.status_code}")
                partial = (dl_resp.status_code == 206)
                if resume_from > 0 and not partial:
                    log.info("取回：服务端未接受 Range（HTTP %s），改从头下载 %s",
                             dl_resp.status_code, job.get("filename"))
                    resume_from = 0
                cl = int(dl_resp.headers.get("content-length") or 0)
                # 206 的 content-length 只是剩余部分，总长要加上已下部分
                total = (resume_from + cl) if (partial and cl) else (cl or known_size or 0)
                if total > 0:
                    job["size_bytes"] = total
                downloaded = resume_from
                job["downloaded_bytes"] = downloaded
                if total > 0:
                    job["progress"] = min(99, int(downloaded * 100 / total))
                if resume_from > 0:
                    log.info("取回断点续传：从 %d 字节继续 %s（共 %s）",
                             resume_from, job.get("filename"), total or "?")
                fh = await asyncio.to_thread(open, temp_path, "ab" if resume_from else "wb")
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

            # 完整性校验：只有字节数达标才算成功。
            # 传输被截断（连接中断但没抛异常）时，若不校验就 os.replace 成正片，
            # 用户会拿到一个播到一半就坏的视频 —— 比明确失败更糟。
            final_size = await asyncio.to_thread(os.path.getsize, temp_path)
            if total > 0 and final_size < total:
                raise RuntimeError(
                    "下载不完整：期望 %d 字节，实际 %d 字节（已保留断点，重试将自动续传）"
                    % (total, final_size))

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
            # 用户显式取消：清掉残片（不要留垃圾）。失败分支则相反 —— 保留残片
            # 供断点续传，见下方注释。
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            job["state"] = "failed"
            job["error"] = str(e)
            job["updated_at"] = time.time()
            # 刻意**保留** .part 残片：下一次重试会带 Range 从断点续传，
            # 几 GB 的媒体文件不必从头再来。残留由 TTL 清理兜底。
            # 历史行为是这里直接 os.remove，等于把续传的进度丢掉。
            try:
                kept = int(os.path.getsize(temp_path)) if os.path.exists(temp_path) else 0
            except OSError:
                kept = 0
            if kept:
                log.info("取回失败但已保留断点 %d 字节，重试将续传: %s",
                         kept, job.get("filename"))
            log.warning("云端取回失败 (%s): %s", job["remote_path"], e)
        finally:
            _RETRIEVE_TASKS.pop(job.get("id"), None)


def retrieve_status() -> List[Dict[str, Any]]:
    jobs = sorted(_RETRIEVE_JOBS.values(), key=lambda j: j.get("created_at") or 0.0, reverse=True)
    return [_retrieve_public(j) for j in jobs[:50]]


# 残片 TTL：失败会刻意保留 .part 供断点续传，但若任务从此不再重试，
# 这些文件会永远占着磁盘（媒体文件几 GB 一个，不能不管）。
_RETRIEVE_PART_TTL = float(os.environ.get("BRIDGE_RETRIEVE_PART_TTL", str(7 * 86400)))


def _retrieve_parts_cleanup() -> int:
    """清理孤儿 .part 残片：超过 TTL 且无活跃任务在用的删掉。返回删除数。"""
    try:
        import glob
        roots = [os.path.join(APP_ROOT_DIR, "downloads"), APP_ROOT_DIR]
        alive = {
            str(j.get("target_path") or "") + f".part.{j.get('id')}"
            for j in _RETRIEVE_JOBS.values()
            if j.get("id")
        }
        now = time.time()
        removed = 0
        for root in roots:
            if not os.path.isdir(root):
                continue
            for p in glob.glob(os.path.join(root, "**", "*.part.*"), recursive=True):
                if p in alive:
                    continue
                try:
                    if now - os.path.getmtime(p) < _RETRIEVE_PART_TTL:
                        continue
                    size = os.path.getsize(p)
                    os.remove(p)
                    removed += 1
                    log.info("清理过期取回残片（%.1f MB）: %s", size / 1048576, p)
                except OSError:
                    continue
        return removed
    except Exception as e:  # noqa: BLE001
        log.warning("取回残片清理异常: %s", e)
        return 0


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
