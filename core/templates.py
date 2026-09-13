# -*- coding: utf-8 -*-
"""
core/templates.py — Jinja2 模板渲染配置、状态映射与过滤器
=========================================================
提供 Jinja2Templates 实例、静态资源处理与全局模板过滤器。
"""
from typing import Any, Dict, List, Optional, Tuple
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from core.config import (
    TEMPLATES_DIR, _JAVA_TO_UI, _fmt_size, _fmt_time, _fmt_dur
)


class CachedStaticFiles(StaticFiles):
    """带 Cache-Control 的静态文件（指纹不变的本地 vendor 脚本可长缓存）。"""

    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        if getattr(resp, "status_code", 500) == 200:
            resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp


templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.filters["fmt_size"] = _fmt_size
templates.env.filters["fmt_time"] = _fmt_time
templates.env.filters["fmt_dur"] = _fmt_dur


def map_status(java_status: Any) -> str:
    """Java download_status → 前端 status。无法识别时回退 pending。"""
    if java_status is None:
        return "pending"
    key = str(java_status).strip().lower()
    return _JAVA_TO_UI.get(key, "pending")


def _stages(status: str, rec: Optional[Dict[str, Any]] = None, arch_job: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """阶段时间线：用 FileRecord 真实时间戳与归档 job 填充。

    每个阶段是三态，而不是「完成 / 未完成」两态：
      done   — 已完成（实心圆点）
      active — 正在进行（实心圆点 + 呼吸动画，dur 显示当前百分比）
      idle   — 尚未开始（空心圆点）

    历史缺陷（用户可见）：下载/上传「进行中」也被标成 done，用户看到的是
    「上传已经是已完成状态」；更糟的是把上传的 6% 贴在下载阶段旁边，
    看起来像下载卡在 6%。所以进行中必须与已完成区分开。
    """
    rec = rec or {}
    date_v, start_v, comp_v = rec.get("date"), rec.get("startDate"), rec.get("completionDate")
    dl_status = str(rec.get("downloadStatus") or "").strip().lower()
    dl_dur = "—"
    if start_v and comp_v:
        try:
            dl_dur = _fmt_dur(float(comp_v) - float(start_v))
        except (TypeError, ValueError):
            dl_dur = "—"

    def _pct(done_bytes: Any, total_bytes: Any) -> Optional[int]:
        try:
            if total_bytes and done_bytes is not None:
                return max(0, min(100, int(float(done_bytes) / float(total_bytes) * 100)))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        return None

    dl_pct = _pct(rec.get("downloadedSize"), rec.get("size"))

    arch_state = str(arch_job.get("state") or "") if arch_job else ""
    arch_pct = int(arch_job.get("progress") or 0) if arch_job else 0
    arch_done = arch_state == "done" or status == "archived"
    arch_time = _fmt_time(arch_job.get("archived_at")) if (arch_job and arch_job.get("archived_at")) else ("—" if not arch_done else _fmt_time(comp_v))
    dl_done = dl_status == "completed" or status in ("downloaded", "upload", "archived")
    dl_active = (not dl_done) and (dl_status == "downloading" or status == "download" or (dl_pct is not None and dl_pct > 0))
    up_active = (not arch_done) and arch_state == "uploading"
    up_queued = (not arch_done) and arch_state == "queued"

    def _mk(name: str, kind: str, time_s: str, dur_s: str, pct: Optional[int] = None) -> Dict[str, Any]:
        return {"name": name, "done": "yes" if kind == "done" else "no",
                "state": kind, "time": time_s, "dur": dur_s, "pct": pct}

    base = [
        _mk("入队", "done", _fmt_time(date_v), "—"),
        _mk("下载", "done" if dl_done else ("active" if dl_active else "idle"),
            _fmt_time(start_v),
            dl_dur if dl_done else (("%d%%" % dl_pct) if (dl_active and dl_pct is not None) else "—"),
            dl_pct),
        _mk("校验", "done" if dl_done else "idle", "—", "—"),
        _mk("上传", "done" if arch_done else ("active" if (up_active or up_queued) else "idle"),
            "—",
            "排队中" if up_queued else (("%d%%" % arch_pct) if up_active else "—"),
            arch_pct if (up_active or up_queued) else None),
        _mk("归档", "done" if arch_done else "idle", arch_time, "—"),
    ]
    return base


templates.env.filters["stages"] = _stages


def _spark_points(values: List[int]) -> str:
    """把序列压成 90x30 sparkline 的 polyline points（归一化，全 0 时平线）。"""
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

def _speed_chart_smooth_path(values: List[float],
                             width: float = 460.0, height: float = 120.0,
                             pad_x: float = 8.0, pad_y: float = 14.0) -> str:
    """把归一化后的坐标序列转成 Catmull-Rom 平滑贝塞尔 path。

    历史缺陷：polyline 直连折点，速率突增/骤降时呈现生硬的尖角折线。
    Catmull-Rom 样条经过每个数据点且相邻段斜率连续，过渡自然。
    空闲（全 0 平线）时输出水平直线段，视觉完全静止。
    """
    if len(values) < 2:
        return ""
    pts = list(values)
    d = [f"M {pts[0][0]:.1f} {pts[0][1]:.1f}"]
    n = len(pts)
    for i in range(n - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1 = pts[i]
        p2 = pts[i + 1]
        p3 = pts[i + 2] if i + 2 < n else p2
        # Catmull-Rom → Bezier 控制点（张力 1/6）
        c1x = p1[0] + (p2[0] - p0[0]) / 6.0
        c1y = p1[1] + (p2[1] - p0[1]) / 6.0
        c2x = p2[0] - (p3[0] - p1[0]) / 6.0
        c2y = p2[1] - (p3[1] - p1[1]) / 6.0
        d.append(f"C {c1x:.1f} {c1y:.1f} {c2x:.1f} {c2y:.1f} {p2[0]:.1f} {p2[1]:.1f}")
    return " ".join(d)


def _speed_chart_points(values: Optional[List[float]] = None,
                        width: float = 460.0, height: float = 120.0,
                        is_upload: bool = False, count: int = 14) -> Tuple[str, List[float], str]:
    """生成速率曲线的归一化坐标、原始值序列与平滑 path。

    历史缺陷：空闲时曾用「假正弦波」填充曲线（两条线反向起伏假装有流量），
    用户看到没下载/上传时折线还在动，已按需求移除 —— 空闲时曲线必须
    静止贴底（全 0 平线），只有真实流量才让曲线起伏。

    返回 (points, raw_values, smooth_path)：
    - points：460x120 视区坐标串（pad_x=8, pad_y=14），峰值归一化，
      不足 count 补 0 到满窗；全 0/空序列为贴底平线；
    - raw_values：与 points 一一对应的原始 bps 序列（满窗 14 点，前补 0），
      供前端 data-raw 初始化 —— 服务端首屏几何与 JS 重绘共用同一套
      归一化，避免坐标系切换导致的跳变；
    - smooth_path：Catmull-Rom 平滑贝塞尔 path（polyline 尖角的替代）。
    """
    pad_x = 8.0
    pad_y = 14.0
    usable_w = width - 2 * pad_x
    usable_h = height - 2 * pad_y
    step = usable_w / max(count - 1, 1)

    vals = [float(v) for v in (values or [])]
    if len(vals) < count:
        vals = [0.0] * (count - len(vals)) + vals
    else:
        vals = vals[-count:]
    mx = max(vals) or 1.0

    coords = []
    for i, v in enumerate(vals):
        x = pad_x + i * step
        y = (height - pad_y) - (v / mx) * usable_h
        coords.append((x, y))
    points = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    return points, vals, _speed_chart_smooth_path(coords, width, height, pad_x, pad_y)




