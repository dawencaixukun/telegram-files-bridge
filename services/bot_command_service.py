# -*- coding: utf-8 -*-
"""
services/bot_command_service.py — Telegram Bot 双向交互与远程操控服务（getUpdates 长轮询）
========================================================================================
让 TG Bot 从「只外发通知」升级为「全功能可操作终端」：
1. 自动向 Telegram 注册命令菜单（setMyCommands + setChatMenuButton），输入框输入 `/`
   立即像 Hermes Bot 一样弹出完整命令选项；
2. 支持命令与内联按钮（Inline Keyboard / callback_query）双向操作：
   - /up      立即上传待归档文件到网盘（支持全部上传、单文件上传、点选目标网盘目录上传）
   - /retry   一键重试失败的网盘上传任务与 TG 下载任务（支持单条/批量重试、换目录重试、清理失败记录）
   - /dir     查看或设定上传网盘路径（支持直接输入路径，或通过内联按钮实时浏览 OpenList 目录点选设定，并支持一键同步到收藏夹等订阅规则）
   - /dl      解析 t.me 消息链接并加入下载队列（也可直接把 t.me 链接发给 Bot）
   - /ck      查看下载任务进度与可操作列表（带立即上传/重试按钮）
   - /yd      查看云端归档上传任务队列（done 不罗列；带重试/取消/清理按钮）
   - /st      系统总览一屏速读与快捷操作面板
   - /err     最近归档失败原因与一键重试/清理
   - /help    查看全部命令与操作说明

安全模型：
  - 仅响应管理员配置的白名单 chatId（message 与 callback_query 均严格校验），陌生人消息一律静默忽略；
  - 所有 callback_data 经短键映射严格控制在 64 字节以内；
  - 响应文本经 HTML 转义与 Bot Token 脱敏清洗。
"""
import os
import re
import time
import secrets
import hashlib
import asyncio
import posixpath
from typing import Any, Dict, List, Optional, Tuple
import httpx
from core.config import (
    _fmt_size, _fmt_time, _clean_archive_filename,
    _resolve_host_local_path, _archive_norm_dir, _archive_join,
    _ensure_archive_ext, _archive_unique_name, _archive_name_suffix,
)
from core.state import (
    _NOTIFY_CONFIG, _ARCHIVE_JOBS, _ARCHIVE_CONFIG, _SUB_RULES,
    _DELETED_LOCAL_UIDS, _QUICK_ARCHIVE_REGISTRY,
    _WAITING_DISK_TASKS, _FLOOD_WAIT_STATE, _is_flood_wait_active,
    _archive_save, _archive_config_save, _subs_save,
    _archive_path_owners, _archive_active_of, _archive_latest_raw_of,
    _BATCH_RETRY_LOCK, _MAX_JOB_RETRIES, _tasks_cache_invalidate,
)
from core.backend import BACKEND
from core.logging import log, LOG_STORE

# 长轮询间隔与单次超时：Telegram 官方推荐 long-poll timeout ≤ 50s
_POLL_INTERVAL = 2.0
_POLL_TIMEOUT = 35.0
_update_offset = 0

# Telegram 输入框 `/` 弹出的命令菜单定义（command 须为小写字母/数字/下划线）
BOT_COMMANDS: List[Dict[str, str]] = [
    {"command": "up", "description": "立即上传待归档文件到网盘（或选目录上传）"},
    {"command": "retry", "description": "一键重试失败的网盘上传与下载任务"},
    {"command": "dir", "description": "查看或设定上传网盘路径（支持点选目录）"},
    {"command": "dl", "description": "解析 t.me 消息链接并加入下载队列"},
    {"command": "ck", "description": "查看下载任务进度与可操作列表"},
    {"command": "yd", "description": "查看云端上传队列与失败处理"},
    {"command": "cloudtasks", "description": "查看当前网盘正在复制/移动的任务与进度"},
    {"command": "st", "description": "系统总览与快捷操作面板"},
    {"command": "err", "description": "最近归档失败详情与一键重试/清理"},
    {"command": "help", "description": "查看全部命令与操作说明"},
]

# 最近一次归档失败快照（/err 用，环形保留 8 条）
_RECENT_ARCHIVE_ERRORS: List[Dict[str, Any]] = []
_RECENT_ARCHIVE_ERRORS_MAX = 8

# Callback 短键映射表（保证 callback_data 永远 <= 64 字节）
_CB_MAP: Dict[str, str] = {}
_CB_MAP_MAX = 1000


def _cb_put(val: str) -> str:
    """将任意长度的字符串（如路径、uniqueId）映射为 10 位十六进制短键。"""
    s = str(val or "")
    k = hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]
    if len(_CB_MAP) >= _CB_MAP_MAX and k not in _CB_MAP:
        try:
            oldest = next(iter(_CB_MAP))
            _CB_MAP.pop(oldest, None)
        except StopIteration:
            pass
    _CB_MAP[k] = s
    return k


def _cb_resolve_uid(key: str, tasks: Optional[List[Dict[str, Any]]] = None) -> str:
    """从短键还原 uniqueId；若进程重启过导致内存表清空，从任务列表中按哈希反查。"""
    if key in _CB_MAP:
        return _CB_MAP[key]
    for t in (tasks or []):
        uid = str(t.get("_unique_id") or "")
        if uid and (uid == key or hashlib.sha1(uid.encode("utf-8")).hexdigest()[:10] == key):
            _CB_MAP[key] = uid
            return uid
    for j in _ARCHIVE_JOBS.values():
        uid = str(j.get("unique_id") or "")
        if uid and (uid == key or hashlib.sha1(uid.encode("utf-8")).hexdigest()[:10] == key):
            _CB_MAP[key] = uid
            return uid
    return key


def _cb_resolve_dir(key: str) -> str:
    """从短键还原网盘目录路径。"""
    if key in ("root", "/", ""):
        return "/"
    if key in _CB_MAP:
        return _CB_MAP[key]
    if key.startswith("/"):
        return key
    candidates = [str(_ARCHIVE_CONFIG.get("defaultDir") or "")]
    for r in _SUB_RULES.values():
        candidates.append(str(r.get("dirTemplate") or ""))
    for j in _ARCHIVE_JOBS.values():
        candidates.append(str(j.get("remote_dir") or ""))
    for c in candidates:
        if c and hashlib.sha1(c.encode("utf-8")).hexdigest()[:10] == key:
            _CB_MAP[key] = c
            return c
    return "/"


def remember_archive_error(job: Dict[str, Any]) -> None:
    """归档失败时由 archive_service 调用，快照供 /err 展示。"""
    try:
        _RECENT_ARCHIVE_ERRORS.insert(0, {
            "id": str(job.get("id") or ""),
            "unique_id": str(job.get("unique_id") or ""),
            "filename": str(job.get("filename") or "未知文件"),
            "remote_path": str(job.get("remote_path") or "—"),
            "error": str(job.get("error") or "未知错误")[:160],
            "at": float(job.get("updated_at") or time.time()),
        })
        del _RECENT_ARCHIVE_ERRORS[_RECENT_ARCHIVE_ERRORS_MAX:]
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------
# 通知卡片配套的内联按钮构造器（供 notification_service 调用）
# ---------------------------------------------------------------------
def build_notify_download_kb(task: Dict[str, Any]) -> Dict[str, Any]:
    """「大文件下载完成」通知卡片的内联操作按钮。"""
    uid = str(task.get("_unique_id") or task.get("uniqueId") or task.get("id") or "").strip()
    rows: List[List[Dict[str, str]]] = []
    if uid:
        uk = _cb_put(uid)
        rows.append([
            {"text": "☁️ 立即上传网盘", "callback_data": f"u:{uk}"},
            {"text": "📂 选目录上传", "callback_data": f"ud:{uk}"},
        ])
    rows.append([
        {"text": "📥 下载进度", "callback_data": "act:ck"},
        {"text": "📂 设定网盘路径", "callback_data": "act:dir"},
    ])
    return {"inline_keyboard": rows}


def build_notify_archive_failed_kb(job: Dict[str, Any]) -> Dict[str, Any]:
    """「网盘归档失败告警」通知卡片的内联操作按钮。"""
    jid = str(job.get("id") or "").strip()
    rows: List[List[Dict[str, str]]] = []
    if jid and len(jid) <= 32:
        rows.append([
            {"text": "🔄 立即重试上传", "callback_data": f"rj:{jid}"},
            {"text": "📂 换目录重试", "callback_data": f"jd:{jid}"},
        ])
        rows.append([
            {"text": "🗑️ 删除此失败记录", "callback_data": f"dj:{jid}"},
            {"text": "📊 查看上传队列", "callback_data": "act:yd"},
        ])
    else:
        rows.append([
            {"text": "🔄 一键重试失败任务", "callback_data": "act:retry_all"},
            {"text": "📊 查看上传队列", "callback_data": "act:yd"},
        ])
    return {"inline_keyboard": rows}


def build_notify_archive_success_kb(job: Dict[str, Any]) -> Dict[str, Any]:
    """「网盘归档成功」通知卡片的内联操作按钮。"""
    return {
        "inline_keyboard": [[
            {"text": "📊 查看上传队列", "callback_data": "act:yd"},
            {"text": "📈 系统总览", "callback_data": "act:st"},
        ]]
    }


def build_notify_disk_alert_kb() -> Dict[str, Any]:
    """「磁盘高水位熔断告警」通知卡片的内联操作按钮。"""
    return {
        "inline_keyboard": [[
            {"text": "🧹 立即清理并唤醒任务", "callback_data": "act:disk_wake"},
            {"text": "📈 系统总览", "callback_data": "act:st"},
        ]]
    }


# ---------------------------------------------------------------------
# 基础工具与只读视图
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


async def _tasks_all_safe(force: bool = False) -> List[Dict[str, Any]]:
    """拉取任务列表（复用 tasks_all 的 8s 缓存），6s 超时降级为空集，
    保证 Bot 命令响应永不卡死。"""
    from services.task_service import tasks_all
    try:
        if force:
            try:
                return await asyncio.wait_for(tasks_all(force=True), timeout=6.0)
            except TypeError:
                return await asyncio.wait_for(tasks_all(), timeout=6.0)
        return await asyncio.wait_for(tasks_all(), timeout=6.0)
    except Exception as e:  # noqa: BLE001
        log.warning("Bot 命令拉取任务失败: %s", e)
        return []


def _local_exists_for_task(t: Dict[str, Any]) -> Tuple[bool, str]:
    """检查任务的本地物理文件是否存在。返回 (exists, resolved_path)。"""
    uid = str(t.get("_unique_id") or "")
    if uid and uid in _DELETED_LOCAL_UIDS:
        return False, ""
    raw_lp = str(t.get("local_path") or "")
    if not raw_lp or raw_lp == "—":
        return False, ""
    lp = _resolve_host_local_path(raw_lp)
    if not lp:
        return False, ""
    try:
        return os.path.exists(lp), lp
    except OSError:
        return False, ""


def _resolve_target_dir_for_task(t: Dict[str, Any], rules: List[Dict[str, Any]],
                                 override_dir: str = "") -> Tuple[Optional[str], str, bool]:
    """为单个下载完成任务计算归档目标目录、冲突策略与 deleteLocal。"""
    uid = str(t.get("_unique_id") or "")
    default_policy = "skip" if str(_ARCHIVE_CONFIG.get("policy") or "").lower() == "skip" else "overwrite"
    default_del = bool(_ARCHIVE_CONFIG.get("deleteLocal"))

    if override_dir:
        norm = _archive_norm_dir(override_dir)
        return norm, default_policy, default_del

    quick = _QUICK_ARCHIVE_REGISTRY.get(uid)
    if quick is not None:
        norm = _archive_norm_dir(str(quick.get("remoteDir") or ""))
        pol = "skip" if str(quick.get("policy") or "").lower() == "skip" else "overwrite"
        dl = bool(quick.get("deleteLocal", default_del))
        if norm and norm != "/":
            return norm, pol, dl

    from services.subscription_service import _sub_match_rule, _render_dir_template
    rule = _sub_match_rule(t, rules)
    if rule is not None:
        rendered = _render_dir_template(
            str(rule.get("dirTemplate") or ""),
            source=str(t.get("source") or rule.get("chatTitle") or ""),
            ftype=str(t.get("_type") or ""),
            ts=t.get("_date_ts") if isinstance(t.get("_date_ts"), (int, float)) else None,
            filename=str(t.get("filename") or ""),
            chat_title=str(rule.get("chatTitle") or t.get("source") or ""),
            width=t.get("width"),
            height=t.get("height"),
        )
        if rendered and rendered != "/":
            pol = "skip" if str(rule.get("policy") or "") == "skip" else "overwrite"
            dl = bool(rule.get("deleteLocal", True))
            return rendered, pol, dl

    def_dir = _archive_norm_dir(str(_ARCHIVE_CONFIG.get("defaultDir") or ""))
    if def_dir and def_dir != "/":
        return def_dir, default_policy, default_del
    return None, default_policy, default_del


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
        n_dl = sum(1 for t in done if t.get("status") == "downloaded")
        n_arch = sum(1 for t in done if t.get("status") == "archived")
        total_sz = sum(int(t.get("_size_bytes") or 0) for t in done)
        lines.append("\n✅ <b>已完成（完整性校验通过）</b>")
        lines.append(f"  📦 已下载待归档：<b>{n_dl}</b> 个 · 已归档：<b>{n_arch}</b> 个")
        lines.append(f"  💾 累计体积：<b>{_fmt_size(total_sz)}</b>")

    if failed:
        lines.append(f"\n❌ <b>失败/隔离 {len(failed)} 个</b>（可点下方按钮一键重试）")
        for t in failed[:3]:
            fn = _esc(str(t.get("filename") or "未知"))[:32]
            err = _esc(str(t.get("error_msg") or ""))[:60]
            lines.append(f"  · {fn}\n    {err}")

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"<i>共 {len(tasks)} 个任务 · 发送 /up 立即上传网盘 · /st 看系统总览</i>")
    return "\n".join(lines)


async def build_ck_panel() -> Tuple[str, Dict[str, Any]]:
    """/ck 文本 + 交互操作按钮。"""
    text = await build_ck_reply()
    tasks = await _tasks_all_safe()
    unarchived = [t for t in tasks if t.get("status") == "downloaded"]
    failed = [t for t in tasks if t.get("status") == "failed"]

    rows: List[List[Dict[str, str]]] = []
    if unarchived:
        rows.append([{"text": f"☁️ 一键上传待归档 ({len(unarchived)})", "callback_data": "act:up_all"}])
        for t in unarchived[:3]:
            uid = str(t.get("_unique_id") or "")
            if uid:
                uk = _cb_put(uid)
                fn_short = str(t.get("filename") or "文件")[:14]
                rows.append([
                    {"text": f"☁️ 上传 {fn_short}", "callback_data": f"u:{uk}"},
                    {"text": "📂 选目录", "callback_data": f"ud:{uk}"},
                ])
    if failed:
        rows.append([{"text": f"🔄 重试失败下载 ({len(failed)})", "callback_data": "act:retry_all"}])
    rows.append([
        {"text": "🔃 刷新进度", "callback_data": "act:ck"},
        {"text": "📊 上传队列", "callback_data": "act:yd"},
        {"text": "📂 网盘路径", "callback_data": "act:dir"},
    ])
    return text, {"inline_keyboard": rows}


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
        lines.append(f"\n❌ <b>失败 {len(failed)} 个</b>（可点下方按钮一键重试或换目录）")
        for j in failed[:4]:
            fn = _esc(str(j.get("filename") or "未知"))[:32]
            err = _esc(str(j.get("error") or ""))[:60]
            lines.append(f"  · {fn}\n    {err}")

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"<i>历史已归档 <b>{done_count}</b> 个（不罗列） · 自动归档: {'开' if _ARCHIVE_CONFIG.get('autoArchive') else '关'}</i>")
    return "\n".join(lines)


async def build_yd_panel() -> Tuple[str, Dict[str, Any]]:
    """/yd 文本 + 交互操作按钮。"""
    text = await build_yd_reply()
    active_states = ("queued", "uploading", "verifying")
    active = [j for j in _ARCHIVE_JOBS.values() if str(j.get("state") or "") in active_states]
    failed = [j for j in _ARCHIVE_JOBS.values() if str(j.get("state") or "") == "failed"]
    active.sort(key=lambda j: float(j.get("created_at") or 0.0), reverse=True)
    failed.sort(key=lambda j: float(j.get("updated_at") or 0.0), reverse=True)

    rows: List[List[Dict[str, str]]] = []
    for j in active[:2]:
        jid = str(j.get("id") or "")
        if jid and len(jid) <= 32:
            fn_short = str(j.get("filename") or "任务")[:14]
            rows.append([{"text": f"⏹ 取消上传: {fn_short}", "callback_data": f"cj:{jid}"}])

    if failed:
        rows.append([
            {"text": f"🔄 一键重试失败 ({len(failed)})", "callback_data": "act:retry_all"},
            {"text": "🗑️ 清理失败记录", "callback_data": "act:dismiss_all"},
        ])
        for j in failed[:2]:
            jid = str(j.get("id") or "")
            if jid and len(jid) <= 32:
                fn_short = str(j.get("filename") or "任务")[:12]
                rows.append([
                    {"text": f"🔄 重试 {fn_short}", "callback_data": f"rj:{jid}"},
                    {"text": "📂 换目录重试", "callback_data": f"jd:{jid}"},
                ])

    rows.append([
        {"text": "☁️ 触发待归档上传", "callback_data": "act:up_all"},
        {"text": "📂 设定网盘路径", "callback_data": "act:dir"},
    ])
    rows.append([
        {"text": "🔃 刷新队列", "callback_data": "act:yd"},
        {"text": "📈 系统总览", "callback_data": "act:st"},
    ])
    return text, {"inline_keyboard": rows}


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
    def_dir = str(_ARCHIVE_CONFIG.get("defaultDir") or "未设置")
    lines = [
        "📊 <b>【系统总览】</b>",
        "━━━━━━━━━━━━━━━━━━",
        "⚡ <b>实时速率</b>",
        f"  下载：<b>{_fmt_speed(dl_now)}</b> · 上传：<b>{_fmt_speed(ul_speed)}</b>",
        "📋 <b>任务</b>",
        f"  进行中 <b>{running}</b> · 完成 <b>{archived}</b> · 失败 <b>{failed}</b>",
        "☁️ <b>归档队列与路径</b>",
    ]
    uploading = sum(1 for j in _ARCHIVE_JOBS.values() if str(j.get("state") or "") in ("queued", "uploading", "verifying"))
    failed_arch = sum(1 for j in _ARCHIVE_JOBS.values() if j.get("state") == "failed")
    lines.append(f"  上传中 <b>{uploading}</b> · 失败 <b>{failed_arch}</b> · 已归档 <b>{sum(1 for j in _ARCHIVE_JOBS.values() if j.get('state') == 'done')}</b>")
    lines.append(f"  默认路径：<code>{_esc(def_dir)}</code>")
    # 跨盘传输任务统计
    try:
        from services.openlist_service import _CLOUD_TRANSFER_TASKS, _openlist_tasks_undone
        c_copy = await _openlist_tasks_undone("copy")
        c_move = await _openlist_tasks_undone("move")
        active_transfers = len(c_copy) + len(c_move) + sum(1 for t in _CLOUD_TRANSFER_TASKS.values() if t.get("state") == "running")
    except Exception:
        active_transfers = 0
    if active_transfers > 0:
        lines.append("🔄 <b>跨盘传输</b>")
        lines.append(f"  进行中 <b>{active_transfers}</b> 个任务（发送 /cloudtasks 查看详情）")

    lines.append("💽 <b>磁盘</b>")
    lines.append(f"  已用 <b>{disk_pct:.1f}%</b> · 可用 <b>{free_gb:.1f} GB</b>")
    if _WAITING_DISK_TASKS:
        lines.append(f"  ⏸️ 磁盘挂起任务：<b>{len(_WAITING_DISK_TASKS)}</b> 个")
    if fw:
        rem = max(0, fw_rem)
        lines.append(f"🚦 <b>FloodWait 冷却中</b>（剩余 {rem // 60}分{rem % 60}秒）")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"<i>{_fmt_time(time.time())} · 发送 /help 查看全部命令</i>")
    return "\n".join(lines)


async def build_st_panel() -> Tuple[str, Dict[str, Any]]:
    """/st 文本 + 快捷操控按钮面板。"""
    text = await build_st_reply()
    kb = {
        "inline_keyboard": [
            [
                {"text": "☁️ 立即上传待归档", "callback_data": "act:up_all"},
                {"text": "🔄 一键重试失败", "callback_data": "act:retry_all"},
            ],
            [
                {"text": "📂 设定上传网盘路径", "callback_data": "act:dir"},
                {"text": "📥 查看下载进度", "callback_data": "act:ck"},
            ],
            [
                {"text": "📊 云端上传队列", "callback_data": "act:yd"},
                {"text": "🔄 跨盘传输任务", "callback_data": "act:cloud_tasks"},
            ],
            [
                {"text": "🔃 刷新总览", "callback_data": "act:st"},
            ],
        ]
    }
    return text, kb


async def build_cloud_tasks_reply() -> str:
    """/cloudtasks — 查看当前网盘正在复制/移动的任务与进度。"""
    from services.openlist_service import _openlist_tasks_undone, _CLOUD_TRANSFER_TASKS
    try:
        copy_tasks = await _openlist_tasks_undone("copy")
    except Exception:
        copy_tasks = []
    try:
        move_tasks = await _openlist_tasks_undone("move")
    except Exception:
        move_tasks = []

    all_tasks: List[Dict[str, Any]] = []
    for t in copy_tasks:
        all_tasks.append({"action": "复制", **t})
    for t in move_tasks:
        all_tasks.append({"action": "移动", **t})

    for tid, lt in _CLOUD_TRANSFER_TASKS.items():
        if lt.get("state") == "running" and not any(t.get("id") == tid for t in all_tasks):
            fn_str = "、".join(lt.get("names") or [])
            all_tasks.append({
                "id": tid,
                "action": "移动" if lt.get("action") == "move" else "复制",
                "name": f"[{fn_str}] → [{lt.get('dst_dir')}]",
                "progress": lt.get("progress", 0.0),
                "status": "running",
            })

    lines = [
        "☁️ <b>【网盘跨盘传输任务队列】</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    if not all_tasks:
        lines.append("当前暂无正在执行的跨网盘复制或移动任务。")
        lines.append("")
        lines.append("💡 <i>在网页版「浏览」资产页卡片上可直接操作跨盘复制与移动</i>")
    else:
        lines.append(f"当前共有 <b>{len(all_tasks)}</b> 个传输任务正在进行：\n")
        for i, t in enumerate(all_tasks, 1):
            act = t.get("action", "传输")
            name = _esc(t.get("name") or "未命名任务")
            pct = float(t.get("progress") or 0.0)
            bar_str = _bar(pct, width=10)
            lines.append(f"<b>{i}. [{act}]</b> <code>{name}</code>")
            lines.append(f"   进度：<code>{bar_str}</code> <b>{pct:.1f}%</b>")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"<i>{_fmt_time(time.time())} · 发送 /st 查看系统总览</i>")
    return "\n".join(lines)


async def build_cloud_tasks_panel() -> Tuple[str, Dict[str, Any]]:
    """/cloudtasks 文本 + 快捷面板。"""
    text = await build_cloud_tasks_reply()
    kb = {
        "inline_keyboard": [
            [
                {"text": "🔄 刷新进度", "callback_data": "act:cloud_tasks"},
                {"text": "📈 系统总览 (/st)", "callback_data": "act:st"},
            ]
        ]
    }
    return text, kb


def build_err_reply() -> str:
    """/err — 最近归档失败原因与建议。"""
    failed_jobs = [j for j in _ARCHIVE_JOBS.values() if j.get("state") == "failed"]
    failed_jobs.sort(key=lambda j: float(j.get("updated_at") or 0.0), reverse=True)

    items: List[Dict[str, Any]] = []
    seen_keys = set()
    for j in failed_jobs[:5]:
        k = (str(j.get("filename") or ""), str(j.get("remote_path") or ""))
        seen_keys.add(k)
        items.append({
            "filename": j.get("filename") or "未知",
            "remote_path": j.get("remote_path") or "—",
            "error": j.get("error") or "未知",
            "at": j.get("updated_at") or j.get("created_at") or 0,
        })
    for e in _RECENT_ARCHIVE_ERRORS:
        if len(items) >= 5:
            break
        k = (str(e.get("filename") or ""), str(e.get("remote_path") or ""))
        if k not in seen_keys:
            seen_keys.add(k)
            items.append(e)

    if not items:
        return "✅ 最近没有归档失败记录。"
    lines = ["🚨 <b>【最近归档失败】</b>", "━━━━━━━━━━━━━━━━━━"]
    for e in items[:5]:
        fn = _esc(e.get("filename") or "未知")[:36]
        rp = _esc(e.get("remote_path") or "—")[:40]
        err = _esc(e.get("error") or "未知")[:80]
        t = _fmt_time(e.get("at") or 0)
        lines.append(f"📦 {fn}\n  📂 {rp}\n  ❌ {err}\n  🕒 {t}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("<i>💡 可直接点击下方按钮一键重试、更换网盘路径或清理失败记录</i>")
    return "\n".join(lines)


def build_err_panel() -> Tuple[str, Dict[str, Any]]:
    """/err 文本 + 重试/清理按钮。"""
    text = build_err_reply()
    failed_jobs = [j for j in _ARCHIVE_JOBS.values() if j.get("state") == "failed"]
    failed_jobs.sort(key=lambda j: float(j.get("updated_at") or 0.0), reverse=True)

    rows: List[List[Dict[str, str]]] = [
        [
            {"text": "🔄 一键重试全部失败", "callback_data": "act:retry_all"},
            {"text": "🗑️ 清空失败记录", "callback_data": "act:dismiss_all"},
        ],
    ]
    for j in failed_jobs[:3]:
        jid = str(j.get("id") or "")
        if jid and len(jid) <= 32:
            fn_short = str(j.get("filename") or "任务")[:12]
            rows.append([
                {"text": f"🔄 重试 {fn_short}", "callback_data": f"rj:{jid}"},
                {"text": "📂 换目录重试", "callback_data": f"jd:{jid}"},
            ])
    rows.append([
        {"text": "📂 设定网盘路径", "callback_data": "act:dir"},
        {"text": "📊 上传队列", "callback_data": "act:yd"},
    ])
    return text, {"inline_keyboard": rows}


HELP_TEXT = "\n".join([
    "🤖 <b>【TG 归档台 Bot 操控中心】</b>",
    "━━━━━━━━━━━━━━━━━━",
    "<b>⚡ 核心操作命令：</b>",
    "/up — 立即上传待归档文件（支持 <code>/up /网盘/目录</code>）",
    "/retry — 一键重试失败的上传与下载任务",
    "/dir — 查看或设定上传网盘路径（支持点选目录或 <code>/dir /onedrive/xxx</code>）",
    "/dl — 解析 <code>t.me/...</code> 链接下载（或直接把链接发给 Bot）",
    "",
    "<b>📊 状态监控命令：</b>",
    "/ck — 下载任务进度与完整性",
    "/yd — 云端归档上传队列（已归档不罗列）",
    "/cloudtasks — 查看当前网盘正在复制/移动的任务与进度",
    "/st 或 /status — 系统总览与快捷操作面板",
    "/err — 最近归档失败详情与一键处理",
    "/help — 本帮助与快捷菜单",
    "━━━━━━━━━━━━━━━━━━",
    "<i>💡 输入框输入 / 即可弹出命令菜单；所有通知与面板均支持直接点击底部按钮操作。</i>",
])


def build_help_panel() -> Tuple[str, Dict[str, Any]]:
    """/help 文本 + 快捷操控面板。"""
    kb = {
        "inline_keyboard": [
            [
                {"text": "☁️ 立即上传待归档 (/up)", "callback_data": "act:up_all"},
                {"text": "🔄 一键重试失败 (/retry)", "callback_data": "act:retry_all"},
            ],
            [
                {"text": "📂 设定上传网盘路径 (/dir)", "callback_data": "act:dir"},
            ],
            [
                {"text": "📥 下载进度 (/ck)", "callback_data": "act:ck"},
                {"text": "📊 上传队列 (/yd)", "callback_data": "act:yd"},
                {"text": "📈 系统总览 (/st)", "callback_data": "act:st"},
            ],
        ]
    }
    return HELP_TEXT, kb


# ---------------------------------------------------------------------
# 核心可操作能力实现：上传 (/up)、重试 (/retry)、设定网盘路径 (/dir)、链接直投 (/dl)
# ---------------------------------------------------------------------
async def build_up_reply(arg: str = "", remote_dir_override: str = "") -> Tuple[str, Dict[str, Any]]:
    """/up — 立即触发网盘归档上传（支持全部待归档、指定文件、指定目标目录）。"""
    raw_arg = (arg or "").strip()
    target = "all"
    if raw_arg:
        if raw_arg.startswith("/") and not remote_dir_override:
            remote_dir_override = raw_arg
            target = "all"
        elif " /" in raw_arg and not remote_dir_override:
            parts = raw_arg.rsplit(" ", 1)
            if parts[1].startswith("/"):
                target = parts[0].strip() or "all"
                remote_dir_override = parts[1].strip()
            else:
                target = raw_arg
        else:
            target = raw_arg

    if remote_dir_override:
        norm_override = _archive_norm_dir(remote_dir_override)
        if not norm_override or norm_override == "/":
            return (
                "❌ <b>目标网盘目录格式无效</b>\n"
                "请填写以 <code>/</code> 开头且指向具体网盘子目录的路径（例如 <code>/onedrive/yello</code>）。",
                {"inline_keyboard": [[{"text": "📂 点选网盘目录", "callback_data": "act:dir"}]]}
            )
        remote_dir_override = norm_override

    tasks = await _tasks_all_safe(force=True)
    from services.subscription_service import _sub_rules_sorted
    from services.archive_service import _ARCHIVE_TASKS, _archive_worker
    enabled_rules = [r for r in _sub_rules_sorted() if r.get("enabled")]

    # 筛选本地物理存在且下载完成的任务
    local_completed: List[Tuple[Dict[str, Any], str]] = []
    for t in tasks:
        if str(t.get("_download_status") or "") != "completed" and t.get("status") not in ("downloaded", "archived", "upload"):
            continue
        ok_lp, lp = _local_exists_for_task(t)
        if ok_lp and lp:
            local_completed.append((t, lp))

    pending_upload: List[Tuple[Dict[str, Any], str]] = []
    already_archived_local: List[Tuple[Dict[str, Any], str]] = []
    active_uploading: List[Dict[str, Any]] = []

    for t, lp in local_completed:
        uid = str(t.get("_unique_id") or "")
        if not uid:
            continue
        active_job = _archive_active_of(uid)
        if active_job is not None:
            active_uploading.append(active_job)
            continue
        latest = _archive_latest_raw_of(uid)
        if latest is not None and latest.get("state") == "done":
            already_archived_local.append((t, lp))
        else:
            pending_upload.append((t, lp))

    to_enqueue: List[Tuple[Dict[str, Any], str]] = []
    if target.lower() in ("", "all", "*"):
        to_enqueue = pending_upload
    else:
        all_candidates = pending_upload + already_archived_local
        if target.isdigit():
            idx = int(target) - 1
            if 0 <= idx < len(all_candidates):
                to_enqueue = [all_candidates[idx]]
        if not to_enqueue:
            q_low = target.lower()
            for t, lp in all_candidates:
                uid = str(t.get("_unique_id") or "")
                fn = str(t.get("filename") or "")
                if uid == target or uid.lower().startswith(q_low) or q_low in fn.lower():
                    to_enqueue.append((t, lp))

    if not to_enqueue:
        lines = ["☁️ <b>【网盘上传操作】</b>", "━━━━━━━━━━━━━━━━━━"]
        if target.lower() not in ("", "all", "*"):
            lines.append(f"⚠️ 未找到匹配的本地可上传文件：<code>{_esc(target)}</code>")
        elif active_uploading:
            lines.append(f"⏳ 当前已有 <b>{len(active_uploading)}</b> 个文件正在排队/上传中。")
        else:
            lines.append("✅ 当前没有待上传的新下载文件。")

        def_dir = str(_ARCHIVE_CONFIG.get("defaultDir") or "未设置")
        lines.append(f"📂 当前默认网盘路径：<code>{_esc(def_dir)}</code>")

        rows: List[List[Dict[str, str]]] = []
        if already_archived_local:
            lines.append(f"\n📦 <b>本地在存文件（{len(already_archived_local)} 个，可点击重新上传或换目录）：</b>")
            for idx, (t, _) in enumerate(already_archived_local[:5], 1):
                uid = str(t.get("_unique_id") or "")
                fn = _esc(str(t.get("filename") or "未知"))[:32]
                sz = str(t.get("size") or "—")
                lines.append(f"  {idx}. {fn} ({sz})")
                if uid:
                    uk = _cb_put(uid)
                    fn_btn = str(t.get("filename") or f"文件#{idx}")[:12]
                    rows.append([
                        {"text": f"☁️ 重传: {fn_btn}", "callback_data": f"u:{uk}"},
                        {"text": "📂 选目录上传", "callback_data": f"ud:{uk}"},
                    ])
        failed_cnt = sum(1 for j in _ARCHIVE_JOBS.values() if j.get("state") == "failed")
        if failed_cnt:
            lines.append(f"\n❌ 另有 <b>{failed_cnt}</b> 个上传失败任务可重试。")
            rows.append([{"text": f"🔄 一键重试失败任务 ({failed_cnt})", "callback_data": "act:retry_all"}])

        rows.append([
            {"text": "📂 设定上传网盘路径", "callback_data": "act:dir"},
            {"text": "📊 查看上传队列", "callback_data": "act:yd"},
        ])
        return "\n".join(lines), {"inline_keyboard": rows}

    path_owners: Dict[str, set] = _archive_path_owners()
    started: List[Dict[str, Any]] = []
    missing_dir_files: List[str] = []

    for t, lp in to_enqueue:
        uid = str(t.get("_unique_id") or "")
        if not uid:
            continue
        active_job = _archive_active_of(uid)
        if active_job is not None:
            started.append(active_job)
            continue

        remote_dir, policy, delete_local = _resolve_target_dir_for_task(
            t, enabled_rules, override_dir=remote_dir_override
        )
        if not remote_dir or remote_dir == "/":
            missing_dir_files.append(str(t.get("filename") or uid))
            continue

        raw_name = _ensure_archive_ext(
            str(t.get("filename") or "未命名"),
            local_path=str(t.get("local_path") or ""),
            mime=str(t.get("_mimeType") or ""),
            ftype=str(t.get("_type") or ""),
        )
        clean_name = _clean_archive_filename(raw_name, enabled=bool(_ARCHIVE_CONFIG.get("cleanFilename", True)))
        clean_name = _archive_unique_name(
            remote_dir, clean_name,
            owner_of=path_owners, uid=uid,
            suffix=_archive_name_suffix(raw_name, clean_name),
        )
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
            "rule_id": "bot",
        }
        _ARCHIVE_JOBS[job["id"]] = job
        _ARCHIVE_TASKS[job["id"]] = asyncio.create_task(_archive_worker(job))
        started.append(job)
        log.info("归档任务已入队(Bot): %s → %s（策略 %s, 删本地: %s）",
                 job["filename"], job["remote_path"], policy, delete_local)

    if started:
        _archive_save()

    if not started and missing_dir_files:
        text, kb = await build_dir_reply("", "/")
        head = (
            "⚠️ <b>尚未配置默认上传网盘路径</b>\n"
            "请先在下方点选一个网盘目录设为默认路径，或直接发送：\n"
            "<code>/up /网盘名/目录名</code>\n\n"
        )
        return head + text, kb

    lines = [
        "☁️ <b>【已加入网盘上传队列】</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"✅ <b>已入队上传：{len(started)} 个文件</b>",
    ]
    for j in started[:6]:
        fn = _esc(str(j.get("filename") or "未知"))[:34]
        rd = _esc(str(j.get("remote_dir") or "—"))[:38]
        sz = _fmt_size(j.get("size_bytes"))
        lines.append(f"📦 {fn} ({sz})\n  📂 <code>{rd}</code>")
    if len(started) > 6:
        lines.append(f"  … 等共 {len(started)} 个文件")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("<i>💡 上传完成后将自动核验云端大小并推送结果通知。</i>")

    kb = {
        "inline_keyboard": [
            [
                {"text": "📊 查看上传进度 (/yd)", "callback_data": "act:yd"},
                {"text": "📂 设定网盘路径 (/dir)", "callback_data": "act:dir"},
            ]
        ]
    }
    return "\n".join(lines), kb


async def build_file_dir_picker(uid: str, browse_path: str = "/") -> Tuple[str, Dict[str, Any]]:
    """为单个本地文件构建交互式网盘目录选择器。"""
    tasks = await _tasks_all_safe()
    real_uid = _cb_resolve_uid(uid, tasks)
    t_hit = next((t for t in tasks if str(t.get("_unique_id") or "") == real_uid), None)
    fn = str((t_hit or {}).get("filename") or real_uid or "文件")
    uk = _cb_put(real_uid)

    norm_browse = _archive_norm_dir(browse_path) or "/"
    from services.openlist_service import openlist_dirs
    res = await openlist_dirs(norm_browse)
    dirs = (res.get("dirs") or []) if isinstance(res, dict) and res.get("ok") else []

    lines = [
        "📂 <b>【选择上传目标目录】</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"📦 <b>文件：</b><code>{_esc(fn[:42])}</code>",
        f"📍 <b>当前浏览位置：</b><code>{_esc(norm_browse)}</code>",
    ]
    if isinstance(res, dict) and not res.get("ok"):
        lines.append(f"⚠️ <i>读取 OpenList 目录提示：{_esc(res.get('message') or '不可达')}</i>")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("<i>点击子目录进入浏览，或点击「上传到此目录」立即开始上传：</i>")

    rows: List[List[Dict[str, str]]] = []
    if norm_browse != "/":
        bk = _cb_put(norm_browse)
        rows.append([{"text": f"✅ 立即上传到此目录 ({norm_browse})", "callback_data": f"uz:{uk}:{bk}"}])
        parent = posixpath.dirname(norm_browse.rstrip("/")) or "/"
        pk = _cb_put(parent)
        rows.append([{"text": f"⬆️ 返回上级目录 ({parent})", "callback_data": f"ub:{uk}:{pk}"}])
    else:
        def_dir = _archive_norm_dir(str(_ARCHIVE_CONFIG.get("defaultDir") or ""))
        if def_dir and def_dir != "/":
            dk = _cb_put(def_dir)
            rows.append([{"text": f"⚡ 上传到默认目录 ({def_dir})", "callback_data": f"uz:{uk}:{dk}"}])

    cur_row: List[Dict[str, str]] = []
    for d in dirs[:12]:
        d_name = str(d.get("name") or "")
        d_path = str(d.get("path") or "")
        if not d_name or not d_path:
            continue
        dk = _cb_put(d_path)
        cur_row.append({"text": f"📁 {d_name[:16]}", "callback_data": f"ub:{uk}:{dk}"})
        if len(cur_row) == 2:
            rows.append(cur_row)
            cur_row = []
    if cur_row:
        rows.append(cur_row)

    rows.append([{"text": "🔙 返回下载列表 (/ck)", "callback_data": "act:ck"}])
    return "\n".join(lines), {"inline_keyboard": rows}


async def build_job_dir_picker(job_id: str, browse_path: str = "/") -> Tuple[str, Dict[str, Any]]:
    """为失败的归档任务构建交互式换目录重试选择器。"""
    job = _ARCHIVE_JOBS.get(str(job_id or ""))
    if job is None:
        return "⚠️ 该归档任务不存在或已被清理。", {"inline_keyboard": [[{"text": "📊 查看上传队列", "callback_data": "act:yd"}]]}

    fn = str(job.get("filename") or "未知文件")
    norm_browse = _archive_norm_dir(browse_path) or "/"
    from services.openlist_service import openlist_dirs
    res = await openlist_dirs(norm_browse)
    dirs = (res.get("dirs") or []) if isinstance(res, dict) and res.get("ok") else []

    lines = [
        "📂 <b>【更换网盘目录并重试】</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"📦 <b>文件：</b><code>{_esc(fn[:42])}</code>",
        f"❌ <b>原失败路径：</b><code>{_esc(job.get('remote_path') or '—')}</code>",
        f"📍 <b>当前浏览位置：</b><code>{_esc(norm_browse)}</code>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    rows: List[List[Dict[str, str]]] = []
    if norm_browse != "/":
        bk = _cb_put(norm_browse)
        rows.append([{"text": f"✅ 改到此目录并立即重试 ({norm_browse})", "callback_data": f"jz:{job_id}:{bk}"}])
        parent = posixpath.dirname(norm_browse.rstrip("/")) or "/"
        pk = _cb_put(parent)
        rows.append([{"text": f"⬆️ 返回上级目录 ({parent})", "callback_data": f"jb:{job_id}:{pk}"}])
    else:
        def_dir = _archive_norm_dir(str(_ARCHIVE_CONFIG.get("defaultDir") or ""))
        if def_dir and def_dir != "/":
            dk = _cb_put(def_dir)
            rows.append([{"text": f"⚡ 改到默认目录并重试 ({def_dir})", "callback_data": f"jz:{job_id}:{dk}"}])

    cur_row: List[Dict[str, str]] = []
    for d in dirs[:12]:
        d_name = str(d.get("name") or "")
        d_path = str(d.get("path") or "")
        if not d_name or not d_path:
            continue
        dk = _cb_put(d_path)
        cur_row.append({"text": f"📁 {d_name[:16]}", "callback_data": f"jb:{job_id}:{dk}"})
        if len(cur_row) == 2:
            rows.append(cur_row)
            cur_row = []
    if cur_row:
        rows.append(cur_row)

    rows.append([{"text": "🔙 返回上传队列 (/yd)", "callback_data": "act:yd"}])
    return "\n".join(lines), {"inline_keyboard": rows}


async def build_retry_reply(target: str = "", remote_dir_override: str = "") -> Tuple[str, Dict[str, Any]]:
    """/retry — 一键重试失败的网盘上传任务与 TG 下载任务。"""
    raw_target = (target or "").strip()
    is_all = raw_target.lower() in ("", "all", "*")

    from services.archive_service import (
        _classify_archive_error, _PERMANENT_ARCHIVE_ERRORS, _ARCHIVE_TASKS, _archive_worker
    )
    from services.openlist_service import _openlist_relogin

    retried_arch: List[Dict[str, Any]] = []
    blocked_arch: List[Tuple[Dict[str, Any], str]] = []
    has_token_expired = False

    norm_override = _archive_norm_dir(remote_dir_override) if remote_dir_override else None
    if norm_override == "/":
        norm_override = None

    async with _BATCH_RETRY_LOCK:
        failed_jobs = [j for j in _ARCHIVE_JOBS.values() if j.get("state") == "failed"]
        failed_jobs.sort(key=lambda j: float(j.get("updated_at") or 0.0), reverse=True)
        path_owners = _archive_path_owners()

        for j in failed_jobs:
            jid = str(j.get("id") or "")
            uid = str(j.get("unique_id") or "")
            fn = str(j.get("filename") or "")
            if not is_all:
                if raw_target not in (jid, uid) and raw_target.lower() not in fn.lower():
                    continue

            raw_err = str(j.get("error") or "")
            cat_code, cat_label, _ = _classify_archive_error(raw_err)

            lp = _resolve_host_local_path(str(j.get("local_path") or ""))
            local_ok = bool(lp and os.path.exists(lp))
            if not local_ok:
                blocked_arch.append((j, "🗑️ 本地源文件已不存在，无法重传"))
                continue

            if cat_code in _PERMANENT_ARCHIVE_ERRORS and not norm_override and cat_code == "source_missing":
                blocked_arch.append((j, cat_label))
                continue

            if int(j.get("retry_count") or 0) >= _MAX_JOB_RETRIES:
                blocked_arch.append((j, f"已达最大重试上限 ({_MAX_JOB_RETRIES})"))
                continue

            if norm_override:
                raw_fn = str(j.get("raw_filename") or j.get("filename") or "未命名")
                clean_fn = _clean_archive_filename(raw_fn, enabled=bool(_ARCHIVE_CONFIG.get("cleanFilename", True)))
                clean_fn = _archive_unique_name(
                    norm_override, clean_fn,
                    owner_of=path_owners, uid=uid,
                    suffix=_archive_name_suffix(raw_fn, clean_fn),
                )
                j["remote_dir"] = norm_override
                j["filename"] = clean_fn
                j["remote_path"] = _archive_join(norm_override, clean_fn)
                path_owners.setdefault(j["remote_path"].lower(), set()).add(uid)

            if cat_code == "token_expired":
                has_token_expired = True

            j["state"] = "queued"
            j["progress"] = 0
            j["error"] = ""
            j["updated_at"] = time.time()
            j["retry_count"] = int(j.get("retry_count") or 0) + 1
            if jid not in _ARCHIVE_TASKS or _ARCHIVE_TASKS[jid].done():
                _ARCHIVE_TASKS[jid] = asyncio.create_task(_archive_worker(j))
            retried_arch.append(j)

        if has_token_expired and retried_arch:
            try:
                await _openlist_relogin()
            except Exception as e:  # noqa: BLE001
                log.warning("Bot 重试前刷新 OpenList 令牌失败: %s", e)

        if retried_arch:
            _archive_save()

    retried_dl: List[Dict[str, Any]] = []
    tasks = await _tasks_all_safe(force=True)
    failed_dl = [t for t in tasks if t.get("status") == "failed"]
    for t in failed_dl:
        uid = str(t.get("_unique_id") or "")
        tid_str = str(t.get("id") or "")
        fn = str(t.get("filename") or "")
        if not is_all:
            if raw_target not in (uid, tid_str) and raw_target.lower() not in fn.lower():
                continue
        tg_id = t.get("_telegram_id")
        chat_id = t.get("_chat_id")
        file_id = t.get("_file_id")
        msg_id = t.get("msg_id")
        if tg_id is not None and chat_id is not None and file_id is not None and msg_id is not None:
            try:
                await BACKEND.start_download(tg_id, {
                    "chatId": int(chat_id),
                    "messageId": int(msg_id),
                    "fileId": int(file_id),
                })
                retried_dl.append(t)
            except Exception as e:  # noqa: BLE001
                log.warning("Bot 重试下载任务失败 (%s): %s", fn, e)

    if retried_dl:
        _tasks_cache_invalidate()

    if not retried_arch and not retried_dl and not blocked_arch:
        return (
            "✅ <b>【任务重试】</b>\n当前没有需要重试的失败上传或下载任务。",
            {"inline_keyboard": [[
                {"text": "📊 查看上传队列", "callback_data": "act:yd"},
                {"text": "📥 查看下载进度", "callback_data": "act:ck"},
            ]]}
        )

    lines = ["🔄 <b>【任务重试结果】</b>", "━━━━━━━━━━━━━━━━━━"]
    if retried_arch:
        lines.append(f"☁️ <b>已重新调度网盘上传：{len(retried_arch)} 个</b>")
        for j in retried_arch[:5]:
            fn = _esc(str(j.get("filename") or "未知"))[:34]
            rd = _esc(str(j.get("remote_dir") or "—"))[:36]
            lines.append(f"  · {fn} → <code>{rd}</code>")
    if retried_dl:
        lines.append(f"📥 <b>已重新加入下载队列：{len(retried_dl)} 个</b>")
        for t in retried_dl[:5]:
            fn = _esc(str(t.get("filename") or "未知"))[:34]
            lines.append(f"  · {fn}")
    if blocked_arch:
        lines.append(f"\n⚠️ <b>无法重试的记录：{len(blocked_arch)} 个</b>")
        for j, reason in blocked_arch[:4]:
            fn = _esc(str(j.get("filename") or "未知"))[:30]
            lines.append(f"  · {fn}：{reason}")

    lines.append("━━━━━━━━━━━━━━━━━━")
    rows: List[List[Dict[str, str]]] = []
    if blocked_arch:
        rows.append([{"text": f"🗑️ 清理无法重试的失败记录 ({len(blocked_arch)})", "callback_data": "act:dismiss_all"}])
    rows.append([
        {"text": "📊 查看上传队列 (/yd)", "callback_data": "act:yd"},
        {"text": "📥 查看下载进度 (/ck)", "callback_data": "act:ck"},
    ])
    return "\n".join(lines), {"inline_keyboard": rows}


def do_dismiss_failed_jobs(target_id: str = "all") -> Tuple[str, Dict[str, Any]]:
    """清理失败的归档记录（只删失败记录本身，不碰本地/云端文件）。"""
    raw = (target_id or "all").strip()
    is_all = raw.lower() in ("", "all", "*")
    deleted = 0
    for jid, j in list(_ARCHIVE_JOBS.items()):
        if j.get("state") != "failed":
            continue
        if is_all or jid == raw:
            _ARCHIVE_JOBS.pop(jid, None)
            deleted += 1
    if deleted:
        _archive_save()
        if is_all:
            _RECENT_ARCHIVE_ERRORS.clear()
        LOG_STORE.append("INFO", f"已通过 Bot 清理 {deleted} 条失败归档记录")
    msg = f"🗑️ 已清理 <b>{deleted}</b> 条失败归档记录。" if deleted else "📭 当前没有可清理的失败归档记录。"
    kb = {
        "inline_keyboard": [[
            {"text": "📊 查看上传队列 (/yd)", "callback_data": "act:yd"},
            {"text": "📈 系统总览 (/st)", "callback_data": "act:st"},
        ]]
    }
    return msg, kb


def do_cancel_archive_job(job_id: str) -> Tuple[str, Dict[str, Any]]:
    """取消正在排队/上传的归档任务。"""
    from services.archive_service import _ARCHIVE_TASKS
    jid = str(job_id or "").strip()
    job = _ARCHIVE_JOBS.get(jid)
    if job is None:
        return "⚠️ 该上传任务不存在或已结束。", {"inline_keyboard": [[{"text": "📊 返回上传队列", "callback_data": "act:yd"}]]}
    fn = _esc(str(job.get("filename") or jid))
    if job.get("state") in ("queued", "uploading", "verifying"):
        t = _ARCHIVE_TASKS.get(jid)
        if t is not None and not t.done():
            t.cancel()
        job["state"] = "cancelled"
        job["error"] = "用户通过 Bot 取消"
        job["updated_at"] = time.time()
        _archive_save()
        return f"⏹ 已取消上传任务：<code>{fn}</code>", {"inline_keyboard": [[{"text": "📊 返回上传队列 (/yd)", "callback_data": "act:yd"}]]}
    return f"ℹ️ 任务 <code>{fn}</code> 当前状态为 {job.get('state')}，无需取消。", {"inline_keyboard": [[{"text": "📊 返回上传队列 (/yd)", "callback_data": "act:yd"}]]}


async def build_dir_reply(arg: str = "", browse_path: str = "/") -> Tuple[str, Dict[str, Any]]:
    """/dir — 查看或设定上传网盘路径（支持直接指定路径，或点选 OpenList 真实目录树）。"""
    raw_arg = (arg or "").strip()

    if raw_arg:
        sync_all = False
        if raw_arg.lower().startswith("all "):
            sync_all = True
            raw_path = raw_arg[4:].strip()
        elif raw_arg.lower() == "sync":
            sync_all = True
            raw_path = str(_ARCHIVE_CONFIG.get("defaultDir") or "").strip()
        else:
            raw_path = raw_arg

        norm_dir = _archive_norm_dir(raw_path)
        if not norm_dir or norm_dir == "/":
            return (
                "❌ <b>网盘目录格式无效</b>\n"
                "路径需以 <code>/</code> 开头且指向具体网盘目录（不能为根目录 <code>/</code>），例如：\n"
                "<code>/dir /onedrive/yello</code>",
                {"inline_keyboard": [[{"text": "📂 点选网盘目录", "callback_data": "act:dir"}]]}
            )

        _ARCHIVE_CONFIG["defaultDir"] = norm_dir
        _ARCHIVE_CONFIG["autoArchive"] = True
        _archive_config_save()

        synced_rules = 0
        if sync_all and _SUB_RULES:
            for r in _SUB_RULES.values():
                r["dirTemplate"] = norm_dir
                synced_rules += 1
            _subs_save()

        log.info("已通过 Bot 更新上传网盘路径: defaultDir=%s, sync_rules=%d", norm_dir, synced_rules)

        diff_rules = [
            r for r in _SUB_RULES.values()
            if r.get("enabled") and str(r.get("dirTemplate") or "") != norm_dir
        ]

        lines = [
            "✅ <b>【上传网盘路径已更新】</b>",
            "━━━━━━━━━━━━━━━━━━",
            f"📁 <b>默认上传路径：</b><code>{_esc(norm_dir)}</code>",
            f"⚙️ <b>自动归档：</b>已开启 ✅ · <b>归档后删本地：</b>{'开启' if _ARCHIVE_CONFIG.get('deleteLocal') else '关闭'}",
        ]
        if synced_rules:
            lines.append(f"📌 <b>已同步更新 {synced_rules} 条订阅规则（含收藏夹）路径为：</b><code>{_esc(norm_dir)}</code>")
        elif diff_rules:
            r_names = "、".join(_esc(str(r.get("chatTitle") or "会话")) for r in diff_rules[:3])
            lines.append(
                f"\n💡 <i>提示：会话规则「{r_names}」当前使用独立路径模板 "
                f"<code>{_esc(diff_rules[0].get('dirTemplate'))}</code>（优先级高于默认路径）。"
                f"如需统一归档到新路径，请点击下方「同步应用到收藏/订阅规则」。</i>"
            )
        lines.append("━━━━━━━━━━━━━━━━━━")

        dk = _cb_put(norm_dir)
        rows: List[List[Dict[str, str]]] = []
        if diff_rules:
            rows.append([{"text": f"📌 同步将「收藏」等规则也设为 {norm_dir}", "callback_data": f"sr:{dk}"}])
        rows.append([
            {"text": "☁️ 立即上传待归档文件", "callback_data": "act:up_all"},
            {"text": "📂 继续浏览目录", "callback_data": f"cd:{dk}"},
        ])
        return "\n".join(lines), {"inline_keyboard": rows}

    norm_browse = _archive_norm_dir(browse_path) or "/"
    from services.openlist_service import openlist_dirs
    res = await openlist_dirs(norm_browse)
    dirs = (res.get("dirs") or []) if isinstance(res, dict) and res.get("ok") else []

    def_dir = str(_ARCHIVE_CONFIG.get("defaultDir") or "未设置")
    auto_on = bool(_ARCHIVE_CONFIG.get("autoArchive"))
    del_on = bool(_ARCHIVE_CONFIG.get("deleteLocal"))

    lines = [
        "📂 <b>【网盘上传路径设定】</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"📁 <b>默认上传路径：</b><code>{_esc(def_dir)}</code>",
        f"⚙️ <b>自动归档：</b>{'开启 ✅' if auto_on else '关闭 ⏸️'} · <b>归档后删本地：</b>{'开启 ✅' if del_on else '关闭 ⏸️'}",
    ]
    enabled_rules = [r for r in _SUB_RULES.values() if r.get("enabled")]
    if enabled_rules:
        lines.append("📌 <b>会话专属规则路径（优先于默认路径）：</b>")
        for r in enabled_rules[:4]:
            ct = _esc(str(r.get("chatTitle") or "会话"))[:20]
            tpl = _esc(str(r.get("dirTemplate") or "—"))[:42]
            lines.append(f"  · {ct} → <code>{tpl}</code>")

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"📍 <b>当前浏览网盘目录：</b><code>{_esc(norm_browse)}</code>")
    if isinstance(res, dict) and not res.get("ok"):
        lines.append(f"⚠️ <i>读取 OpenList 目录提示：{_esc(res.get('message') or '请检查 OpenList 登录状态')}</i>")
    elif not dirs:
        lines.append("<i>（此目录下无子目录，可直接设为上传路径或返回上一级）</i>")
    else:
        lines.append("<i>👇 点击文件夹进入子目录，或点击设为上传路径：</i>")
    lines.append("<i>💡 也可直接发送命令：<code>/dir /网盘名/目录名</code></i>")

    rows: List[List[Dict[str, str]]] = []
    if norm_browse != "/":
        bk = _cb_put(norm_browse)
        rows.append([{"text": f"✅ 设为默认路径: {norm_browse}", "callback_data": f"sd:{bk}"}])
        if _SUB_RULES:
            rows.append([{"text": "📌 设为默认并同步到收藏/订阅规则", "callback_data": f"sr:{bk}"}])
        parent = posixpath.dirname(norm_browse.rstrip("/")) or "/"
        pk = _cb_put(parent)
        rows.append([{"text": f"⬆️ 返回上级目录 ({parent})", "callback_data": f"cd:{pk}"}])

    cur_row: List[Dict[str, str]] = []
    for d in dirs[:12]:
        d_name = str(d.get("name") or "")
        d_path = str(d.get("path") or "")
        if not d_name or not d_path:
            continue
        dk = _cb_put(d_path)
        cur_row.append({"text": f"📁 {d_name[:16]}", "callback_data": f"cd:{dk}"})
        if len(cur_row) == 2:
            rows.append(cur_row)
            cur_row = []
    if cur_row:
        rows.append(cur_row)

    rows.append([
        {"text": f"🔄 自动归档: {'开 ✅' if auto_on else '关 ⏸️'}", "callback_data": "act:toggle_auto"},
        {"text": f"🗑️ 删本地: {'开 ✅' if del_on else '关 ⏸️'}", "callback_data": "act:toggle_del"},
    ])
    if enabled_rules and def_dir and def_dir != "未设置" and any(str(r.get("dirTemplate") or "") != def_dir for r in enabled_rules):
        rows.append([{"text": f"📌 将收藏/订阅规则统一同步为 {def_dir}", "callback_data": "act:sync_rules"}])

    rows.append([
        {"text": "☁️ 立即上传待归档", "callback_data": "act:up_all"},
        {"text": "📈 返回系统总览", "callback_data": "act:st"},
    ])
    return "\n".join(lines), {"inline_keyboard": rows}


async def build_dl_reply(arg: str) -> Tuple[str, Dict[str, Any]]:
    """/dl — 解析 t.me 链接并提交下载。"""
    from services.task_service import _parse_link, _resolve_links_to_files
    from services.watermark_service import _disk_guard_or_enqueue_links

    raw = (arg or "").strip()
    tokens = re.split(r"[\s,;\n]+", raw) if raw else []
    links = [tok for tok in tokens if tok and _parse_link(tok)]

    if not links:
        return (
            "📥 <b>【链接直投下载】</b>\n"
            "请发送 <code>/dl https://t.me/...</code>，或直接把 Telegram 消息链接发给本机器人即可加入下载队列。",
            {"inline_keyboard": [[{"text": "📥 查看下载进度 (/ck)", "callback_data": "act:ck"}]]}
        )

    guard = await _disk_guard_or_enqueue_links(links, "bot")
    if guard is not None:
        return (
            f"⏸️ <b>【磁盘高水位保护】</b>\n{_esc(guard.get('message') or '任务已转入挂起队列')}",
            {"inline_keyboard": [[
                {"text": "🧹 清理并唤醒任务", "callback_data": "act:disk_wake"},
                {"text": "📈 系统总览", "callback_data": "act:st"},
            ]]}
        )

    ok_count, err = await _resolve_links_to_files(links)
    if ok_count:
        _tasks_cache_invalidate()
        return (
            f"📥 <b>【已提交下载队列】</b>\n"
            f"✅ 成功解析并加入下载：<b>{ok_count}</b> 个媒体文件\n"
            f"📂 自动归档状态：<b>{'开启（下载完自动上传网盘）' if _ARCHIVE_CONFIG.get('autoArchive') else '关闭（下载后可点按钮上传）'}</b>",
            {"inline_keyboard": [[
                {"text": "📥 查看下载进度 (/ck)", "callback_data": "act:ck"},
                {"text": "📂 设定网盘路径 (/dir)", "callback_data": "act:dir"},
            ]]}
        )
    return (
        f"❌ <b>链接提交下载失败</b>\n原因：{_esc(err or '未找到可下载的媒体文件')}",
        {"inline_keyboard": [[{"text": "📥 查看下载进度 (/ck)", "callback_data": "act:ck"}]]}
    )


async def do_disk_wake_action() -> Tuple[str, Dict[str, Any]]:
    """手动触发磁盘清理并唤醒 waiting_disk 挂起任务。"""
    from services.watermark_service import (
        _disk_guard_check, _check_and_wake_waiting_disk_tasks,
        _get_disk_usage_percent, _get_disk_free_gb,
    )
    cleaned = await _disk_guard_check()
    woken = await _check_and_wake_waiting_disk_tasks()
    pct = _get_disk_usage_percent()
    free_gb = _get_disk_free_gb()
    text = (
        "🧹 <b>【磁盘清理与任务唤醒】</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🗑️ 已清理已归档本地副本：<b>{cleaned}</b> 个\n"
        f"🚀 已唤醒挂起下载任务：<b>{woken}</b> 个\n"
        f"💽 当前磁盘占用：<b>{pct:.1f}%</b>（可用 <b>{free_gb:.1f} GB</b>）"
    )
    return text, {"inline_keyboard": [[
        {"text": "📥 查看下载进度 (/ck)", "callback_data": "act:ck"},
        {"text": "📈 系统总览 (/st)", "callback_data": "act:st"},
    ]]}


# ---------------------------------------------------------------------
# Bot API 通信层（setMyCommands / sendMessage / editMessageText / answerCallbackQuery）
# ---------------------------------------------------------------------
def _bot_base(bot_token: str) -> str:
    return f"https://api.telegram.org/bot{bot_token}"


def _clean_token_text(s: Any) -> str:
    return re.sub(r"bot[0-9]+:[A-Za-z0-9_-]+", "bot<REDACTED>", str(s))


async def register_bot_commands(client: httpx.AsyncClient, bot_token: str, chat_id: str = "") -> bool:
    """向 Telegram Bot API 注册 `/` 命令菜单与左下角菜单按钮（使其像 Hermes Bot 一样输入 `/` 即弹出）。"""
    bot_token = str(bot_token or "").strip()
    if not bot_token:
        return False
    base = _bot_base(bot_token)
    ok_default = False
    scopes: List[Optional[Dict[str, Any]]] = [
        None,
        {"type": "all_private_chats"},
        {"type": "all_group_chats"},
    ]
    cid_str = str(chat_id or "").strip()
    if cid_str:
        cid_val: Any = int(cid_str) if cid_str.lstrip("-").isdigit() else cid_str
        scopes.append({"type": "chat", "chat_id": cid_val})

    for sc in scopes:
        payload: Dict[str, Any] = {"commands": BOT_COMMANDS}
        if sc is not None:
            payload["scope"] = sc
        try:
            resp = await client.post(
                f"{base}/setMyCommands",
                json=payload,
                timeout=httpx.Timeout(10.0, connect=5.0),
            )
            if sc is None and resp.status_code == 200:
                data = resp.json()
                ok_default = bool(isinstance(data, dict) and data.get("ok"))
        except Exception as e:  # noqa: BLE001
            log.debug("Bot setMyCommands (scope=%s) 异常: %s", sc, _clean_token_text(e))

    try:
        await client.post(
            f"{base}/setChatMenuButton",
            json={"menu_button": {"type": "commands"}},
            timeout=httpx.Timeout(8.0, connect=5.0),
        )
    except Exception as e:  # noqa: BLE001
        log.debug("Bot setChatMenuButton 异常: %s", _clean_token_text(e))

    if ok_default:
        log.info("TG Bot 命令菜单已同步注册（共 %d 条命令，输入 / 即可弹出）", len(BOT_COMMANDS))
    return ok_default


async def ensure_bot_commands_registered(bot_token: str = "", chat_id: str = "") -> bool:
    """独立客户端注册命令菜单（供设置页保存/测试或启动时异步触发）。"""
    tok = str(bot_token or _NOTIFY_CONFIG.get("botToken") or "").strip()
    cid = str(chat_id or _NOTIFY_CONFIG.get("chatId") or "").strip()
    if not tok:
        return False
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            return await register_bot_commands(client, tok, cid)
    except Exception as e:  # noqa: BLE001
        log.debug("ensure_bot_commands_registered 异常: %s", _clean_token_text(e))
        return False


async def _fetch_updates(client: httpx.AsyncClient, bot_token: str, offset: int) -> List[Dict[str, Any]]:
    """getUpdates 长轮询（同时接收文本命令 message 与内联按钮点击 callback_query）。"""
    try:
        resp = await client.post(
            f"{_bot_base(bot_token)}/getUpdates",
            json={"offset": offset, "timeout": int(_POLL_TIMEOUT),
                  "allowed_updates": ["message", "callback_query"]},
            timeout=httpx.Timeout(_POLL_TIMEOUT + 10.0, connect=8.0),
        )
        if resp.status_code != 200:
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


async def _send_reply(client: httpx.AsyncClient, bot_token: str, chat_id: str,
                      html_text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        await client.post(
            f"{_bot_base(bot_token)}/sendMessage",
            json=payload,
            timeout=httpx.Timeout(12.0, connect=6.0),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Bot 命令回复发送失败: %s", _clean_token_text(e))


async def _dispatch_reply(client: Any, bot_token: str, chat_id: str,
                          html_text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
    """兼容单测 4 参数桩函数 fake_reply(client, token, cid, text) 的安全派发器。"""
    if reply_markup:
        try:
            await _send_reply(client, bot_token, chat_id, html_text, reply_markup=reply_markup)
            return
        except TypeError:
            pass
    await _send_reply(client, bot_token, chat_id, html_text)


async def _edit_or_send(client: Any, bot_token: str, chat_id: str,
                        message_id: Any, html_text: str,
                        reply_markup: Optional[Dict[str, Any]] = None) -> None:
    """内联按钮点击后优先原地刷新消息（无感切换）；若原消息不可编辑则回退新发一条。"""
    if message_id and hasattr(client, "post"):
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            resp = await client.post(
                f"{_bot_base(bot_token)}/editMessageText",
                json=payload,
                timeout=httpx.Timeout(10.0, connect=5.0),
            )
            if resp.status_code == 200:
                return
            try:
                desc = str((resp.json() or {}).get("description") or "")
                if "message is not modified" in desc.lower():
                    return
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            log.debug("Bot editMessageText 回退 sendMessage: %s", _clean_token_text(e))
    await _dispatch_reply(client, bot_token, chat_id, html_text, reply_markup=reply_markup)


async def _answer_callback(client: Any, bot_token: str, callback_query_id: str,
                           text: str = "", show_alert: bool = False) -> None:
    """应答 callback_query，立即消除按钮转圈并弹出顶部轻提示（toast）。"""
    if not callback_query_id or not hasattr(client, "post"):
        return
    payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text[:180]
    if show_alert:
        payload["show_alert"] = True
    try:
        await client.post(
            f"{_bot_base(bot_token)}/answerCallbackQuery",
            json=payload,
            timeout=httpx.Timeout(8.0, connect=4.0),
        )
    except Exception as e:  # noqa: BLE001
        log.debug("Bot answerCallbackQuery 异常: %s", _clean_token_text(e))


async def _handle_callback_query(client: Any, bot_token: str, allowed_chat: str,
                                 cb: Dict[str, Any]) -> None:
    """处理内联按钮点击（callback_query）。"""
    cb_id = str(cb.get("id") or "")
    msg = cb.get("message") if isinstance(cb.get("message"), dict) else {}
    chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
    chat_id = str(chat.get("id") or (cb.get("from") or {}).get("id") or "")
    message_id = msg.get("message_id")
    data = str(cb.get("data") or "").strip()

    if allowed_chat and chat_id != allowed_chat:
        return
    if not chat_id or not data:
        await _answer_callback(client, bot_token, cb_id)
        return

    toast = ""
    reply_text = ""
    reply_kb: Optional[Dict[str, Any]] = None
    in_place = True

    if data == "act:st":
        toast = "已刷新系统总览"
        reply_text, reply_kb = await build_st_panel()
    elif data == "act:cloud_tasks":
        toast = "已刷新跨盘传输任务"
        reply_text, reply_kb = await build_cloud_tasks_panel()
    elif data == "act:ck":
        toast = "已刷新下载任务进度"
        reply_text, reply_kb = await build_ck_panel()
    elif data == "act:yd":
        toast = "已刷新云端上传队列"
        reply_text, reply_kb = await build_yd_panel()
    elif data == "act:err":
        toast = "已加载失败详情"
        reply_text, reply_kb = build_err_panel()
    elif data == "act:help":
        reply_text, reply_kb = build_help_panel()
    elif data == "act:dir":
        reply_text, reply_kb = await build_dir_reply("", "/")
    elif data == "act:up_all":
        toast = "☁️ 正在扫描并入队上传…"
        in_place = False
        reply_text, reply_kb = await build_up_reply("all")
    elif data == "act:retry_all":
        toast = "🔄 正在重试失败任务…"
        in_place = False
        reply_text, reply_kb = await build_retry_reply("all")
    elif data == "act:dismiss_all":
        toast = "🗑️ 已清理失败记录"
        reply_text, reply_kb = do_dismiss_failed_jobs("all")
    elif data == "act:disk_wake":
        toast = "🧹 正在执行磁盘清理与唤醒…"
        in_place = False
        reply_text, reply_kb = await do_disk_wake_action()
    elif data == "act:toggle_auto":
        new_val = not bool(_ARCHIVE_CONFIG.get("autoArchive"))
        _ARCHIVE_CONFIG["autoArchive"] = new_val
        _archive_config_save()
        toast = f"自动归档已{'开启 ✅' if new_val else '关闭 ⏸️'}"
        reply_text, reply_kb = await build_dir_reply("", "/")
    elif data == "act:toggle_del":
        new_val = not bool(_ARCHIVE_CONFIG.get("deleteLocal"))
        _ARCHIVE_CONFIG["deleteLocal"] = new_val
        _archive_config_save()
        toast = f"归档后删本地已{'开启 ✅' if new_val else '关闭 ⏸️'}"
        reply_text, reply_kb = await build_dir_reply("", "/")
    elif data == "act:sync_rules":
        def_dir = _archive_norm_dir(str(_ARCHIVE_CONFIG.get("defaultDir") or ""))
        if def_dir and def_dir != "/":
            toast = f"📌 已同步规则路径为 {def_dir}"
            reply_text, reply_kb = await build_dir_reply(f"all {def_dir}")
        else:
            toast = "请先设定默认上传路径"
            reply_text, reply_kb = await build_dir_reply("", "/")
    elif data.startswith("cd:"):
        target_dir = _cb_resolve_dir(data[3:])
        reply_text, reply_kb = await build_dir_reply("", target_dir)
    elif data.startswith("sd:"):
        target_dir = _cb_resolve_dir(data[3:])
        toast = f"✅ 默认上传路径已设为 {target_dir}"
        reply_text, reply_kb = await build_dir_reply(target_dir)
    elif data.startswith("sr:"):
        target_dir = _cb_resolve_dir(data[3:])
        toast = f"📌 默认与规则路径已统一设为 {target_dir}"
        reply_text, reply_kb = await build_dir_reply(f"all {target_dir}")
    elif data.startswith("u:"):
        uid_key = data[2:]
        tasks = await _tasks_all_safe()
        uid = _cb_resolve_uid(uid_key, tasks)
        toast = "☁️ 已提交上传队列"
        in_place = False
        reply_text, reply_kb = await build_up_reply(uid)
    elif data.startswith("ud:"):
        uid_key = data[3:]
        reply_text, reply_kb = await build_file_dir_picker(uid_key, "/")
    elif data.startswith("ub:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            uid_key, dir_key = parts[1], parts[2]
            target_dir = _cb_resolve_dir(dir_key)
            reply_text, reply_kb = await build_file_dir_picker(uid_key, target_dir)
    elif data.startswith("uz:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            uid_key, dir_key = parts[1], parts[2]
            tasks = await _tasks_all_safe()
            uid = _cb_resolve_uid(uid_key, tasks)
            target_dir = _cb_resolve_dir(dir_key)
            toast = f"☁️ 已入队上传至 {target_dir}"
            reply_text, reply_kb = await build_up_reply(uid, remote_dir_override=target_dir)
    elif data.startswith("rj:"):
        jid = data[3:]
        toast = "🔄 已重新加入上传队列"
        in_place = False
        reply_text, reply_kb = await build_retry_reply(jid)
    elif data.startswith("jd:"):
        jid = data[3:]
        reply_text, reply_kb = await build_job_dir_picker(jid, "/")
    elif data.startswith("jb:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            jid, dir_key = parts[1], parts[2]
            target_dir = _cb_resolve_dir(dir_key)
            reply_text, reply_kb = await build_job_dir_picker(jid, target_dir)
    elif data.startswith("jz:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            jid, dir_key = parts[1], parts[2]
            target_dir = _cb_resolve_dir(dir_key)
            toast = f"🔄 已更换目录至 {target_dir} 并重试"
            reply_text, reply_kb = await build_retry_reply(jid, remote_dir_override=target_dir)
    elif data.startswith("dj:"):
        jid = data[3:]
        toast = "🗑️ 已删除该失败记录"
        reply_text, reply_kb = do_dismiss_failed_jobs(jid)
    elif data.startswith("cj:"):
        jid = data[3:]
        toast = "⏹ 已取消上传任务"
        reply_text, reply_kb = do_cancel_archive_job(jid)
    elif data.startswith("rd:"):
        uid_key = data[3:]
        tasks = await _tasks_all_safe()
        uid = _cb_resolve_uid(uid_key, tasks)
        toast = "🔄 已重新加入下载队列"
        in_place = False
        reply_text, reply_kb = await build_retry_reply(uid)
    else:
        toast = "未知操作"

    await _answer_callback(client, bot_token, cb_id, text=toast)
    if reply_text:
        if in_place and message_id:
            await _edit_or_send(client, bot_token, chat_id, message_id, reply_text, reply_markup=reply_kb)
        else:
            await _dispatch_reply(client, bot_token, chat_id, reply_text, reply_markup=reply_kb)


async def _handle_update(client: httpx.AsyncClient, bot_token: str,
                         allowed_chat: str, update: Dict[str, Any]) -> int:
    """处理单条 update（支持文本命令、直接发送 t.me 链接、以及内联按钮 callback_query）。"""
    uid = int(update.get("update_id") or 0)

    if isinstance(update.get("callback_query"), dict):
        await _handle_callback_query(client, bot_token, allowed_chat, update["callback_query"])
        return uid + 1

    msg = update.get("message") or {}
    if not isinstance(msg, dict):
        return uid + 1
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = str(msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return uid + 1

    if allowed_chat and chat_id != allowed_chat:
        return uid + 1

    if not text.startswith("/"):
        if re.search(r"https?://(?:t\.me|telegram\.me|telegram\.dog)/", text, re.I):
            reply, kb = await build_dl_reply(text)
            await _dispatch_reply(client, bot_token, chat_id, reply, reply_markup=kb)
        return uid + 1

    parts = text.split(None, 1)
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    reply_kb: Optional[Dict[str, Any]] = None
    if cmd == "/ck":
        reply, reply_kb = await build_ck_panel()
    elif cmd == "/yd":
        reply, reply_kb = await build_yd_panel()
    elif cmd in ("/st", "/status"):
        reply, reply_kb = await build_st_panel()
    elif cmd in ("/cloudtasks", "/pan", "/transfer", "/cptasks"):
        reply, reply_kb = await build_cloud_tasks_panel()
    elif cmd == "/err":
        reply, reply_kb = build_err_panel()
    elif cmd in ("/up", "/upload", "/archive"):
        reply, reply_kb = await build_up_reply(arg)
    elif cmd in ("/retry", "/rt"):
        reply, reply_kb = await build_retry_reply(arg)
    elif cmd in ("/dir", "/path"):
        reply, reply_kb = await build_dir_reply(arg)
    elif cmd in ("/dl", "/download"):
        reply, reply_kb = await build_dl_reply(arg)
    elif cmd in ("/help", "/start", "/menu"):
        reply, reply_kb = build_help_panel()
    else:
        reply = f"❓ 未识别命令 {_esc(cmd)}，发送 /help 查看可用命令。"
        reply_kb = build_help_panel()[1]

    await _dispatch_reply(client, bot_token, chat_id, reply, reply_markup=reply_kb)
    return uid + 1


async def bot_command_loop() -> None:
    """Bot 命令与交互长轮询主循环（启动于 bridge startup；自动注册 `/` 命令菜单，异常自愈不退出）。"""
    global _update_offset
    log.info("TG Bot 交互引擎启动（/up /retry /dir /dl /ck /yd /st /err /help）")
    registered_pair = ("", "")
    while True:
        bot_token = str(_NOTIFY_CONFIG.get("botToken") or "").strip()
        chat_id = str(_NOTIFY_CONFIG.get("chatId") or "").strip()
        if not bot_token or not chat_id:
            await asyncio.sleep(5.0)
            continue
        try:
            async with httpx.AsyncClient(follow_redirects=False) as client:
                if (bot_token, chat_id) != registered_pair:
                    if await register_bot_commands(client, bot_token, chat_id):
                        registered_pair = (bot_token, chat_id)
                while True:
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
