# -*- coding: utf-8 -*-

"""
routers/browse.py — 表现层路由模块：Telegram 频道资源浏览与文件批量下载
"""
from core import *
from services import *
import time
from typing import Any, Dict, List
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

# 「转发即下载」默认目录模板：转发场景会话名统一是收藏，按月份分目录。
router = APIRouter()

@router.get("/browse", response_class=HTMLResponse)
async def browse_page(request: Request, tg: str = "", chat: str = "", type: str = "document", hide_archived: str = ""):
    """聊天浏览页：选聊天 → 浏览文件 → 勾选下载（收藏 Saved Messages 置顶）。"""
    tree = await _browse_tree()
    if (not tg or not chat) and tree:
        tg = str(tree[0]["telegramId"])
        chats0 = tree[0]["chats"]
        if chats0:
            chat = str(chats0[0]["chatId"])
    if type not in {t for t, _ in _BROWSE_TYPES}:
        type = "document"
    cur_title = "—"
    if tree and tg and chat:
        for acc in tree:
            if str(acc.get("telegramId")) == str(tg):
                for c in acc.get("chats", []):
                    if str(c.get("chatId")) == str(chat):
                        cur_title = str(c.get("title") or "聊天")
                        break
                break
    hide_arch = bool(hide_archived and hide_archived not in ("0", "false", "False"))
    rows, count, cursor, dedup = await _browse_files(tg, chat, type, hide_archived=hide_arch)
    ctx = await _ctx(request, "browse", "browse", extra={
        "tree": tree,
        "sel_tg": str(tg or ""),
        "sel_chat": str(chat or ""),
        "sel_type": type,
        "cur_title": cur_title,
        "browse_rows": rows,
        "browse_count": count,
        "browse_cursor": cursor,
        "browse_collapsed": dedup["collapsed"],
        "browse_loaded": dedup["loaded"],
        "browse_types": _BROWSE_TYPES,
        "hide_archived": hide_arch,
        # OpenList 外部访问域名：卡片上的「OpenList」直达按钮要用它拼公网地址
        # （内网 127.0.0.1 在用户浏览器里打不开）。
        "openlist_public_base": str(_ARCHIVE_CONFIG.get("publicBaseUrl") or ""),
        # 侧栏黑名单：模板据此显示「已隐藏 N 个会话 / 全部显示」提示条
        "browse_pins": _browse_pins_all(),
    })
    return templates.TemplateResponse("browse.html", ctx)


@router.get("/browse/account-tree", response_class=JSONResponse)
async def browse_account_tree(tg: str = ""):
    """返回**完整**会话树（不做黑名单隐藏），供浏览页「管理会话」面板列出候选。

    必须用 full=True：面板要能列出全部会话。hidden 字段 = 当前被隐藏的 chatId 列表
    （同时保留 pins 旧字段名兼容）。
    """
    tree = await _browse_tree(full=True)
    hidden = _browse_pins_all()
    return JSONResponse({"ok": True, "tree": tree, "hidden": hidden, "pins": hidden})


@router.post("/browse/pins")
async def browse_set_pins(request: Request):
    """设置单个会话的显示态（黑名单模型：关 = 隐藏）。

    体：{"tg": "<telegramId>", "chat": "<chatId>", "pinned": true|false}
    pinned=True  => 在侧栏显示；pinned=False => 从侧栏隐藏。
    返回的 shown 字段是该会话当前是否在侧栏显示。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    chat = str(body.get("chat") or "").strip()
    if not chat:
        return JSONResponse({"ok": False, "message": "缺少会话 ID"}, status_code=400)
    pinned = bool(body.get("pinned"))
    now_shown = _browse_pin_apply(chat, pinned)
    return JSONResponse({"ok": True, "chat": chat, "pinned": pinned,
                         "shown": now_shown, "hidden": _browse_pins_all()})


@router.post("/browse/pins/bulk")
async def browse_set_pins_bulk(request: Request):
    """批量设置显示态。体：{"chats": [...], "pinned": true|false}

    pinned=True => 显示；pinned=False => 隐藏（黑名单）。
    一次请求、一次落盘：前端「全选/取消全选」一次提交几十上百个 chatId。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    chats = body.get("chats")
    if not isinstance(chats, list):
        return JSONResponse({"ok": False, "message": "chats 必须是数组"}, status_code=400)
    changed = _browse_pins_apply_many(chats, bool(body.get("pinned")))
    return JSONResponse({"ok": True, "changed": changed, "pins": _browse_pins_all()})


@router.post("/browse/pins/clear")
async def browse_clear_pins(request: Request):
    """一键清空黑名单：全部隐藏的会话立即恢复显示。

    能一次做完就一次做完 —— 逐个 POST /browse/pins 取消会有 N 次磁盘写入与 N 次
    网络往返，且中途失败会留下「关了一半」的状态。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    removed = _browse_pins_clear()
    return JSONResponse({"ok": True, "removed": removed, "pins": _browse_pins_all()})


@router.get("/browse/watch-rules")
async def browse_watch_rules():
    """返回「转发即下载」状态：chatKey => watch 规则 id 映射。

    管理会话面板据此渲染每个会话的开关：一条 watch 订阅规则 = 一个自动下载会话。
    只输出 watch 规则（普通订阅规则不进这个面板，避免语义混淆）。
    """
    rules: Dict[str, Any] = {}
    for r in _SUB_RULES.values():
        if not r.get("watch"):
            continue
        key = f"{r.get('telegramId')}:{r.get('chatId')}"
        rules[key] = {
            "id": r.get("id"),
            "chatTitle": str(r.get("chatTitle") or ""),
            "enabled": bool(r.get("enabled")),
            "dirTemplate": str(r.get("dirTemplate") or ""),
        }
    return JSONResponse({"ok": True, "rules": rules})


@router.post("/browse/watch-rules")
async def browse_watch_rules_set(request: Request):
    """设置单个会话的「转发即下载」开关（无确认、即时落盘）。

    体：{"tg": "<telegramId>", "chat": "<chatId>", "title": "<会话名>", "on": true|false}
    on=true  => 建一条 watch=True 的订阅规则（默认目录模板，订阅页可改）
    on=false => 删除该会话的 watch 规则（规则不存在按成功处理，幂等）
    开关语义与「管理会话」一致：拨上去立即生效，成功失败都弹 toast，不弹确认框。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    tg = str(body.get("tg") or "").strip()
    chat = str(body.get("chat") or "").strip()
    title = str(body.get("title") or "聊天").strip() or "聊天"
    on = bool(body.get("on"))
    if not tg or not chat:
        return JSONResponse({"ok": False, "message": "缺少账号或会话 ID"}, status_code=400)

    # 定位该会话已有的规则（无论是否 watch —— 同一会话只允许一条规则）
    existing = None
    for r in _SUB_RULES.values():
        if str(r.get("telegramId")) == tg and str(r.get("chatId")) == chat:
            existing = r
            break

    if on:
        if existing is not None:
            # 已有规则：只翻 watch 开关，不动用户的目录模板等配置
            if not existing.get("watch"):
                existing["watch"] = True
                _subs_save()
            return JSONResponse({"ok": True, "on": True, "ruleId": existing.get("id"),
                                 "message": "已开启：新转发将自动下载并归档"})
        if len(_SUB_RULES) >= _SUBS_MAX_RULES:
            return JSONResponse({"ok": False, "message": "规则数量已达上限（%d 条）" % _SUBS_MAX_RULES})
        # 默认目录不写死网盘前缀（各机器 OpenList 挂载不同：本机是 /阿里云盘，34 是 /onedrive…）。
        # 以归档设置里的 defaultDir 为根（用户在设置页自己配的、必然是其 OpenList 实际存在的挂载）；
        # 未配置则拒绝开启并明确引导，绝不瞎猜挂载名（瞎猜 = "storage not found" 归档全挂）。
        base_dir = str(_ARCHIVE_CONFIG.get("defaultDir") or "").strip().rstrip("/")
        if not base_dir or base_dir == "/":
            return JSONResponse({
                "ok": False,
                "message": "请先在「归档设置 → 默认归档目录」里配置你的网盘目录（如 /onedrive 或 /阿里云盘），再开启转发即下载",
            })
        rule = {
            "id": secrets.token_hex(6),
            "telegramId": tg,
            "chatId": chat,
            "chatTitle": title,
            "enabled": True,
            "priority": 0,
            "dirTemplate": f"{base_dir}/tg-archive/{{chat_title}}/{{YYYY-MM}}",
            "deleteLocal": True,
            "policy": "skip",
            "watch": True,
            "created_at": time.time(),
            "stats": {"enqueued": 0, "done": 0, "failed": 0, "last_hit_at": 0.0},
        }
        _SUB_RULES[rule["id"]] = rule
        _subs_save()
        LOG_STORE.append("INFO", "开启转发即下载：%s（监听新消息自动下载归档）" % title)
        return JSONResponse({"ok": True, "on": True, "ruleId": rule["id"],
                             "message": "已开启：新转发将自动下载并归档"})

    # on=false：关掉监听。普通订阅规则只关 watch 开关（保留归档配置）；
    # 没有规则视为从未开启，幂等返回成功。
    if existing is None:
        return JSONResponse({"ok": True, "on": False, "message": "已关闭"})
    if existing.get("watch"):
        existing["watch"] = False
        _subs_save()
        LOG_STORE.append("INFO", "关闭转发即下载：%s" % str(existing.get("chatTitle") or title))
    return JSONResponse({"ok": True, "on": False, "message": "已关闭"})


@router.get("/partials/browse-files", response_class=HTMLResponse)
async def partial_browse_files(request: Request, tg: str = "", chat: str = "",
                               type: str = "document", cursor: str = "0", hide_archived: str = ""):
    """文件列表 htmx 片段（含"加载更多"游标行）。/partials/* 已由门禁返回 401 JSON。"""
    try:
        cur = int(cursor or 0)
    except ValueError:
        cur = 0
    if type not in {t for t, _ in _BROWSE_TYPES}:
        type = "document"
    hide_arch = bool(hide_archived and hide_archived not in ("0", "false", "False"))
    rows, count, next_cursor, dedup = await _browse_files(tg, chat, type, cur, hide_archived=hide_arch)
    return templates.TemplateResponse("partials/_browse_files.html", {
        "request": request,
        "browse_rows": rows,
        "browse_count": count,
        "browse_cursor": next_cursor,
        "browse_collapsed": dedup["collapsed"],
        "browse_loaded": dedup["loaded"],
        "sel_tg": str(tg or ""),
        "sel_chat": str(chat or ""),
        "sel_type": type,
        "hide_archived": hide_arch,
    })


@router.post("/browse/download")
async def browse_download(request: Request):
    """浏览页勾选下载：{files:[{telegramId,chatId,messageId,fileId}]} → 批量下载。
    与 /submit 的两跳链接解析不同，浏览页手里已是完整 FileRecord，
    直接走 /files/start-download-multiple（后端契约见 start_download_multiple）。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    # 请求体可能是合法 JSON 但不是对象（列表/字符串/数字）。下方多处按 dict 取值，
    # 不归一化就会 AttributeError → HTTP 500。归一化为 dict 后再处理。
    if not isinstance(body, dict):
        body = {}
    raw = body.get("files")
    seen = set()
    payload_files: List[Dict[str, Any]] = []
    skipped_dup = 0
    skipped_local = 0
    last_cloud_res = None
    last_local_res = None
    force = bool(body.get("force"))

    tasks_list = None
    if not force:
        try:
            tasks_list = await tasks_all()
        except Exception:
            tasks_list = []

    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict):
            continue
        try:
            item = {
                "telegramId": int(f.get("telegramId")),
                "chatId": int(f.get("chatId")),
                "messageId": int(f.get("messageId")),
                "fileId": int(f.get("fileId")),
            }
        except (TypeError, ValueError):
            continue
        uid = str(f.get("uniqueId") or "")
        fn = str(f.get("filename") or f.get("name") or "")
        sz = f.get("size")

        # 查重指纹与智能关联（非 force 时）：已在云端网盘或本地在存均进行智能拦截与提示
        if not force:
            d_res = await _check_file_dedup(uid, fn, sz, tasks_list=tasks_list)
            if d_res.get("duplicate"):
                if d_res.get("duplicateType") == "cloud":
                    skipped_dup += 1
                    last_cloud_res = d_res
                    continue
                elif d_res.get("duplicateType") == "local":
                    skipped_local += 1
                    last_local_res = d_res
                    continue

        key = (item["telegramId"], item["chatId"], uid) if uid else \
              (item["telegramId"], item["chatId"], item["messageId"], item["fileId"])
        if key in seen:
            skipped_dup += 1
            continue
        seen.add(key)
        payload_files.append(item)

    if not payload_files:
        if last_cloud_res:
            arch_date = last_cloud_res["asset"]["archivedDate"]
            drive = last_cloud_res["asset"]["drive"]
            rp = last_cloud_res["asset"]["cloudPath"]
            msg = f"云端已于 {arch_date} 归档至 {drive} 路径：{rp}"
            return JSONResponse({
                "ok": False,
                "code": "DUPLICATE_ASSET",
                "duplicateType": "cloud",
                "message": msg,
                "skippedDup": skipped_dup,
                "asset": last_cloud_res["asset"],
                "actions": last_cloud_res["actions"]
            })
        elif last_local_res:
            msg = "本地在存：该文件已在本地中转区存在，无需重复下载"
            return JSONResponse({
                "ok": False,
                "code": "DUPLICATE_ASSET",
                "duplicateType": "local",
                "message": msg,
                "skippedDup": skipped_local,
                "asset": last_local_res["asset"],
                "actions": last_local_res["actions"]
            })
        msg = f"选中的 {skipped_dup} 个文件均已在网盘归档中，已自动跳过重复下载" if skipped_dup else "没有选中可下载的文件"
        return JSONResponse({"ok": False, "message": msg, "skippedDup": skipped_dup})

    # 磁盘高低水位熔断保护：85% 熔断挂起 / 75% 唤醒
    guard = await _disk_guard_or_enqueue(raw, payload_files, "/browse/download")
    if guard is not None:
        guard["skippedDuplicates"] = skipped_dup
        return JSONResponse(guard)

    try:
        await BACKEND.start_download_multiple({"files": payload_files})
        _tasks_cache_invalidate()  # 任务页/角标立即可见新任务
        # 关键：/files 在 BackendClient._cached 还有独立 TTL 缓存，不清的话
        # 任务重建拿到的是提交前的旧数据 —— 新任务既进不了任务列表，也触发不了告警
        BACKEND._cache.clear()
        return JSONResponse({"ok": True, "count": len(payload_files), "skippedDuplicates": skipped_dup})
    except Exception as e:  # noqa: BLE001
        log.error("browse 批量下载失败: %s", e)
        return JSONResponse({"ok": False, "message": _tg_err_public(e)})

