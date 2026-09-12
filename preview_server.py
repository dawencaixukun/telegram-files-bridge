"""
preview_server.py — TG 视频下载与归档管理系统 · 前端预览服务器
===============================================================
仅用于前端 UI 预览：FastAPI TemplateResponse 渲染 + 模拟数据注入。
不含任何真实业务逻辑（无真实 TG / OpenList 调用）。

启动：  uvicorn preview_server:app --host 127.0.0.1 --port 8000
访问：  http://127.0.0.1:8000/            （路由表见 README）
"""
import asyncio
import json
import os
import posixpath
import random
import time
import datetime as dt
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------
# 配置（不依赖启动时 cwd）
# ---------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

app = FastAPI(title="TG 归档台 · 前端预览")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """为所有响应注入基础安全头（点击劫持/MIME 嗅探/引用来源纵深防御）。"""
    resp = await call_next(request)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp

# 全局 Nav 状态（模拟）
SESSION_STATE = "ok"          # ok / warn / err
SESSION_DOT = {"ok": "ok", "warn": "warn", "err": "err"}
SESSION_LABEL = {"ok": "已连接", "warn": "未登录", "err": "session 失效"}


# ---------------------------------------------------------------------
# Mock 数据（高度真实感）
# ---------------------------------------------------------------------
def _fx_hash(n):
    """生成 n 位 base64 风格的 quickXorHash（43 位）"""
    charset = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    return "".join(random.choice(charset) for _ in range(n))


def _md5():
    import hashlib
    return hashlib.md5(str(random.random()).encode()).hexdigest()


def _stages(status):
    """根据状态生成阶段时间线（与真实后端同构：校验/上传无时间戳，恒为 ghost '—'）"""
    base = [
        {"name": "入队", "done": "yes", "time": "08:31:12", "dur": "—"},
        {"name": "下载", "done": "yes", "time": "08:31:15", "dur": "2m 41s"},
        {"name": "校验", "done": "no", "time": "—", "dur": "—"},
        {"name": "上传", "done": "no", "time": "—", "dur": "—"},
        {"name": "归档", "done": "no", "time": "—", "dur": "—"},
    ]
    if status in ("archived", "isolated"):
        base[3]["done"] = "yes" if status == "archived" else "no"
        base[4]["done"] = "yes" if status == "archived" else "no"
        base[4]["time"] = "09:39:05" if status == "archived" else "—"
        base[4]["dur"] = "—"
    elif status == "downloaded":
        base[1]["done"] = "yes"
        base[2]["done"] = "yes"
        base[3]["done"] = "no"
        base[4]["done"] = "no"
    if status == "failed":
        base[1]["done"] = "no"; base[1]["time"] = "08:34:40"; base[1]["dur"] = "FloodWait 58s"
    return base


CHATS = ["极客飞船频道", "每日资源更新", "纪录片放映室", "前端私享群"]


def _task(i, status, filename, size, chat, time_str, msg_id):
    qx = _fx_hash(43)
    return {
        "id": i,
        "time": time_str,
        "source": chat,
        "msg_id": msg_id,
        "filename": filename,
        "size": size,
        "status": status,
        "loaded": f"{random.randint(20, 98)}%",
        "progress": random.randint(20, 98),
        "md5": _md5(),
        "qx": qx,
        "local_path": f"/data/tg-archive/{chat[:2]}/{i:04d}_" + _fx_hash(8) + ".mp4",
        "cloud_path": f"/openlist/tg-videos/202608/{chat[:2]}/{i:04d}.mp4",
        "source_url": f"https://t.me/{_fx_hash(6)}/{msg_id}",
        "error_msg": "FloodWait exceeds limit (58s) — rate limit exceeded" if status == "failed" else "",
        "stages": _stages(status),
        # 桥接层内部字段：_tasks_table / task_detail 模板经 tojson 输出（缺失会让模板 500）
        "_unique_id": f"uid-{i:06d}",
        "_telegram_id": 10086 + i,
    }


TASKS_RAW = [
    _task(101, "download", "【航拍】城市夜景 4K 实拍素材.mp4", "1.7 GB", CHATS[0], "08:31:12", 8841),
    _task(102, "upload",   "Python 后端架构演进视频课程 第12讲.mp4", "856 MB", CHATS[3], "08:22:40", 3301),
    _task(103, "archived", "纪录片《蓝色星球》EP03 高清修复版.mp4", "2.3 GB", CHATS[2], "07:58:44", 220),
    _task(104, "verify",   "前端周刊 2026-08 第3期 剪辑版.mp4", "412 MB", CHATS[1], "09:02:31", 1902),
    _task(105, "pending",  "科技发布会全程回顾 1080p.mp4", "1.1 GB", CHATS[0], "09:41:10", 9020),
    _task(106, "isolated", "【疑似垃圾】广告推广视频.mp4", "24 MB", CHATS[1], "09:12:26", 2001),
    _task(107, "failed",   "大型课程合集（含字幕）part3.mp4", "3.5 GB", CHATS[3], "08:34:40", 3312),
    _task(108, "download", "无人机航拍 沿海公路 超清.mp4", "680 MB", CHATS[2], "09:20:15", 233),
]
TASKS = TASKS_RAW


def _running_count():
    return sum(1 for t in TASKS if t["status"] in ("download", "upload", "verify"))


def _recent_tasks():
    return TASKS[:8]


LOCAL_FILES = [
    {"filename": "【航拍】城市夜景 4K 实拍素材.mp4", "size": "1.7 GB", "source": CHATS[0], "time": "08-31 08:31", "status": "download",
     "_download_status": "downloading", "_transfer_status": "idle", "_type": "video", "_mimeType": "video/mp4", "_size_bytes": 1825361101,
     "unique_id": "uid-000101", "_unique_id": "uid-000101", "local_exists": True, "can_delete_local": True,
     "arch_btn": {"enabled": False, "label": "立即归档", "title": "文件尚未下载完成，下载完成后可归档"}},
    {"filename": "纪录片《蓝色星球》EP03.mp4", "size": "2.3 GB", "source": CHATS[2], "time": "08-29 07:58", "status": "archived",
     "_download_status": "completed", "_transfer_status": "completed", "_type": "video", "_mimeType": "video/mp4", "_size_bytes": 2469606197,
     "unique_id": "uid-000102", "_unique_id": "uid-000102", "local_exists": True, "can_delete_local": True,
     "arch_btn": {"enabled": True, "label": "立即归档", "title": "上传到 OpenList 云端归档"}},
    {"filename": "前端周刊 2026-08 第3期.mp4", "size": "412 MB", "source": CHATS[1], "time": "08-31 09:02", "status": "verify",
     "_download_status": "completed", "_transfer_status": "idle", "_type": "video", "_mimeType": "video/mp4", "_size_bytes": 432013107,
     "unique_id": "uid-000103", "_unique_id": "uid-000103", "local_exists": True, "can_delete_local": True,
     "arch_btn": {"enabled": True, "label": "立即归档", "title": "上传到 OpenList 云端归档"}},
    {"filename": "Python 后端架构演进-12讲.mp4", "size": "856 MB", "source": CHATS[3], "time": "08-31 08:22", "status": "upload",
     "_download_status": "completed", "_transfer_status": "transferring", "_type": "video", "_mimeType": "video/mp4", "_size_bytes": 897881268,
     "unique_id": "uid-000104", "_unique_id": "uid-000104", "local_exists": True, "can_delete_local": True,
     "arch_btn": {"enabled": True, "label": "立即归档", "title": "上传到 OpenList 云端归档"}},
    {"filename": "科技发布会全程回顾.mp4", "size": "1.1 GB", "source": CHATS[0], "time": "08-31 09:41", "status": "pending",
     "_download_status": "idle", "_transfer_status": "idle", "_type": "video", "_mimeType": "video/mp4", "_size_bytes": 1181116006,
     "unique_id": "uid-000105", "_unique_id": "uid-000105", "local_exists": False, "can_delete_local": False,
     "arch_btn": {"enabled": False, "label": "立即归档", "title": "文件尚未下载完成，下载完成后可归档"}},
    {"filename": "无人机航拍 沿海公路.mp4", "size": "680 MB", "source": CHATS[2], "time": "08-31 09:20", "status": "download",
     "_download_status": "downloading", "_transfer_status": "idle", "_type": "video", "_mimeType": "video/mp4", "_size_bytes": 713031680,
     "unique_id": "uid-000106", "_unique_id": "uid-000106", "local_exists": True, "can_delete_local": True,
     "arch_btn": {"enabled": False, "label": "立即归档", "title": "文件尚未下载完成，下载完成后可归档"}},
    {"filename": "BBC 地球脉动 第07集.mp4", "size": "1.9 GB", "source": CHATS[2], "time": "08-30 21:33", "status": "archived",
     "_download_status": "completed", "_transfer_status": "completed", "_type": "video", "_mimeType": "video/mp4", "_size_bytes": 2040109466,
     "unique_id": "uid-000107", "_unique_id": "uid-000107", "local_exists": True, "can_delete_local": True,
     "arch_btn": {"enabled": True, "label": "立即归档", "title": "上传到 OpenList 云端归档"}},
    {"filename": "前端工程化实战 合集.mp4", "size": "3.2 GB", "source": CHATS[3], "time": "08-29 18:05", "status": "archived",
     "_download_status": "completed", "_transfer_status": "completed", "_type": "video", "_mimeType": "video/mp4", "_size_bytes": 3435973837,
     "unique_id": "uid-000108", "_unique_id": "uid-000108", "local_exists": True, "can_delete_local": True,
     "arch_btn": {"enabled": True, "label": "立即归档", "title": "上传到 OpenList 云端归档"}},
    # 音频 + 小文件各一条，让类型/大小筛选在预览里也能看出效果
    {"filename": "深夜技术电台 第12期.mp3", "size": "86 MB", "source": CHATS[1], "time": "08-30 22:10", "status": "download",
     "_download_status": "downloading", "_transfer_status": "idle", "_type": "audio", "_mimeType": "audio/mpeg", "_size_bytes": 90177536,
     "unique_id": "uid-000109", "_unique_id": "uid-000109", "local_exists": True, "can_delete_local": True,
     "arch_btn": {"enabled": False, "label": "立即归档", "title": "文件尚未下载完成，下载完成后可归档"}},
]


def _preview_sum_human(files):
    """按 _size_bytes 汇总为人类可读（与 bridge._fmt_size 同格式）。"""
    total = sum(int(f["_size_bytes"]) for f in files if isinstance(f.get("_size_bytes"), (int, float)))
    if not total:
        return "—"
    num = float(total)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024.0:
            return "{:.0f} {}".format(num, unit) if unit == "B" else "{:.1f} {}".format(num, unit)
        num /= 1024.0
    return "{:.1f} PB".format(num)


def _preview_classify(f):
    """类型筛选大类（与 bridge._classify_file_type 同规则，作用于 mock 条目）。"""
    t = str(f.get("_type") or "").strip().lower()
    if t in ("video", "animation"):
        return "video"
    if t == "photo":
        return "photo"
    if t == "audio":
        return "audio"
    m = str(f.get("_mimeType") or "").strip().lower()
    if m.startswith("video/"):
        return "video"
    if m.startswith("image/"):
        return "photo"
    if m.startswith("audio/"):
        return "audio"
    return "document"


def _preview_match_size(f, bucket):
    b = f.get("_size_bytes")
    if not bucket:
        return True
    if not isinstance(b, (int, float)):
        return False
    b = int(b)
    if bucket == "lt100mb":
        return 0 < b < 100 * 1024 ** 2
    if bucket == "100mb-500mb":
        return 100 * 1024 ** 2 <= b < 500 * 1024 ** 2
    if bucket == "gt1gb":
        return b >= 1024 ** 3
    return False


CLOUD_FILES = [
    {"archived_time": "08-29 07:58", "filename": "纪录片《蓝色星球》EP03.mp4", "size": "2.3 GB",
     "size_bytes": 2469606197, "cloud_path": "/阿里云盘/tg-archive/纪录片/EP03.mp4",
     "remote_dir": "/阿里云盘/tg-archive/纪录片", "drive": "阿里云盘",
     "openlist_url": "http://127.0.0.1:5244/阿里云盘/tg-archive/纪录片/EP03.mp4",
     "id": "pv-c101", "unique_id": "uid-000102", "source_url": "https://t.me/xxxx/220", "status": "archived"},
    {"archived_time": "08-30 21:33", "filename": "BBC 地球脉动 第07集.mp4", "size": "1.9 GB",
     "size_bytes": 2040109466, "cloud_path": "/Google Drive/tg-archive/纪录片/EP07.mp4",
     "remote_dir": "/Google Drive/tg-archive/纪录片", "drive": "Google Drive",
     "openlist_url": "http://127.0.0.1:5244/Google Drive/tg-archive/纪录片/EP07.mp4",
     "id": "pv-c102", "unique_id": "uid-000107", "source_url": "https://t.me/xxxx/231", "status": "archived"},
    {"archived_time": "08-31 08:40", "filename": "前端工程化实战 合集.mp4", "size": "3.2 GB",
     "size_bytes": 3435973837, "cloud_path": "/OneDrive/tg-archive/前端/合集.mp4",
     "remote_dir": "/OneDrive/tg-archive/前端", "drive": "OneDrive",
     "openlist_url": "http://127.0.0.1:5244/OneDrive/tg-archive/前端/合集.mp4",
     "id": "pv-c103", "unique_id": "uid-000108", "source_url": "https://t.me/xxxx/3321", "status": "archived"},
    {"archived_time": "08-31 09:12", "filename": "【疑似垃圾】广告推广视频.mp4", "size": "24 MB",
     "size_bytes": 25165824, "cloud_path": "/阿里云盘/隔离区/202608/广告推广001.mp4",
     "remote_dir": "/阿里云盘/隔离区/202608", "drive": "阿里云盘",
     "openlist_url": "http://127.0.0.1:5244/阿里云盘/隔离区/202608/广告推广001.mp4",
     "id": "pv-c104", "unique_id": "uid-000106", "source_url": "https://t.me/xxxx/2001", "status": "missing"},
]

SEED_LOGS = [
    {"level": "INFO", "taskId": "101", "time": "08:31:12", "msg": "任务 #101 开始下载 → 极客飞船频道/8841"},
    {"level": "INFO", "taskId": "102", "time": "08:32:40", "msg": "任务 #102 校验通过 md5=3f9c...a1"},
    {"level": "WARN", "taskId": "107", "time": "08:34:42", "msg": "FloodWait 触发 (58s)，任务 #107 进入冷却"},
    {"level": "ERROR", "taskId": "107", "time": "08:34:43", "msg": "任务 #107 上传失败：FloodWait exceeds limit"},
    {"level": "INFO", "taskId": "103", "time": "08:40:05", "msg": "任务 #103 已归档 → /openlist/tg-videos/202608/纪录片/EP03.mp4"},
    {"level": "WARN", "taskId": "106", "time": "08:41:20", "msg": "任务 #106 校验异常，已隔离"},
    {"level": "INFO", "taskId": "104", "time": "08:52:31", "msg": "任务 #104 校验中 (size 412MB)"},
]

ALERTS = [
    {"title": "FloodWait 触发", "time": "08:34:42", "color": "var(--wait)", "unread": True},
    {"title": "任务 #106 被隔离", "time": "08:36:10", "color": "var(--iso)", "unread": True},
    {"title": "任务 #107 上传失败", "time": "08:37:05", "color": "var(--err)", "unread": True},
    {"title": "任务 #101 下载完成", "time": "08:31:57", "color": "var(--ok)", "unread": False},
]

# 任务状态机六态 + 失败，保证每种至少一条
STATUS_OPTIONS = ["pending", "download", "downloaded", "verify", "upload", "archived", "isolated", "failed"]


def _stats():
    """模拟 dashboard 数据（与 bridge_server._dashboard_stats 同形：
    KPI 为累计下载/上传总量；摘要卡为实时上传/下载速率双色卡）。"""
    trend = [random.randint(0, 8) for _ in range(14)]
    kpis = [
        {"label": "累计下载", "icon": "ic-download", "value": "3.7", "unit": "GB", "id": "kpi-dl-total",
         "delta": "", "d": "flat", "spark": "#22d3ee", "points": _spark_points(trend)},
        {"label": "累计上传", "icon": "ic-upload", "value": "1.2", "unit": "GB", "id": "kpi-ul-total",
         "delta": "", "d": "flat", "spark": "#60a5fa", "points": _spark_points(trend[::-1])},
        {"label": "本地磁盘占用", "icon": "ic-hdd", "value": "312", "unit": "GB",
         "delta": "", "d": "flat", "spark": "#8b5cf6", "points": _spark_points([10.0] * 14)},
        {"label": "云端归档累计", "icon": "ic-cloud", "value": "1,286", "unit": "个",
         "delta": "", "d": "flat", "spark": "#34d399", "points": _spark_points(trend)},
    ]
    summary = [{
        "label": "实时速率",
        "href": "/tasks",
        "lines": [
            {"speed_key": "upload", "label": "上传", "value": "8.1 MB/s",
             "color": "#a78bfa", "icon": "ic-upload"},
            {"speed_key": "download", "label": "下载", "value": "12.4 MB/s",
             "color": "#22d3ee", "icon": "ic-download"},
        ],
    }]
    # 平滑曲线字段（points/raw/path 三元组）
    ul_pts, ul_raw, ul_path = _speed_chart_points(trend)
    dl_pts, dl_raw, dl_path = _speed_chart_points(trend[::-1])
    summary[0]["lines"][0].update({"points": ul_pts, "raw": ul_raw, "path": ul_path})
    summary[0]["lines"][1].update({"points": dl_pts, "raw": dl_raw, "path": dl_path})
    return {
        "kpis": kpis,
        "summary": summary,
        "total_bytes": 312 * (1 << 30),
        "trend_labels": [(dt.date.today() - dt.timedelta(days=d)).strftime("%m-%d") for d in range(6, -1, -1)],
        "trend_tasks": [random.randint(0, 6) for _ in range(7)],
        "trend_errors": [random.randint(0, 2) for _ in range(7)],
    }


def _spark_points(values):
    if not values:
        return "2,28 90,28"
    mx = max(values) or 1
    step = 88.0 / max(len(values) - 1, 1)
    pts = []
    for i, v in enumerate(values):
        x = 2 + i * step
        y = 28 - (v / mx) * 24
        pts.append(f"{x:.0f},{y:.0f}")
    return " ".join(pts)

def _speed_chart_points(values=None, width=460.0, height=120.0, is_upload=False, count=14):
    """与 core.templates._speed_chart_points 同形：返回 (points, raw, smooth_path)。
    假波形已按用户要求移除（空闲贴底平线），mock 用随机速率模拟真实流量。"""
    pad_x, pad_y = 8.0, 14.0
    usable_h = height - 2 * pad_y
    step = (width - 2 * pad_x) / max(count - 1, 1)
    vals = [float(v) for v in (values or [])]
    if len(vals) < count:
        vals = [0.0] * (count - len(vals)) + vals
    else:
        vals = vals[-count:]
    mx = max(vals) or 1.0
    coords = [(pad_x + i * step, (height - pad_y) - (v / mx) * usable_h)
              for i, v in enumerate(vals)]
    points = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    # Catmull-Rom 平滑 path
    d = ""
    if len(coords) >= 2:
        d = f"M {coords[0][0]:.1f} {coords[0][1]:.1f}"
        for i in range(len(coords) - 1):
            p0 = coords[i - 1] if i > 0 else coords[i]
            p1, p2 = coords[i], coords[i + 1]
            p3 = coords[i + 2] if i + 2 < len(coords) else p2
            c1x = p1[0] + (p2[0] - p0[0]) / 6.0
            c1y = p1[1] + (p2[1] - p0[1]) / 6.0
            c2x = p2[0] - (p3[0] - p1[0]) / 6.0
            c2y = p2[1] - (p3[1] - p1[1]) / 6.0
            d += f" C {c1x:.1f} {c1y:.1f} {c2x:.1f} {c2y:.1f} {p2[0]:.1f} {p2[1]:.1f}"
    return points, vals, d

# ---------------------------------------------------------------------
# 公共上下文
# ---------------------------------------------------------------------
def _ctx(request, page_id="", active_nav="", active_page="", extra=None):
    ctx = {
        "request": request,
        "page_id": page_id,
        "active_nav": active_nav,
        "active_page": active_page,
        "session_state": SESSION_STATE,
        "session_dot_class": SESSION_DOT[SESSION_STATE],
        "session_label": SESSION_LABEL[SESSION_STATE],
        "session_sub": "点击进入登录向导" if SESSION_STATE != "ok" else "账号正常",
        "tg_state": "ok",
        "tg_dot_class": "ok",
        "tg_label": "TG 已登录",
        "tg_sub": "账号正常",
        "tg_accounts": [{"id": 1, "name": "预览账号", "phone": "+8610000000000"}],
        "running_count": _running_count(),
        "local_unarchived": 4,
        "isolated_count": sum(1 for t in TASKS if t["status"] == "isolated"),
        "alert_count": sum(1 for a in ALERTS if a["unread"]),
        "alerts": ALERTS,
        "done_today": 68,
        "stats": _stats(),
        "now_fmt": time.strftime("%H:%M"),
    }
    if extra:
        ctx.update(extra)
    return ctx


# ---------------------------------------------------------------------
# 本地预览路由（9+1）
# ---------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "variant": "login", "error": error})


@app.get("/init", response_class=HTMLResponse)
async def init(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "variant": "init", "error": ""})


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    ctx = _ctx(request, "dashboard", "dashboard", extra={"recent_tasks": _recent_tasks()})
    return templates.TemplateResponse("dashboard.html", ctx)

_SIM_SPEED_STEP = 0

@app.get("/api/speeds")
async def api_speeds():
    """实时速率轮询端点：上传与下载动态相互高低交错。"""
    global _SIM_SPEED_STEP
    _SIM_SPEED_STEP += 1
    import math
    t = _SIM_SPEED_STEP * 0.45
    dl_wave = (math.sin(t) + 1.0) / 2.0
    up_wave = (math.sin(t + math.pi) + 1.0) / 2.0
    dl_mb = 3.5 + dl_wave * 22.0
    up_mb = 1.8 + up_wave * 18.0
    return {
        "ok": True,
        "download": {"bps": round(dl_mb * 1024 * 1024, 1), "label": f"{dl_mb:.1f} MB/s"},
        "upload": {"bps": round(up_mb * 1024 * 1024, 1), "label": f"{up_mb:.1f} MB/s"},
    }


@app.get("/tasks", response_class=HTMLResponse)
async def tasks(request: Request):
    ctx = _ctx(request, "tasks", "tasks", extra={"tasks": TASKS, "chat_sources": []})
    return templates.TemplateResponse("tasks.html", ctx)


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: int):
    task = next((t for t in TASKS if t["id"] == task_id), TASKS[0])
    ctx = _ctx(request, "task-detail", "tasks", extra={"task": task})
    return templates.TemplateResponse("task_detail.html", ctx)


@app.get("/library/local", response_class=HTMLResponse)
async def library_local(request: Request, category: str = ""):
    files = [f for f in LOCAL_FILES]
    if category:
        st = category.lower().strip()
        if st in ("unarchived", "pending", "not_archived"):
            files = [f for f in files if not (f.get("archive") and f.get("archive", {}).get("state") == "done")]
        elif st in ("archived", "done"):
            files = [f for f in files if f.get("archive") and f.get("archive", {}).get("state") == "done"]
    ctx = _ctx(request, "library-local", "library", "local", extra={
        "files": files,
        "total_size": _preview_sum_human(files),
        "filtered": bool(category),
        "selected_category": category,
        "unarchived_count": sum(1 for f in LOCAL_FILES if not (f.get("archive") and f.get("archive", {}).get("state") == "done")),
        "archived_count": sum(1 for f in LOCAL_FILES if f.get("archive") and f.get("archive", {}).get("state") == "done"),
        "chat_sources": [{"chatId": i, "title": c, "telegramId": 1} for i, c in enumerate(CHATS)],
    })
    return templates.TemplateResponse("library_local.html", ctx)


@app.get("/partials/local-files", response_class=HTMLResponse)
async def partial_local_files(request: Request, source: str = "", type: str = "", size: str = "", category: str = ""):
    """本地在存结果区局部刷新（htmx 假过滤，与 bridge /partials/local-files 同形）。"""
    filtered = [f for f in LOCAL_FILES]
    if source:
        filtered = [f for f in filtered if f["source"] == source]
    if type:
        filtered = [f for f in filtered if _preview_classify(f) == type]
    if size:
        filtered = [f for f in filtered if _preview_match_size(f, size)]
    if category:
        st = category.lower().strip()
        if st in ("unarchived", "pending", "not_archived"):
            filtered = [f for f in filtered if not (f.get("archive") and f.get("archive", {}).get("state") == "done")]
        elif st in ("archived", "done"):
            filtered = [f for f in filtered if f.get("archive") and f.get("archive", {}).get("state") == "done"]
    return templates.TemplateResponse("partials/_local_files.html", {
        "request": request,
        "files": filtered,
        "total_size": _preview_sum_human(filtered),
        "filtered": bool(source or type or size or category),
        "selected_category": category,
    })


@app.get("/library/cloud", response_class=HTMLResponse)
async def library_cloud(request: Request):
    ctx = _ctx(request, "library-cloud", "library", "cloud", extra={"cloud_files": CLOUD_FILES, "openlist_public_base": ""})
    return templates.TemplateResponse("library_cloud.html", ctx)


@app.get("/submit", response_class=HTMLResponse)
async def submit(request: Request):
    return templates.TemplateResponse("submit.html", _ctx(request, "submit", "submit"))


@app.get("/tg-login", response_class=HTMLResponse)
async def tg_login(request: Request):
    return templates.TemplateResponse("tg_login.html", _ctx(request, "tg-login", "account"))


@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    return templates.TemplateResponse("settings.html", _ctx(request, "settings", "settings"))


@app.get("/logs", response_class=HTMLResponse)
async def logs(request: Request):
    ctx = _ctx(request, "logs", "logs", extra={"seed_logs": SEED_LOGS})
    return templates.TemplateResponse("logs.html", ctx)


@app.get("/account", response_class=HTMLResponse)
async def account(request: Request):
    return templates.TemplateResponse("account.html", _ctx(request, "account", "account"))


@app.get("/profile", response_class=HTMLResponse)
async def profile(request: Request):
    return templates.TemplateResponse("account.html", _ctx(request, "account", "account"))


# ---------------------------------------------------------------------
# htmx 局部刷新（假过滤）— 服务端渲染任务表格
# ---------------------------------------------------------------------
@app.get("/partials/tasks", response_class=HTMLResponse)
async def partial_tasks(request: Request, status: str = "", source: str = "", date_from: str = "", date_to: str = ""):
    filtered = [t for t in TASKS]
    if status:
        if status in ("active", "unarchived", "hide_archived"):
            filtered = [t for t in filtered if t.get("status") != "archived"]
        else:
            filtered = [t for t in filtered if t.get("status") == status]
    if source:
        filtered = [t for t in filtered if t["source"] == source]
    # 返回不带 base 的局部模板
    return templates.TemplateResponse("partials/_tasks_table.html", {"request": request, "tasks": filtered})


# ---------------------------------------------------------------------
# SSE 模拟流（logs / tasks）
# ---------------------------------------------------------------------
_LOG_LEVELS = ["INFO", "INFO", "INFO", "WARN", "ERROR"]
_LOG_MSGS = [
    "任务 {id} 校验通过 md5={m}",
    "任务 {id} 写入磁盘 → {path}",
    "FloodWait 触发 ({sec}s)，任务 {id} 进入冷却",
    "任务 {id} 上传中 {pct}% ({loaded}/{size})",
    "任务 {id} 已归档 → {cloud}",
]


@app.get("/sse/logs")
async def sse_logs():
    async def gen():
        try:
            i = 0
            while True:
                i += 1
                level = random.choice(_LOG_LEVELS)
                t = random.choice(TASKS)
                payload = {
                    "level": level,
                    "taskId": str(t["id"]),
                    "msg": random.choice(_LOG_MSGS).format(id=t["id"], m=t["md5"][:8], path=t["local_path"],
                                                          sec=random.randint(10, 120),
                                                          pct=random.randint(5, 99),
                                                          loaded=t["loaded"], size=t["size"], cloud=t["cloud_path"]),
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                await asyncio.sleep(random.uniform(1.0, 2.5))
        except asyncio.CancelledError:
            return
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/logs.txt")
async def api_logs_txt():
    """导出模拟日志缓冲为纯文本（与 bridge_server 同形，预览下载按钮可用）。"""
    body = "\n".join(
        "{time} [{level}] {tid}{msg}".format(
            time=ln.get("time", "—"), level=ln["level"],
            tid=("[" + str(ln.get("taskId", "")) + "] ") if ln.get("taskId") else "",
            msg=ln["msg"])
        for ln in SEED_LOGS)
    return Response(content=(body or "(暂无日志)") + "\n",
                    media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="bridge-logs-preview.txt"'})


@app.get("/sse/tasks")
async def sse_tasks():
    async def gen():
        try:
            i = 0
            while True:
                i += 1
                t = random.choice(TASKS)
                payload = {"id": t["id"], "status": t["status"], "progress": t["progress"],
                           "filename": t["filename"]}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                await asyncio.sleep(random.uniform(2.0, 4.0))
        except asyncio.CancelledError:
            return
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/tasks")
async def post_task(request: Request):
    """提交下载（模拟入队）：fetch JSON 路径返回结果对象，与 bridge 行为一致。"""
    if "application/json" in request.headers.get("content-type", ""):
        return JSONResponse({"ok": True, "message": "已提交（预览模拟）"})
    return templates.TemplateResponse("partials/_tasks_table.html", {"request": request, "tasks": TASKS})


# ---------------------------------------------------------------------
# 浏览下载（/browse）预览 mock
# ---------------------------------------------------------------------
_BROWSE_TYPES = (
    ("document", "全部"),
    ("media", "媒体"),
    ("video", "视频"),
    ("audio", "音频"),
)
# 「图片」分类已按用户要求移除（与 bridge_server 侧 _BROWSE_TYPES 同步）
_BROWSE_TYPE_LABELS = {"video": "视频", "photo": "图片", "audio": "音频",
                       "document": "文档", "file": "文件", "animation": "动图"}
BROWSE_TREE = [
    {"telegramId": 8652569586, "name": "donk小", "chats": [
        {"chatId": 8652569586, "title": "收藏 (Saved Messages)", "saved": True},
        {"chatId": -1001695180959, "title": "NodeSeek官方频道", "saved": False},
        {"chatId": -1002575070088, "title": "FlyClash 交流群", "saved": False},
        {"chatId": 6549939461, "title": "SOSO搜搜", "saved": False},
        {"chatId": -1001242245865, "title": "书墨资源", "saved": False},
        {"chatId": 777000, "title": "Telegram", "saved": False},
    ]},
]

_BROWSE_MOCK_SPEC = [
    ("【示例】产品发布会完整录像 1080p.mp4", "video", 501120516, "idle", 1556),
    ("2026-08-28 会议记录扫描.pdf", "document", 2458624, "completed", 0),
    ("屏幕录制 2026-08-23 223204.mp4", "video", 83615039, "downloading", 512),
    ("壁纸合集 4K (12张).zip", "file", 1288490188, "idle", 0),
    ("语音消息.ogg", "audio", 3145728, "idle", 47),
    ("设计稿-终版.png", "photo", 6291456, "completed", 0),
    ("824是小桃呢-seg1.mp4", "video", 1313233646, "error", 0),
    ("字幕文件.srt", "file", 20480, "idle", 0),
    ("【示例】开箱评测.mp4", "video", 1108486126, "paused", 900),
    ("快照.jpg", "photo", 2097152, "idle", 0),
]


def _browse_mock_rows(cursor=0):
    """游标翻页 mock：cursor=0 → 前 6 条(next=8)；cursor=8 → 后 4 条(next=0)。"""
    base_msg = 3792699392
    start = 0 if not cursor else 6
    spec = _BROWSE_MOCK_SPEC[start:start + 6] if not cursor else _BROWSE_MOCK_SPEC[start:start + 4]
    rows = []
    for i, (name, ftype, size, dl, dur) in enumerate(spec):
        msg = base_msg - (start + i) * 1024
        rows.append({
            "fileId": 1400 + start + i, "messageId": msg, "chatId": 8652569586,
            "telegramId": 8652569586, "uniqueId": "AgADmock%02d" % (start + i),
            "name": name, "ext": (name.rsplit(".", 1)[-1] if "." in name else "file").upper()[:4],
            "size_str": ("%.1f GB" % (size / 1024**3)) if size >= 1024**3 else
                        ("%.1f MB" % (size / 1024**2)) if size >= 1024**2 else
                        ("%.0f KB" % (size / 1024)),
            "date_str": "2026-08-2%d 21:0%d" % (8 - (start + i) % 9, (start + i) % 10),
            "type": ftype, "type_label": _BROWSE_TYPE_LABELS.get(ftype, "文件"),
            "thumb": "", "dl": dl, "tr": "idle",
            "is_archived": False, "is_archiving": False, "cloud_path": "",
            "dur_str": "%d:%02d:%02d" % (dur // 3600, dur % 3600 // 60, dur % 60) if dur else "",
        })
    total = len(_BROWSE_MOCK_SPEC)
    nxt = 8 if not cursor else 0
    return rows, total, nxt


def _browse_ctx_files(tg, chat, type_="document", cursor=0):
    rows, total, nxt = _browse_mock_rows(cursor)
    if type_ == "media":
        rows = [r for r in rows if r["type"] in ("video", "photo", "audio")]
        total = len(rows)
        nxt = 0
    elif type_ in ("video", "photo", "audio"):
        rows = [r for r in rows if r["type"] == type_]
        total = len(rows)
        nxt = 0
    return rows, total, nxt


@app.get("/browse", response_class=HTMLResponse)
async def browse(request: Request, tg: str = "", chat: str = "", type: str = "document"):
    sel_tg = tg or "8652569586"
    sel_chat = chat or "8652569586"
    cur_title = "—"
    for acc in BROWSE_TREE:
        if str(acc.get("telegramId")) == str(sel_tg):
            for c in acc.get("chats", []):
                if str(c.get("chatId")) == str(sel_chat):
                    cur_title = str(c.get("title") or "聊天")
                    break
            break
    rows, total, nxt = _browse_ctx_files(sel_tg, sel_chat, type)
    ctx = _ctx(request, "browse", "browse", extra={
        "tree": BROWSE_TREE,
        "sel_tg": sel_tg,
        "sel_chat": sel_chat,
        "sel_type": type,
        "cur_title": cur_title,
        "browse_rows": rows,
        "browse_count": total,
        "browse_cursor": nxt,
        "browse_collapsed": 0,
        "browse_loaded": len(rows),
        "browse_types": _BROWSE_TYPES,
    })
    return templates.TemplateResponse("browse.html", ctx)


@app.get("/partials/browse-files", response_class=HTMLResponse)
async def partial_browse_files(request: Request, tg: str = "", chat: str = "",
                               type: str = "document", cursor: str = "0"):
    try:
        cur = int(cursor or 0)
    except ValueError:
        cur = 0
    rows, total, nxt = _browse_ctx_files(tg, chat, type, cur)
    return templates.TemplateResponse("partials/_browse_files.html", {
        "request": request, "browse_rows": rows, "browse_count": total,
        "browse_cursor": nxt, "sel_tg": tg, "sel_chat": chat, "sel_type": type,
        "browse_collapsed": 0, "browse_loaded": len(rows) + (6 if cur else 0),
    })


@app.post("/browse/download")
async def browse_download(request: Request):
    return JSONResponse({"ok": True, "count": 0, "message": "已提交（预览模拟）"})


@app.post("/alerts/read")
async def alerts_read(request: Request):
    """告警全部标记已读（预览模拟：内存置已读）。"""
    for a in ALERTS:
        a["unread"] = False
    return JSONResponse({"ok": True, "unread": 0})


# ---------------------------------------------------------------------
# OpenList 一键归档（预览模拟：无真实上传，仅让弹窗/按钮流程可演示）
# 真实实现在 bridge_server.py 的「OpenList 一键归档」区块。
# ---------------------------------------------------------------------
@app.get("/openlist/status")
async def _pv_openlist_status():
    return JSONResponse({"ok": True, "loggedIn": True, "username": "preview",
                         "baseUrl": "http://127.0.0.1:5244", "verified": True,
                         "loggedAt": time.time(), "message": ""})


@app.post("/openlist/login")
async def _pv_openlist_login(request: Request):
    try:
        raw = await request.json()
        body = raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        body = {}
    return JSONResponse({"ok": True, "username": body.get("username") or "admin",
                         "baseUrl": body.get("baseUrl") or "http://127.0.0.1:5244", "verified": True})


@app.get("/openlist/dirs")
async def _pv_openlist_dirs(path: str = "/"):
    if path in ("/", ""):
        return JSONResponse({"ok": True, "path": "/", "dirs": [
            {"name": "本地存储", "path": "/本地存储"},
            {"name": "阿里云盘", "path": "/阿里云盘"},
            {"name": "夸克网盘", "path": "/夸克网盘"},
        ]})
    return JSONResponse({"ok": True, "path": path, "dirs": [
        {"name": "tg-archive", "path": path.rstrip("/") + "/tg-archive"}]})


@app.post("/archive/start")
@app.post("/archive/batch")
async def _pv_archive_start(request: Request):
    try:
        raw = await request.json()
        body = raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        body = {}
    uids = body.get("uniqueIds")
    if not isinstance(uids, list):
        single = body.get("uniqueId")
        uids = [single] if single else []
    delete_local = bool(body.get("deleteLocal") or body.get("delete_local"))
    rdir = str(body.get("remoteDir") or "/阿里云盘/tg-archive")
    n = len(uids)
    jobs = []
    for u in uids:
        jobs.append({
            "id": f"pv-job-{random.randint(1000, 9999)}",
            "uniqueId": u,
            "filename": "预览文件.mp4",
            "remoteDir": rdir,
            "remotePath": f"{rdir}/预览文件.mp4",
            "state": "done",
            "progress": 100,
            "error": "",
            "deleteLocal": delete_local,
            "localDeleted": delete_local,
            "pillCls": "archived",
            "pillLabel": "已归档",
        })
    return JSONResponse({"ok": True, "started": n, "jobs": jobs, "errors": []})


@app.get("/archive/status")
async def _pv_archive_status():
    return JSONResponse({"ok": True, "jobs": []})


@app.post("/archive/cancel")
async def _pv_archive_cancel(request: Request):
    return JSONResponse({"ok": True, "job": None})


_PV_ARCHIVE_CONFIG = {
    "autoArchive": True,
    "defaultDir": "/阿里云盘/tg-archive",
    "policy": "overwrite",
    "deleteLocal": False,
    "stats": {"enqueued": 5, "done": 5, "failed": 0, "lastHitAt": time.time()}
}


@app.get("/archive/config")
async def _pv_archive_config_get():
    return JSONResponse({"ok": True, "config": _PV_ARCHIVE_CONFIG})


@app.post("/archive/config")
async def _pv_archive_config_post(request: Request):
    try:
        raw = await request.json()
    except Exception:
        raw = {}
    if isinstance(raw, dict):
        _PV_ARCHIVE_CONFIG.update(raw)
    return JSONResponse({"ok": True, "config": _PV_ARCHIVE_CONFIG})


@app.post("/archive/sweep")
async def _pv_archive_sweep():
    return JSONResponse({"ok": True, "enqueued": 0, "message": "预览环境扫描完成，暂无新任务"})


@app.post("/library/local/delete")
@app.post("/api/local/delete")
async def _pv_local_delete(request: Request):
    """本地文件单项与批量删除（预览模拟）"""
    try:
        raw = await request.json()
        body = raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        body = {}
    uids = body.get("uniqueIds")
    if not isinstance(uids, list):
        single = body.get("uniqueId")
        uids = [single] if single else []
    uids = [str(u) for u in uids if u]
    deleted = 0
    global LOCAL_FILES
    for u in uids:
        before = len(LOCAL_FILES)
        LOCAL_FILES = [f for f in LOCAL_FILES if str(f.get("unique_id") or f.get("_unique_id") or "") != u]
        if len(LOCAL_FILES) < before:
            deleted += 1
    return JSONResponse({
        "ok": True,
        "deleted": deleted,
        "deletedUids": uids,
        "errors": [],
        "message": f"成功删除 {deleted} 个本地文件"
    })


@app.post("/library/cloud/delete")
@app.post("/archive/cloud/delete")
async def _pv_cloud_delete(request: Request):
    """云端文件删除（预览模拟）"""
    try:
        raw = await request.json()
        body = raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        body = {}
    paths = body.get("remotePaths") or []
    if not isinstance(paths, list):
        p = body.get("remotePath") or body.get("cloudPath")
        paths = [p] if p else []
    jids = body.get("jobIds") or []
    if not isinstance(jids, list):
        j = body.get("jobId") or body.get("id")
        jids = [j] if j else []
    deleted = len(paths) or len(jids) or 0
    global CLOUD_FILES
    CLOUD_FILES = [f for f in CLOUD_FILES if f.get("cloud_path") not in paths and f.get("id") not in jids]
    return JSONResponse({
        "ok": True,
        "deleted": deleted,
        "cleanedJobs": deleted,
        "errors": [],
        "message": f"成功删除 {deleted} 个云端记录"
    })


@app.post("/library/cloud/clear-missing")
@app.post("/archive/cloud/clear-missing")
async def _pv_cloud_clear_missing():
    """清理已失效记录（预览模拟）"""
    global CLOUD_FILES
    before = len(CLOUD_FILES)
    CLOUD_FILES = [f for f in CLOUD_FILES if f.get("status") != "missing"]
    cleared = before - len(CLOUD_FILES)
    return JSONResponse({
        "ok": True,
        "cleared": cleared,
        "message": f"已清理 {cleared} 条云端失效记录"
    })


@app.post("/library/cloud/retrieve")
@app.post("/archive/cloud/retrieve")
async def _pv_cloud_retrieve(request: Request):
    """云端文件取回（预览模拟）"""
    try:
        raw = await request.json()
        body = raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        body = {}
    p = str(body.get("remotePath") or body.get("cloudPath") or "/阿里云盘/tg-archive/file.mp4")
    fn = p.split("/")[-1] or "file.mp4"
    job = {
        "id": f"pv-ret-{random.randint(1000, 9999)}",
        "remotePath": p,
        "filename": fn,
        "targetPath": f"/data/downloads/{fn}",
        "state": "done",
        "progress": 100,
        "sizeBytes": 1024 * 1024 * 50,
        "downloadedBytes": 1024 * 1024 * 50,
        "error": "",
        "createdAt": time.time(),
        "updatedAt": time.time(),
        "finishedAt": time.time(),
    }
    return JSONResponse({"ok": True, "job": job, "message": f"已将「{fn}」加入取回队列"})


@app.get("/library/cloud/retrieve/status")
@app.get("/archive/cloud/retrieve/status")
async def _pv_cloud_retrieve_status():
    return JSONResponse({"ok": True, "jobs": []})


@app.post("/library/cloud/retrieve/cancel")
async def _pv_cloud_retrieve_cancel():
    return JSONResponse({"ok": True, "message": "已取消"})


@app.get("/library/cloud/files")
@app.get("/archive/cloud/files")
async def _pv_cloud_files():
    return JSONResponse({"ok": True, "files": CLOUD_FILES})


@app.get("/openlist/direct-url")
async def _pv_openlist_direct_url(path: str = ""):
    raw = str(path or "").strip().replace("\\", "/")
    norm = posixpath.normpath("/" + raw).lstrip("/")
    if not norm or norm.startswith("..") or norm == ".":
        return JSONResponse({"ok": False, "message": "非法路径"}, status_code=400)
    clean_path = quote(norm, safe="/")
    return JSONResponse({"ok": True, "url": f"http://127.0.0.1:5244/{clean_path}"})


@app.get("/openlist/stream-url")
async def _pv_openlist_stream_url(path: str = ""):
    norm = posixpath.normpath("/" + str(path or "/test.mp4").strip().replace("\\", "/"))
    filename = posixpath.basename(norm)
    return JSONResponse({
        "ok": True,
        "url": f"http://127.0.0.1:5244/d{norm}",
        "directUrl": f"http://127.0.0.1:5244/d{norm}",
        "path": norm,
        "filename": filename
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("preview_server:app", host="127.0.0.1", port=8000, reload=False)
