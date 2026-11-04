# -*- coding: utf-8 -*-
"""
services/archive_service.py — 网盘归档工作流调度引擎
====================================================
负责归档任务生命周期、流式上传调度、文件指纹去重查重、失败分类诊断与自动归档扫描。
"""
import os
import re
import time
import secrets
import asyncio
import posixpath
from typing import Any, Dict, List, Optional, Tuple
from core.config import (
    _fmt_size, _fmt_time, _clean_archive_filename,
    _resolve_host_local_path, _archive_norm_dir, _archive_join,
    _ensure_archive_ext, _same_file_name,
    _archive_unique_name, _archive_name_suffix,
    _CUSTOM_FILE_NOTES, _is_meaningless_filename,
)
from core.state import (
    _ARCHIVE_JOBS, _ARCHIVE_CONFIG, _DELETED_LOCAL_UIDS, _QUICK_ARCHIVE_REGISTRY, _SUB_RULES, _archive_save, _archive_config_save, _archive_sem, _archive_registry_lookup, _archive_public, _archive_state_of, _archive_latest_raw_of,
    _archive_path_owners, _archive_upload_lock, _ARCHIVE_CONCURRENCY,
)
from core.backend import BACKEND
from core.logging import log, LOG_STORE
from services.openlist_service import (
    _openlist_token, _openlist_relogin, _openlist_mkdir_tree, _openlist_put_once, _openlist_ready, _openlist_direct_url, _openlist_client, _openlist_env, _OpenListAuthErr, _openlist_stat,
    _openlist_stat_ex, _openlist_verify_size, _openlist_list_all,
    STAT_OK, STAT_MISSING, STAT_UNKNOWN,
)
from services.notification_service import notify_archive_success, notify_archive_failed
from services.bot_command_service import remember_archive_error as _remember_archive_error

_ARCHIVE_TASKS: Dict[str, Any] = {}
_SWEEP_LOCK = asyncio.Lock()
_AUTO_ARCHIVE_MAX_ATTEMPTS = 3

_OPENLIST_OFF_HINT = "OpenList 未挂载/未登录，请到设置页登录后归档"


def _arch_button_view(f: Dict[str, Any], openlist_ready: bool, local_exists: bool = True) -> Dict[str, Any]:
    arch = f.get("archive")
    if arch:
        st = arch.get("state")
        if st == "uploading":
            return {"enabled": False, "label": "归档中 %s%%" % (arch.get("progress") or 0),
                    "title": arch.get("remotePath") or ""}
        if st == "queued":
            return {"enabled": False, "label": "排队中…", "title": arch.get("remotePath") or ""}
        if st == "done":
            if not local_exists:
                return {"enabled": False, "label": "已归档",
                        "title": ("已归档到 " + (arch.get("remotePath") or "") + "（本地文件已清理）")}
            return {"enabled": openlist_ready, "label": "重新归档",
                    "title": ("已归档到 " + (arch.get("remotePath") or ""))
                             if openlist_ready else _OPENLIST_OFF_HINT}
        if st == "failed":
            if not local_exists:
                return {"enabled": False, "label": "归档失败 (本地已清理)",
                        "title": (arch.get("error") or "归档失败，且本地文件已不存在")}
            return {"enabled": openlist_ready, "label": "归档失败 · 重试",
                    "title": (arch.get("error") or "归档失败，点击重试")
                             if openlist_ready else _OPENLIST_OFF_HINT}
    if not local_exists and str(f.get("_download_status") or "") == "completed":
        return {"enabled": False, "label": "本地已清理", "title": "本地文件已被删除或清理"}
    if not openlist_ready:
        return {"enabled": False, "label": "立即归档", "title": _OPENLIST_OFF_HINT}
    if not f.get("unique_id"):
        return {"enabled": False, "label": "立即归档", "title": "该记录缺少 uniqueId，无法归档"}
    if str(f.get("_download_status") or "") != "completed":
        return {"enabled": False, "label": "立即归档",
                "title": "文件尚未下载完成，下载完成后可归档"}
    if not local_exists:
        return {"enabled": False, "label": "本地已清理", "title": "本地文件已不存在"}
    return {"enabled": True, "label": "立即归档", "title": "上传到 OpenList 云端归档"}


def _enrich_archive(f: Dict[str, Any], openlist_ready: bool,
                    arch_index: Optional[Any] = None) -> Dict[str, Any]:
    uid = f.get("_unique_id")
    uid_str = str(uid) if uid else ""
    f["unique_id"] = uid_str
    lp = _resolve_host_local_path(f.get("local_path"))
    local_exists = bool(lp and os.path.exists(lp) and uid_str not in _DELETED_LOCAL_UIDS)
    f["local_exists"] = local_exists
    f["can_delete_local"] = local_exists or (str(f.get("_download_status") or "") == "completed" and uid_str not in _DELETED_LOCAL_UIDS)
    f["archivable"] = bool(uid) and str(f.get("_download_status") or "") == "completed" and local_exists
    if arch_index is not None:
        latest_view = arch_index.latest_of(uid_str) if uid_str else None
        f["archive"] = _archive_public(latest_view) if latest_view else None
    else:
        f["archive"] = _archive_state_of(uid_str) if uid_str else None
    f["arch_btn"] = _arch_button_view(f, openlist_ready, local_exists)
    return f


from services.watermark_service import _delete_local_file_by_job


async def _archive_worker(job: Dict[str, Any]) -> None:
    """归档单个任务。并发受 _archive_sem 限制（见 core/state.py 的说明）。

    关键：**拿到信号量之前不得改状态**。旧实现虽然把 state='uploading' 写在
    信号量内部，但 _archive_enqueue 会给每个排队文件立刻 create_task，
    而前端读到的「归档中」等于已入队数量（实测单轮入队 35 个），
    用户观感就是「并发过头、一堆卡在归档中」，而真实并发一直是 2。
    现在排队中保持 queued（前端显示「排队中」），真正拿到并发额度才转
    uploading，状态与实际资源占用严格一致。
    """
    if job.get("state") in ("done", "cancelled"):
        return
    # 排队阶段显式标记，供前端区分「已入队等待」与「正在上传」
    if str(job.get("state") or "") in ("", "queued"):
        job["state"] = "queued"
        job.setdefault("error", "")
        _archive_save()
    async with _archive_sem:
        if job.get("state") in ("done", "cancelled"):
            return
        job["state"] = "uploading"
        job["progress"] = 0
        job["error"] = ""
        job["updated_at"] = time.time()
        _archive_save()
        try:
            token = await _openlist_token()

            lp = _resolve_host_local_path(str(job.get("local_path") or ""))
            job["local_path"] = lp
            local_exists = await asyncio.to_thread(os.path.exists, lp)
            if not local_exists:
                # 本地已被删除时不能一律报失败：先核对云端是否已存在完整副本。
                # 典型场景：同一文件被重复入队，前一个任务已归档并删本地，
                # 后到的任务就会误报「本地文件不存在」并长期挂在失败列表里。
                #
                # 但「探测失败」不能被当成「云端没有」——那会把一个其实已归档的
                # 文件判成永久性 source_missing（前端置灰不可重试），死记录再也
                # 出不来。三态里只有明确 missing/0 字节才允许走失败分支。
                expected = int(job.get("size_bytes") or 0)
                st_code, cloud_size, _raw = await _openlist_stat_ex(token, job["remote_path"])
                if st_code == STAT_OK and cloud_size > 0 and (not expected or cloud_size == expected):
                    job["state"] = "done"
                    job["progress"] = 100
                    job["archived_at"] = time.time()
                    job["updated_at"] = job["archived_at"]
                    job["error"] = ""
                    job["remote_size"] = cloud_size
                    job["local_deleted"] = True
                    _archive_save()
                    log.info("本地文件已被删除，但云端已存在完整副本，判定为已归档: %s（%d 字节）",
                             job["remote_path"], cloud_size)
                    return
                if st_code == STAT_UNKNOWN:
                    raise RuntimeError(
                        "本地文件不存在，且云端状态未能确认（OpenList 探测超时），稍后重试可自动判定")
                raise RuntimeError(
                    "本地文件不存在或已被移动，且云端没有完整副本（云端 %s 字节）"
                    % (cloud_size if st_code == STAT_OK else "缺失"))
            size = await asyncio.to_thread(os.path.getsize, lp)
            if size <= 0:
                raise RuntimeError("本地文件为空（0 字节），无法归档")
            job["size_bytes"] = size

            if job.get("policy") == "skip":
                skip_code, skip_size, _skip_raw = await _openlist_stat_ex(token, job["remote_path"])
                remote_stat = {"size": skip_size} if skip_code == STAT_OK else None
                if skip_code == STAT_UNKNOWN:
                    # 查不清就按「云端可能有同名文件」处理会静默覆盖用户已有文件；
                    # 按「不存在」处理会白传一遍。宁可走覆盖上传（幂等、只花带宽），
                    # 但必须在日志里说清为什么没走 skip。
                    log.warning("跳过策略：云端同名文件状态未能确认（探测超时），改为覆盖上传: %s",
                                job["remote_path"])
                    job["policy"] = "overwrite"
                if remote_stat is not None:
                    remote_size = int(remote_stat.get("size") or 0)
                    # 关键红线：只有云端文件**大小与本地一致且非 0** 才允许跳过并删本地。
                    # 历史事故：上传被取消/中断后云端残留 0 字节空壳，旧逻辑只判「存在」，
                    # 于是判 done 并执行 deleteLocal —— 本地被删、云端是空文件，真实数据丢失。
                    if remote_size == size and remote_size > 0:
                        job["state"] = "done"
                        job["progress"] = 100
                        job["archived_at"] = time.time()
                        job["error"] = ""
                        job["updated_at"] = job["archived_at"]
                        job["remote_size"] = remote_size
                        _archive_save()
                        log.info("归档跳过（云端已存在完整同名文件，大小一致 %d 字节）: %s",
                                 remote_size, job["remote_path"])
                        notify_archive_success(job)
                        if job.get("delete_local"):
                            await _delete_local_file_by_job(job)
                        return
                    # 云端存在但大小不符（0 字节或残缺）：绝不当成功，继续走覆盖上传修复它
                    log.warning(
                        "云端同名文件不完整（云端 %s 字节 / 本地 %s 字节），不跳过，改为重新上传覆盖: %s",
                        remote_size, size, job["remote_path"])
                    job["policy"] = "overwrite"

            await _openlist_mkdir_tree(token, str(job["remote_dir"]))

            # 同路径串行（数据丢失级防线二）：文件名唯一化只保证**新**入队的任务
            # 不撞车，但历史遗留 / 重试任务仍可能指向同一路径。两个上传并发写同一
            # 路径时 OneDrive 返回 resourceModified（eTag mismatch），多轮重试全灭
            # （实测日志 30 次）；顺利时则静默覆盖。这里按路径加锁串行化。
            async with _archive_upload_lock(str(job.get("remote_path") or "")):
                try:
                    await _openlist_put_once(token, job)
                except _OpenListAuthErr:
                    token = await _openlist_relogin()
                    await _openlist_put_once(token, job)
                except FileNotFoundError:
                    # 竞态：同一文件被重复入队，拿到锁时另一路已归档成功并按
                    # deleteLocal 删掉了本地文件（实测线上 1 例）。这不是失败，
                    # 重新核对云端：完整就判 done，否则才如实报源文件已丢失。
                    verify_code2, verify_size2 = await _openlist_verify_size(token, job["remote_path"])
                    if verify_code2 == STAT_OK and verify_size2 == size:
                        job["state"] = "done"
                        job["progress"] = 100
                        job["archived_at"] = time.time()
                        job["updated_at"] = job["archived_at"]
                        job["error"] = ""
                        job["remote_size"] = verify_size2
                        job["local_deleted"] = True
                        _archive_save()
                        log.info("上传时本地文件已被并行任务清理，但云端副本完整，判定已归档: %s（%d 字节）",
                                 job["remote_path"], verify_size2)
                        return
                    raise RuntimeError("本地文件在归档过程中被删除，且云端没有完整副本，请重新下载后再归档")

                # 上传返回 200 不等于落盘完整：必须回查云端真实大小再判成功。
                # 只有确认 size 与本地一致，才允许写 done 并按 deleteLocal 删本地文件。
                #
                # 三态闭环（线上实测痛点）：fs/get 单文件探测在 OneDrive 侧会飙到
                # 10~12s，旧逻辑一次超时就直接判「核验失败」，53 条告警里 8 条是
                # 「文件其实已传完」的假失败。现在：重试 → 目录列表兜底 → 仍查不清
                # 时才报错，且报的是「无法确认」而不是「不存在」，绝不当成 0 字节
                # 去触发整文件重传；也确实存疑时延后由下一轮 sweep 复查。
                verify_code, verify_size = await _openlist_verify_size(token, job["remote_path"])
                if verify_code == STAT_OK and verify_size != size:
                    # 拿到了确定的大小且不一致 → 这才是真的没传完整，重新上传
                    raise RuntimeError(
                        f"上传后云端大小核验不一致（云端 {verify_size} 字节 / 本地 {size} 字节），判定归档未完成")
                if verify_code != STAT_OK:
                    # 结论未知：不删本地、不判失败，交给自动归档下一轮复查。
                    # 判 failed 会让用户看到一条无从下手的告警，且源文件若被清理
                    # 就再也救不回来 —— 保持可重试状态才是安全侧。
                    job["state"] = "unverified"
                    job["error"] = ""
                    job["verify_pending"] = True
                    job["verify_attempts"] = int(job.get("verify_attempts") or 0) + 1
                    job["updated_at"] = time.time()
                    _archive_save()
                    log.warning(
                        "上传完成但云端大小暂时无法确认（第 %d 次）：%s，已延后复查，本地文件保留",
                        job["verify_attempts"], job["remote_path"])
                    return

            job["state"] = "done"
            job["progress"] = 100
            job["archived_at"] = time.time()
            job["updated_at"] = job["archived_at"]
            job["remote_size"] = verify_size
            _archive_save()
            log.info("归档完成: %s → %s（已核验 %d 字节）", job["filename"], job["remote_path"], verify_size)
            notify_archive_success(job)
            if job.get("delete_local"):
                await _delete_local_file_by_job(job)
        except asyncio.CancelledError:
            # 取消有两种来源，必须区分开：
            #   1) 用户点「取消」→ routers/archive.py 会先把 job 置 cancelled 再
            #      task.cancel()，这里看到的状态已经是 cancelled，保持原样即可。
            #   2) **进程退出** → bridge_server 的 shutdown 对所有 in-flight 任务
            #      调 task.cancel()，此时 job 还是 uploading/queued。旧代码一律写
            #      「用户已取消」，于是每次重启都凭空产生一批「用户已取消」记录，
            #      而文件其实还躺在本地、从未归档 —— 实测线上 5 个「本地在存」的
            #      文件就是这样被标记的（09:16:05 两条、03:10:24 两条…同一秒成对
            #      出现，正好等于并发槽位，明显是批量取消而非人工点击）。
            #      这类必须可自动重试，不能伪装成用户的决定。
            if str(job.get("state") or "") in ("cancelled",) or job.get("user_cancelled"):
                job["error"] = job.get("error") or "用户已取消"
                job["user_cancelled"] = True
                log.info("归档已取消: %s", job["filename"])
            else:
                job["state"] = "queued"
                job["resume_pending"] = True
                job["error"] = "上传被中断（bridge 重启），可重试"
                job["interrupted_by_restart"] = True
                log.info("归档被进程退出中断，已标记为待续跑: %s", job["filename"])
            job["updated_at"] = time.time()
            _archive_save()
        except RuntimeError as e:
            job["state"] = "failed"
            job["error"] = str(e)
            job["updated_at"] = time.time()
            _archive_save()
            log.warning("归档失败（%s → %s）: %s", job["filename"], job["remote_path"], e)
            _remember_archive_error(job)
            notify_archive_failed(job)
        except Exception as e:  # noqa: BLE001
            job["state"] = "failed"
            job["error"] = "上传失败：" + e.__class__.__name__
            job["updated_at"] = time.time()
            _archive_save()
            log.error("归档异常（%s）: %s", job["filename"], e)
            _remember_archive_error(job)
            notify_archive_failed(job)
        finally:
            _ARCHIVE_TASKS.pop(job.get("id"), None)
            from services.subscription_service import _sub_on_job_finished
            _sub_on_job_finished(job)


_REMOTE_DIR_LIST_CACHE: Dict[str, Tuple[float, int, set, List[Dict[str, Any]]]] = {}


async def _cloud_archive_rows(check_remote: bool = True) -> List[Dict[str, Any]]:
    done = [j for j in _ARCHIVE_JOBS.values() if j.get("state") == "done"]
    done.sort(key=lambda j: j.get("archived_at") or j.get("created_at") or 0.0, reverse=True)
    latest: Dict[str, Dict[str, Any]] = {}
    for j in done:
        key = str(j.get("remote_path") or "").lower()
        if key and key not in latest:
            latest[key] = j
    rows = list(latest.values())
    if not rows:
        return []

    remote_names: Dict[str, set] = {}
    remote_files_by_dir: Dict[str, List[Dict[str, Any]]] = {}
    if check_remote:
        try:
            token = await _openlist_token()
            dirs = sorted({str(j.get("remote_dir") or "") for j in rows if j.get("remote_dir")})
            is_unmocked = _openlist_client.__dict__.get("post") is None
            now_m = time.monotonic()
            jobs_sig = len(rows)
            for d in dirs:
                if is_unmocked and d in _REMOTE_DIR_LIST_CACHE:
                    exp, sig, c_names, c_files = _REMOTE_DIR_LIST_CACHE[d]
                    if exp > now_m and sig == jobs_sig:
                        remote_names[d] = c_names
                        remote_files_by_dir[d] = c_files
                        continue
                # 必须翻页拉全（单页上限 500）：目录超过 500 个文件时，第一页
                # 之后真实存在的文件会被判成「云端失效」——线上实测 635 个文件
                # 的目录里，452 条归档记录有 80 条被误报为已删除/已移动。
                code, content, _msg = await _openlist_list_all(token, d)
                if code != 200:
                    continue
                c_names = {str(c.get("name") or "") for c in content}
                c_files = [
                    c for c in content if isinstance(c, dict) and not c.get("is_dir")
                ]
                remote_names[d] = c_names
                remote_files_by_dir[d] = c_files
                if is_unmocked:
                    # 90s：这是「云端目录列表」快照，只用于给归档行打上存在/失效角标。
                    # 原来 15s，导致每 15 秒就有一页 /library/cloud 要白付 2s 的
                    # OpenList 目录翻页；归档记录本身才是真相来源，角标可以慢一点。
                    _REMOTE_DIR_LIST_CACHE[d] = (now_m + 90.0, jobs_sig, c_names, c_files)
        except Exception as e:  # noqa: BLE001
            log.warning("云端归档存在性校验失败（按日志原样展示）: %s", e)

    out = []
    for j in rows:
        rp = str(j.get("remote_path") or "")
        rd = str(j.get("remote_dir") or "")
        fn = str(j.get("filename") or "")
        exists = True
        if check_remote and rd in remote_names:
            exists = fn in remote_names[rd]
        drive = (rp.strip("/").split("/")[0] if "/" in rp.strip("/") else "默认网盘") if rp else "默认网盘"
        status = "archived"
        if d_name := str(j.get("remote_dir") or ""):
            if d_name in remote_names:
                status = "archived" if fn in remote_names[d_name] else "missing"
        # 同一行内 _fmt_time / _openlist_direct_url 各被调用 2~3 次填同值别名键
        # （archived_time/archivedAt、openlist_url/directUrl/openlistUrl）。
        # 实测 50 行归档 → _openlist_direct_url 被调用 150 次（应为 50）。
        # _openlist_direct_url 内部有 rstrip + normpath + quote，循环里是纯浪费。
        _t = _fmt_time(j.get("archived_at") or j.get("created_at"))
        _url = _openlist_direct_url(rp)
        out.append({
            "id": j.get("id"),
            "unique_id": j.get("unique_id"),
            "uniqueId": j.get("unique_id"),
            "filename": fn,
            "raw_filename": j.get("raw_filename") or fn,
            "rawFilename": j.get("raw_filename") or fn,
            "size": _fmt_size(j.get("size_bytes")),
            "size_bytes": j.get("size_bytes") or 0,
            "sizeBytes": j.get("size_bytes") or 0,
            "remote_dir": rd,
            "remoteDir": rd,
            "remote_path": rp,
            "remotePath": rp,
            "cloud_path": rp,
            "cloudPath": rp,
            "drive": drive,
            "status": status,
            "archived_time": _t,
            "archivedAt": _t,
            "archivedTimestamp": j.get("archived_at") or j.get("created_at") or 0.0,
            "openlist_url": _url,
            "directUrl": _url,
            "openlistUrl": _url,
            "exists": exists,
            "deleteLocal": bool(j.get("delete_local")),
            "localDeleted": bool(j.get("local_deleted")),
            "localPath": j.get("local_path") or "",
        })

    # ---------------- 改名自愈 ----------------
    # 用户在 OpenList 网页里改过文件名 -> 记录里的 remote_path 失效，
    # status 判为 missing、OpenList 按钮回落首页、取回失败。
    # 这里对 missing 行尝试按"同目录 + 相同文件大小"重定位；唯一匹配则回写
    # 记录（内存 + 落盘），按钮 URL 与状态同步刷新。多匹配/无匹配保持 missing
    # —— 宁可如实失败也不静默指错文件。
    if status_missing := [r for r in out if r.get("status") == "missing" and r.get("remote_path")]:
        try:
            from services.openlist_service import _openlist_relocate_after_rename
            _tok = ""
            try:
                _tok = await _openlist_token()
            except Exception:  # noqa: BLE001
                _tok = ""
            if _tok:
                saved_dirty = False
                for row in status_missing:
                    r_dir = str(row.get("remote_dir") or "")
                    r_size = int(row.get("sizeBytes") or 0)
                    new_path = None
                    if r_dir in remote_files_by_dir:
                        if r_size > 0:
                            old_name = posixpath.basename(str(row["remote_path"]))
                            cands = [
                                str(c.get("name") or "")
                                for c in remote_files_by_dir[r_dir]
                                if int(c.get("size") or 0) == r_size and str(c.get("name") or "") != old_name
                            ]
                            if len(cands) == 1:
                                new_path = posixpath.join(r_dir, cands[0])
                    else:
                        new_path = await _openlist_relocate_after_rename(
                            _tok, str(row["remote_path"]), r_size)
                    if new_path:
                        rid = str(row.get("id") or "")
                        job = _ARCHIVE_JOBS.get(rid)
                        if job is not None:
                            job["remote_path"] = new_path
                            job["filename"] = posixpath.basename(new_path)
                            saved_dirty = True
                        row["remote_path"] = new_path
                        row["remotePath"] = new_path
                        row["cloud_path"] = new_path
                        row["cloudPath"] = new_path
                        row["filename"] = posixpath.basename(new_path)
                        row["status"] = "archived"
                        row["exists"] = True
                        _url2 = _openlist_direct_url(new_path)
                        row["openlist_url"] = _url2
                        row["directUrl"] = _url2
                        row["openlistUrl"] = _url2
                if saved_dirty:
                    try:
                        _archive_save()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001
            log.debug("改名自愈批处理跳过: %s", e)

    # 卡片化联查（历史缺陷：云端页旧表格只有文件维度字段，缩略图/消息定位
    # 存放在 TG 任务记录里，模板拿不到，卡片化后无图可显）。
    # 联查数据源一：bridge 任务列表（与旧版一致，含速率/状态富化）
    # 历史缺陷：已归档并删除本地文件的记录会被 Java 后端把 downloadStatus
    # 重置为 'idle'，而 tasks_all 只接受 downloading/paused/completed/error，
    # 整条被过滤 → VPS 实测 tasks_all 返回 0 条、56 张卡片联查全失败、
    # 全部落「无图占位」。缩略图字段（thumbnailUniqueId/thumbnail/telegramId/
    # chatId/messageId）与下载状态无关，因此联查数据源二：直接回源后端
    # 原始 /api/files 记录，任务列表命中优先、原始记录兜底。
    raw_map: Dict[str, Dict[str, Any]] = {}



    # 联查数据源二辅助：回源 Java 后端原始 /api/files 记录（不经
    # downloadStatus 过滤）。独立成闭包便于测试 patch 与复用。
    async def _raw_files():
        _r = await BACKEND.list_all_files_page_info(force=False)
        return list(BACKEND._unwrap_files(_r) or [])

    try:
        for f in await _raw_files():
            u = str(f.get("uniqueId") or "")
            if u:
                raw_map[u] = f
    except Exception as e:  # noqa: BLE001
        log.debug("云端卡片联查后端原始记录失败（任务列表兜底）: %s", e)
    uid_map: Dict[str, Dict[str, Any]] = {}
    if any(r.get("uniqueId") for r in out):
        try:
            # 延迟导入防循环依赖：task_service 也在函数体内反向引用本模块
            from services.task_service import tasks_all
            for t in await tasks_all():
                tu = str(t.get("_unique_id") or "")
                if tu:
                    uid_map[tu] = t
        except Exception as e:  # noqa: BLE001
            log.warning("云端卡片联查缩略图失败（按无图占位渲染）: %s", e)
    for r in out:
        t = uid_map.get(str(r.get("uniqueId") or ""))
        raw = raw_map.get(str(r.get("uniqueId") or ""))
        if t is None and raw is not None:
            # 任务列表无此 uid（downloadStatus=idle 被过滤），用后端原始记录兜底。
            # 缩略图与消息定位字段两条链路同构，直接映射即可。
            t = {
                "_thumb_uid": str(raw.get("thumbnailUniqueId") or ""),
                "_thumb": str(raw.get("thumbnail") or ""),
                "_telegram_id": raw.get("telegramId"),
                "_chat_id": raw.get("chatId"),
                "msg_id": raw.get("messageId"),
            }
        if t:
            thumb_uid = str(t.get("_thumb_uid") or "")
            # 历史缺陷：早期归档任务只存了主文件 uniqueId，没有 thumbnailUniqueId，
            # 联查后 thumb_uid 为空 → 卡片只能落 minithumbnail/占位图。
            # 与本地在存页同款兜底：退回主文件 uid 并标记 heal=1，
            # 由 /preview 经 GetMessage 定位补下载真正的全尺寸缩略图。
            thumb_heal = False
            if not thumb_uid:
                main_uid = str(r.get("uniqueId") or (t.get("_unique_id") or ""))
                if main_uid:
                    thumb_uid = main_uid
                    thumb_heal = True
            r.update({
                "thumb": str(t.get("_thumb") or ""),
                "thumb_uid": thumb_uid,
                "thumb_heal": thumb_heal,
                "telegramId": t.get("_telegram_id"),
                "chatId": t.get("_chat_id"),
                "messageId": t.get("msg_id"),
            })
        else:
            # 无匹配任务（历史残留记录/预览件）：显式落空值，模板按占位图渲染
            r.setdefault("thumb", "")
            r.setdefault("thumb_uid", "")
            r.setdefault("thumb_heal", False)
            r.setdefault("telegramId", "")
            r.setdefault("chatId", "")
            r.setdefault("messageId", "")
    return out


def _classify_archive_error(raw_err: str) -> Tuple[str, str, str]:
    """把归档失败原因归类，用于前端分诊与「能否重试」判定。

    注意：分类顺序即优先级，必须把**永久性错误**放在前面判断，
    否则「本地文件已丢失」会被后面的宽泛规则（如 exist/连接）误判成可重试。

    返回值 (code, label, 建议动作)。
    """
    err = str(raw_err or "").strip()
    # 0) 新增：这两类此前全部落进 unknown，前端只显示「❓ 未知异常」，
    #    用户完全看不到发生了什么（线上 53 条告警全是这两种文案）。
    #    它们都是**可重试的临时状态**，必须单独成类并给出可执行建议。
    if re.search(r"上传被中断|bridge 重启导致上传中断|重启导致上传中断", err, re.I):
        return "interrupted", "⏸️ 上传被中断", "服务重启打断了归档，点击重试即可从断点重新入队"
    if re.search(r"核验失败|云端大小.{0,6}无法确认|状态未能确认|存在性核验", err, re.I):
        return "verify_pending", "🔎 云端核验未完成", "文件已上传但云端大小暂时查不到（OpenList 探测超时），稍后重试或等待自动复查"
    # 1) 永久性：本地源文件不存在，重试一万次也不会变好。
    #    历史缺陷：这类错误落到 unknown 且被前端当作可重试，用户每点一次
    #    「重试」就再跑一遍注定失败的归档（文件已删/已移动，重试无意义）。
    if re.search(r"本地文件不存在|本地文件为空|未在磁盘找到|本地已清理|本地文件已被移动", err, re.I):
        return "source_missing", "🗑️ 本地文件已丢失", "本地原文件已不存在，无法归档；请重新下载后再归档"
    # 2) 永久性：云端对象在会话期间被改动（eTag 失配），重试同样无用，
    #    必须重新走一次「上传」而不是重放旧的会话。
    if re.search(r"resourceModified|resource has changed|etag mismatch|eTag", err, re.I):
        return "remote_changed", "🔄 云端文件已变动", "云端同名文件已被其他操作改动，请重新下载后再归档"
    if re.search(r"itemNotFound|resource could not be found", err, re.I):
        return "remote_missing", "🔍 云端对象不存在", "云端目标已不存在，请确认网盘挂载与目标目录后重试"
    # 3) 临时性：鉴权、容量、冲突、网络抖动，重试有意义。
    if re.search(r"401|403|token|unauthorized|OpenListAuthErr|认证|登录失效|凭证", err, re.I):
        return "token_expired", "🔑 鉴权过期", "重新登录刷新 OpenList 令牌后重试"
    if re.search(r"507|quota|full|space|容量|空间|超限|insufficient", err, re.I):
        return "storage_full", "💾 容量超限", "清理网盘空间或更换归档目录"
    if re.search(r"409|already exists|conflict|同名|冲突|重名", err, re.I):
        return "conflict", "⚠️ 文件冲突", "启用覆盖策略后重试"
    if re.search(r"502|504|timeout|timed out|refused|connect|readtimeout|reset by peer|超时|断网|连接", err, re.I):
        return "timeout", "🌐 网络超时", "网络抖动，建议一键重新入队"
    return "unknown", "❓ 未知异常", "建议查看日志详情分析堆栈"


# 永久性失败分类：重试不会改变结果，必须禁止无限重试（否则形成死循环）。
_PERMANENT_ARCHIVE_ERRORS = frozenset({"source_missing", "remote_changed", "remote_missing"})


# 自动重试退避档位（秒）：第 1/2/3 次失败后分别等待这么久再自动重试。
# 旧的 sweep 没有任何退避，失败任务在下一轮（30s 后）就被立刻重新入队，
# _AUTO_ARCHIVE_MAX_ATTEMPTS=3 的额度在一两分钟内烧光，之后只能手动点重试。
_AUTO_RETRY_BACKOFF = (30.0, 120.0, 480.0)


def _archive_sweep_batch_limit() -> int:
    """单轮自动归档最多入队多少个（可配 BRIDGE_ARCHIVE_SWEEP_BATCH）。

    默认取「并发额度 × 4」：留出足够的排队深度让上传协程永远不空转，
    又不至于把几百个任务堆在队列里。实测线上单轮峰值入队 35 个，
    排队深度远超并发额度，用户看到的就是「一堆归档中、进度不动」。
    """
    default = max(4, _ARCHIVE_CONCURRENCY * 4)
    try:
        return max(1, int(os.environ.get("BRIDGE_ARCHIVE_SWEEP_BATCH", str(default))))
    except (TypeError, ValueError):
        return default


def _archive_retry_due(job: Dict[str, Any], attempts: int) -> bool:
    """该失败任务现在是否已过退避窗口、可以自动重试。

    attempts 是已经失败的次数。第 1 次失败等 30s，第 2 次等 2min，第 3 次等 8min。
    没有 updated_at 的历史记录直接放行（不让老数据永远卡住）。
    """
    try:
        last = float(job.get("updated_at") or job.get("created_at") or 0.0)
    except (TypeError, ValueError):
        return True
    if last <= 0:
        return True
    idx = max(0, min(int(attempts or 1) - 1, len(_AUTO_RETRY_BACKOFF) - 1))
    delay = _AUTO_RETRY_BACKOFF[idx]
    return (time.time() - last) >= delay


def _is_retryable_archive_error(raw_err: str) -> bool:
    """该失败是否值得重试。永久性错误（源文件丢失等）返回 False。"""
    code, _, _ = _classify_archive_error(raw_err)
    return code not in _PERMANENT_ARCHIVE_ERRORS


async def _check_file_dedup(unique_id: str = "", filename: str = "", size_bytes: Any = None, tasks_list: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """统一多维文件指纹检查函数：
    1. 优先查云端网盘已归档 (_ARCHIVE_JOBS, state=='done')
    2. 其次查本地中转区在存资产 (tasks_all, downloadStatus=='completed', 且磁盘文件存在且未标记删除)
    3. 再次查正在进行/排队的下载任务 (tasks_all, downloadStatus in ('downloading', 'queued', 'pending', 'waiting_disk'))
    """
    uid = str(unique_id or "").strip()
    norm_fn = str(filename or "").strip().lower()
    sz = None
    if isinstance(size_bytes, (int, float)) and size_bytes > 0:
        sz = int(size_bytes)

    # 1. 检查云端网盘已归档资产
    arch_job = _archive_registry_lookup(uid, norm_fn, sz)
    if arch_job and (arch_job.get("state") == "done" or arch_job.get("remote_path")):
        rp = str(arch_job.get("remote_path") or "")
        drive = (rp.strip("/").split("/")[0] if "/" in rp.strip("/") else "默认网盘") if rp else "默认网盘"
        arch_ts = arch_job.get("archived_at") or arch_job.get("created_at") or 0.0
        arch_date = _fmt_time(arch_ts) if arch_ts else "近期"
        openlist_url = _openlist_direct_url(rp) if rp else ""
        matched_by = "unique_id" if (uid and str(arch_job.get("unique_id") or "").strip() == uid) else "name_size"
        fn_display = str(arch_job.get("filename") or filename or "未知文件")
        size_display = arch_job.get("size_bytes") or sz

        return {
            "duplicate": True,
            "duplicateType": "cloud",
            "matchedBy": matched_by,
            "prompt": f"云端已于 {arch_date} 归档至 {drive}：{rp}",
            "message": f"云端已于 {arch_date} 归档至 {drive} 路径：{rp}",
            "asset": {
                "status": "archived",
                "uniqueId": str(arch_job.get("unique_id") or uid),
                "filename": fn_display,
                "size": size_display,
                "sizeHuman": _fmt_size(size_display) if size_display else "—",
                "cloudPath": rp,
                "drive": drive,
                "openlistUrl": openlist_url,
                "archivedAt": arch_ts,
                "archivedDate": arch_date,
                "localPath": None,
                "localExists": False,
                "taskId": None,
                "progress": None,
            },
            "actions": [
                {"type": "open_cloud", "label": "直达网盘查看", "url": openlist_url},
                {"type": "retrieve_cloud", "label": "直接取回至本地", "action": "retrieve", "path": rp},
                {"type": "force_download", "label": "强制重新下载", "action": "force"},
            ]
        }

    # 获取任务列表以比对本地在存与排队中任务
    if tasks_list is None:
        try:
            from services.task_service import tasks_all
            tasks_list = await tasks_all()
        except Exception:
            tasks_list = []

    # 2. 检查本地在存资产与下载任务
    for t in tasks_list:
        t_uid = str(t.get("_unique_id") or t.get("uniqueId") or "").strip()
        t_fn = str(t.get("filename") or t.get("name") or "").strip().lower()
        t_sz = t.get("_size_bytes")
        if not t_sz and isinstance(t.get("size"), (int, float)):
            t_sz = int(t.get("size"))

        match_uid = bool(uid and t_uid and uid == t_uid)
        match_ns = bool(norm_fn and t_fn and _same_file_name(norm_fn, t_fn) and (sz is None or t_sz == sz))

        if match_uid or match_ns:
            st = str(t.get("_download_status") or t.get("status") or "").lower()
            lp_raw = str(t.get("local_path") or t.get("localPath") or "")
            lp = _resolve_host_local_path(lp_raw) if (lp_raw and lp_raw != "—") else ""
            uid_cand = t_uid or uid

            # 2.1 本地已下载完成且磁盘上真实在存
            if st == "completed" and lp and os.path.exists(lp) and uid_cand not in _DELETED_LOCAL_UIDS:
                matched_by = "unique_id" if match_uid else "name_size"
                fn_display = str(t.get("filename") or filename or "未知文件")
                size_display = t_sz or sz
                return {
                    "duplicate": True,
                    "duplicateType": "local",
                    "matchedBy": matched_by,
                    "prompt": f"本地在存（路径：{lp}），无需重复下载",
                    "message": "本地在存：该文件已在本地中转区存在，支持直接查看或立即归档",
                    "asset": {
                        "status": "downloaded",
                        "uniqueId": uid_cand,
                        "filename": fn_display,
                        "size": size_display,
                        "sizeHuman": _fmt_size(size_display) if size_display else "—",
                        "cloudPath": None,
                        "drive": None,
                        "openlistUrl": None,
                        "archivedAt": None,
                        "archivedDate": None,
                        "localPath": lp,
                        "localExists": True,
                        "taskId": t.get("id"),
                        "progress": 100,
                    },
                    "actions": [
                        {"type": "open_local", "label": "查看本地在存", "url": "/library/local"},
                        {"type": "archive_now", "label": "立即归档", "action": "archive", "uniqueId": uid_cand},
                        {"type": "force_download", "label": "强制重新下载", "action": "force"},
                    ]
                }

            # 2.2 正在下载或排队中
            if st in ("downloading", "queued", "pending", "waiting_disk"):
                matched_by = "unique_id" if match_uid else "name_size"
                fn_display = str(t.get("filename") or filename or "未知文件")
                size_display = t_sz or sz
                prog = t.get("progress") or 0
                return {
                    "duplicate": True,
                    "duplicateType": "task",
                    "matchedBy": matched_by,
                    "prompt": f"该文件已在下载队列中（状态: {t.get('status')}，进度: {prog}%）",
                    "message": f"该文件已在下载队列中（状态: {t.get('status')}，进度: {prog}%），无需重复提交",
                    "asset": {
                        "status": t.get("status") or st,
                        "uniqueId": uid_cand,
                        "filename": fn_display,
                        "size": size_display,
                        "sizeHuman": _fmt_size(size_display) if size_display else "—",
                        "cloudPath": None,
                        "drive": None,
                        "openlistUrl": None,
                        "archivedAt": None,
                        "archivedDate": None,
                        "localPath": lp or None,
                        "localExists": False,
                        "taskId": t.get("id"),
                        "progress": prog,
                    },
                    "actions": [
                        {"type": "view_task", "label": "查看任务", "url": f"/tasks/{t.get('id')}"},
                        {"type": "force_download", "label": "强制重新下载", "action": "force"},
                    ]
                }

    return {
        "duplicate": False,
        "duplicateType": "none",
        "matchedBy": "none",
        "prompt": "",
        "message": "未发现重复资产",
        "asset": None,
        "actions": []
    }



async def _auto_archive_sweep() -> int:
    if _SWEEP_LOCK.locked():
        return 0
    async with _SWEEP_LOCK:
        return await _auto_archive_sweep_impl()


async def _auto_archive_sweep_impl() -> int:
    has_sub_rules = any(r.get("enabled") for r in _SUB_RULES.values())
    default_enabled = bool(_ARCHIVE_CONFIG.get("autoArchive"))
    default_dir = _archive_norm_dir(str(_ARCHIVE_CONFIG.get("defaultDir") or ""))
    has_default_rule = default_enabled and bool(default_dir and default_dir != "/")
    if _QUICK_ARCHIVE_REGISTRY:
        now_ts = time.time()
        for k in [k for k, v in _QUICK_ARCHIVE_REGISTRY.items() if now_ts - v.get("created_at", now_ts) > 172800]:
            _QUICK_ARCHIVE_REGISTRY.pop(k, None)
    has_quick_targets = bool(_QUICK_ARCHIVE_REGISTRY)

    if not has_sub_rules and not has_default_rule and not has_quick_targets:
        return 0
    if not await _openlist_ready():
        return 0
    try:
        from services.task_service import tasks_all
        tasks = await tasks_all(force=True)
    except Exception as e:  # noqa: BLE001
        log.warning("自动归档：任务聚合失败: %s", e)
        return 0
    enqueued = 0
    from services.subscription_service import (
        _sub_match_rule, _render_dir_template, _sub_bump, _sub_rules_sorted)

    # 规则集在单轮 sweep 内不会变化，预排序一次即可：_sub_rules_sorted() 每次
    # 都做完整 sorted() + 比较器（实测 100 条规则 138.9µs/次），逐任务重排会让
    # 单轮 2000 任务白耗 150ms+。
    _sweep_rules = [r for r in _sub_rules_sorted() if r.get("enabled")]

    # 路径占用表（{remote_path.lower(): {uid}}），本轮 sweep 内累积。
    # 必须在循环外建一次：循环内每轮重建会把「本批刚入队的路径」漏掉，
    # 同一轮里的两个同名任务就又撞在一起了。
    path_owners: Dict[str, set] = _archive_path_owners()

    for t in tasks:
        if str(t.get("_download_status") or "") != "completed":
            continue
        uid = str(t.get("_unique_id") or "")
        if not uid:
            continue

        rule = _sub_match_rule(t, _sweep_rules)
        remote_dir = None
        policy = "overwrite"
        delete_local = False
        rule_id = None
        rule_label = ""

        quick_target = _QUICK_ARCHIVE_REGISTRY.get(uid)
        if quick_target is not None:
            remote_dir = _archive_norm_dir(str(quick_target.get("remoteDir") or ""))
            policy = str(quick_target.get("policy") or "overwrite")
            delete_local = bool(quick_target.get("deleteLocal", False))
            rule_id = "quick"
            rule_label = "直投快捷归档"
        elif rule is not None:
            raw_tpl = str(rule.get("dirTemplate") or "")
            if has_default_rule and "/tg-archive/" in raw_tpl:
                # 兼容旧版「转发即下载」自动生成的 /tg-archive/{chat_title}/{YYYY-MM} 模板：
                # 若用户在设置页配置了默认归档目录（如 /onedrive/yello），直接以设置页目录与策略为准
                remote_dir = default_dir
                policy = "skip" if str(_ARCHIVE_CONFIG.get("policy") or "").lower() == "skip" else "overwrite"
                delete_local = bool(_ARCHIVE_CONFIG.get("deleteLocal", True))
            else:
                remote_dir = _render_dir_template(
                    raw_tpl,
                    source=str(t.get("source") or rule.get("chatTitle") or ""),
                    ftype=str(t.get("_type") or ""),
                    ts=t.get("_date_ts") if isinstance(t.get("_date_ts"), (int, float)) else None,
                    filename=str(t.get("filename") or ""),
                    chat_title=str(rule.get("chatTitle") or t.get("source") or ""),
                    width=t.get("width"),
                    height=t.get("height"))
                policy = "skip" if str(rule.get("policy") or "") == "skip" else "overwrite"
                delete_local = bool(rule.get("deleteLocal", True))
            rule_id = rule.get("id")
            rule_label = f"规则「{rule.get('chatTitle') or ''}」"
        elif has_default_rule:
            remote_dir = default_dir
            policy = "skip" if str(_ARCHIVE_CONFIG.get("policy") or "").lower() == "skip" else "overwrite"
            delete_local = bool(_ARCHIVE_CONFIG.get("deleteLocal"))
            rule_id = "default"
            rule_label = "默认归档"
        else:
            continue

        if not remote_dir or remote_dir == "/":
            if rule is not None:
                log.warning("自动归档：规则 %s 目录模板渲染失败，跳过 %s", rule.get("id"), uid)
            continue

        latest = _archive_latest_raw_of(uid)
        attempts = 0
        if latest is not None:
            st = str(latest.get("state") or "")
            # unverified：上传已成功、只差云端大小确认。绝不能在这里重新入队——
            # worker 第一步就是覆盖上传，会把几个 GB 白传一遍。它由
            # _archive_resume_pending 的核验-only 复查路径负责推进。
            if st in ("queued", "uploading", "done", "cancelled", "unverified"):
                continue
            attempts = int(latest.get("auto_attempts") or 0)
            # 永久性失败（本地源文件已丢失等）不再自动重试：重扫一轮也注定再失败一次，
            # 既白耗 I/O，又会让失败列表越来越长。用户重新下载后会产生新的 job。
            if not _is_retryable_archive_error(str(latest.get("error") or "")):
                continue
            if attempts >= _AUTO_ARCHIVE_MAX_ATTEMPTS:
                continue
            # 指数退避：旧实现没有退避，30s 一轮的 sweep 会立刻把失败任务重新入队，
            # 三次尝试在一两分钟内烧完（实测 4 条「云端核验未完成」就是这样被反复
            # 重传、又反复撞上 OpenList 探测慢窗口），最后只能由用户手动点重试。
            # 现在每次失败后的等待时间是 30s → 2min → 8min，给云端和网盘一个冷却期。
            if not _archive_retry_due(latest, attempts):
                continue
        lp = _resolve_host_local_path(t.get("local_path"))
        if not lp or not os.path.exists(lp):
            continue
        raw_name = str(t.get("filename") or "未命名")
        custom_note = _CUSTOM_FILE_NOTES.get(uid) or _CUSTOM_FILE_NOTES.get(f"msg:{t.get('_chat_id')}:{t.get('msg_id')}")
        if custom_note:
            raw_name = custom_note
        # 后端偶发不返回 fileName，显示名会退化成「文件 <uniqueId>」（无后缀）。
        # 直接归档会在云端留下没有后缀的文件，播放器/刮削器全认不出来。
        # 这里先用本地路径/MIME 把真实后缀补回来，再做广告清洗。
        raw_name = _ensure_archive_ext(
            raw_name,
            local_path=str(t.get("local_path") or ""),
            mime=str(t.get("_mimeType") or ""),
            ftype=str(t.get("_type") or ""),
        )
        clean_name = _clean_archive_filename(raw_name, enabled=bool(_ARCHIVE_CONFIG.get("cleanFilename", True)))
        # 唯一化（数据丢失级防线，与手动归档同一套逻辑）。
        # 实测背景：订阅规则 c625fdb66130 一路把 22 个不同文件入队到同一个
        # remote_path，云端只剩最后一个、本地已被 deleteLocal 删掉 —— 这条
        # 自动路径原先**完全没有**同名守卫，是事故的主入口。
        clean_name = _archive_unique_name(
            remote_dir, clean_name,
            owner_of=path_owners, uid=uid,
            suffix=_archive_name_suffix(raw_name, clean_name))
        remote_path = _archive_join(remote_dir, clean_name)
        path_owners.setdefault(remote_path.lower(), set()).add(uid)
        job = {
            "id": secrets.token_hex(6),
            "unique_id": uid,
            "filename": clean_name,
            "raw_filename": raw_name,
            "size_bytes": t.get("_size_bytes") if isinstance(t.get("_size_bytes"), (int, float)) else None,
            "local_path": lp,
            "remote_dir": remote_dir,
            "remote_path": remote_path,
            "policy": policy,
            "delete_local": delete_local,
            "state": "queued",
            "progress": 0,
            "error": "",
            "created_at": time.time(),
            "updated_at": time.time(),
            "archived_at": 0.0,
            "auto": True,
            "auto_attempts": attempts + 1,
            "rule_id": rule_id,
        }
        _ARCHIVE_JOBS[job["id"]] = job
        _archive_enqueue(job)
        # 单轮 sweep 的入队上限：sweep 会把「所有已下载待归档」一次性入队
        # （实测单轮 35 个），虽然 _archive_sem 限住了真实并发，但几百个
        # job 同时排队会让状态面板长期显示几十个「归档中」，也让失败重试
        # 与人工操作挤在同一个队列里。这里按并发额度的倍数截断，余下的
        # 下一轮 sweep（30s 后）自动接着入队 —— 不丢任务，只是摊开。
        if enqueued + 1 >= _archive_sweep_batch_limit():
            log.info("自动归档：本轮已达入队上限 %d 个，余下任务下一轮继续",
                     _archive_sweep_batch_limit())
            break
        if rule_id and rule_id not in ("default", "quick") and rule is not None:
            _sub_bump(rule.get("id"), "enqueued")
        elif rule_id == "quick":
            _QUICK_ARCHIVE_REGISTRY.pop(uid, None)
        elif rule_id == "default":
            stats = _ARCHIVE_CONFIG.setdefault("stats", {})
            stats["enqueued"] = int(stats.get("enqueued") or 0) + 1
            stats["lastHitAt"] = time.time()
            _archive_config_save()

        LOG_STORE.append("INFO", "自动归档已入队：%s → %s（%s，%s）" % (
            job["filename"], job["remote_path"], rule_label,
            ("第 %d 次尝试" % job["auto_attempts"]) if attempts else "首次"),
            task_id=str(t.get("id")))
        enqueued += 1

    if enqueued:
        _archive_save()

    try:
        from services.watermark_service import _disk_guard_check, _check_and_wake_waiting_disk_tasks
        await _disk_guard_check()
        await _check_and_wake_waiting_disk_tasks()
    except Exception as e:
        log.debug("自动归档后触发磁盘保护巡检异常: %s", e)

    return enqueued


def _archive_enqueue(job: Dict[str, Any]) -> None:
    """把一个 job 交给 worker（幂等：已在跑就不重复起 task）。"""
    jid = str(job.get("id") or "")
    if not jid:
        return
    t = _ARCHIVE_TASKS.get(jid)
    if t is not None and not t.done():
        return
    _ARCHIVE_TASKS[jid] = asyncio.create_task(_archive_worker(job))


_VERIFY_MAX_ATTEMPTS = 5


async def _archive_reverify_job(job: Dict[str, Any], token: str) -> bool:
    """只做核验、不重传：unverified 任务的复查路径。

    为什么不能直接把 unverified 改回 queued 再丢给 worker —— worker 的第一步是
    「本地文件存在 → 覆盖上传」，那会把几个 GB 重新传一遍，而文件大概率早就
    上传成功了，仅仅是因为 fs/get 超时没查着。所以这里单独做一次核验：
      大小一致 → done（并按 deleteLocal 清理本地）
      大小不符 → failed（这一次是真的没传完整，可以重传）
      仍查不清 → 保持 unverified，等下一轮，超过上限才判失败
    """
    expected = int(job.get("size_bytes") or 0)
    try:
        code, size = await _openlist_verify_size(token, str(job.get("remote_path") or ""))
    except _OpenListAuthErr:
        return False
    except Exception as e:  # noqa: BLE001
        log.debug("待核验任务复查异常: %s", e)
        return False

    now = time.time()
    if code == STAT_OK and expected > 0 and size == expected:
        job["state"] = "done"
        job["progress"] = 100
        job["error"] = ""
        job["archived_at"] = now
        job["updated_at"] = now
        job["remote_size"] = size
        job.pop("verify_pending", None)
        log.info("待核验任务复查通过，判定已归档: %s（已核验 %d 字节）",
                 job["remote_path"], size)
        _archive_save()
        notify_archive_success(job)
        if job.get("delete_local"):
            await _delete_local_file_by_job(job)
        return True

    if code == STAT_OK:
        # 拿到了确定的大小且不符：真的没传完整，交给重传路径
        job["state"] = "failed"
        job["error"] = (
            "上传后云端大小核验不一致（云端 %d 字节 / 本地 %d 字节），判定归档未完成"
            % (size, expected))
        job["updated_at"] = now
        job.pop("verify_pending", None)
        _archive_save()
        log.warning("待核验任务复查发现大小不符: %s（云端 %d / 本地 %d）",
                    job["remote_path"], size, expected)
        notify_archive_failed(job)
        return False

    attempts = int(job.get("verify_attempts") or 0)
    if attempts >= _VERIFY_MAX_ATTEMPTS:
        job["state"] = "failed"
        job["error"] = (
            "上传已完成，但云端大小连续 %d 次无法确认（OpenList 探测超时），"
            "本地文件已保留，可稍后重试" % attempts)
        job["updated_at"] = now
        job.pop("verify_pending", None)
        _archive_save()
        log.warning("待核验任务超过复查上限，判为失败（本地文件保留）: %s", job["remote_path"])
        notify_archive_failed(job)
    return False


def _archive_heal_restart_cancelled() -> int:
    """把「进程退出被误标成用户已取消」的历史记录改回待归档。

    旧代码在 worker 的 CancelledError 分支里不区分来源，一律写
    state=cancelled / error=「用户已取消」。实测线上 30 条里有 9 组的
    updated_at 精确相同（同一秒成对出现，等于并发槽位数），那是 shutdown
    批量 cancel，不是人手点出来的。这些文件还躺在本地、云端没有副本。

    判定（必须同时满足，避免误改真正被用户取消的）：
      * state=cancelled 且没有 user_cancelled 标记（旧记录都没有）；
      * error 恰好是「用户已取消」（重启中断写的也是这条，靠下一条区分）；
      * 同一 uid 没有任何 done 记录（真归档过的不动）；
      * 本地文件仍然存在（文件没了就没法重传，改状态只会制造假待办）。
    """
    done_uids = {
        str(j.get("unique_id") or "")
        for j in _ARCHIVE_JOBS.values()
        if str(j.get("state") or "") == "done" and j.get("unique_id")
    }
    healed = 0
    for job in _ARCHIVE_JOBS.values():
        if str(job.get("state") or "") != "cancelled":
            continue
        if job.get("user_cancelled") or job.get("superseded_by_done"):
            continue
        if str(job.get("error") or "") != "用户已取消":
            continue
        uid = str(job.get("unique_id") or "")
        if uid and uid in done_uids:
            continue
        lp = _resolve_host_local_path(str(job.get("local_path") or ""))
        if not lp or not os.path.exists(lp):
            continue
        job["state"] = "queued"
        job["resume_pending"] = True
        job["interrupted_by_restart"] = True
        job["error"] = "上传被中断（bridge 重启），可重试"
        job["updated_at"] = time.time()
        healed += 1
    if healed:
        _archive_save()
        log.info("归档取消收敛：%d 条「用户已取消」实为进程退出中断，已重新排队", healed)
    return healed


async def _archive_resume_pending() -> int:
    """启动后续跑 + 待核验复查（由 startup 与后台巡检周期调用）。

      1. resume_pending 的 queued 任务 → 重新入队。它们从未开始上传，
         重启时判 failed 只会给用户制造一条「什么都没发生」的告警。
      2. unverified 任务 → 走核验-only 复查（不重传，见 _archive_reverify_job）。
    """
    resumed = 0
    reverified = 0
    token = None
    # 先把僵尸失败收敛掉（同文件后来已 done），再谈续跑/复查：
    # 否则重启后「45 条告警」会原样留着，用户以为修复没生效。
    try:
        _archive_supersede_stale_failures()
    except Exception as e:  # noqa: BLE001
        log.debug("僵尸失败收敛跳过: %s", e)
    # 修正历史误标：旧代码在进程退出时也写「用户已取消」，这些文件其实还在
    # 本地、从未归档。清掉用户取消语义并排回队列，让自动归档重新接手。
    try:
        _archive_heal_restart_cancelled()
    except Exception as e:  # noqa: BLE001
        log.debug("重启误标取消收敛跳过: %s", e)
    for job in list(_ARCHIVE_JOBS.values()):
        st = str(job.get("state") or "")
        if job.get("resume_pending") and st == "queued":
            job.pop("resume_pending", None)
            job["updated_at"] = time.time()
            _archive_enqueue(job)
            resumed += 1
            continue
        if st == "unverified":
            jid = str(job.get("id") or "")
            running = _ARCHIVE_TASKS.get(jid)
            if running is not None and not running.done():
                continue
            if token is None:
                try:
                    token = await _openlist_token()
                except Exception as e:  # noqa: BLE001
                    log.debug("待核验复查跳过（OpenList 不可用）: %s", e)
                    break
            if await _archive_reverify_job(job, token):
                reverified += 1
    if resumed:
        _archive_save()
        log.info("归档续跑：%d 条排队任务重新入队", resumed)
    return resumed + reverified


def _archive_supersede_stale_failures() -> int:
    """收敛僵尸失败记录：同一文件后来已归档成功时，旧失败记录不再是「异常」。

    线上实测：45 条「上传中断」里有 41 条，其对应 unique_id 早已有另一条
    state=done 的记录（重试后成功），但失败记录本身仍留在列表里，于是告警
    中心永远显示几十条红色条目，用户点重试只会再跑一遍已完成的归档。
    这里把它们标成 superseded_cancelled（终态、不再是 failed），保留记录本身
    以便追溯，只是不再计入告警。
    """
    done_uids = {
        str(j.get("unique_id") or "")
        for j in _ARCHIVE_JOBS.values()
        if str(j.get("state") or "") == "done" and j.get("unique_id")
    }
    if not done_uids:
        return 0
    n = 0
    now = time.time()
    for j in _ARCHIVE_JOBS.values():
        if str(j.get("state") or "") != "failed":
            continue
        uid = str(j.get("unique_id") or "")
        if not uid or uid not in done_uids:
            continue
        j["state"] = "cancelled"
        j["error"] = ""
        j["superseded_by_done"] = True
        j["updated_at"] = now
        n += 1
    if n:
        _archive_save()
        log.info("归档告警收敛：%d 条失败记录的同一文件已归档成功，已标记为已被取代", n)
    return n


async def _archive_heal_false_failures() -> int:
    """复核历史假失败：云端大小与本地一致时直接改判 done。

    背景（线上实测）：旧版核验只发一次 fs/get，超时即判「上传后云端存在性
    核验失败」。事后逐条核对，这些文件的云端大小与本地字节数完全一致 ——
    数据其实早就归档成功了，只是告警一直挂着。这里在启动后与后续巡检里
    自动纠正，用户不需要手动点重试。
    """
    candidates = [
        j for j in _ARCHIVE_JOBS.values()
        if str(j.get("state") or "") == "failed"
        and "核验失败" in str(j.get("error") or "")
    ]
    if not candidates:
        return 0
    try:
        token = await _openlist_token()
    except Exception as e:  # noqa: BLE001
        log.debug("复核假失败跳过（OpenList 不可用）: %s", e)
        return 0
    healed = 0
    for job in candidates:
        expected = int(job.get("size_bytes") or 0)
        if expected <= 0:
            continue
        try:
            code, size = await _openlist_verify_size(token, str(job.get("remote_path") or ""))
        except _OpenListAuthErr:
            break
        except Exception as e:  # noqa: BLE001
            log.debug("复核假失败探测异常: %s", e)
            continue
        if code == STAT_OK and size == expected:
            job["state"] = "done"
            job["progress"] = 100
            job["error"] = ""
            job["archived_at"] = job.get("archived_at") or time.time()
            job["updated_at"] = time.time()
            job["remote_size"] = size
            job["healed_from_false_failure"] = True
            healed += 1
    if healed:
        _archive_save()
        log.info("归档告警自愈：%d 条「核验失败」实为云端已完整，已自动改判为已归档", healed)
    return healed


async def _auto_archive_loop() -> None:
    """后台周期扫描（启动时由 _startup 挂起，进程生命周期内常驻）。"""
    from services.subscription_service import _AUTO_ARCHIVE_FIRST_DELAY, _AUTO_ARCHIVE_INTERVAL
    try:
        await asyncio.sleep(_AUTO_ARCHIVE_FIRST_DELAY)
        while True:
            try:
                from services.watermark_service import _disk_guard_check, _check_and_wake_waiting_disk_tasks
                n = await _auto_archive_sweep()
                if n:
                    log.info("自动归档：本轮新入队 %d 个文件", n)
                # 复核历史假失败（核验超时被误判的那批）：云端其实完整就直接转 done。
                # 没有候选时内部是零成本早退，所以每轮都调是安全的。
                try:
                    await _archive_heal_false_failures()
                except Exception as e:  # noqa: BLE001
                    log.debug("归档假失败复核跳过: %s", e)
                # 「待核验」任务的周期复查（只核验、不重传，见 _archive_reverify_job）
                try:
                    await _archive_resume_pending()
                except Exception as e:  # noqa: BLE001
                    log.debug("待核验复查跳过: %s", e)
                await _disk_guard_check()
                await _check_and_wake_waiting_disk_tasks()
                # 过期取回残片清理（失败保留的 .part 供断点续传，但不能永久堆积）
                try:
                    from services.retrieve_service import _retrieve_parts_cleanup
                    _retrieve_parts_cleanup()
                except Exception as e:  # noqa: BLE001
                    log.debug("残片清理跳过: %s", e)
            except Exception as e:  # noqa: BLE001
                log.warning("自动归档扫描异常: %s", e)
            await asyncio.sleep(_AUTO_ARCHIVE_INTERVAL)
    except asyncio.CancelledError:
        return
