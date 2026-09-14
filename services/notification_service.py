# -*- coding: utf-8 -*-
"""
services/notification_service.py — Telegram 多通道通知外发服务 (Bot / Saved Messages)
==================================================================================
支持基于 Bot API 与 TDLib Saved Messages 的大文件下载完成、网盘归档成功/失败、磁盘熔断告警派发。
"""
import re
import time
import asyncio
from urllib.parse import urlsplit
from typing import Any, Dict, List, Optional, Tuple
import httpx
from core.config import _fmt_size
from core.state import _NOTIFY_CONFIG
from core.backend import telegram_api_call
from core.logging import log
from services.openlist_service import _openlist_direct_url

RE_BOT_TOKEN = re.compile(r"^[A-Za-z0-9_:-]{3,100}$")
RE_CHAT_ID = re.compile(r"^[A-Za-z0-9_@-]{1,64}$")
_NOTIFY_LAST_DISK_ALERT = 0.0

# 持有派发任务的强引用：asyncio 事件循环只持弱引用，裸 create_task 的返回值
# 随时可能被 GC 回收，任务在完成前就被丢掉（异常还会被内部吞掉，调用方无从
# 发现），表现为偶发「通知没发出」。
_NOTIFY_PENDING_TASKS: set = set()


def _spawn_notification(kind: str, card: str) -> None:
    """派发通知并持有任务引用，完成后自动从集合移除。"""
    task = asyncio.create_task(_dispatch_notification(kind, card))
    _NOTIFY_PENDING_TASKS.add(task)
    task.add_done_callback(_NOTIFY_PENDING_TASKS.discard)


async def _send_via_bot(bot_token: str, chat_id: str, html_text: str) -> Tuple[bool, str]:
    bot_token = str(bot_token or "").strip()
    chat_id = str(chat_id or "").strip()
    if not bot_token or not chat_id:
        return False, "缺少 Bot Token 或 Chat ID"

    if not RE_BOT_TOKEN.match(bot_token):
        return False, "非法 Telegram Bot Token 格式（需为合法数字ID与密钥组合）"
    if not RE_CHAT_ID.match(chat_id):
        return False, "非法 Telegram Chat ID 格式（需为数字ID或@频道群组名）"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "api.telegram.org" or parsed.port not in (None, 443):
        return False, "非法 Telegram Bot API 目标地址"

    payload = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                return True, ""
            err_data = {}
            try:
                err_data = resp.json()
            except Exception:
                pass
            desc = err_data.get("description") or f"HTTP {resp.status_code}"
            clean_desc = re.sub(r"bot[0-9]+:[A-Za-z0-9_-]+", "bot<REDACTED>", str(desc))
            return False, clean_desc
    except Exception as e:
        clean_err = re.sub(r"bot[0-9]+:[A-Za-z0-9_-]+", "bot<REDACTED>", str(e))
        return False, clean_err


async def _send_via_saved_messages(text: str) -> Tuple[bool, str]:
    try:
        from core.backend import chat_sources
        sources = await chat_sources()
        if not sources:
            return False, "尚未登录任何 Telegram 账号"
        tg_id = None
        for s in sources:
            if s.get("telegramId"):
                tg_id = s.get("telegramId")
                break
        if not tg_id:
            return False, "未找到有效的 Telegram 账号"
        plain_text = re.sub(r"<[^>]+>", "", text)
        await telegram_api_call("SendMessage", {
            "chatId": int(tg_id),
            "inputMessageContent": {
                "@type": "inputMessageText",
                "text": {"@type": "formattedText", "text": plain_text}
            }
        }, timeout=15.0)
        return True, ""
    except Exception as e:
        log.warning("通过 Saved Messages 发送通知失败: %s", e)
        return False, str(e)


async def _dispatch_notification(event_type: str, html_content: str, force: bool = False,
                                override_bot_token: str = "", override_chat_id: str = "",
                                override_channel: str = "") -> Dict[str, Any]:
    """统一通知外发分发器。非阻塞、异常静默降级、支持双通道。"""
    try:
        if not force and not _NOTIFY_CONFIG.get("enabled", False):
            return {"ok": False, "skipped": True, "reason": "通知未全局开启"}

        events_cfg = _NOTIFY_CONFIG.get("events", {})
        if not force and not events_cfg.get(event_type, True):
            return {"ok": False, "skipped": True, "reason": f"事件 {event_type} 未勾选开启"}

        channel = override_channel or str(_NOTIFY_CONFIG.get("channel") or "both")
        bot_token = override_bot_token or str(_NOTIFY_CONFIG.get("botToken") or "").strip()
        chat_id = override_chat_id or str(_NOTIFY_CONFIG.get("chatId") or "").strip()

        results = {}
        errors = []

        if channel in ("bot", "both"):
            if bot_token and chat_id:
                ok, err = await _send_via_bot(bot_token, chat_id, html_content)
                results["bot"] = ok
                if not ok:
                    errors.append(f"Bot 发送失败: {err}")
            else:
                errors.append("Bot 通道缺少配置 (Token 或 ChatID)")

        if channel in ("saved_messages", "both"):
            ok, err = await _send_via_saved_messages(html_content)
            results["saved_messages"] = ok
            if not ok:
                errors.append(f"Saved Messages 发送失败: {err}")

        success = any(results.values())
        if not success and errors:
            log.warning("Telegram 通知外发失败: %s", "; ".join(errors))
        return {
            "ok": success,
            "results": results,
            "errors": errors
        }
    except Exception as e:
        log.warning("通知分发异常: %s", e)
        return {"ok": False, "errors": [str(e)]}


def notify_download_completed(task: Dict[str, Any]) -> None:
    """大文件下载完成通知。"""
    try:
        sz_bytes = task.get("_size_bytes")
        if not sz_bytes and isinstance(task.get("size"), (int, float)):
            sz_bytes = int(task.get("size"))
        min_mb = int(_NOTIFY_CONFIG.get("minFileSizeMB", 50) or 50)
        if sz_bytes and sz_bytes < min_mb * 1024 * 1024:
            return

        fn = str(task.get("filename") or "未知文件")
        sz_str = str(task.get("size") or _fmt_size(sz_bytes))
        chat_title = str(task.get("source") or "未知会话")
        lp = str(task.get("local_path") or "—")
        card = (
            "🎉 <b>【大文件下载完成】</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>文件名称：</b><code>{fn}</code>\n"
            f"📊 <b>文件大小：</b><code>{sz_str}</code>\n"
            f"💬 <b>来源会话：</b><code>{chat_title}</code>\n"
            f"💾 <b>本地路径：</b><code>{lp}</code>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>💡 该文件已安全落地，等待归档转存。</i>"
        )
        _spawn_notification("downloadCompleted", card)
    except Exception as e:
        log.debug("notify_download_completed 异常: %s", e)


def notify_archive_success(job: Dict[str, Any]) -> None:
    """网盘归档成功通知。"""
    try:
        fn = str(job.get("filename") or "未知文件")
        sz = _fmt_size(job.get("size_bytes"))
        rp = str(job.get("remote_path") or "—")
        drive = (rp.strip("/").split("/")[0] if "/" in rp.strip("/") else "默认网盘") if rp else "默认网盘"
        url = _openlist_direct_url(rp)
        start_ts = job.get("created_at") or 0.0
        done_ts = job.get("archived_at") or time.time()
        elapsed = max(0, int(done_ts - start_ts)) if start_ts else 0
        elapsed_str = f"{elapsed // 60}分{elapsed % 60}秒" if elapsed >= 60 else f"{elapsed}秒"

        card = (
            "☁️ <b>【网盘归档成功】</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>文件名称：</b><code>{fn}</code>\n"
            f"📊 <b>文件大小：</b><code>{sz}</code>\n"
            f"📁 <b>目标网盘：</b><code>{drive}</code>\n"
            f"📂 <b>存储路径：</b><code>{rp}</code>\n"
            f"⏱️ <b>上传耗时：</b><code>{elapsed_str}</code>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f'🔗 <a href="{url}">点击直接在 OpenList 中查看文件</a>'
        )
        _spawn_notification("archiveSuccess", card)
    except Exception as e:
        log.debug("notify_archive_success 异常: %s", e)


def notify_archive_failed(job: Dict[str, Any]) -> None:
    """网盘归档失败通知。"""
    try:
        fn = str(job.get("filename") or "未知文件")
        rp = str(job.get("remote_path") or "—")
        raw_err = str(job.get("error") or "未知错误")
        cat = "未知异常"
        action = "建议前往管理台查看日志"
        if re.search(r"401|403|token|unauthorized|OpenListAuthErr", raw_err, re.I):
            cat = "🔑 鉴权过期"
            action = "重新登录刷新 OpenList 令牌后重试"
        elif re.search(r"quota|full|space|容量|空间|超限", raw_err, re.I):
            cat = "💾 容量超限"
            action = "清理网盘空间或更换目标盘符"
        elif re.search(r"exist|conflict|409|冲突|重名", raw_err, re.I):
            cat = "⚠️ 文件冲突"
            action = "启用覆盖策略后重试"
        elif re.search(r"timeout|refused|connect|502|503|504|超时|网络", raw_err, re.I):
            cat = "🌐 网络超时"
            action = "网络抖动，建议稍后自动重试"

        card = (
            "🚨 <b>【网盘归档失败告警】</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>文件名称：</b><code>{fn}</code>\n"
            f"📂 <b>目标路径：</b><code>{rp}</code>\n"
            f"⚠️ <b>故障类别：</b><b>{cat}</b>\n"
            f"❌ <b>详细原因：</b><code>{raw_err[:120]}</code>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"<i>🔧 处理建议：{action}</i>"
        )
        _spawn_notification("archiveFailed", card)
    except Exception as e:
        log.debug("notify_archive_failed 异常: %s", e)


def notify_disk_watermark_alert(cur_pct: float, high_thresh: float, free_gb: float, waiting_count: int) -> None:
    """磁盘高水位熔断告警（600s 节流防抖，防暴击刷屏）。"""
    global _NOTIFY_LAST_DISK_ALERT
    now = time.monotonic()
    if now - _NOTIFY_LAST_DISK_ALERT < 600.0:
        return
    _NOTIFY_LAST_DISK_ALERT = now
    try:
        card = (
            "⚠️ <b>【VPS 磁盘高水位熔断告警】</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📈 <b>当前磁盘占用：</b><code>{cur_pct:.1f}%</code>（警戒阈值: <code>{high_thresh:.1f}%</code>）\n"
            f"💽 <b>剩余可用空间：</b><code>{free_gb:.2f} GB</code>\n"
            f"⏸️ <b>调度状态：</b>新任务已自动安全置入挂起队列（{waiting_count} 个任务等待）\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>🚨 系统已启动自动应急清理与保护，请关注存储安全。</i>"
        )
        _spawn_notification("diskWatermarkAlert", card)
    except Exception as e:
        log.debug("notify_disk_watermark_alert 异常: %s", e)

