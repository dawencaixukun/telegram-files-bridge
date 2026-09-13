# -*- coding: utf-8 -*-
"""
services/subscription_service.py — 自动化订阅与规则匹配引擎
============================================================
负责目录命名模板动态渲染、订阅规则优先级排序、规则匹配与后台自动归档巡检循环。
"""
import os
import re
import time
import asyncio
from typing import Any, Dict, List, Optional
from fastapi import Request
from core.config import _archive_norm_dir
from core.state import (
    _SUB_RULES, _ARCHIVE_CONFIG, _subs_save, _archive_config_save
)
from core.logging import log

_SUB_TPL_PATTERN = re.compile(r"\{([A-Za-z0-9_\-]+)\}")
_AUTO_ARCHIVE_INTERVAL = float(os.environ.get("BRIDGE_AUTO_ARCHIVE_INTERVAL", "30"))
_AUTO_ARCHIVE_FIRST_DELAY = 5.0
_TPL_HINT = "需以 / 开头，可用变量 {source} {chat_title} {type} {resolution} {ext} {YYYY} {MM} {DD} {YYYY-MM}"


def _sub_clean_seg(v: Any) -> str:
    """清洗单个目录段：网盘/对象存储通用禁忌字符 + 首尾点号空白，消除 .. 逃逸，限长 64。"""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", str(v or ""))
    s = re.sub(r"\s+", " ", s).strip()
    while ".." in s:
        s = s.replace("..", ".")
    s = s.strip(".").strip()
    return s[:64]


def _render_dir_template(tpl: str, *, source: str = "", ftype: str = "",
                         ts: Optional[float] = None, filename: str = "",
                         resolution: str = "", ext: str = "",
                         chat_title: str = "", width: Optional[int] = None,
                         height: Optional[int] = None) -> Optional[str]:
    """渲染目录模板。变量：{source} {chat_title} {type} {resolution} {ext} {YYYY} {MM} {DD} {YYYY-MM}。"""
    tpl = (tpl or "").strip()
    if not tpl:
        return None
    t = time.localtime(ts if (ts and ts > 0) else time.time())

    res_val = str(resolution or "").strip().lower()
    if not res_val:
        if height:
            try:
                h = int(height)
                w = int(width or 0)
                if h >= 2160 or w >= 3840:
                    res_val = "4k"
                elif h >= 1080 or w >= 1920:
                    res_val = "1080p"
                elif h >= 720 or w >= 1280:
                    res_val = "720p"
                elif h >= 480:
                    res_val = "480p"
                elif h >= 360:
                    res_val = "360p"
                else:
                    res_val = f"{h}p"
            except (ValueError, TypeError):
                res_val = ""
    if not res_val and filename:
        m = re.search(r"(?i)(?:^|[\W_])(4k|2160p|1080p|1080i|720p|480p|360p)(?:[\W_]|$)", filename)
        if m:
            res_val = m.group(1).lower()
        else:
            m2 = re.search(r"(?i)(?:^|[\W_])(\d{3,4})[xX](\d{3,4})(?:[\W_]|$)", filename)
            if m2:
                try:
                    h = int(m2.group(2))
                    if h >= 2160:
                        res_val = "4k"
                    elif h >= 1080:
                        res_val = "1080p"
                    elif h >= 720:
                        res_val = "720p"
                    elif h >= 480:
                        res_val = "480p"
                    else:
                        res_val = f"{h}p"
                except (ValueError, TypeError):
                    res_val = "unknown"
            else:
                res_val = "unknown"
    if not res_val:
        res_val = "unknown"

    ext_val = str(ext or "").strip().lower()
    if not ext_val:
        if filename and "." in filename:
            raw_ext = filename.rsplit(".", 1)[-1].lower().strip()
            clean_ext = re.sub(r"[^a-z0-9]", "", raw_ext)[:10]
            ext_val = clean_ext or "bin"
        else:
            ext_val = "mp4" if ftype == "video" else ("jpg" if ftype == "photo" else "bin")

    ct_clean = _sub_clean_seg(chat_title or source) or "未分类"
    src_clean = _sub_clean_seg(source or chat_title) or "未分类"

    mapping = {
        "source": src_clean,
        "chat_title": ct_clean,
        "type": _sub_clean_seg(ftype) or "file",
        "resolution": _sub_clean_seg(res_val) or "unknown",
        "ext": _sub_clean_seg(ext_val) or "bin",
        "YYYY": "%04d" % t.tm_year,
        "MM": "%02d" % t.tm_mon,
        "DD": "%02d" % t.tm_mday,
        "YYYY-MM": "%04d-%02d" % (t.tm_year, t.tm_mon),
    }
    unknown = []

    def _rep(m):
        key = m.group(1)
        if key in mapping:
            return mapping[key]
        unknown.append(key)
        return ""

    rendered = _SUB_TPL_PATTERN.sub(_rep, tpl)
    if unknown or re.search(r"\{[^{}]*\}", rendered):
        return None
    return _archive_norm_dir(rendered)


def _sub_rules_sorted() -> List[Dict[str, Any]]:
    """按 (priority DESC, specificity DESC, created_at ASC, id ASC) 确定性排序规则"""
    def _sort_key(r: Dict[str, Any]):
        prio = int(r.get("priority", 0) or 0)
        ch = str(r.get("chatId") or "")
        spec = 100 if (ch and ch != "*") else 10
        created = float(r.get("createdAt") or r.get("created_at") or 0.0)
        rid = str(r.get("id") or "")
        return (-prio, -spec, created, rid)
    return sorted(_SUB_RULES.values(), key=_sort_key)


def _sub_match_rule(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """任务 → 命中的启用规则（按优先级权重由高至低依次判定，首个全匹配即止）。"""
    tg = str(task.get("_telegram_id") or "")
    ch = str(task.get("_chat_id") or "")
    if not tg and not ch:
        return None
    for r in _sub_rules_sorted():
        if not r.get("enabled"):
            continue
        rule_tg = str(r.get("telegramId") or "")
        rule_ch = str(r.get("chatId") or "")
        tg_match = (not rule_tg or rule_tg == "*" or rule_tg == tg)
        ch_match = (rule_ch == "*" or (ch and rule_ch == ch))
        if tg_match and ch_match:
            return r
    return None


def _sub_bump(rule_id: Any, key: str) -> None:
    r = _SUB_RULES.get(str(rule_id or ""))
    if r is None:
        return
    stats = r.setdefault("stats", {})
    stats[key] = int(stats.get(key) or 0) + 1
    stats["last_hit_at"] = time.time()
    _subs_save()


def _sub_on_job_finished(job: Dict[str, Any]) -> None:
    """归档 worker 终态回调（finally 中调用）：只统计自动任务，手动归档不计。"""
    if not job.get("auto"):
        return
    state = str(job.get("state") or "")
    rule_id = str(job.get("rule_id") or "")
    if rule_id == "default":
        stats = _ARCHIVE_CONFIG.setdefault("stats", {})
        if state == "done":
            stats["done"] = int(stats.get("done") or 0) + 1
        elif state == "failed":
            stats["failed"] = int(stats.get("failed") or 0) + 1
        stats["lastHitAt"] = time.time()
        _archive_config_save()
    elif rule_id:
        if state == "done":
            _sub_bump(rule_id, "done")
        elif state == "failed":
            _sub_bump(rule_id, "failed")


def _sub_public(rule: Dict[str, Any]) -> Dict[str, Any]:
    stats = rule.get("stats") if isinstance(rule.get("stats"), dict) else {}
    preview = _render_dir_template(str(rule.get("dirTemplate") or ""),
                                   source=str(rule.get("chatTitle") or "频道"),
                                   ftype="video",
                                   filename="Sample_1080p.mp4",
                                   chat_title=str(rule.get("chatTitle") or "频道"))
    return {
        "id": rule.get("id"),
        "telegramId": rule.get("telegramId"),
        "chatId": rule.get("chatId"),
        "chatTitle": str(rule.get("chatTitle") or "聊天"),
        "enabled": bool(rule.get("enabled")),
        "priority": int(rule.get("priority", 0) or 0),
        "dirTemplate": str(rule.get("dirTemplate") or ""),
        "deleteLocal": bool(rule.get("deleteLocal", True)),
        "policy": str(rule.get("policy") or "skip"),
        "createdAt": float(rule.get("created_at") or 0.0),
        "previewDir": preview or "",
        "stats": {
            "enqueued": int(stats.get("enqueued") or 0),
            "done": int(stats.get("done") or 0),
            "failed": int(stats.get("failed") or 0),
            "lastHitAt": float(stats.get("last_hit_at") or 0.0),
        },
    }


def _sub_validate_chat(tg_id: Any, chat_id: Any, sources: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for s in sources:
        if str(s.get("telegramId")) == str(tg_id) and str(s.get("chatId")) == str(chat_id):
            return s
    return None


async def _subs_json_body(request: Request) -> Optional[Dict[str, Any]]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return None
    return body if isinstance(body, dict) else None

