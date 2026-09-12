# -*- coding: utf-8 -*-
"""
core/logging.py — 运行日志缓冲存储、LogRecord桥接与 SSE 广播器
===========================================================
提供 LogStore 环形缓冲 + JSONL 落盘，以及 SSE 实时消息分发 Hub。
"""
import os
import json
import time
import logging
import threading
import asyncio
from collections import deque
from typing import Any, Dict, List, Optional, Set

from core.config import (
    APP_ROOT_DIR, _LOG_TIME_FMT, _LOG_STORE_LEVELS, TG_STATE_NAMES,
    _fmt_time, _fmt_size
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("bridge")


class LogStore:
    """进程级运行日志：内存 deque 环形缓冲 + JSONL 落盘（重启恢复）。

    线程安全（logging handler 可能来自任意线程）；append 内部绝不调用
    logging，避免递归。落盘失败静默降级为纯内存 —— 日志中心不能反向拖垮业务。
    """

    MAX_LINES = 1000
    MAX_MSG = 300
    FILE_MAX_BYTES = 5 * 1024 * 1024   # logs.jsonl 超过后轮转为 .1
    _ROTATE_CHECK = 128                # 每 N 次落盘检查一次大小（省 stat）

    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._items: "deque[Dict[str, Any]]" = deque(maxlen=self.MAX_LINES)
        self._seq = 0
        self._path = path
        self._since_check = 0
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError:
            pass
        self._load()

    def _load(self) -> None:
        """启动时从 logs.jsonl 恢复历史（重新编 seq，文件只保顺序与时间戳）。"""
        try:
            if not os.path.exists(self._path):
                return
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    self._seq += 1
                    self._items.append({
                        "seq": self._seq,
                        "level": rec.get("level") if rec.get("level") in _LOG_STORE_LEVELS else "INFO",
                        "taskId": str(rec.get("taskId") or "")[:64],
                        "msg": str(rec.get("msg") or ""),
                        "time": str(rec.get("time") or "—"),
                    })
        except Exception:  # noqa: BLE001
            pass

    def _persist(self, rec: Dict[str, Any]) -> None:
        try:
            self._since_check += 1
            if self._since_check >= self._ROTATE_CHECK:
                self._since_check = 0
                try:
                    if os.path.getsize(self._path) > self.FILE_MAX_BYTES:
                        os.replace(self._path, self._path + ".1")
                except OSError:
                    pass
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def append(self, level: str, msg: str, task_id: str = "",
               ts: Optional[float] = None, time_str: str = "") -> Dict[str, Any]:
        """写入一条日志；time_str 优先（WS 事件带后端时间戳），否则用 ts/当前时间。"""
        level = level if level in _LOG_STORE_LEVELS else "INFO"
        text = " ".join(str(msg).split())[: self.MAX_MSG] or "(空消息)"
        if time_str and time_str != "—":
            t = time_str
        else:
            t = time.strftime(_LOG_TIME_FMT, time.localtime(ts if ts is not None else time.time()))
        with self._lock:
            self._seq += 1
            rec = {"seq": self._seq, "level": level, "taskId": str(task_id or "")[:64],
                   "msg": text, "time": t}
            self._items.append(rec)
        self._persist(rec)
        return rec

    def snapshot(self, limit: int = 0, since_seq: int = 0) -> List[Dict[str, Any]]:
        """按 seq 升序返回缓冲内容；since_seq>0 只取更新的，limit>0 只取尾部 N 条。"""
        with self._lock:
            items = [dict(r) for r in self._items if r["seq"] > since_seq]
        return items[-limit:] if limit and len(items) > limit else items

    def last_seq(self) -> int:
        with self._lock:
            return self._seq


LOG_STORE = LogStore(os.path.join(APP_ROOT_DIR, "logs.jsonl"))


class _LogStoreHandler(logging.Handler):
    """把 bridge / uvicorn.error 的运行日志映射进日志中心（级别对齐前端三档）。"""

    _MAP = {"WARNING": "WARN", "CRITICAL": "ERROR"}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = self._MAP.get(record.levelname, record.levelname)
            if level not in _LOG_STORE_LEVELS:
                return
            LOG_STORE.append(level, record.getMessage(), ts=record.created)
        except Exception:  # noqa: BLE001
            pass


for _ls_name in ("bridge", "uvicorn.error"):
    logging.getLogger(_ls_name).addHandler(_LogStoreHandler())


class Broadcaster:
    """极简广播器：把后端 WS 事件分发给所有订阅的前端 SSE 连接。"""

    MAX_SUBSCRIBERS = 64

    def __init__(self):
        self._tasks_q: Set[asyncio.Queue] = set()
        self._logs_q: Set[asyncio.Queue] = set()

    def subscribe(self, group: str) -> Optional[asyncio.Queue]:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        target = self._tasks_q if group == "tasks" else self._logs_q
        if len(target) >= self.MAX_SUBSCRIBERS:
            return None
        target.add(q)
        return q

    def unsubscribe(self, group: str, q: asyncio.Queue) -> None:
        target = self._tasks_q if group == "tasks" else self._logs_q
        target.discard(q)

    async def publish(self, group: str, payload: Any) -> None:
        target = self._tasks_q if group == "tasks" else self._logs_q
        if not target:
            return
        text = json.dumps(payload, ensure_ascii=False)
        for q in list(target):
            try:
                q.put_nowait(text)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(text)
                except Exception:  # noqa: BLE001
                    pass


HUB = Broadcaster()


def _log_line_from_event(ev: Dict[str, Any], task_id: Any = None) -> Dict[str, str]:
    """把 WS 事件降级为一条前端日志行。"""
    typ = ev.get("type")
    data = ev.get("data")
    if not isinstance(data, dict):
        data = {}
    code = ev.get("code", "")
    ts = ev.get("timestamp")
    time_str = _fmt_time(ts) or "—"
    if typ == -1:
        msg = "错误: " + str(data.get("message", "unknown"))
        return {"level": "ERROR", "taskId": str(task_id or ""), "msg": msg, "time": time_str}
    if typ == 1:
        state = TG_STATE_NAMES.get(data.get("constructor"), str(data.get("constructor", data)))
        return {"level": "INFO", "taskId": str(task_id or ""), "msg": "授权状态: %s" % state, "time": time_str}
    if typ == 2:
        msg = "TDLib 结果 code=%s" % code
        return {"level": "INFO", "taskId": str(task_id or ""), "msg": msg, "time": time_str}
    if typ == 3:
        local = data.get("local") if isinstance(data.get("local"), dict) else {}
        dl = local.get("downloadedSize")
        name = ""
        remote = data.get("remote")
        if isinstance(remote, dict):
            name = str(remote.get("uniqueId") or "")
        msg = "文件更新: %s%s" % (name[:60] or "?", (" · %s" % _fmt_size(dl)) if dl is not None else "")
        return {"level": "INFO", "taskId": str(task_id or ""), "msg": msg, "time": time_str}
    if typ == 4:
        total = data.get("totalSize")
        got = data.get("downloadedSize")
        msg = "下载进度: {}/{} ({} 个)".format(_fmt_size(got), _fmt_size(total), data.get("totalCount", "?"))
        return {"level": "INFO", "taskId": str(task_id or ""), "msg": msg, "time": time_str}
    if typ == 5:
        st = data.get("downloadStatus")
        msg = "文件状态: %s · %s" % (st or "?", str(data.get("localPath") or data.get("uniqueId") or ""))[:160]
        return {"level": "INFO", "taskId": str(task_id or data.get("fileId", "")), "msg": msg, "time": time_str}
    if typ == 6:
        state = data.get("state", "?")
        level = "WARN" if state in ("waitingForNetwork", "connectingToProxy", "unknown") else "INFO"
        return {"level": level, "taskId": str(task_id or ""), "msg": "连接状态: %s" % state, "time": time_str}

    msg = json.dumps(ev, ensure_ascii=False)[:200]
    return {"level": "INFO", "taskId": str(task_id or ""), "msg": msg, "time": time_str}
