# -*- coding: utf-8 -*-

"""
routers/tasks.py — 表现层路由模块：任务列表、任务详情、链接直投提交与任务重试/取消
"""
from core import *
from services import *
import time
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse



router = APIRouter()

# 任务表分页：一页 50 条。历史实现把 900+ 条任务一次性塞进 HTML（1.6 MB），
# 服务端序列化 + 浏览器建 DOM 都要几百毫秒，切页明显发顿。
# 现在首屏只渲染第一页，其余由「加载更多」按 htmx 追加（已渲染的行不重建，
# 展开/勾选状态不丢）。
TASKS_PAGE_SIZE = 50
TASKS_PAGE_MAX = 1000      # 「加载更多」/自动刷新一次最多回多少条，防被撑爆


def _filter_tasks(all_tasks, status="", source="", date_from="", date_to=""):
    """与历史 /partials/tasks 完全一致的口径（状态 / 来源 / 日期）。"""
    filtered = list(all_tasks)
    if status:
        # active/unarchived/hide_archived 都是「排除已归档」这一个语义
        if status in ("active", "unarchived", "hide_archived"):
            filtered = [t for t in filtered if t.get("status") != "archived"]
        else:
            filtered = [t for t in filtered if t.get("status") == status]
    if source:
        filtered = [t for t in filtered if t.get("source") == source]
    import datetime as _dt
    try:
        if date_from:
            ts_from = _dt.datetime.combine(_dt.date.fromisoformat(date_from), _dt.time.min).timestamp()
            filtered = [t for t in filtered if t.get("_date_ts") and t["_date_ts"] >= ts_from]
        if date_to:
            ts_to = _dt.datetime.combine(_dt.date.fromisoformat(date_to), _dt.time.max).timestamp()
            filtered = [t for t in filtered if t.get("_date_ts") and t["_date_ts"] <= ts_to]
    except ValueError:
        pass  # 非法日期串直接忽略该过滤条件
    return filtered


def _page_slice(filtered, offset=0, limit=TASKS_PAGE_SIZE):
    """把过滤结果切成一段，并返回渲染所需的页码参数。"""
    try:
        limit = int(limit or TASKS_PAGE_SIZE)
    except (TypeError, ValueError):
        limit = TASKS_PAGE_SIZE
    try:
        offset = int(offset or 0)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, TASKS_PAGE_MAX))
    offset = max(0, offset)
    return filtered[offset:offset + limit], offset, limit, len(filtered)

@router.get("/tasks", response_class=HTMLResponse)
async def tasks(request: Request):
    all_tasks = await tasks_all()
    page, offset, limit, total = _page_slice(all_tasks, 0, TASKS_PAGE_SIZE)
    ctx = await _ctx(request, "tasks", "tasks", extra={
        "tasks": page,
        "tasks_total": total,
        "tasks_offset": offset,
        "tasks_limit": limit,
        "chat_sources": await chat_sources(),
    })
    return templates.TemplateResponse("tasks.html", ctx)


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: str):
    tasks_list = await tasks_all()
    # 查无此 id 必须落到「任务不存在」占位，不能静默回退成第一条任务
    task = next((t for t in tasks_list if str(t["id"]) == str(task_id)), None)
    if task is None:
        task = {
            "id": task_id, "time": "—", "source": "—", "msg_id": "—", "filename": "任务不存在",
            "size": "—", "status": "failed", "loaded": "—", "progress": 0,
            "local_path": "—", "cloud_path": "—", "source_url": "",
            "error_msg": "后端未返回该任务", "stages": _stages("failed"),
            "_unique_id": None, "_telegram_id": None,
        }
    ctx = await _ctx(request, "task-detail", "tasks", extra={"task": task})
    return templates.TemplateResponse("task_detail.html", ctx)


@router.post("/tasks", response_class=HTMLResponse)
async def post_task(request: Request):
    """顶栏模态的 hx-post='/tasks' 与 fetch JSON /tasks 转发（提交 links 给后端批量下载）。

    每行格式：`链接` 或 `链接 备注名`（备注间可用空格/制表符/｜/|/, 分隔）。
    备注名会绑定到解析出的文件上，作为下载与归档的最终文件名。
    """
    ok = False
    message = ""
    try:
        content_type = request.headers.get("content-type", "")
        body_notes = {}
        if "application/json" in content_type:
            body = await request.json()
            links = body.get("links", "") if isinstance(body, dict) else ""
            if isinstance(body, dict) and isinstance(body.get("notes"), dict):
                body_notes = {str(k): str(v) for k, v in body["notes"].items() if str(v or "").strip()}
        else:
            form = await request.form()
            links = str(form.get("links", ""))
        # links 可能是字符串（每行一条）也可能是数组（fetch JSON 路径）；
        # 数组元素允许是 {"url": "...", "note": "..."} 或 "链接 备注" 字符串。
        if isinstance(links, list):
            raw_lines = [str(l) for l in links]
        else:
            raw_lines = str(links).splitlines()
        links_clean, notes_by_link = _split_link_notes(raw_lines)
        # 前端显式传来的 notes（链接→备注名）优先级最高
        for k, v in body_notes.items():
            notes_by_link[k.strip()] = v.strip()
        if links_clean:
            ok, err = await _resolve_links_to_files(links_clean, notes_by_link=notes_by_link)
            if ok:
                message = "已提交 %d 条链接" % ok
            else:
                message = err or "提交失败"
        else:
            message = "没有可识别的链接"
    except Exception as e:  # noqa: BLE001
        log.error("批量下载(任务)提交失败: %s", e)
        message = "提交失败：" + _tg_err_public(e)
    # htmx 请求返回局部表格；fetch JSON 请求返回结果对象
    if "application/json" in request.headers.get("content-type", ""):
        return JSONResponse({"ok": ok, "message": message})
    _all = await tasks_all(force=True)
    _page, _off, _lim, _total = _page_slice(_all, 0, TASKS_PAGE_SIZE)
    table_ctx = await _ctx(request, "tasks", "tasks", extra={
        "tasks": _page,
        "tasks_total": _total,
        "tasks_offset": _off,
        "tasks_limit": _lim,
        "submit_error": message if not ok else "",
        "submit_ok": ok if ok else 0,
    })
    return templates.TemplateResponse("partials/_tasks_table.html", table_ctx)


@router.get("/submit", response_class=HTMLResponse)
async def submit(request: Request):
    sources = await chat_sources()
    # 注入来源到模板上下文（模板本身是 Alpine 前端校验，这里只是让 /submit 可查）
    ctx = await _ctx(request, "submit", "submit", extra={"chat_sources": sources})
    return templates.TemplateResponse("submit.html", ctx)


@router.post("/submit", response_class=HTMLResponse)
async def submit_post(request: Request):
    """提交下载：解析 links（每行一条 t.me 链接），两跳转成批量下载。"""
    form = await request.form()
    links = str(form.get("links", ""))
    force = bool(str(form.get("force") or "").lower() in ("1", "true", "yes"))
    lines = [l.strip() for l in links.splitlines() if l.strip()]
    error = ""
    ok_count = 0
    if lines:
        guard = await _disk_guard_or_enqueue_links(lines, "submit")
        if guard is not None:
            error = guard["message"]
            log.warning("磁盘水位熔断拦截 [/submit]：%s", error)
        else:
            ok_count, error = await _resolve_links_to_files(lines, force=force)
            if error:
                log.warning("submit 提交失败: %s", error)
            elif ok_count:
                log.info("已提交下载：成功解析 %s/%s 条链接", ok_count, len(lines))
    else:
        error = "没有输入任何链接"
    _all = await tasks_all(force=True)
    _page, _off, _lim, _total = _page_slice(_all, 0, TASKS_PAGE_SIZE)
    table_ctx = await _ctx(request, "tasks", "tasks", extra={
        "tasks": _page,
        "tasks_total": _total,
        "tasks_offset": _off,
        "tasks_limit": _lim,
        "submit_error": error,
        "submit_ok": ok_count,
    })
    return templates.TemplateResponse("partials/_tasks_table.html", table_ctx)


@router.post("/task/retry")
async def task_retry(request: Request):
    """重试任务。前端传 {uniqueId, telegramId}；后端契约要 {chatId, messageId, fileId}。

    bridge 负责把 uniqueId 解析成真实字段（任务缓存内查找），前端接口保持不变。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "请求体不是合法 JSON"}
    unique_id = (body or {}).get("uniqueId")
    if not unique_id:
        return {"ok": False, "message": "缺少 uniqueId"}
    rec = _find_task_by_uid(await tasks_all(force=True), unique_id)
    if rec is None:
        return {"ok": False, "message": "任务不存在或已被移除"}
    telegram_id = rec.get("_telegram_id")
    chat_id = rec.get("_chat_id")
    file_id = rec.get("_file_id")
    msg_id = rec.get("msg_id")
    if telegram_id is None or chat_id is None or file_id is None or msg_id is None:
        return {"ok": False, "message": "任务记录缺少 chatId/fileId/messageId，无法重试"}
    try:
        await BACKEND.start_download(telegram_id, {
            "chatId": int(chat_id),
            "messageId": int(msg_id),
            "fileId": int(file_id),
        })
        _tasks_cache_invalidate()
        return {"ok": True, "message": "已重新加入下载队列"}
    except Exception as e:  # noqa: BLE001
        log.error("任务重试失败: %s", e)
        return {"ok": False, "message": "重试失败：" + _tg_err_public(e)}


@router.post("/task/cancel")
async def task_cancel(request: Request):
    """取消任务。前端传 {uniqueId}；后端契约要 {fileId}。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "请求体不是合法 JSON"}
    unique_id = str((body or {}).get("uniqueId") or "")
    if not unique_id:
        return {"ok": False, "message": "缺少 uniqueId"}

    # 挂起队列中的任务直接出队取消
    if unique_id in _WAITING_DISK_TASKS:
        _WAITING_DISK_TASKS.pop(unique_id, None)
        _waiting_disk_save()
        _tasks_cache_invalidate()
        return {"ok": True, "message": "已从磁盘挂起队列中取消该任务"}
    for tid, w in list(_WAITING_DISK_TASKS.items()):
        if str(w.get("uniqueId")) == unique_id or str(w.get("id")) == unique_id:
            _WAITING_DISK_TASKS.pop(tid, None)
            _waiting_disk_save()
            _tasks_cache_invalidate()
            return {"ok": True, "message": "已从磁盘挂起队列中取消该任务"}

    # 节流队列中的任务同样支持出队取消（含"预算耗尽挂起待次日"的那些）
    hit = throttle_cancel(task_id=unique_id, unique_id=unique_id)
    if hit is not None:
        return {"ok": True, "message": "已从节流队列中取消该任务"}

    # 若该任务正在归档上传/排队，同步取消归档上传任务
    arch_cancelled = False
    for jid, j in list(_ARCHIVE_JOBS.items()):
        if str(j.get("unique_id") or "") == unique_id and j.get("state") in ("queued", "uploading", "waiting_disk"):
            act_t = _ARCHIVE_TASKS.get(jid)
            if act_t and not act_t.done():
                act_t.cancel()
            j["state"] = "cancelled"
            j["error"] = "用户已取消"
            j["updated_at"] = time.time()
            arch_cancelled = True
    if arch_cancelled:
        _archive_save()

    rec = _find_task_by_uid(await tasks_all(force=True), unique_id)
    if rec is None:
        if arch_cancelled:
            _tasks_cache_invalidate()
            return {"ok": True, "message": "已取消归档上传任务"}
        return {"ok": False, "message": "任务不存在或已被移除"}
    telegram_id = rec.get("_telegram_id")
    file_id = rec.get("_file_id")
    if telegram_id is None or file_id is None:
        if arch_cancelled:
            _tasks_cache_invalidate()
            return {"ok": True, "message": "已取消归档上传任务"}
        return {"ok": False, "message": "任务记录缺少 fileId，无法取消"}
    try:
        if str(rec.get("_download_status") or "") != "completed" or not arch_cancelled:
            await BACKEND.cancel_download(telegram_id, {"fileId": int(file_id)})
        BACKEND._cache.clear()
        _tasks_cache_invalidate()
        return {"ok": True, "message": "已取消任务"}
    except Exception as e:  # noqa: BLE001
        if arch_cancelled:
            BACKEND._cache.clear()
            _tasks_cache_invalidate()
            return {"ok": True, "message": "已取消归档上传任务"}
        log.error("任务取消失败: %s", e)
        return {"ok": False, "message": "取消失败：" + _tg_err_public(e)}


@router.get("/partials/tasks", response_class=HTMLResponse)
async def partial_tasks(request: Request, status: str = "", source: str = "",
                        date_from: str = "", date_to: str = "", refresh: str = "",
                        offset: int = 0, limit: int = TASKS_PAGE_SIZE,
                        rows_only: int = 0):
    """任务表分片。

    rows_only=1 时只回 <tr> 片段：「加载更多」用它把新行追加到已有表格里，
    已渲染的行不被重建（展开行、勾选状态都不会丢）。
    不带 rows_only 时回整张表（首屏 / 筛选变更 / 5s 自动刷新都走这条路）。
    """
    force = bool(refresh)
    all_tasks = await tasks_all(force=force)
    filtered = _filter_tasks(all_tasks, status, source, date_from, date_to)
    page, offset, limit, total = _page_slice(filtered, offset, limit)
    ctx = {
        "request": request,
        "tasks": page,
        "tasks_total": total,
        "tasks_offset": offset,
        "tasks_limit": limit,
        # 过滤条件要回传给模板，才能拼出「加载更多」的下一段 URL
        "status": status,
        "source": source,
        "date_from": date_from,
        "date_to": date_to,
    }
    tpl = "partials/_tasks_rows.html" if rows_only else "partials/_tasks_table.html"
    return templates.TemplateResponse(tpl, ctx)
