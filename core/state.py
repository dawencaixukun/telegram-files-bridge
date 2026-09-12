# -*- coding: utf-8 -*-
"""
core/state.py — 全局单例状态字典、线程/异步安全锁与磁盘持久化
=============================================================
本模块集中管理系统的全局运行时单例字典、队列状态与状态落盘逻辑。
"""
import os
import json
import time
import math
import asyncio
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from core.config import (
    APP_ROOT_DIR, OPENLIST_URL, _WAITING_DISK_FILE, _WAITING_DISK_TASKS_MAX,
    _ARCHIVE_FILE, _ARCHIVE_CFG_FILE, _SUBS_FILE, _SUBS_RULES_MAX,
    _NOTIFY_FILE, _OPENLIST_FILE, _SESSION_BACKUP_STATUS_FILE,
    _FLOOD_WAIT_FILE, _mask_token, _same_file_name,
)
from core.logging import log

# ---------------------------------------------------------------------
# 1. 登录与风控状态
# ---------------------------------------------------------------------
_login_failures: Dict[str, List[float]] = {}

_FLOOD_WAIT_STATE: Dict[str, Any] = {
    "active": False,
    "account": "default",
    "wait_seconds": 0,
    "triggered_at": 0.0,
    "cooldown_until": 0.0,
    "reason": "",
    "suspended_tasks": {},
}
_FLOOD_WAIT_TIMER_TASK: Optional[asyncio.Task] = None

# ---------------------------------------------------------------------
# 2. 缓存与队列
# ---------------------------------------------------------------------
_TASKS_CACHE: Dict[str, Any] = {"expire": 0.0, "value": None}
_BROWSE_SEEN_CACHE: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

_WAITING_DISK_TASKS: Dict[str, Dict[str, Any]] = {}

# ---------------------------------------------------------------------
# 3. 归档与取回状态
# ---------------------------------------------------------------------
_ARCHIVE_JOBS: Dict[str, Dict[str, Any]] = {}
_ARCHIVE_MAX_JOBS = 5000
_DELETED_LOCAL_UIDS: Set[str] = set()
_QUICK_ARCHIVE_REGISTRY: Dict[str, Dict[str, Any]] = {}

_ARCHIVE_CONFIG_FILE = os.path.join(APP_ROOT_DIR, ".archive_config.json")
_ARCHIVE_CONFIG: Dict[str, Any] = {
    "autoArchive": True,
    "defaultDir": "",
    # 会话冷备在 OpenList 上的目标目录（空 = 自动推导；见 backup_service 解析优先级）
    "sessionBackupDir": "",
    # 本地会话冷备目录（空 = 自动推导：与 app 平级的 session-backups，回退到数据目录内）。
    # 用户可指定任意绝对路径，把本地快照落到独立磁盘/挂载卷上。
    "localBackupDir": "",
    "publicBaseUrl": "",
    "policy": "overwrite",
    "deleteLocal": False,
    "cleanFilename": True,
    "diskWatermarkGB": 5.0,
    "diskAutoClean": True,
    "diskHighWatermarkPercent": 85.0,
    "diskLowWatermarkPercent": 75.0,
    "stats": {
        "enqueued": 0,
        "done": 0,
        "failed": 0,
        "lastHitAt": 0.0,
    }
}

_RETRIEVE_JOBS: Dict[str, Dict[str, Any]] = {}
_RETRIEVE_FILE = os.path.join(APP_ROOT_DIR, ".retrieve_jobs.json")
_RETRIEVE_JOBS_MAX = 500

# ---------------------------------------------------------------------
# 4. 订阅规则
# ---------------------------------------------------------------------
_SUBS_MAX_RULES = 50
_SUB_RULES: Dict[str, Dict[str, Any]] = {}

# ---------------------------------------------------------------------
# 5. 通知配置
# ---------------------------------------------------------------------
_NOTIFY_CONFIG_FILE = os.path.join(APP_ROOT_DIR, ".notify_config.json")
_NOTIFY_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "channel": "both",  # "bot", "saved_messages", "both"
    "botToken": "",
    "chatId": "",
    "minFileSizeMB": 50,
    "events": {
        "downloadCompleted": True,
        "archiveSuccess": True,
        "archiveFailed": True,
        "diskWatermarkAlert": True,
    }
}
_NOTIFY_LAST_DISK_ALERT = 0.0

# ---------------------------------------------------------------------
# 6. OpenList 状态与客户端
# ---------------------------------------------------------------------
_openlist_client = httpx.AsyncClient(
    base_url=OPENLIST_URL,
    timeout=httpx.Timeout(10.0),
    follow_redirects=False,
)
_openlist_upload_client = httpx.AsyncClient(
    base_url=OPENLIST_URL,
    timeout=httpx.Timeout(connect=10.0, read=3600.0, write=3600.0, pool=10.0),
    follow_redirects=False,
)
_OPENLIST_FILE = os.path.join(APP_ROOT_DIR, ".openlist_auth")
_OPENLIST: Dict[str, Any] = {}

# ---------------------------------------------------------------------
# 7. 会话备份与告警状态
# ---------------------------------------------------------------------
_SESSION_BACKUP_STATUS: Dict[str, Any] = {}

_ALERT_EVENT_LIMIT = 8
_ALERTS_MAX = 50
_alert_state: Dict[str, Any] = {
    "events": [],
    "read_ts": 0.0,
    "seen": {},
    "baselined": False,
}

# ---------------------------------------------------------------------
# 8. 异步并发锁与信号量
# ---------------------------------------------------------------------
_archive_lock = asyncio.Lock()
_archive_sem = asyncio.Semaphore(2)
_retrieve_lock = asyncio.Lock()
_retrieve_sem = asyncio.Semaphore(2)
_subs_lock = asyncio.Lock()
_disk_lock = asyncio.Lock()
_openlist_lock = asyncio.Lock()

# ---------------------------------------------------------------------
# 9. 状态持久化与管理函数
# ---------------------------------------------------------------------
def _flood_wait_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps(_FLOOD_WAIT_STATE, ensure_ascii=False, indent=2).encode("utf-8")
        fd = os.open(_FLOOD_WAIT_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("保存 flood_wait 状态失败: %s", e)


def _flood_wait_load() -> None:
    try:
        if os.path.exists(_FLOOD_WAIT_FILE):
            with open(_FLOOD_WAIT_FILE, "r", encoding="utf-8") as f:
                d = json.loads(f.read())
            if isinstance(d, dict):
                _FLOOD_WAIT_STATE.clear()
                _FLOOD_WAIT_STATE.update(d)
                now = time.time()
                until = float(_FLOOD_WAIT_STATE.get("cooldown_until") or 0.0)
                if now >= until:
                    _FLOOD_WAIT_STATE["active"] = False
                    _FLOOD_WAIT_STATE["wait_seconds"] = 0
                else:
                    _FLOOD_WAIT_STATE["active"] = True
                    log.info("已恢复 Telegram FloodWait 冷却状态: 剩余 %d 秒", int(until - now))
    except Exception as e:  # noqa: BLE001
        log.warning("恢复 flood_wait 状态异常: %s", e)


def _trigger_flood_wait(account: str = "default", wait_seconds: int = 30, reason: str = "") -> float:
    """触发或延长 FloodWait 风控冷却。并发多次触发时取最大到期时间，防击穿。"""
    now = time.time()
    try:
        w_val = int(wait_seconds if wait_seconds is not None else 30)
    except (ValueError, TypeError):
        w_val = 30
    wait_seconds = max(1, min(w_val, 86400))
    current_until = float(_FLOOD_WAIT_STATE.get("cooldown_until") or 0.0)
    new_until = max(current_until, now + wait_seconds)
    actual_wait = int(new_until - now)

    _FLOOD_WAIT_STATE["active"] = True
    _FLOOD_WAIT_STATE["account"] = account or "default"
    _FLOOD_WAIT_STATE["wait_seconds"] = actual_wait
    _FLOOD_WAIT_STATE["triggered_at"] = now
    _FLOOD_WAIT_STATE["cooldown_until"] = new_until
    _FLOOD_WAIT_STATE["reason"] = reason or f"FLOOD_WAIT_{actual_wait}"
    _flood_wait_save()

    log.warning("Telegram 账号 [%s] 触发风控冷却: %s, 冷却至 %s（共 %d 秒）",
                account, _FLOOD_WAIT_STATE["reason"],
                time.strftime("%H:%M:%S", time.localtime(new_until)), actual_wait)

    _ensure_flood_wait_timer()

    global _TASKS_CACHE
    _TASKS_CACHE = {"expire": 0.0, "value": None}
    return new_until


def _is_flood_wait_active(account: str = "default") -> bool:
    if not _FLOOD_WAIT_STATE.get("active"):
        return False
    now = time.time()
    until = float(_FLOOD_WAIT_STATE.get("cooldown_until") or 0.0)
    if now >= until:
        _FLOOD_WAIT_STATE["active"] = False
        _FLOOD_WAIT_STATE["wait_seconds"] = 0
        return False
    return True


def _get_flood_wait_status(account: str = "default") -> Dict[str, Any]:
    active = _is_flood_wait_active(account)
    now = time.time()
    until = float(_FLOOD_WAIT_STATE.get("cooldown_until") or 0.0)
    rem = max(0, int(math.ceil(until - now))) if active else 0
    total_wait = int(_FLOOD_WAIT_STATE.get("wait_seconds") or 0)
    reason = str(_FLOOD_WAIT_STATE.get("reason") or "")
    suspended = _FLOOD_WAIT_STATE.get("suspended_tasks") or {}
    msg = (f"Telegram 账号触发风控保护 ({reason or 'FLOOD_WAIT'})，已安全挂起调度，预计剩余 {rem} 秒自动恢复"
           if active else "Telegram 账号状态正常，无限流冷却")
    return {
        "isCooling": active,
        "account": _FLOOD_WAIT_STATE.get("account") or account,
        "remainingSeconds": rem,
        "totalWaitSeconds": total_wait,
        "cooldownUntil": until,
        "reason": reason,
        "suspendedTasksCount": len(suspended),
        "message": msg,
    }


def _reset_flood_wait(account: str = "default") -> None:
    """强制重置 FloodWait 状态"""
    _FLOOD_WAIT_STATE["active"] = False
    _FLOOD_WAIT_STATE["wait_seconds"] = 0
    _FLOOD_WAIT_STATE["cooldown_until"] = 0.0
    _FLOOD_WAIT_STATE["reason"] = ""
    _FLOOD_WAIT_STATE["suspended_tasks"] = {}
    _flood_wait_save()
    global _TASKS_CACHE
    _TASKS_CACHE = {"expire": 0.0, "value": None}
    log.info("管理员已强制清除 Telegram FloodWait 冷却状态")


async def _wake_flood_wait_tasks() -> int:
    suspended = dict(_FLOOD_WAIT_STATE.get("suspended_tasks", {}))
    _FLOOD_WAIT_STATE["suspended_tasks"].clear()
    _flood_wait_save()
    global _TASKS_CACHE
    _TASKS_CACHE = {"expire": 0.0, "value": None}
    woken = len(suspended)
    if woken > 0:
        log.info("已自动唤醒 %d 条 FloodWait 挂起任务", woken)
    return woken


async def _flood_wait_timer_loop() -> None:
    global _FLOOD_WAIT_TIMER_TASK
    try:
        while _is_flood_wait_active():
            await asyncio.sleep(1.0)
        _FLOOD_WAIT_STATE["active"] = False
        _FLOOD_WAIT_STATE["wait_seconds"] = 0
        _flood_wait_save()
        await _wake_flood_wait_tasks()
        log.info("FloodWait 冷却结束，已自动恢复调度并续跑")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.warning("FloodWait 定时器循环异常: %s", e)
    finally:
        _FLOOD_WAIT_TIMER_TASK = None


def _ensure_flood_wait_timer() -> None:
    global _FLOOD_WAIT_TIMER_TASK
    if _FLOOD_WAIT_STATE.get("active") and (_FLOOD_WAIT_TIMER_TASK is None or _FLOOD_WAIT_TIMER_TASK.done()):
        try:
            loop = asyncio.get_running_loop()
            _FLOOD_WAIT_TIMER_TASK = loop.create_task(_flood_wait_timer_loop())
        except RuntimeError:
            pass


def _format_flood_wait_task(w: Dict[str, Any], rem_sec: int) -> Dict[str, Any]:
    now = float(w.get("created_at") or time.time())
    t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    reason = _FLOOD_WAIT_STATE.get("reason") or "FLOOD_WAIT"
    return {
        "id": w.get("id") or "flood-wait-task",
        "filename": w.get("filename", "未知文件"),
        "size": w.get("size_str", "未知大小"),
        "size_bytes": w.get("size", 0),
        "source": w.get("source", "风控冷却挂起队列"),
        "source_url": "",
        "msg_id": w.get("msg_id", "—"),
        "status": "waiting_disk",
        "progress": 0,
        "loaded": "0 B",
        "time": t_str,
        "local_path": "—（Telegram 风控保护中，自动倒计时续跑）",
        "error_msg": f"触发 Telegram 风控限流 ({reason})，安全冷却中（剩余 {rem_sec} 秒）",
        "stages": [
            {"name": "风控冷却", "done": "yes", "time": t_str, "dur": f"剩余 {rem_sec}s"},
            {"name": "下载媒体", "done": "no", "time": "—", "dur": "—"},
            {"name": "本地落盘", "done": "no", "time": "—", "dur": "—"},
        ],
        "_unique_id": w.get("uniqueId", ""),
        "_telegram_id": None,
        "_file_id": None,
    }


def _waiting_disk_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps({"tasks": _WAITING_DISK_TASKS}, ensure_ascii=False, indent=2).encode("utf-8")
        fd = os.open(_WAITING_DISK_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("保存 waiting_disk 挂起队列失败: %s", e)


def _waiting_disk_load() -> None:
    try:
        if os.path.exists(_WAITING_DISK_FILE):
            with open(_WAITING_DISK_FILE, "r", encoding="utf-8") as f:
                d = json.loads(f.read())
            if isinstance(d, dict) and isinstance(d.get("tasks"), dict):
                _WAITING_DISK_TASKS.clear()
                _WAITING_DISK_TASKS.update(d["tasks"])
                log.info("已恢复 waiting_disk 磁盘挂起队列: %d 条任务", len(_WAITING_DISK_TASKS))
    except Exception as e:  # noqa: BLE001
        log.warning("恢复 waiting_disk 队列异常: %s", e)


def _format_waiting_disk_task(w: Dict[str, Any]) -> Dict[str, Any]:
    now = float(w.get("created_at") or time.time())
    t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    return {
        "id": w["id"],
        "filename": w.get("filename", "未知文件"),
        "size": w.get("size_str", "未知大小"),
        "size_bytes": w.get("size", 0),
        "source": w.get("source", "磁盘挂起队列"),
        "source_url": "",
        "msg_id": w.get("msg_id", "—"),
        "status": "waiting_disk",
        "progress": 0,
        "loaded": "0 B",
        "time": t_str,
        "local_path": "—（磁盘高水位保护中，等待空间释放恢复）",
        "error_msg": w.get("error_msg", "本地磁盘占用率过高，已安全挂起排队"),
        "stages": [
            {"name": "等待磁盘空间", "done": "yes", "time": t_str, "dur": "挂起中"},
            {"name": "下载媒体", "done": "no", "time": "—", "dur": "—"},
            {"name": "本地落盘", "done": "no", "time": "—", "dur": "—"},
        ],
        "_unique_id": w.get("uniqueId", ""),
        "_telegram_id": None,
        "_file_id": None,
    }



def _archive_load() -> None:
    """启动时恢复归档日志；仍在 queued/uploading 的标记 failed 可重试。"""
    candidates = [_ARCHIVE_FILE]
    alt_file = os.path.join(APP_ROOT_DIR, "archive_jobs.json")
    if alt_file != _ARCHIVE_FILE and os.path.exists(alt_file):
        candidates.append(alt_file)

    for cf in candidates:
        try:
            with open(cf, "rb") as f:
                d = json.loads(f.read().decode("utf-8"))
            jobs = d.get("jobs") if isinstance(d, dict) else None
            if isinstance(jobs, dict):
                for k, v in jobs.items():
                    if isinstance(v, dict) and v.get("id"):
                        if str(k) not in _ARCHIVE_JOBS:
                            _ARCHIVE_JOBS[str(k)] = v
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("归档日志恢复失败 (%s): %s", cf, e)

    resumed = sum(
        1 for j in _ARCHIVE_JOBS.values()
        if j.get("state") in ("queued", "uploading"))
    for j in _ARCHIVE_JOBS.values():
        if j.get("state") in ("queued", "uploading"):
            j["state"] = "failed"
            j["error"] = "bridge 重启导致上传中断，可重试"
    if _ARCHIVE_JOBS:
        log.info("已恢复归档日志（%d 条任务，%d 条中断标记失败）",
                 len(_ARCHIVE_JOBS), resumed)


def _archive_save() -> None:
    try:
        if len(_ARCHIVE_JOBS) > _ARCHIVE_MAX_JOBS:
            finished = sorted(
                (j for j in _ARCHIVE_JOBS.values()
                 if j.get("state") in ("done", "failed", "cancelled")),
                key=lambda j: j.get("created_at") or 0.0)
            drop = len(_ARCHIVE_JOBS) - _ARCHIVE_MAX_JOBS
            for j in finished[:drop]:
                _ARCHIVE_JOBS.pop(j.get("id"), None)
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps({"jobs": _ARCHIVE_JOBS}).encode("utf-8")
        fd = os.open(_ARCHIVE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    except Exception as e:  # noqa: BLE001
        log.warning("归档日志持久化失败: %s", e)


def _archive_config_load() -> None:
    try:
        with open(_ARCHIVE_CONFIG_FILE, "rb") as f:
            d = json.loads(f.read().decode("utf-8"))
        if isinstance(d, dict):
            if "autoArchive" in d:
                _ARCHIVE_CONFIG["autoArchive"] = bool(d["autoArchive"])
            if "defaultDir" in d:
                _ARCHIVE_CONFIG["defaultDir"] = str(d["defaultDir"] or "")
            if "sessionBackupDir" in d:
                _ARCHIVE_CONFIG["sessionBackupDir"] = str(d["sessionBackupDir"] or "").strip()
            if "localBackupDir" in d:
                _ARCHIVE_CONFIG["localBackupDir"] = str(d["localBackupDir"] or "").strip()
            if "publicBaseUrl" in d or "public_base_url" in d:
                _ARCHIVE_CONFIG["publicBaseUrl"] = str(d.get("publicBaseUrl") or d.get("public_base_url") or "").strip().rstrip("/")
            if "policy" in d:
                _ARCHIVE_CONFIG["policy"] = "skip" if str(d["policy"]).lower() == "skip" else "overwrite"
            if "deleteLocal" in d:
                _ARCHIVE_CONFIG["deleteLocal"] = bool(d["deleteLocal"])
            if "cleanFilename" in d:
                _ARCHIVE_CONFIG["cleanFilename"] = bool(d["cleanFilename"])
            if "diskWatermarkGB" in d:
                _ARCHIVE_CONFIG["diskWatermarkGB"] = float(d["diskWatermarkGB"] or 5.0)
            if "diskAutoClean" in d:
                _ARCHIVE_CONFIG["diskAutoClean"] = bool(d["diskAutoClean"])
            if "diskHighWatermarkPercent" in d:
                _ARCHIVE_CONFIG["diskHighWatermarkPercent"] = float(d["diskHighWatermarkPercent"] or 85.0)
            if "diskLowWatermarkPercent" in d:
                _ARCHIVE_CONFIG["diskLowWatermarkPercent"] = float(d["diskLowWatermarkPercent"] or 75.0)
            if isinstance(d.get("stats"), dict):
                _ARCHIVE_CONFIG["stats"] = d["stats"]
        if _ARCHIVE_CONFIG.get("defaultDir"):
            log.info("已恢复默认归档配置: 目录=%s, 自动归档=%s",
                     _ARCHIVE_CONFIG["defaultDir"], _ARCHIVE_CONFIG["autoArchive"])
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("默认归档配置恢复失败: %s", e)


def _archive_config_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps(_ARCHIVE_CONFIG, ensure_ascii=False).encode("utf-8")
        fd = os.open(_ARCHIVE_CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    except Exception as e:  # noqa: BLE001
        log.warning("默认归档配置持久化失败: %s", e)


def _archive_config_public() -> Dict[str, Any]:
    stats = _ARCHIVE_CONFIG.get("stats") if isinstance(_ARCHIVE_CONFIG.get("stats"), dict) else {}
    return {
        "autoArchive": bool(_ARCHIVE_CONFIG.get("autoArchive")),
        "defaultDir": str(_ARCHIVE_CONFIG.get("defaultDir") or ""),
        "sessionBackupDir": str(_ARCHIVE_CONFIG.get("sessionBackupDir") or ""),
        "localBackupDir": str(_ARCHIVE_CONFIG.get("localBackupDir") or ""),
        "publicBaseUrl": str(_ARCHIVE_CONFIG.get("publicBaseUrl") or ""),
        "policy": str(_ARCHIVE_CONFIG.get("policy") or "overwrite"),
        "deleteLocal": bool(_ARCHIVE_CONFIG.get("deleteLocal")),
        "cleanFilename": bool(_ARCHIVE_CONFIG.get("cleanFilename", True)),
        "diskWatermarkGB": float(_ARCHIVE_CONFIG.get("diskWatermarkGB") or 5.0),
        "diskAutoClean": bool(_ARCHIVE_CONFIG.get("diskAutoClean", True)),
        "diskHighWatermarkPercent": float(_ARCHIVE_CONFIG.get("diskHighWatermarkPercent") or 85.0),
        "diskLowWatermarkPercent": float(_ARCHIVE_CONFIG.get("diskLowWatermarkPercent") or 75.0),
        "stats": {
            "enqueued": int(stats.get("enqueued") or 0),
            "done": int(stats.get("done") or 0),
            "failed": int(stats.get("failed") or 0),
            "lastHitAt": float(stats.get("lastHitAt") or 0.0),
        }
    }


def _subs_load() -> None:
    try:
        with open(_SUBS_FILE, "rb") as f:
            d = json.loads(f.read().decode("utf-8"))
        rules = d.get("rules") if isinstance(d, dict) else None
        if isinstance(rules, dict):
            for k, v in rules.items():
                if isinstance(v, dict) and v.get("id"):
                    _SUB_RULES[str(k)] = v
        if _SUB_RULES:
            log.info("已恢复订阅归档规则（%d 条）", len(_SUB_RULES))
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("订阅规则恢复失败: %s", e)


def _subs_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps({"rules": _SUB_RULES}, ensure_ascii=False).encode("utf-8")
        fd = os.open(_SUBS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    except Exception as e:  # noqa: BLE001
        log.warning("订阅规则持久化失败: %s", e)


def _notify_config_load() -> None:
    try:
        if os.path.exists(_NOTIFY_CONFIG_FILE):
            try:
                os.chmod(_NOTIFY_CONFIG_FILE, 0o600)
            except Exception:
                pass
            with open(_NOTIFY_CONFIG_FILE, "r", encoding="utf-8") as f:
                d = json.loads(f.read())
            if isinstance(d, dict):
                if "enabled" in d:
                    _NOTIFY_CONFIG["enabled"] = bool(d["enabled"])
                if "channel" in d:
                    _NOTIFY_CONFIG["channel"] = str(d["channel"] or "both")
                if "botToken" in d:
                    _NOTIFY_CONFIG["botToken"] = str(d["botToken"] or "")
                if "chatId" in d:
                    _NOTIFY_CONFIG["chatId"] = str(d["chatId"] or "")
                if "minFileSizeMB" in d:
                    _NOTIFY_CONFIG["minFileSizeMB"] = int(d["minFileSizeMB"] or 50)
                if isinstance(d.get("events"), dict):
                    _NOTIFY_CONFIG["events"].update(d["events"])
            log.info("已恢复 Telegram 通知配置（启用: %s, 通道: %s）",
                     _NOTIFY_CONFIG["enabled"], _NOTIFY_CONFIG["channel"])
    except Exception as e:  # noqa: BLE001
        log.warning("读取 .notify_config.json 异常: %s", e)


def _notify_config_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps(_NOTIFY_CONFIG, indent=2, ensure_ascii=False).encode("utf-8")
        fd = os.open(_NOTIFY_CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        try:
            os.chmod(_NOTIFY_CONFIG_FILE, 0o600)
        except Exception:
            pass
        log.info("Telegram 通知配置已更新并落盘")
    except Exception as e:  # noqa: BLE001
        log.warning("保存 .notify_config.json 异常: %s", e)


_BATCH_RETRY_LOCK = asyncio.Lock()
_MAX_JOB_RETRIES = 20


def _get_notify_config_public() -> Dict[str, Any]:
    masked = _mask_token(_NOTIFY_CONFIG.get("botToken", ""))
    return {
        "enabled": bool(_NOTIFY_CONFIG.get("enabled", False)),
        "channel": str(_NOTIFY_CONFIG.get("channel", "both")),
        "botToken": masked,
        "botTokenMasked": masked,
        "hasBotToken": bool(_NOTIFY_CONFIG.get("botToken")),
        "chatId": str(_NOTIFY_CONFIG.get("chatId", "")),
        "minFileSizeMB": int(_NOTIFY_CONFIG.get("minFileSizeMB", 50)),
        "events": dict(_NOTIFY_CONFIG.get("events", {})),
    }



def _openlist_update_base_url(url: str) -> str:
    global _openlist_client, _openlist_upload_client
    raw = str(url or "").strip().rstrip("/")
    if raw:
        try:
            parsed = urlparse(raw)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                u = raw
            else:
                u = OPENLIST_URL.rstrip("/")
        except Exception:
            u = OPENLIST_URL.rstrip("/")
    else:
        u = OPENLIST_URL.rstrip("/")
    _OPENLIST["baseUrl"] = u
    _openlist_client.base_url = httpx.URL(u)
    _openlist_upload_client.base_url = httpx.URL(u)
    return u


def _openlist_load() -> None:
    try:
        candidates = [_OPENLIST_FILE]
        alt_file = os.path.join(APP_ROOT_DIR, "openlist.json")
        if alt_file != _OPENLIST_FILE and os.path.exists(alt_file):
            candidates.append(alt_file)
        for cf in candidates:
            if os.path.exists(cf):
                with open(cf, "rb") as f:
                    d = json.loads(f.read().decode("utf-8"))
                for k in ("username", "password", "token", "baseUrl"):
                    v = str(d.get(k) or "")
                    if v and not _OPENLIST.get(k):
                        _OPENLIST[k] = v
                if not _OPENLIST.get("logged_at"):
                    _OPENLIST["logged_at"] = float(d.get("logged_at") or 0.0)
                if _OPENLIST.get("baseUrl"):
                    _openlist_update_base_url(_OPENLIST["baseUrl"])
        if _OPENLIST.get("username"):
            log.info("已恢复 OpenList 登录态（用户: %s, 地址: %s）",
                     _OPENLIST["username"], _OPENLIST.get("baseUrl") or OPENLIST_URL)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("OpenList 凭据恢复失败: %s", e)


def _openlist_save() -> None:
    try:
        os.makedirs(APP_ROOT_DIR, exist_ok=True)
        payload = json.dumps({
            "baseUrl": _OPENLIST.get("baseUrl") or OPENLIST_URL,
            "username": _OPENLIST.get("username", ""),
            "password": _OPENLIST.get("password", ""),
            "token": _OPENLIST.get("token", ""),
            "logged_at": _OPENLIST.get("logged_at", 0.0),
        }).encode("utf-8")
        fd = os.open(_OPENLIST_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    except Exception as e:  # noqa: BLE001
        log.warning("OpenList 凭据持久化失败: %s", e)


def _openlist_clear() -> None:
    _OPENLIST.clear()
    try:
        os.remove(_OPENLIST_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("OpenList 凭据清除失败: %s", e)


_ARCH_PILL = {
    "queued": ("pending", "排队中"),
    "uploading": ("upload", "归档中"),
    "done": ("archived", "已归档"),
    "failed": ("failed", "归档失败"),
    "cancelled": ("pending", "已取消"),
}


def _archive_public(job: Dict[str, Any]) -> Dict[str, Any]:
    """给前端展示的 job 视图（绝不携带本地路径）。"""
    state = str(job.get("state") or "queued")
    cls, label = _ARCH_PILL.get(state, ("pending", state))
    return {
        "id": job.get("id"),
        "uniqueId": job.get("unique_id"),
        "filename": job.get("filename"),
        "remoteDir": job.get("remote_dir"),
        "remotePath": job.get("remote_path"),
        "state": state,
        "progress": int(job.get("progress") or 0),
        "error": job.get("error") or "",
        "archivedAt": float(job.get("archived_at") or 0.0),
        "createdAt": float(job.get("created_at") or 0.0),
        "deleteLocal": bool(job.get("delete_local")),
        "localDeleted": bool(job.get("local_deleted")),
        "localDeleteError": str(job.get("local_delete_error") or ""),
        "pillCls": cls,
        "pillLabel": label,
    }


def _archive_state_of(unique_id: Any) -> Optional[Dict[str, Any]]:
    """某文件的最新归档任务视图（library_local 渲染按钮/胶囊用）。"""
    if not unique_id:
        return None
    latest = None
    for j in _ARCHIVE_JOBS.values():
        if str(j.get("unique_id") or "") == str(unique_id):
            if latest is None or (j.get("created_at") or 0) > (latest.get("created_at") or 0):
                latest = j
    return _archive_public(latest) if latest else None


def _archive_active_of(unique_id: str) -> Optional[Dict[str, Any]]:
    """进行中（queued/uploading）的归档任务，幂等去重用。"""
    for j in _ARCHIVE_JOBS.values():
        if str(j.get("unique_id") or "") == unique_id and j.get("state") in ("queued", "uploading"):
            return j
    return None


def _archive_latest_raw_of(unique_id: str) -> Optional[Dict[str, Any]]:
    """某文件最新一条归档任务的原始 job。"""
    latest = None
    for j in _ARCHIVE_JOBS.values():
        if str(j.get("unique_id") or "") == str(unique_id):
            if latest is None or (j.get("created_at") or 0) > (latest.get("created_at") or 0):
                latest = j
    return latest


def _archive_registry_lookup(unique_id: str = "", filename: str = "", size_bytes: Any = None) -> Optional[Dict[str, Any]]:
    if not _ARCHIVE_JOBS:
        return None
    uid = str(unique_id or "").strip()
    norm_fn = str(filename or "").strip().lower()
    sz = None
    if isinstance(size_bytes, (int, float)) and size_bytes > 0:
        sz = int(size_bytes)

    for j in reversed(list(_ARCHIVE_JOBS.values())):
        if j.get("state") not in ("done", "uploading", "queued"):
            continue
        if uid and str(j.get("unique_id") or "").strip() == uid:
            return j
        j_fn = str(j.get("filename") or j.get("raw_filename") or "").strip().lower()
        if norm_fn and j_fn and _same_file_name(norm_fn, j_fn):
            if sz is None or j.get("size_bytes") == sz:
                return j
    return None


# 模块装载时从磁盘加载已有状态
_flood_wait_load()
_waiting_disk_load()
_archive_load()
_archive_config_load()
_subs_load()
_notify_config_load()
_openlist_load()
