# -*- coding: utf-8 -*-

"""
routers/system.py — 表现层路由模块：系统设置、日志中心、OpenList控制、TG向导、Doctor探针、Session冷备与磁盘保护
"""
import os
import re
import time
import json
import asyncio
from typing import Any, Dict, List, Optional, Tuple, Union
from fastapi import APIRouter, Request, Response, Form, Query, Header, Cookie, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse, PlainTextResponse
from core import *
from services import *



router = APIRouter()

@router.get("/sse/logs")
async def sse_logs(since: int = 0):
    async def gen():
        q = HUB.subscribe("logs")
        if q is None:
            yield "data: {}\n\n".format(json.dumps({"level": "ERROR", "taskId": "", "msg": "订阅数已达上限，请关闭多余页面后重试", "time": _fmt_time(time.time())}, ensure_ascii=False))
            return
        try:
            # 先给一条心跳，避免连接被立即判定死链
            yield "data: {}\n\n".format(json.dumps({"level": "INFO", "taskId": "", "msg": "后端日志流已连接", "time": _fmt_time(time.time())}, ensure_ascii=False))
            # 回放环形缓冲历史（?since= 由 /logs 种子的最大 seq 传入，避免重复）。
            # bridge 重启后 seq 重新从小值编起：客户端 since 大于当前尾 seq 视为
            # 跨代（中间发生过重启），退化为全量回放。先订阅再快照：回放期间新
            # 事件已进 q，直播循环按 seq 去重 —— 不丢也不重。
            last = int(since or 0)
            if last and last > LOG_STORE.last_seq():
                last = 0
            for line in LOG_STORE.snapshot(since_seq=last):
                last = max(last, line["seq"])
                yield f"data: {json.dumps(line, ensure_ascii=False)}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                    # 帧去重：无 seq 的帧（心跳类）原样放行，带 seq 的只发比回放点新的
                    try:
                        seq = json.loads(payload).get("seq", 0)
                    except ValueError:
                        seq = 0
                    if seq:
                        if seq <= last:
                            continue
                        last = seq
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            return
        finally:
            HUB.unsubscribe("logs", q)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/sse/tasks")
async def sse_tasks():
    async def gen():
        q = HUB.subscribe("tasks")
        if q is None:
            yield "data: {}\n\n".format(json.dumps({"id": None, "status": "error", "progress": 0, "filename": "订阅数已达上限，请关闭多余页面后重试"}, ensure_ascii=False))
            return
        try:
            # 快照当前任务，作为一个初始帧
            try:
                tasks = await tasks_all()
                for t in tasks[:40]:
                    yield f"data: {json.dumps({'id': t['id'], 'status': t['status'], 'progress': t['progress'], 'filename': t['filename']}, ensure_ascii=False)}\n\n"
            except Exception:  # noqa: BLE001
                pass
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            return
        finally:
            HUB.unsubscribe("tasks", q)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/logs")
async def api_logs(limit: int = 500, level: str = "", q: str = "", since: int = 0):
    """日志中心历史查询（JSON，环形缓冲）。

    level 逗号分隔多级过滤（INFO,WARN,ERROR）；q 关键词匹配 msg/taskId；
    since 只取更大 seq；limit 尾部截取（1..1000）。
    """
    items = LOG_STORE.snapshot(limit=min(max(limit, 1), 1000), since_seq=max(since, 0))
    if level:
        want = {lv.strip().upper() for lv in level.split(",") if lv.strip()}
        items = [i for i in items if i["level"] in want]
    if q:
        kw = q.lower()
        items = [i for i in items if kw in i["msg"].lower() or kw in i["taskId"].lower()]
    return {"ok": True, "lastSeq": LOG_STORE.last_seq(), "logs": items}


@router.get("/api/logs.txt")
async def api_logs_txt():
    """导出当前日志缓冲为纯文本（日志中心「下载日志文件」按钮）。"""
    lines = LOG_STORE.snapshot()
    body = "\n".join(
        "{time} [{level}] {tid}{msg}".format(
            time=ln["time"], level=ln["level"],
            tid=("[" + ln["taskId"] + "] ") if ln["taskId"] else "",
            msg=ln["msg"])
        for ln in lines)
    if not body:
        body = "(暂无日志)"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Response(content=body + "\n", media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="bridge-logs-{stamp}.txt"'})


@router.get("/api/system/doctor")
@router.get("/api/doctor/check")
async def api_system_doctor():
    """System Doctor 全链路系统健康与依赖一键自检接口。"""
    report = await _run_system_doctor_check()
    return {"ok": True, "data": report, "report": report}


@router.get("/api/system/doctor/ping")
async def api_system_doctor_ping():
    return {"ok": True, "pong": True, "timestamp": time.time()}


@router.get("/tg-login", response_class=HTMLResponse)
async def tg_login(request: Request):
    return templates.TemplateResponse("tg_login.html", await _ctx(request, "tg-login", "account"))


@router.get("/tg-login/account")
async def tg_login_account():
    """当前已授权账号信息（登录向导完成页展示真实账号）。"""
    accounts = await _tg_accounts()
    if accounts:
        return {"ok": True, "name": accounts[0].get("name") or "TG 账号", "id": accounts[0].get("id")}
    return {"ok": False}


@router.post("/tg-login/step1")
async def tg_login_step1(request: Request):
    """步骤1：创建客户端并发送验证码 → SetAuthenticationPhoneNumber → 等 WAIT_CODE。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    country = str((body or {}).get("country", "+86")).strip()
    phone = str((body or {}).get("phone", "")).strip()
    if not phone:
        return {"ok": False, "message": "请输入手机号"}
    full_phone = phone if phone.startswith("+") else f"{country}{phone}"
    try:
        created = await _tg_ensure_client()
        telegram_id = created.get("id")
        await BACKEND.telegram_api("SetAuthenticationPhoneNumber",
                                   {"phoneNumber": full_phone, "settings": None})
        state = await _tg_wait_state(telegram_id, {TG_WAIT_CODE, TG_WAIT_PASSWORD, TG_READY})
        if state == TG_WAIT_CODE:
            return {"ok": True, "state": "WAIT_CODE", "telegramId": telegram_id}
        if state in (TG_WAIT_PASSWORD, TG_READY):
            # 号码已被记住/直接通过，跳到对应结果
            return {"ok": True, "state": TG_STATE_NAMES.get(state, str(state)),
                    "telegramId": telegram_id, "skipCode": True, "done": state == TG_READY}
        if state in (TG_CLOSED, TG_CLOSING):
            return {"ok": False, "state": TG_STATE_NAMES.get(state, str(state)),
                    "message": "TDLib 连接已关闭：常见原因为 API_ID/API_HASH 无效、网络不通或被限流，请检查后端凭据"}
        # 超时：看最终状态区分「卡在 WAIT_PHONE（手机号被拒，常见于 API_ID 无效）」与未知
        final_state = await _tg_state_of(telegram_id)
        if final_state == TG_WAIT_PHONE:
            return {"ok": False, "state": "WAIT_PHONE_NUMBER",
                    "message": "手机号验证未生效（状态停在 WAIT_PHONE）：常见原因为 API_ID/API_HASH 无效被 Telegram 拒绝，或号码格式不正确"}
        return {"ok": False, "message": "等待授权状态超时，请稍后重试或检查后端日志"}
    except Exception as e:  # noqa: BLE001
        log.warning("TG step1 失败: %s", e)
        return {"ok": False, "message": _tg_err_public(e)}


@router.post("/tg-login/step2")
async def tg_login_step2(request: Request):
    """步骤2：校验验证码 → CheckAuthenticationCode → 等 WAIT_PASSWORD 或 READY。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    code = str((body or {}).get("code", "")).strip()
    if not code:
        return {"ok": False, "message": "请输入验证码"}
    try:
        created = await _tg_ensure_client()
        telegram_id = created.get("id")
        await BACKEND.telegram_api("CheckAuthenticationCode", {"code": code})
        state = await _tg_wait_state(telegram_id, {TG_WAIT_PASSWORD, TG_READY, TG_WAIT_PHONE})
        if state == TG_READY:
            return {"ok": True, "state": "READY", "done": True}
        if state == TG_WAIT_PASSWORD:
            return {"ok": True, "state": "WAIT_PASSWORD", "needPassword": True}
        if state == TG_WAIT_PHONE:
            return {"ok": False, "state": "WAIT_PHONE_NUMBER",
                    "message": "验证码错误或已过期，请重新发送"}
        if state in (TG_CLOSED, TG_CLOSING):
            return {"ok": False, "state": "CLOSED", "message": "TDLib 连接已关闭，请重新开始登录"}
        final_state = await _tg_state_of(telegram_id)
        if final_state == TG_WAIT_CODE:
            return {"ok": False, "state": "WAIT_CODE", "message": "验证码错误或已过期，请重新输入"}
        return {"ok": False, "message": "验证码校验超时，请重试"}
    except Exception as e:  # noqa: BLE001
        log.warning("TG step2 失败: %s", e)
        return {"ok": False, "message": _tg_err_public(e)}


@router.post("/tg-login/step3")
async def tg_login_step3(request: Request):
    """步骤3：两步验证密码 → CheckAuthenticationPassword → 等 READY。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    password = str((body or {}).get("password", ""))
    try:
        created = await _tg_ensure_client()
        telegram_id = created.get("id")
        await BACKEND.telegram_api("CheckAuthenticationPassword", {"password": password})
        state = await _tg_wait_state(telegram_id, {TG_READY, TG_WAIT_PASSWORD})
        if state == TG_READY:
            return {"ok": True, "state": "READY", "done": True}
        if state == TG_WAIT_PASSWORD:
            return {"ok": False, "state": "WAIT_PASSWORD", "message": "两步验证密码错误，请重试"}
        if state in (TG_CLOSED, TG_CLOSING):
            return {"ok": False, "state": "CLOSED", "message": "TDLib 连接已关闭，请重新开始登录"}
        return {"ok": False, "message": "两步验证超时，请重试"}
    except Exception as e:  # noqa: BLE001
        log.warning("TG step3 失败: %s", e)
        return {"ok": False, "message": _tg_err_public(e)}


@router.get("/openlist/status")
async def openlist_status():
    """设置页回显 OpenList 登录态（只回账号与验证结果，不回 token/密码）。"""
    try:
        return {"ok": True, **(await _openlist_status())}
    except Exception as e:  # noqa: BLE001
        log.error("OpenList 状态检查失败: %s", e)
        return {"ok": False, "message": "OpenList 状态检查失败"}


@router.post("/openlist/login")
async def openlist_login(request: Request):
    """OpenList 账号登录：支持自定义服务器地址 baseUrl。"""
    try:
        raw_body = await request.json()
        body = raw_body if isinstance(raw_body, dict) else {}
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "请求体不是合法 JSON"}
    base_url = str(body.get("baseUrl") or "").strip().rstrip("/")
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", "") or "")
    if not username or not password:
        return {"ok": False, "message": "请填写 OpenList 用户名与密码"}
    ip = _client_ip(request)
    if _login_blocked(ip):
        return {"ok": False, "message": "尝试过于频繁，请稍后再试"}

    if base_url:
        try:
            parsed = urlparse(base_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return {"ok": False, "message": "服务器地址格式不正确，必须以 http:// 或 https:// 开头"}
        except Exception:
            return {"ok": False, "message": "服务器地址格式不正确，必须以 http:// 或 https:// 开头"}

    prev_url = _OPENLIST.get("baseUrl") or OPENLIST_URL
    target_url = _openlist_update_base_url(base_url or prev_url)
    token, msg = await _openlist_api_login(username, password)
    if not token:
        if prev_url != target_url:
            _openlist_update_base_url(prev_url)
        _record_login_failure(ip)
        log.info("OpenList 登录失败（用户 %s，地址 %s）: %s", username, target_url, msg)
        return {"ok": False, "message": "OpenList 登录失败：" + msg}
    _clear_login_failures(ip)
    _OPENLIST.update({"baseUrl": target_url, "username": username, "password": password,
                      "token": token, "logged_at": time.time()})
    _openlist_save()
    log.info("OpenList 登录成功（用户 %s，地址 %s，经 OpenList API 验证）", username, target_url)
    return {"ok": True, "username": username, "baseUrl": target_url, "verified": True}


@router.post("/openlist/logout")
async def openlist_logout():
    """退出 OpenList：尽力吊销远端会话，然后清除本地凭据。"""
    token = _OPENLIST.get("token", "")
    if token:
        try:
            await _openlist_client.get("/api/auth/logout", headers={"Authorization": token})
        except Exception:  # noqa: BLE001
            pass
    _openlist_clear()
    return {"ok": True}


@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    ctx = await _ctx(request, "settings", "settings")
    return templates.TemplateResponse("settings.html", ctx)


@router.get("/settings/load")
async def settings_load():
    """读取后端真实设置（仅合法 SettingKey），原样返回 JSON。"""
    try:
        # 只用后端 SettingKey 枚举里真实存在的键（不存在的键会让后端
        # SettingKey.valueOf 抛异常导致请求挂起超时）。
        # 键列表必须与 settings.html 可写的键一致，否则「可写不可读」的键
        # 保存后回显会被静默重置（如 showSensitiveContent）。
        return await BACKEND.get_setting("automation,autoDownloadLimit,thumbnailAutoLoad,avgSpeedInterval,speedUnits,uniqueOnly,alwaysHide,shareEnabled,showSensitiveContent")
    except Exception as e:  # noqa: BLE001
        log.error("读取设置失败: %s", e)
        return {"ok": False, "message": "读取设置失败：" + _tg_err_public(e)}


@router.post("/settings/save")
async def settings_save(request: Request):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            return {"ok": False, "message": "请求体格式错误"}
        # 键白名单 + 值必须是标量（后端平铺字符串表）
        filtered = {}
        for k, v in body.items():
            if k in _SETTING_KEYS and isinstance(v, (str, int, float, bool)):
                filtered[k] = str(v).lower() if isinstance(v, bool) else str(v)
        if not filtered:
            return {"ok": False, "message": "没有合法的设置键"}
        await BACKEND.create_setting(filtered)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        log.error("保存设置失败: %s", e)
        return {"ok": False, "message": "保存设置失败：" + _tg_err_public(e)}


@router.get("/logs", response_class=HTMLResponse)
async def logs(request: Request):
    # 服务端注入真实历史（环形缓冲 + 落盘），不再是「最近归档文件」伪装的日志行；
    # logs_last_seq 随种子下发，前端以 /sse/logs?since= 续播避免重复
    seed = LOG_STORE.snapshot(limit=300)
    ctx = await _ctx(request, "logs", "logs",
                     extra={"seed_logs": seed, "logs_last_seq": LOG_STORE.last_seq()})
    return templates.TemplateResponse("logs.html", ctx)


@router.get("/account", response_class=HTMLResponse)
async def account(request: Request):
    return templates.TemplateResponse("account.html", await _ctx(request, "account", "account"))


@router.get("/profile", response_class=HTMLResponse)
async def profile(request: Request):
    return templates.TemplateResponse("account.html", await _ctx(request, "account", "account"))


@router.post("/alerts/read")
async def alerts_read(request: Request):
    """告警全部标记已读：推进已读游标（服务端状态，不再是前端隐藏徽标的表演）。"""
    _alert_state["read_ts"] = time.time()
    return JSONResponse({"ok": True, "unread": 0})


@router.get("/openlist/dirs")
async def openlist_dirs(path: str = "/"):
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


@router.get("/openlist/stream-url")
async def openlist_stream_url(path: str = ""):
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


@router.get("/api/notify/config")
@router.get("/api/notifications/config")
async def api_notify_get_config():
    """获取 Telegram 通知配置（Bot Token 自动脱敏）。"""
    return {"ok": True, "config": _get_notify_config_public()}


@router.post("/api/notify/config")
@router.post("/api/notifications/config")
async def api_notify_save_config(request: Request):
    """保存 Telegram 通知配置。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "message": "非法请求体"}, status_code=400)

    if "enabled" in body:
        _NOTIFY_CONFIG["enabled"] = bool(body["enabled"])
    if "channel" in body:
        ch = str(body["channel"] or "").lower()
        if ch in ("bot", "saved_messages", "both"):
            _NOTIFY_CONFIG["channel"] = ch
    if "chatId" in body:
        cid = str(body["chatId"] or "").strip()
        if cid and not RE_CHAT_ID.match(cid):
            return JSONResponse({"ok": False, "message": "非法 Telegram Chat ID 格式（需为数字ID或@频道群组名）"}, status_code=400)
        _NOTIFY_CONFIG["chatId"] = cid
    if "minFileSizeMB" in body:
        try:
            _NOTIFY_CONFIG["minFileSizeMB"] = max(1, int(body["minFileSizeMB"]))
        except (ValueError, TypeError):
            pass

    new_token = str(body.get("botToken") or "").strip()
    if new_token and "******" not in new_token:
        if not RE_BOT_TOKEN.match(new_token):
            return JSONResponse({"ok": False, "message": "非法 Telegram Bot Token 格式（需为合法数字ID与密钥组合）"}, status_code=400)
        _NOTIFY_CONFIG["botToken"] = new_token
    elif not new_token and body.get("hasBotToken") is False:
        _NOTIFY_CONFIG["botToken"] = ""

    events = body.get("events")
    if isinstance(events, dict):
        for k in ("downloadCompleted", "archiveSuccess", "archiveFailed", "diskWatermarkAlert"):
            if k in events:
                _NOTIFY_CONFIG["events"][k] = bool(events[k])

    _notify_config_save()
    log.info("Telegram 通知配置已更新并落盘")
    return {"ok": True, "message": "通知配置已保存", "config": _get_notify_config_public()}


@router.post("/api/notify/test")
@router.post("/api/notifications/test")
async def api_notify_test_send(request: Request):
    """发送一条测试通知卡片，验证 Telegram Bot 或 Saved Messages 通道连通性。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    override_token = str(body.get("botToken") or "").strip()
    if "******" in override_token:
        override_token = ""
    if override_token and not RE_BOT_TOKEN.match(override_token):
        return JSONResponse({"ok": False, "message": "非法 Telegram Bot Token 格式（需为合法数字ID与密钥组合）"}, status_code=400)
    override_chat = str(body.get("chatId") or "").strip()
    if override_chat and not RE_CHAT_ID.match(override_chat):
        return JSONResponse({"ok": False, "message": "非法 Telegram Chat ID 格式（需为数字ID或@频道群组名）"}, status_code=400)
    override_channel = str(body.get("channel") or "").strip()

    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    eff_channel = override_channel or str(_NOTIFY_CONFIG.get("channel") or "both")
    ch_label = "双通道 (Bot + 收藏夹)" if eff_channel == "both" else ("Telegram Bot" if eff_channel == "bot" else "Saved Messages 收藏夹")

    test_card = (
        "🔔 <b>【Telegram 通知引擎连通性测试】</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✅ <b>通道状态：</b>配置正确，消息外发畅通\n"
        f"📡 <b>发送通道：</b><code>{ch_label}</code>\n"
        f"⏱️ <b>测试时间：</b><code>{now_str}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>TG 归档台消息通知外发引擎已就绪。</i>"
    )

    res = await _dispatch_notification(
        event_type="test",
        html_content=test_card,
        force=True,
        override_bot_token=override_token,
        override_chat_id=override_chat,
        override_channel=override_channel
    )

    if res.get("ok"):
        return {"ok": True, "message": "测试通知已成功外发，请在 Telegram 中查看", "details": res}
    err_msg = "; ".join(res.get("errors") or []) or "外发失败"
    return JSONResponse({"ok": False, "message": f"测试通知外发失败: {err_msg}", "details": res}, status_code=400)


@router.get("/openlist/direct-url")
async def openlist_direct_url(path: str = ""):
    """生成在 OpenList Web 界面中直达查看该文件的 URL。"""
    if not path:
        return JSONResponse({"ok": False, "message": "缺少 path 参数"}, status_code=400)
    url = _openlist_direct_url(path)
    return {"ok": True, "url": url}


@router.post("/api/session/backup")
async def api_session_backup(request: Request):
    """触发 TDLib Session 加密快照并上传至 OpenList 异地冷备目录。

    请求体可选 {"localOnly": true}：只生成本地快照、跳过 OpenList 上传。
    适用于未配置网盘、或只想把冷备落到自备磁盘/挂载卷的场景。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    local_only = bool((body or {}).get("localOnly"))
    try:
        res = await create_session_backup(local_only=local_only)
        return JSONResponse(res)
    except Exception as e:
        log.error("Session 异地冷备执行失败: %s", e)
        return JSONResponse({"ok": False, "message": f"冷备失败: {e}"}, status_code=500)


@router.get("/api/session/backup/status")
async def api_session_backup_status(request: Request):
    """获取最近 Session 异地冷备时间、大小与健康指示灯数据。"""
    try:
        st = await _get_session_backup_status()
        return JSONResponse(st)
    except Exception as e:
        log.error("获取 Session 冷备状态失败: %s", e)
        return JSONResponse({"ok": False, "status": "err", "dotClass": "err", "label": "冷备异常", "message": str(e)}, status_code=500)


@router.get("/api/session/backups")
async def api_session_backups(request: Request):
    """列出全部真实可还原的会话快照（本地加密包 + 本地每日明文包 + OpenList 云端包）。"""
    try:
        res = await list_session_backups()
        return JSONResponse(res)
    except Exception as e:
        log.error("获取 Session 快照列表失败: %s", e)
        return JSONResponse({"ok": False, "items": [], "message": f"获取快照列表失败: {e}"}, status_code=500)


@router.post("/api/session/backup/dir")
async def api_session_backup_dir(request: Request):
    """单独设置「云端备份目录」，不触碰其它归档配置。

    刻意不复用 /archive/config：那个接口会把未提交的字段重置为默认值，
    只存备份目录会顺手把「自动归档」等开关关掉。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw = str((body or {}).get("dir") or "").strip()
    norm = ""
    if raw:
        norm = _archive_norm_dir(raw)
        if norm is None or norm == "/":
            return JSONResponse(
                {"ok": False, "message": "目录格式无效（需以 / 开头且不能为根目录 /，例如 /onedrive/TG-Backups）"},
                status_code=400)

    _ARCHIVE_CONFIG["sessionBackupDir"] = norm
    _archive_config_save()
    log.info("会话冷备目录已更新: %s", norm or "(自动推导)")
    resolved, source, mounts = await _resolve_session_backup_remote_dir_detail(await _openlist_token_or_empty())
    return JSONResponse({
        "ok": True,
        "dir": norm,
        "resolved": resolved,
        "source": source,
        "mounts": mounts,
        "message": f"已保存，当前生效目录：{resolved}" if resolved else source,
    })


@router.post("/api/session/backup/local-dir")
async def api_session_backup_local_dir(request: Request):
    """单独设置「本地备份目录」（绝对路径），不触碰其它归档配置。

    与云端目录接口同样的理由：不复用 /archive/config，避免顺带重置其它开关。
    空字符串 = 恢复自动推导（与 app 平级 / 数据目录内）。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    raw = str((body or {}).get("dir") or "").strip().strip('"').strip("'")
    norm = ""
    if raw:
        # 必须是绝对路径：相对路径的含义依赖进程 CWD，重启后可能指向别处，
        # 导致「备份看起来还在」但实际写到了另一个目录。
        if not os.path.isabs(raw):
            return JSONResponse(
                {"ok": False, "message": "请填写绝对路径（例如 /mnt/backup/tg-session 或 D:\\tg-backup）"},
                status_code=400)
        norm = os.path.normpath(raw)
        # 可写性预检：现在就告诉用户路径是否可用，而不是等备份时才失败。
        try:
            os.makedirs(norm, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "message": f"目录无法创建或不可写：{e}"}, status_code=400)
        if not os.access(norm, os.W_OK):
            return JSONResponse(
                {"ok": False, "message": f"目录存在但不可写：{norm}"}, status_code=400)

    _ARCHIVE_CONFIG["localBackupDir"] = norm
    _archive_config_save()
    log.info("本地冷备目录已更新: %s", norm or "(自动推导)")
    resolved, source = _resolve_local_backup_dir()
    return JSONResponse({
        "ok": True,
        "dir": norm,
        "resolved": resolved,
        "source": source,
        "writable": os.path.isdir(resolved) and os.access(resolved, os.W_OK),
        "message": f"已保存，本地备份将写入：{resolved}（{source}）",
    })


async def _openlist_token_or_empty() -> str:
    """探测用 token；OpenList 未登录时返回空串（不抛异常，便于前端照常保存设置）。"""
    try:
        return await _openlist_token()
    except Exception:
        return ""


@router.post("/api/session/restore")
async def api_session_restore(request: Request):
    """一键还原指定会话快照（停写后端容器 → 精确落盘 → 重启）。

    破坏性操作的三重门禁：Portal 登录态 + CSRF 双提交（中间件强制）
    + 请求体 confirm=true 二次确认。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str((body or {}).get("name") or "").strip()
    origin = str((body or {}).get("origin") or "local").strip().lower()
    confirm = bool((body or {}).get("confirm"))
    if not name:
        return JSONResponse({"ok": False, "message": "缺少备份包名称"}, status_code=400)
    if origin not in ("local", "remote"):
        return JSONResponse({"ok": False, "message": "来源只能是 local 或 remote"}, status_code=400)
    res = await restore_session_backup_async(name, origin=origin, confirm=confirm)
    if not res.get("ok"):
        return JSONResponse(res, status_code=400)
    return JSONResponse(res)


@router.get("/api/disk/watermark/status")
async def api_disk_watermark_status(request: Request):
    """获取当前 VPS 磁盘高低水位状态、指标与 waiting_disk 挂起队列。"""
    cur_pct = _get_disk_usage_percent()
    free_gb = _get_disk_free_gb()
    high = float(_ARCHIVE_CONFIG.get("diskHighWatermarkPercent", 85.0) or 85.0)
    low = float(_ARCHIVE_CONFIG.get("diskLowWatermarkPercent", 75.0) or 75.0)
    auto_clean = bool(_ARCHIVE_CONFIG.get("diskAutoClean", True))

    status = "ok"
    if cur_pct >= high:
        status = "high"
    elif cur_pct >= low:
        status = "normal"

    return JSONResponse({
        "ok": True,
        "status": status,
        "usagePercent": round(cur_pct, 1),
        "freeGB": round(free_gb, 2),
        "highWatermarkPercent": high,
        "lowWatermarkPercent": low,
        "autoClean": auto_clean,
        "waitingTasksCount": len(_WAITING_DISK_TASKS),
        "waitingTasks": list(_WAITING_DISK_TASKS.values())
    })


@router.post("/api/disk/wake")
async def api_disk_wake(request: Request):
    """手动触发磁盘水位检查并尝试唤醒 waiting_disk 挂起任务。"""
    try:
        cleaned = await _disk_guard_check()
        woken = await _check_and_wake_waiting_disk_tasks()
        cur_pct = _get_disk_usage_percent()
        return JSONResponse({
            "ok": True,
            "cleaned": cleaned,
            "woken": woken,
            "usagePercent": round(cur_pct, 1),
            "remainingWaiting": len(_WAITING_DISK_TASKS),
            "message": f"应急清理释放 {cleaned} 个文件，唤醒 {woken} 个挂起任务，当前使用率 {cur_pct:.1f}%"
        })
    except Exception as e:
        log.error("手动唤醒检查失败: %s", e)
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)

