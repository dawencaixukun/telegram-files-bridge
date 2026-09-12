# -*- coding: utf-8 -*-
"""
services/bot_command_service.py — Telegram Bot 交互命令服务（getUpdates 长轮询）
================================================================================
让 TG Bot 从「只外发通知」升级为「可双向交互」：用户在 Telegram 里直接发命令，
Bot 回复系统实时状态，手机上不打开管理台也能掌握全局。

命令集（经理/用户视角规划的最低实用集）：
  /ck      查看下载任务进度与完整性（进行中 + 最近完成，含速率/进度/校验状态）
  /yd      查看云端归档上传任务（queued/uploading/failed；已归档 done 的不罗列）
  /st      系统总览一屏速读（磁盘水位、FloodWait、任务/归档计数、下载/上传速率）
  /err     最近归档失败原因与处理建议（最多 5 条）
  /help    命令帮助

安全模型：
  - 仅 Bot 通道可用（Saved Messages 通道是用户登录态，无 Bot 概念，不接收命令）；
  - 命令响应只发回配置的 chatId（白名单），陌生人给 Bot 发消息一律忽略；
  - 轮询 token 独立 httpx 客户端，异常静默降级不影响主服务；
  - 响应文本复用通知卡片的安全清洗（bot token 脱敏正则）。
"""
import re
import time
import asyncio
from typing import Any, Dict, List, Optional

import httpx

from core.config import _fmt_size, _fmt_time
from core.state import (
    _NOTIFY_CONFIG, _ARCHIVE_JOBS, _ARCHIVE_CONFIG,
    _WAITING_DISK_TASKS, _FLOOD_WAIT_STATE, _is_flood_wait_active,
    _DELETED_LOCAL_UIDS,
)
from core.backend import BACKEND
from core.logging import log

# 长轮询间隔与单次超时：Telegram 官方推荐 long-poll timeout ≤ 50s
_POLL_INTERVAL = 3.0
_POLL_TIMEOUT = 35.0
_UPDATE_OFFSET_FILE: Optional[str] = None  # 由 core/config 注入 APP_ROOT_DIR 后惰性初始化
_update_offset = 0

# 最近一次归档失败快照（/err 用，环形保留 8 条）
_RECENT_ARCHIVE_ERRORS: List[Dict[str, Any]] = []
_RECENT_ARCHIVE_ERRORS_MAX = 8


def remember_archive_error(job: Dict[str, Any]) -> None:
    """归档失败时由 archive_service 调用，快照供 /err 展示。"""
    try:
        _RECENT_ARCHIVE_ERRORS.insert(0, {
            "filename": str(job.get("filename") or "未知文件"),
            "remote_path": str(job.get("remote_path") or "—"),
            "error": str(job.get("error") or "未知错误")[:160],
            "at": float(job.get("updated_at") or time.time()),
        })
        del _RECENT_ARCHIVE_ERRORS[_RECENT_ARCHIVE_ERRORS_MAX:]
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------
# 数据聚合（只读，复用主服务已有状态，不发额外后端请求）
# ---------------------------------------------------------------------
def _esc(s: Any) -> str:
    """HTML 转义（Bot HTML parse_mode 安全）。"""
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _bar(pct: float, width: int = 12) -> str:
    """文本进度条：▮▮▮▯▯▯ 55%"""
    pct = max(0.0, min(100.0, float(pct or 0)))
    filled = int(round(pct / 100.0 * width))
    return "▮" * filled + "▯" * (width - filled) + f" {pct:.0f}%"


async def _tasks_all_safe() -> List[Dict[str, Any]]:
    """拉取任务列表（复用 tasks_all 的 8s 缓存），6s 超时降级为空集，
    保证 Bot 命令响应永不卡死。"""
    from services.task_service import tasks_all
    try:
        return await asyncio.wait_for(tasks_all(), timeout=6.0)
    except Exception as e:  # noqa: BLE001
        log.warning("Bot 命令拉取任务失败: %s", e)
        return []


async def build_ck_reply() -> str:
    """/ck — 下载任务进度与完整性。"""
    tasks = await _tasks_all_safe()
    if not tasks:
        return "📭 当前没有任何下载任务。"
    active = [t for t in tasks if t.get("status") in ("download", "verify", "pending", "waiting_disk")]
    done = [t for t in tasks if t.get("status") in ("downloaded", "archived")]
    failed = [t for t in tasks if t.get("status") in ("failed", "isolated")]

    lines = ["📥 <b>【下载任务进度】</b>", "━━━━━━━━━━━━━━━━━━"]

    if active:
        lines.append(f"🚀 <b>进行中 {len(active)} 个</b>")
        for t in active[:8]:
            fn = _esc(str(t.get("filename") or "未知"))[:36]
            prog = float(t.get("progress") or 0)
            speed = str(t.get("speed_label") or "")
            sz = str(t.get("size") or "—")
            loaded = str(t.get("loaded") or "—")
            st_label = {"download": "下载中", "verify": "校验中",
                        "pending": "排队中", "waiting_disk": "磁盘挂起"}.get(t.get("status"), "")
            seg = f"{st_label} {fn}\n  {_bar(prog)}"
            if speed:
                seg += f" · ⚡ {speed}"
            seg += f"\n  {loaded} / {sz}"
            lines.append(seg)
        if len(active) > 8:
            lines.append(f"  … 等共 {len(active)} 个")

    if done:
        # 完整性口径：已下载 = 落盘完整；已归档 = 本地完整且云端在存
        n_dl = sum(1 for t in done if t.get("status") == "downloaded")
        n_arch = sum(1 for t in done if t.get("status") == "archived")
        total_sz = sum(int(t.get("_size_bytes") or 0) for t in done)
        lines.append(f"\n✅ <b>已完成（完整性校验通过）</b>")
        lines.append(f"  📦 已下载待归档：<b>{n_dl}</b> 个 · 已归档：<b>{n_arch}</b> 个")
        lines.append(f"  💾 累计体积：<b>{_fmt_size(total_sz)}</b>")

    if failed:
        lines.append(f"\n❌ <b>失败/隔离 {len(failed)} 个</b>（发送 /err 看归档失败详情）")
        for t in failed[:3]:
            fn = _esc(str(t.get("filename") or "未知"))[:32]
            err = _esc(str(t.get("error_msg") or ""))[:60]
            lines.append(f"  · {fn}\n    {err}")

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"<i>共 {len(tasks)} 个任务 · 发送 /st 看系统总览</i>")
    return "\n".join(lines)


async def build_yd_reply() -> str:
    """/yd — 云端归档上传任务（queued/uploading/failed；done 不罗列）。"""
    active_states = ("queued", "uploading", "verifying")
    active = [j for j in _ARCHIVE_JOBS.values() if str(j.get("state") or "") in active_states]
    failed = [j for j in _ARCHIVE_JOBS.values() if str(j.get("state") or "") == "failed"]
    done_count = sum(1 for j in _ARCHIVE_JOBS.values() if str(j.get("state") or "") == "done")
    active.sort(key=lambda j: float(j.get("created_at") or 0.0), reverse=True)
    failed.sort(key=lambda j: float(j.get("updated_at") or 0.0), reverse=True)

    if not active and not failed:
        return (f"☁️ <b>【云端归档队列】</b>\n"
                f"✅ 当前没有进行中或失败的上传任务。\n"
                f"<i>历史已归档 <b>{done_count}</b> 个文件（已忽略不罗列）。</i>")

    lines = ["☁️ <b>【云端归档上传任务】</b>", "━━━━━━━━━━━━━━━━━━"]

    if active:
        lines.append(f"⏳ <b>进行中 {len(active)} 个</b>")
        for j in active[:8]:
            fn = _esc(str(j.get("filename") or "未知"))[:36]
            prog = float(j.get("progress") or 0)
            state = str(j.get("state") or "queued")
            rd = _esc(str(j.get("remote_dir") or "—"))[:40]
            st_label = "排队中" if state == "queued" else ("校验中" if state == "verifying" else "上传中")
            lines.append(f"{st_label} {fn}\n  {_bar(prog)}\n  📂 {rd}")
        if len(active) > 8:
            lines.append(f"  … 等共 {len(active)} 个")

    if failed:
        lines.append(f"\n❌ <b>失败 {len(failed)} 个</b>（发送 /err 看原因与建议）")
        for j in failed[:3]:
            fn = _esc(str(j.get("filename") or "未知"))[:32]
            err = _esc(str(j.get("error") or ""))[:60]
            lines.append(f"  · {fn}\n    {err}")

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"<i>历史已归档 <b>{done_count}</b> 个（不罗列） · 自动归档: {'开' if _ARCHIVE_CONFIG.get('autoArchive') else '关'}</i>")
    return "\n".join(lines)


async def build_st_reply() -> str:
    """/st — 系统总览一屏速读。"""
    from services.watermark_service import _get_disk_usage_percent, _get_disk_free_gb
    tasks = await _tasks_all_safe()

    dl_now = sum(float(t.get("speed") or 0) for t in tasks
                 if str(t.get("status") or "") == "download")
    ul_speed = 0.0
    try:
        from services.task_service import _upload_speed_snapshot
        ul_speed = await asyncio.wait_for(_upload_speed_snapshot(), timeout=2.0)
    except Exception:  # noqa: BLE001
        pass

    running = sum(1 for t in tasks if t.get("status") in ("download", "upload", "verify"))
    archived = sum(1 for t in tasks if t.get("status") in ("downloaded", "archived"))
    failed = sum(1 for t in tasks if t.get("status") in ("failed", "isolated"))

    disk_pct = _get_disk_usage_percent()
    free_gb = _get_disk_free_gb()
    fw = _is_flood_wait_active()
    fw_rem = int(float(_FLOOD_WAIT_STATE.get("cooldown_until") or 0.0) - time.time())

    from services.task_service import _fmt_speed
    lines = [
        "📊 <b>【系统总览】</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"⚡ <b>实时速率</b>",
        f"  下载：<b>{_fmt_speed(dl_now)}</b> · 上传：<b>{_fmt_speed(ul_speed)}</b>",
        f"📋 <b>任务</b>",
        f"  进行中 <b>{running}</b> · 完成 <b>{archived}</b> · 失败 <b>{failed}</b>",
        f"☁️ <b>归档队列</b>",
    ]
    uploading = sum(1 for j in _ARCHIVE_JOBS.values() if str(j.get("state") or "") in ("queued", "uploading", "verifying"))
    lines.append(f"  上传中 <b>{uploading}</b> · 已归档 <b>{sum(1 for j in _ARCHIVE_JOBS.values() if j.get('state') == 'done')}</b>")
    lines.append(f"💽 <b>磁盘</b>")
    lines.append(f"  已用 <b>{disk_pct:.1f}%</b> · 可用 <b>{free_gb:.1f} GB</b>")
    if _WAITING_DISK_TASKS:
        lines.append(f"  ⏸️ 磁盘挂起任务：<b>{len(_WAITING_DISK_TASKS)}</b> 个")
    if fw:
        rem = max(0, fw_rem)
        lines.append(f"🚦 <b>FloodWait 冷却中</b>（剩余 {rem // 60}分{rem % 60}秒）")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"<i>{_fmt_time(time.time())} · 发送 /help 查看全部命令</i>")
    return "\n".join(lines)


def build_err_reply() -> str:
    """/err — 最近归档失败原因与建议。"""
    if not _RECENT_ARCHIVE_ERRORS:
        return "✅ 最近没有归档失败记录。"
    lines = ["🚨 <b>【最近归档失败】</b>", "━━━━━━━━━━━━━━━━━━"]
    for e in _RECENT_ARCHIVE_ERRORS[:5]:
        fn = _esc(e.get("filename") or "未知")[:36]
        rp = _esc(e.get("remote_path") or "—")[:40]
        err = _esc(e.get("error") or "未知")[:80]
        t = _fmt_time(e.get("at") or 0)
        lines.append(f"📦 {fn}\n  📂 {rp}\n  ❌ {err}\n  🕒 {t}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("<i>💡 常见原因：鉴权过期→重登 OpenList；容量超限→清网盘；网络超时→稍后自动重试</i>")
    return "\n".join(lines)


HELP_TEXT = "\n".join([
    "🤖 <b>【TG 归档台 Bot 命令】</b>",
    "━━━━━━━━━━━━━━━━━━",
    "/ck — 下载任务进度与完整性",
    "/yd — 云端归档上传任务（已归档不罗列）",
    "/st — 系统总览（磁盘/速率/任务速读）",
    "/err — 最近归档失败原因与建议",
    "/help — 本帮助",
    "━━━━━━━━━━━━━━━━━━",
    "<i>回复仅发送给管理员配置的 ChatID，其他会话一律忽略。</i>",
])


# ---------------------------------------------------------------------
# Bot API 长轮询
# ---------------------------------------------------------------------
def _bot_base(bot_token: str) -> str:
    return f"https://api.telegram.org/bot{bot_token}"


def _clean_token_text(s: str) -> str:
    return re.sub(r"bot[0-9]+:[A-Za-z0-9_-]+", "bot<REDACTED>", str(s))


async def _fetch_updates(client: httpx.AsyncClient, bot_token: str, offset: int) -> List[Dict[str, Any]]:
    """getUpdates 长轮询；网络异常返回空（静默降级，不打断轮询循环）。"""
    try:
        resp = await client.post(
            f"{_bot_base(bot_token)}/getUpdates",
            json={"offset": offset, "timeout": int(_POLL_TIMEOUT),
                  "allowed_updates": ["message"]},
            timeout=httpx.Timeout(_POLL_TIMEOUT + 10.0, connect=8.0),
        )
        if resp.status_code != 200:
            # 409 说明另一个 getUpdates 在跑（旧进程残留），退避后重试
            await asyncio.sleep(10.0)
            return []
        data = resp.json()
        if not isinstance(data, dict) or not data.get("ok"):
            return []
        updates = data.get("result") or []
        return updates if isinstance(updates, list) else []
    except Exception as e:  # noqa: BLE001
        log.debug("Bot getUpdates 失败（静默重试）: %s", _clean_token_text(e))
        await asyncio.sleep(5.0)
        return []


async def _send_reply(client: httpx.AsyncClient, bot_token: str, chat_id: str, html_text: str) -> None:
    try:
        await client.post(
            f"{_bot_base(bot_token)}/sendMessage",
            json={"chat_id": chat_id, "text": html_text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=httpx.Timeout(12.0, connect=6.0),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Bot 命令回复发送失败: %s", _clean_token_text(e))


async def _handle_update(client: httpx.AsyncClient, bot_token: str,
                         allowed_chat: str, update: Dict[str, Any]) -> int:
    """处理单条 update；返回下一个 offset。只响应白名单 chatId 的文本命令。"""
    uid = int(update.get("update_id") or 0)
    msg = update.get("message") or {}
    if not isinstance(msg, dict):
        return uid + 1
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = str(msg.get("text") or "").strip()
    if not text or not text.startswith("/"):
        return uid + 1
    # 安全校验：陌生人 / 错误群组一律忽略（不回错、不暴露 Bot 存在）
    if allowed_chat and chat_id != allowed_chat:
        return uid + 1

    cmd = text.split()[0].split("@")[0].lower()
    if cmd == "/ck":
        reply = await build_ck_reply()
    elif cmd == "/yd":
        reply = await build_yd_reply()
    elif cmd == "/st":
        reply = await build_st_reply()
    elif cmd == "/err":
        reply = build_err_reply()
    elif cmd in ("/help", "/start"):
        reply = HELP_TEXT
    else:
        # 未识别命令：礼貌提示（仅白名单会话）
        reply = f"❓ 未识别命令 {_esc(cmd)}，发送 /help 查看可用命令。"
    await _send_reply(client, bot_token, chat_id, reply)
    return uid + 1


async def bot_command_loop() -> None:
    """Bot 命令长轮询主循环（启动于 bridge startup；异常自愈不退出）。"""
    global _update_offset
    log.info("TG Bot 命令轮询启动（/ck /yd /st /err /help）")
    while True:
        bot_token = str(_NOTIFY_CONFIG.get("botToken") or "").strip()
        chat_id = str(_NOTIFY_CONFIG.get("chatId") or "").strip()
        # 未配置 Bot 或 ChatID 时挂起等待配置就绪（30s 检查一次，不空转）
        if not bot_token or not chat_id:
            await asyncio.sleep(30.0)
            continue
        try:
            async with httpx.AsyncClient(follow_redirects=False) as client:
                while True:
                    # 配置可能被热更新（清空 token / 换 chatId），每轮重读
                    cur_token = str(_NOTIFY_CONFIG.get("botToken") or "").strip()
                    cur_chat = str(_NOTIFY_CONFIG.get("chatId") or "").strip()
                    if cur_token != bot_token or cur_chat != chat_id or not cur_token or not cur_chat:
                        break
                    updates = await _fetch_updates(client, bot_token, _update_offset)
                    for u in updates:
                        try:
                            _update_offset = await _handle_update(client, bot_token, chat_id, u)
                        except Exception as e:  # noqa: BLE001
                            log.warning("Bot 命令处理异常: %s", _clean_token_text(e))
                            _update_offset = int(u.get("update_id") or _update_offset) + 1
                    if not updates:
                        await asyncio.sleep(_POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("Bot 命令轮询异常（5s 后自愈重试）: %s", _clean_token_text(e))
            await asyncio.sleep(5.0)
