# -*- coding: utf-8 -*-

"""
routers/api.py — 表现层路由模块：全局聚合搜索、文件指纹查重、链接解析与流媒体切片分发
"""
from core import *
from services import *
import os
import time
import asyncio
from typing import Any, Dict, List
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse



router = APIRouter()

@router.get("/api/speeds")
async def api_speeds():
    """当前下载/上传速率（KPI 卡片实时轮询端点）。

    下载速率 = 所有下载中任务 speed 之和（tasks_all 回源时由
    downloadedSize 差分得出）；上传速率 = 归档引擎 uploading 任务的
    size × progress 差分。前端每 2s 轮询本端点刷新 KPI 卡片数字。
    """
    try:
        dl_bps = await _download_speed_snapshot()
        up_bps = await _upload_speed_snapshot()
        _record_speed_sample(dl_bps, up_bps)
        return {
            "ok": True,
            "download": {"bps": round(dl_bps, 1), "label": _fmt_speed(dl_bps)},
            "upload": {"bps": round(up_bps, 1), "label": _fmt_speed(up_bps)},
        }
    except Exception as e:  # noqa: BLE001
        log.warning("速率查询失败: %s", e)
        return {"ok": False,
                "download": {"bps": 0, "label": "0 B/s"},
                "upload": {"bps": 0, "label": "0 B/s"}}


@router.get("/api/search")
@router.get("/api/search/aggregate")
async def api_search_aggregate(request: Request, q: str = "", limit: int = 15):
    """跨库全局聚合搜索接口：
    接收关键词 q，同时并发检索 任务库 (tasks)、本地资产 (local)、云端归档 (cloud) 三大集合。
    具备输入长度防 DoS 截断与防刷限流机制。
    """
    ip = _client_ip(request)
    if _search_rate_limited(ip):
        return JSONResponse({"ok": False, "code": "RATE_LIMITED", "message": "搜索请求过于频繁，请稍后再试"}, status_code=429)

    keyword = str(q or "").strip()[:100]
    limit = max(1, min(50, int(limit or 15)))

    if not keyword:
        return {
            "ok": True,
            "query": "",
            "total": 0,
            "counts": {"tasks": 0, "local": 0, "cloud": 0},
            "results": {"tasks": [], "local": [], "cloud": []}
        }

    tokens = [t.lower() for t in keyword.split() if t][:8]

    def _matches(text: str) -> bool:
        if not text:
            return False
        low = text.lower()
        return all(tok in low for tok in tokens)

    # 预加载全量任务快照（一次查询复用于 tasks 栏与 local 栏，避免重复 I/O 与 CPU 开销）
    tasks_res = []
    local_res = []
    seen_local = set()
    try:
        all_tasks = await tasks_all()
    except Exception as e:
        log.warning("聚合搜索 tasks 异常: %s", e)
        all_tasks = []

    # 1. 任务库匹配（正在进行/排队中）
    for t in all_tasks:
        st = str(t.get("status") or "")
        dl_st = str(t.get("_download_status") or "").lower()
        if dl_st == "completed" or st.lower() == "completed":
            continue
        fn = str(t.get("filename") or "")
        src = str(t.get("source") or "")
        uid = str(t.get("_unique_id") or t.get("uniqueId") or "")
        if _matches(fn) or _matches(src) or (uid and _matches(uid)):
            tasks_res.append({
                "id": t.get("id"),
                "uniqueId": uid,
                "filename": fn or "未知文件",
                "size": t.get("size") if isinstance(t.get("size"), str) else _fmt_size(t.get("size")),
                "status": st,
                "progress": t.get("progress", 0),
                "source": src or "会话任务",
                "time": t.get("time") or "",
                "actionUrl": f"/tasks/{t.get('id')}",
                "localPath": str(t.get("local_path") or ""),
            })

    # 2. 本地在存资产匹配
    for t in all_tasks:
        st = str(t.get("_download_status") or t.get("status") or "").lower()
        lp_raw = str(t.get("local_path") or t.get("localPath") or "")
        lp = _resolve_host_local_path(lp_raw) if (lp_raw and lp_raw != "—") else ""
        uid = str(t.get("_unique_id") or t.get("uniqueId") or "")
        fn = str(t.get("filename") or t.get("name") or "")
        src = str(t.get("source") or "")

        if st == "completed" and lp and os.path.exists(lp) and uid not in _DELETED_LOCAL_UIDS:
            k = uid or lp
            if k in seen_local:
                continue
            seen_local.add(k)
            if _matches(fn) or _matches(lp) or (uid and _matches(uid)) or _matches(src):
                local_res.append({
                    "uniqueId": uid,
                    "filename": fn or "本地文件",
                    "size": t.get("size") if isinstance(t.get("size"), str) else _fmt_size(t.get("size")),
                    "localPath": lp,
                    "source": src or "本地在存",
                    "time": t.get("time") or "",
                    "actionUrl": "/library/local",
                })

    # 3. 云端归档匹配（check_remote=False 纯内存索引检索，零外部网络 I/O 阻塞）
    cloud_res = []
    try:
        all_cloud = await _cloud_archive_rows(check_remote=False)
        for c in all_cloud:
            fn = str(c.get("filename") or "")
            rp = str(c.get("cloud_path") or "")
            drv = str(c.get("drive") or "")
            uid = str(c.get("unique_id") or "")
            if _matches(fn) or _matches(rp) or _matches(drv) or (uid and _matches(uid)):
                cloud_res.append({
                    "id": c.get("id"),
                    "uniqueId": uid,
                    "filename": fn or "云端文件",
                    "size": c.get("size") or "",
                    "cloudPath": rp,
                    "drive": drv,
                    "archivedTime": c.get("archived_time") or "",
                    "openlistUrl": c.get("openlist_url") or _openlist_direct_url(rp),
                    "actionUrl": "/library/cloud",
                })
    except Exception as e:
        log.warning("聚合搜索 cloud 异常: %s", e)

    return {
        "ok": True,
        "query": keyword,
        "total": len(tasks_res) + len(local_res) + len(cloud_res),
        "counts": {
            "tasks": len(tasks_res),
            "local": len(local_res),
            "cloud": len(cloud_res),
        },
        "results": {
            "tasks": tasks_res[:limit],
            "local": local_res[:limit],
            "cloud": cloud_res[:limit],
        }
    }


@router.post("/api/files/check-dedup")
async def api_files_check_dedup(request: Request):
    """全局 uniqueId 与文件指纹查重检测端点：
    入参：{uniqueId, filename, size, link}
    返回：duplicate (bool), duplicateType ('cloud' | 'local' | 'task' | 'none'), asset 资产位置及一键操作选项。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    uid = str(body.get("uniqueId") or "").strip()
    fn = str(body.get("filename") or body.get("name") or "").strip()
    sz = body.get("size")
    link = str(body.get("link") or "").strip()

    # 若仅传 link 未传 uniqueId，尝试自动解析
    if link and not uid:
        try:
            parsed = validate_and_parse_tg_link(link)
            if parsed.get("ok"):
                canonical_url = parsed["canonical_url"]
                sources = await chat_sources()
                for s in sources:
                    tg_id = s.get("telegramId")
                    if tg_id is not None:
                        try:
                            recs = await BACKEND.resolve_link(tg_id, canonical_url)
                            if recs:
                                f0 = recs[0]
                                uid = str(_pick_id(f0) or "")
                                if not fn:
                                    fn = _human_name(f0)
                                if sz is None:
                                    sz = f0.get("size")
                                break
                        except Exception:
                            continue
        except Exception as e:
            log.warning("check-dedup 自动解析 link 异常: %s", e)

    res = await _check_file_dedup(unique_id=uid, filename=fn, size_bytes=sz)
    return JSONResponse({
        "ok": True,
        "code": "SUCCESS",
        "data": res
    })


@router.post("/api/tg/resolve-link")
async def api_tg_resolve_link(request: Request):
    """解析 Telegram 消息链接，获取文件元数据（文件名、大小、类型、指纹对比等）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "code": "INVALID_JSON", "message": "请求体不是合法 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "code": "INVALID_BODY", "message": "请求体必须为 JSON 对象"}, status_code=400)

    link = str(body.get("link") or "").strip()
    if not link:
        return JSONResponse({"ok": False, "code": "MISSING_LINK", "message": "缺少 link 参数"}, status_code=400)

    parsed_info = validate_and_parse_tg_link(link)
    if not parsed_info.get("ok"):
        return JSONResponse({
            "ok": False,
            "code": parsed_info.get("code", "INVALID_LINK_FORMAT"),
            "message": parsed_info.get("message", "链接解析失败")
        }, status_code=400)

    canonical_url = parsed_info["canonical_url"]

    # 确定 Telegram 客户端账号
    tg_id = body.get("telegramId")
    available_sources = []
    try:
        available_sources = await chat_sources()
    except Exception as e:
        log.warning("获取 chat_sources 失败: %s", e)

    if not available_sources:
        return JSONResponse({
            "ok": False,
            "code": "TG_ACCOUNT_UNAVAILABLE",
            "message": "当前未登录任何有效的 Telegram 账号，请先完成 TG 账号登录"
        }, status_code=409)

    candidates = []
    if tg_id is not None:
        candidates.append(tg_id)
    for s in available_sources:
        sid = s.get("telegramId")
        if sid is not None and sid not in candidates:
            candidates.append(sid)

    recs: List[Dict[str, Any]] = []
    resolved_tg_id = None
    last_err = None
    for cand in candidates:
        try:
            got = await BACKEND.resolve_link(cand, canonical_url)
            if got:
                recs = got
                resolved_tg_id = cand
                break
        except Exception as e:
            last_err = e
            log.warning("账号 %s 解析链接 %s 失败: %s", cand, canonical_url, e)
            continue

    if not recs:
        if last_err:
            return JSONResponse({
                "ok": False,
                "code": "TG_BACKEND_ERROR",
                "message": f"解析失败：{_tg_err_public(last_err)}"
            }, status_code=502)
        return JSONResponse({
            "ok": False,
            "code": "NO_FILES_FOUND",
            "message": "该消息中未找到可下载的媒体文件（可能为纯文本、已被删除或无访问权限）"
        }, status_code=404)

    # 提取多媒体元数据
    files_data = []
    for rec in recs:
        fid = rec.get("fileId") or rec.get("id")
        if not fid and not rec.get("messageId"):
            continue
        uid = _pick_id(rec)
        uid_str = str(uid or "")
        filename = _human_name(rec)
        size = rec.get("size")
        size_bytes = size if isinstance(size, (int, float)) else None
        size_human = _fmt_size(size)
        ftype = _classify_file_type(rec.get("type"), rec.get("mimeType"))
        chat_id = rec.get("chatId")
        msg_id = rec.get("messageId") or parsed_info.get("message_id")
        chat_title = _chat_title(rec) or "未知频道"

        # 查重与归档状态智能关联
        dedup_info = await _check_file_dedup(unique_id=uid_str, filename=filename, size_bytes=size_bytes)
        is_archived = (dedup_info.get("duplicateType") == "cloud")
        is_downloaded = (dedup_info.get("duplicateType") == "local" or (str(rec.get("downloadStatus") or "").lower() == "completed"))
        if not is_archived:
            arch_job = _archive_latest_raw_of(uid_str) if uid_str else None
            if arch_job and arch_job.get("state") == "done":
                is_archived = True

        files_data.append({
            "fileId": fid,
            "uniqueId": uid_str,
            "filename": filename,
            "size": size,
            "sizeHuman": size_human,
            "fileType": ftype,
            "mimeType": str(rec.get("mimeType") or ""),
            "chatId": chat_id,
            "chatTitle": chat_title,
            "messageId": msg_id,
            "telegramId": rec.get("telegramId") or resolved_tg_id,
            "thumbnail": str(rec.get("thumbnail") or ""),
            "isAlreadyDownloaded": is_downloaded,
            "isAlreadyArchived": is_archived,
            "dedup": dedup_info,
        })

    if not files_data:
        return JSONResponse({
            "ok": False,
            "code": "NO_FILES_FOUND",
            "message": "该消息中未找到可下载的媒体文件"
        }, status_code=404)

    return JSONResponse({
        "ok": True,
        "code": "SUCCESS",
        "message": "解析成功",
        "data": {
            "link": link,
            "canonicalUrl": canonical_url,
            "linkType": parsed_info["link_type"],
            "chatIdentifier": parsed_info["chat_identifier"],
            "messageId": parsed_info["message_id"],
            "telegramId": resolved_tg_id,
            "files": files_data,
        }
    })


@router.post("/api/tg/quick-download")
async def api_tg_quick_download(request: Request):
    """一键直投下载端点：接收 link 或 files，调起后台下载队列，并可联动自动归档。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "code": "INVALID_JSON", "message": "请求体不是合法 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "code": "INVALID_BODY", "message": "请求体必须为 JSON 对象"}, status_code=400)

    raw_files = body.get("files")
    auto_archive = body.get("autoArchive")
    if auto_archive is None:
        auto_archive = bool(_ARCHIVE_CONFIG.get("autoArchive", False))
    else:
        auto_archive = bool(auto_archive)

    archive_dir = _archive_norm_dir(str(body.get("archiveDir") or _ARCHIVE_CONFIG.get("defaultDir") or "/"))
    policy = "skip" if str(body.get("policy") or _ARCHIVE_CONFIG.get("policy") or "").lower() == "skip" else "overwrite"
    delete_local = bool(body.get("deleteLocal", _ARCHIVE_CONFIG.get("deleteLocal", False)))

    payload_files = []

    if isinstance(raw_files, list) and raw_files:
        for f in raw_files:
            try:
                fid = f.get("fileId") or f.get("id")
                cid = f.get("chatId")
                mid = f.get("messageId")
                tid = f.get("telegramId")
                if fid and cid and mid and tid:
                    payload_files.append({
                        "telegramId": int(tid),
                        "chatId": int(cid),
                        "messageId": int(mid),
                        "fileId": int(fid),
                        "uniqueId": f.get("uniqueId") or f.get("id") or str(fid),
                        "filename": f.get("filename") or "",
                        "size": f.get("size") or 0,
                    })
                    uid = str(f.get("uniqueId") or "")
                    if uid and auto_archive and archive_dir and archive_dir != "/":
                        _QUICK_ARCHIVE_REGISTRY[uid] = {
                            "remoteDir": archive_dir,
                            "policy": policy,
                            "deleteLocal": delete_local,
                            "created_at": time.time(),
                        }
            except (TypeError, ValueError):
                continue

    link = str(body.get("link") or "").strip()
    if not payload_files and link:
        parsed_info = validate_and_parse_tg_link(link)
        if not parsed_info.get("ok"):
            return JSONResponse({
                "ok": False,
                "code": parsed_info.get("code", "INVALID_LINK_FORMAT"),
                "message": parsed_info.get("message", "链接解析失败")
            }, status_code=400)

        canonical_url = parsed_info["canonical_url"]
        sources = await chat_sources()
        if not sources:
            return JSONResponse({
                "ok": False,
                "code": "TG_ACCOUNT_UNAVAILABLE",
                "message": "尚未登录任何 Telegram 账号"
            }, status_code=409)

        target_tg_id = body.get("telegramId")
        cands = [target_tg_id] if target_tg_id else [s.get("telegramId") for s in sources if s.get("telegramId")]

        recs = []
        for cand in cands:
            try:
                recs = await BACKEND.resolve_link(cand, canonical_url)
                if recs:
                    for rec in recs:
                        fid = rec.get("fileId") or rec.get("id")
                        cid = rec.get("chatId")
                        mid = rec.get("messageId") or parsed_info.get("message_id")
                        if fid and cid and mid:
                            payload_files.append({
                                "telegramId": int(rec.get("telegramId") or cand),
                                "chatId": int(cid),
                                "messageId": int(mid),
                                "fileId": int(fid),
                            })
                            uid = str(_pick_id(rec) or "")
                            if uid and auto_archive and archive_dir and archive_dir != "/":
                                _QUICK_ARCHIVE_REGISTRY[uid] = {
                                    "remoteDir": archive_dir,
                                    "policy": policy,
                                    "deleteLocal": delete_local,
                                    "created_at": time.time(),
                                }
                    if payload_files:
                        break
            except Exception as e:
                log.warning("quick-download 逐账号解析失败: %s", e)
                continue

    if not payload_files:
        return JSONResponse({
            "ok": False,
            "code": "NO_FILES_TO_DOWNLOAD",
            "message": "未找到可下载的媒体文件"
        }, status_code=400)

    # 查重检测与智能拦截（非 force 时）：已在云端网盘或本地在存均拦截
    force = bool(body.get("force", False))
    if not force and payload_files:
        tasks_list = None
        try:
            tasks_list = await tasks_all()
        except Exception:
            tasks_list = []
        clean_payload = []
        skipped_dup = 0
        last_dup_res = None
        cand_files = (raw_files if isinstance(raw_files, list) else []) + (recs if (link and 'recs' in locals() and isinstance(recs, list)) else [])
        for item in payload_files:
            uid = ""
            fn = ""
            sz = None
            for rf in cand_files:
                rf_fid = rf.get("fileId") or rf.get("id")
                if rf_fid == item.get("fileId"):
                    uid = str(_pick_id(rf) or rf.get("uniqueId") or "")
                    fn = str(rf.get("filename") or rf.get("name") or "")
                    sz = rf.get("size")
                    break
            dedup_res = await _check_file_dedup(unique_id=uid, filename=fn, size_bytes=sz, tasks_list=tasks_list)
            if dedup_res.get("duplicate"):
                skipped_dup += 1
                last_dup_res = dedup_res
            else:
                clean_payload.append(item)
        if not clean_payload and last_dup_res:
            return JSONResponse({
                "ok": False,
                "code": "DUPLICATE_ASSET",
                "duplicateType": last_dup_res.get("duplicateType"),
                "message": last_dup_res.get("message") or last_dup_res.get("prompt"),
                "asset": last_dup_res.get("asset"),
                "actions": last_dup_res.get("actions"),
                "skippedDuplicates": skipped_dup
            }, status_code=409)
        payload_files = clean_payload

    # 磁盘高低水位熔断保护：85% 熔断挂起 / 75% 唤醒
    guard = await _disk_guard_or_enqueue(raw_files or recs, payload_files, "quick-download")
    if guard is not None:
        return JSONResponse(guard)

    if _is_flood_wait_active():
        st = _get_flood_wait_status()
        rem = st.get("remainingSeconds", 0)
        reason = st.get("reason", "FLOOD_WAIT")
        suspended = _FLOOD_WAIT_STATE.setdefault("suspended_tasks", {})
        for pf in payload_files:
            uid = str(pf.get("uniqueId") or pf.get("id") or "")
            if uid:
                suspended[uid] = {
                    "id": f"flood-{uid}",
                    "uniqueId": uid,
                    "filename": pf.get("filename") or "待下载媒体",
                    "size": pf.get("size") or 0,
                    "size_str": _fmt_size(pf.get("size")),
                    "created_at": time.time(),
                }
        _flood_wait_save()
        msg = f"Telegram 账号正处于风控保护中 ({reason})，已将 {len(payload_files)} 个任务安全置入冷却挂起队列，预计剩余 {rem} 秒自动恢复调度"
        log.warning("FloodWait 限流拦截 [quick-download]：%s", msg)
        return JSONResponse({
            "ok": True,
            "code": "FLOOD_WAIT_SUSPENDED",
            "state": "flood_wait",
            "remainingSeconds": rem,
            "count": len(payload_files),
            "message": msg,
        })

    try:
        backend_files = [
            {"telegramId": f["telegramId"], "chatId": f["chatId"], "messageId": f["messageId"], "fileId": f["fileId"]}
            for f in payload_files
        ]
        await BACKEND.start_download_multiple({"files": backend_files})
        _tasks_cache_invalidate()
        BACKEND._cache.clear()

        log.info("直投下载成功提交 %d 个文件 (auto_archive=%s, dir=%s)", len(payload_files), auto_archive, archive_dir)
        return JSONResponse({
            "ok": True,
            "code": "SUCCESS",
            "count": len(payload_files),
            "autoArchive": auto_archive,
            "archiveDir": archive_dir if auto_archive else None,
            "message": f"成功提交 {len(payload_files)} 个文件直投下载任务"
        })
    except Exception as e:
        # 若下载调用触发了 FloodWait，自动捕获挂起
        w_sec = _extract_flood_wait_seconds(e)
        if w_sec:
            _trigger_flood_wait("default", w_sec, reason=f"FLOOD_WAIT_{w_sec}")
            return JSONResponse({
                "ok": True,
                "code": "FLOOD_WAIT_SUSPENDED",
                "state": "flood_wait",
                "remainingSeconds": w_sec,
                "count": len(payload_files),
                "message": f"Telegram 服务端触发风控限流，已自动将任务挂起并进入 {w_sec} 秒冷却倒计时"
            })
        log.error("直投下载调用后端失败: %s", e)
        return JSONResponse({
            "ok": False,
            "code": "BACKEND_ERROR",
            "message": f"直投下载提交失败：{_tg_err_public(e)}"
        }, status_code=502)


@router.get("/api/tg/floodwait/status")
async def api_tg_floodwait_status():
    """获取当前 Telegram FloodWait 智能冷却与挂起状态。"""
    st = _get_flood_wait_status()
    return {"ok": True, "data": st}


@router.post("/api/tg/floodwait/reset")
async def api_tg_floodwait_reset():
    """管理员强制重置 Telegram FloodWait 冷却状态并唤醒挂起任务。"""
    _reset_flood_wait()
    woken = await _wake_flood_wait_tasks()
    return {"ok": True, "message": f"已成功强制清除 FloodWait 冷却状态，恢复了 {woken} 条挂起调度"}


@router.get("/preview/{telegram_id}/{unique_id}")
async def preview_file(request: Request, telegram_id: str, unique_id: str,
                       chat: str = "", msg: str = "", heal: str = ""):
    """后端文件端点代理：浏览器只认 bridge，全尺寸缩略图经此转发。

    heal=1：调用方（本地在存旧记录无 thumbnailUniqueId）传入的是**主文件**
    uniqueId——跳过直取（主文件可能是几百 MB 视频，_request 会整包缓冲），
    直接走 GetMessage 补图定位缩略图，再取缩略图字节。

    telegram_id 过 _safe_id 白名单、unique_id 过 base64 字符白名单（双重校验
    在 fetch_file_bytes 内），失败一律 404 不回显后端细节。缩略图不可变，
    允许浏览器私有缓存一天，翻页不重复回源。chat/msg 为可选的定位参数，
    供缩略图未就绪时经 GetMessage 补下载。
    """
    if heal not in ("1", "true", "yes"):
        try:
            data, ctype = await BACKEND.fetch_file_bytes(telegram_id, unique_id)
            return Response(content=data, media_type=ctype,
                            headers={"Cache-Control": "private, max-age=86400"})
        except Exception:
            pass
    # 缩略图未在 TDLib 本地（后端 500 "not downloaded"）：补下载一次。
    # 注意补图后引用可能漂移 —— 用 heal 返回的**当前** uniqueId 取字节；
    # 且补图完成后后端的 uid 映射经 WS 异步更新，带两次短退避重试。
    new_uid = await _heal_thumbnail(unique_id, chat, msg)
    if not new_uid:
        return Response(content=b"", status_code=404, media_type="image/jpeg")
    for wait in (0.0, 0.5, 1.5):
        if wait:
            await asyncio.sleep(wait)
        try:
            data, ctype = await BACKEND.fetch_file_bytes(telegram_id, new_uid)
            return Response(content=data, media_type=ctype,
                            headers={"Cache-Control": "private, max-age=86400"})
        except Exception:
            continue
    log.info("preview %s/%s 补图后仍失败 (new uid %s)", telegram_id, unique_id, new_uid)
    return Response(content=b"", status_code=404, media_type="image/jpeg")

