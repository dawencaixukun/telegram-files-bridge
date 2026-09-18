# -*- coding: utf-8 -*-
"""
tests/test_archive_classification.py
====================================
验证云存储分类功能：
1. 本地在存资产将「未归档」内容与「已归档」内容严格划分为独立类别。
2. 云端归档支持独立归档目录隔离与按独立目录筛选。
3. 任务队列支持活跃任务筛选（隐藏已归档任务）。
"""
import os
import shutil
import tempfile
import unittest
import time
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
import bridge_server
from services.task_service import _filter_local_files


class TestArchiveClassification(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(bridge_server.app)
        self.token = bridge_server._make_portal_token()
        self.cookies = {bridge_server.PORTAL_COOKIE: self.token}

    def test_filter_local_files_classification(self):
        """测试 _filter_local_files 对未归档与已归档的独立分类逻辑"""
        sample_files = [
            {
                "filename": "video1.mp4",
                "source": "ChatA",
                "local_exists": True,
                "archive": None,  # 未归档
            },
            {
                "filename": "video2.mp4",
                "source": "ChatA",
                "local_exists": True,
                "archive": {"state": "uploading", "progress": 50},  # 归档中，属于未归档/进行中
            },
            {
                "filename": "video3.mp4",
                "source": "ChatB",
                "local_exists": True,
                "archive": {"state": "done", "remotePath": "/归档/2026/video3.mp4"},  # 已归档
            },
            {
                "filename": "video4.mp4",
                "source": "ChatB",
                "local_exists": True,
                "archive": {"state": "failed", "error": "timeout"},  # 失败，属于未归档类
            },
        ]

        # 1. 过滤「未归档」类别：应包含 video1, video2, video4
        unarchived = _filter_local_files(sample_files, archive_status="unarchived")
        unarchived_names = [f["filename"] for f in unarchived]
        self.assertEqual(len(unarchived), 3)
        self.assertIn("video1.mp4", unarchived_names)
        self.assertIn("video2.mp4", unarchived_names)
        self.assertIn("video4.mp4", unarchived_names)
        self.assertNotIn("video3.mp4", unarchived_names)

        # 2. 过滤「已归档」类别：应仅包含 video3
        archived = _filter_local_files(sample_files, archive_status="archived")
        archived_names = [f["filename"] for f in archived]
        self.assertEqual(len(archived), 1)
        self.assertIn("video3.mp4", archived_names)
        self.assertNotIn("video1.mp4", archived_names)

        # 3. 未传分类或全部：应包含所有
        all_files = _filter_local_files(sample_files, archive_status="")
        self.assertEqual(len(all_files), 4)

    def test_library_local_page_category_filter(self):
        """测试 /library/local 页面及 /partials/local-files 的 category 参数"""
        # 测试 /library/local 渲染包含分类下拉框
        resp = self.client.get("/library/local", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('name="category"', resp.text)
        # 本地在存页保持「全部资产 / 未归档 / 已归档」三项平铺筛选
        # （曾经的「已归档收纳文件夹」已按用户要求回退）
        self.assertIn('全部资产', resp.text)
        self.assertIn('⚡ 未归档', resp.text)
        self.assertIn('📦 已归档', resp.text)
        self.assertNotIn('archived-folder', resp.text)

        # 测试 /partials/local-files 支持 category 过滤
        resp_part = self.client.get("/partials/local-files?category=unarchived", cookies=self.cookies)
        self.assertEqual(resp_part.status_code, 200)

    def test_library_cloud_dedicated_directory_filter(self):
        """测试 /library/cloud 页面包含独立归档目录筛选控件与数据属性

        注意：必须像同类用例那样注入受控归档记录（_render_cloud），
        data-remotedir 属性是渲染在真实归档卡片上的。此前本用例直接请求页面、
        依赖生产数据目录里恰好残存的归档记录才通过 —— 一旦测试数据隔离生效
        （归档为 0 条），它就会假失败。
        """
        html = self._render_cloud(
            [self._row("onedrive")], {"ok": True, "mounts": ["onedrive"], "message": ""})
        self.assertIn('id="cloudRemoteDir"', html)
        self.assertIn('独立归档目录', html)
        self.assertIn('data-remotedir=', html)

    def _row(self, drive, status="archived", fname="v.mp4", rdir=None):
        rdir = rdir if rdir is not None else ("/%s/tg-archive" % drive)
        return {
            "id": "row-1", "filename": fname, "drive": drive, "status": status,
            "remote_dir": rdir, "cloud_path": rdir + "/" + fname,
            "remote_path": rdir + "/" + fname, "size": "1.0 KB",
            "archived_time": "09-10 15:00", "openlist_url": "",
        }

    def _render_cloud(self, rows, mounts_res):
        """以受控归档记录 + 受控挂载渲染 /library/cloud，保证测试不受真实状态影响。"""
        with patch("bridge_server._cloud_archive_rows", new=AsyncMock(return_value=rows)), \
             patch("bridge_server.openlist_mounts", new=AsyncMock(return_value=mounts_res)):
            resp = self.client.get("/library/cloud", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def test_cloud_drive_filter_uses_real_openlist_mounts(self):
        """网盘分类必须来自 OpenList 真实挂载，不得再出现硬编码网盘名。"""
        mounts = ["google", "onedrive", "ppan", "quark", "wopan"]
        html = self._render_cloud(
            [self._row("onedrive")], {"ok": True, "mounts": mounts, "message": ""})

        for m in mounts:
            self.assertIn('value="%s"' % m, html, f"真实挂载 {m} 应作为网盘分类选项")

        # 旧版硬编码的三个假网盘既不在真实挂载里、也不在记录里，必须彻底消失
        self.assertNotIn('value="Google Drive"', html)
        self.assertNotIn('value="阿里云盘"', html)
        # 旧版把 OneDrive 写成驼峰，真实挂载是小写 onedrive
        self.assertNotIn('value="OneDrive"', html)

    def test_cloud_drive_filter_degrades_without_fabricating(self):
        """OpenList 挂载读取失败时，只能退化为记录聚合，不得凭空造网盘。"""
        html = self._render_cloud(
            [self._row("onedrive")], {"ok": False, "mounts": [], "message": "未登录"})

        self.assertIn('id="cloudDrive"', html)
        # 记录里真实存在的网盘仍可作为选项
        self.assertIn('value="onedrive"', html)
        # 但绝不能凭空补上硬编码假网盘
        self.assertNotIn('value="Google Drive"', html)
        self.assertNotIn('value="阿里云盘"', html)
        self.assertNotIn('value="OneDrive"', html)
        # 降级时必须给出可见提示，而不是静默显示空列表
        self.assertIn("未能读取 OpenList 挂载", html)

    def test_cloud_status_class_filter_present_and_wired(self):
        """存档分类（全部/云端在存/云端失效）筛选控件存在且接入过滤逻辑。"""
        html = self._render_cloud(
            [self._row("onedrive", status="missing", fname="bad.mp4")],
            {"ok": True, "mounts": ["onedrive"], "message": ""})

        self.assertIn('id="cloudStatus"', html)
        self.assertIn('x-model="statusFilter"', html)
        self.assertIn('value="archived"', html)
        self.assertIn('value="missing"', html)
        # 行上必须带 data-status，过滤才有依据
        self.assertIn('data-status=', html)
        self.assertIn('data-status="missing"', html)

    def test_tasks_active_filter_hides_archived(self):
        """测试任务列表支持 active 状态过滤以隐藏已归档任务"""
        resp = self.client.get("/tasks", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('value="active"', resp.text)
        self.assertIn('⚡ 进行中 / 活跃任务', resp.text)

        # 局部刷新过滤已归档任务
        resp_part = self.client.get("/partials/tasks?status=active", cookies=self.cookies)
        self.assertEqual(resp_part.status_code, 200)

    def test_dashboard_kpi_total_cards(self):
        """总览页 KPI 行为「累计下载/累计上传」总量卡（服务端渲染，无轮询）"""
        resp = self.client.get("/", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("累计下载", resp.text)
        self.assertIn("累计上传", resp.text)
        self.assertIn('id="kpi-dl-total"', resp.text)
        self.assertIn('id="kpi-ul-total"', resp.text)
        self.assertNotIn("kpi-dl-speed", resp.text)

    def test_dashboard_summary_speed_dual_line_chart(self):
        """「实时速率」单卡双折线：上传紫 + 下载青同图，图例数字实时刷新"""
        resp = self.client.get("/", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        # 单卡双折线：两条 polyline（speed-line-upload/download）在同一 SVG
        self.assertIn('id="speed-chart"', resp.text)
        self.assertIn('id="speed-line-upload"', resp.text)
        self.assertIn('id="speed-line-download"', resp.text)
        self.assertIn("#a78bfa", resp.text)
        self.assertIn("#22d3ee", resp.text)
        # 图例数字元素（summary-speed-*）仍在
        self.assertIn('id="summary-speed-upload"', resp.text)
        self.assertIn('id="summary-speed-download"', resp.text)
        # 前端初始化依赖服务端下发的原始 bps 序列（data-raw）
        self.assertIn("data-raw=", resp.text)
        # 轮询脚本存在且指向速率端点
        self.assertIn("api/speeds", resp.text)

    def test_speed_chart_idle_is_flat_zero_baseline(self):
        """空闲时曲线必须是静止贴底平线 —— 严禁假波形（用户反馈的「没流量
        曲线还在动」缺陷回归）。"""
        from core.templates import _speed_chart_points

        # 全 0 序列：所有点 y 相同（贴底），曲线为水平直线
        pts, raw, path = _speed_chart_points([0.0] * 5)
        self.assertEqual(len(raw), 14)          # 满窗补 0
        ys = [p.split(",")[1] for p in pts.split()]
        self.assertEqual(len(set(ys)), 1)       # 同一水平线
        # 空序列同样为平线
        pts2, raw2, _ = _speed_chart_points([])
        ys2 = [p.split(",")[1] for p in pts2.split()]
        self.assertEqual(len(set(ys2)), 1)

    def test_speed_chart_real_data_normalized(self):
        """有真实速率时曲线按峰值归一化（峰值顶格、0 贴底、满窗 14 点）"""
        from core.templates import _speed_chart_points

        pts, raw, path = _speed_chart_points([0, 0, 100.0])
        self.assertEqual(len(raw), 14)
        self.assertEqual(raw[-1], 100.0)        # 最新点在末位
        ys = [float(p.split(",")[1]) for p in pts.split()]
        self.assertAlmostEqual(min(ys), 14.0, places=1)   # 峰值顶格（pad_y）
        self.assertAlmostEqual(max(ys), 106.0, places=1)  # 0 值贴底（H=120-pad_y=14）
        # 平滑 path：M 开头、C 段存在（Catmull-Rom 贝塞尔）
        self.assertTrue(path.startswith("M "))
        self.assertIn(" C ", path)

    def test_speed_chart_smooth_path_no_hard_corners(self):
        """平滑曲线契约：polyline 尖角已被 path 替代（用户反馈「过渡生硬」）。"""
        from core.templates import _speed_chart_points

        pts, raw, path = _speed_chart_points([0, 50, 0, 100, 0])
        self.assertTrue(path.startswith("M "))
        self.assertEqual(path.count(" C "), 13)  # 14 点 → 13 段贝塞尔
        # path 不含 polyline 的逗号点对语法
        self.assertNotIn(",", path)

    def test_dashboard_trend_chart_removed(self):
        """「近 14 日任务量 & FloodWait」图表及关联代码已按要求整体移除"""
        resp = self.client.get("/", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        for gone in ("trendChart", "近 14 日任务量", "chart.umd", "FloodWait 触发次数"):
            self.assertNotIn(gone, resp.text)

    def test_api_speeds_endpoint(self):
        """/api/speeds 实时速率端点：返回 download/upload 的 bps 与 label"""
        resp = self.client.get("/api/speeds", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("bps", data["download"])
        self.assertIn("label", data["download"])
        self.assertIn("bps", data["upload"])
        self.assertIn("label", data["upload"])

    def test_dashboard_kpi_sparklines_use_own_real_series(self):
        """每个 KPI 的迷你趋势必须来自自身真实序列，不得用别的指标顶替。"""
        import time as _t
        import asyncio as _aio
        from services.task_service import _dashboard_stats, _SPEED_HISTORY
        from core.templates import _spark_points

        now = _t.time()
        tasks = [
            {"status": "archived", "_size_bytes": 1 << 30, "_unique_id": "s1",
             "_download_status": "completed", "_date_ts": now},
        ]
        disk = {"used_gb": 10.0, "total_gb": 100.0, "pct": 10}
        # 磁盘趋势必须同时隔离持久化历史文件：_record_disk_sample 会写入
        # .disk_usage_history.json，跨次运行的真实历史样本（前几天的大值）
        # 会回放进 14 天窗口，让「全 10.0」的断言随机失败。
        from services import task_service as _ts
        orig_hist = _ts._DISK_HISTORY_FILE
        _ts._DISK_HISTORY_FILE = os.path.join(
            tempfile.mkdtemp(prefix="tg_spark_iso_"), ".hist.json")
        orig_speed_hist = list(_SPEED_HISTORY)
        _SPEED_HISTORY.clear()
        try:
            stats = _aio.run(_dashboard_stats(tasks, disk))
        finally:
            _ts._DISK_HISTORY_FILE = orig_hist
            _SPEED_HISTORY.clear()
            _SPEED_HISTORY.extend(orig_speed_hist)

        by_label = {k["label"]: k for k in stats["kpis"]}
        flat = _spark_points([0.0] * 14)

        # 1) 磁盘：必须是真实采样值 10.0 的曲线，而不是旧的「错误数逆序」0 序列
        self.assertEqual(by_label["本地磁盘占用"]["points"], _spark_points([10.0] * 14))
        self.assertNotEqual(by_label["本地磁盘占用"]["points"], flat)
        # 2) KPI 行为累计下载/上传总量卡（值+单位），速率移到摘要卡
        self.assertIn("累计下载", by_label)
        self.assertIn("累计上传", by_label)
        self.assertTrue(by_label["累计下载"]["unit"])
        self.assertTrue(by_label["累计上传"]["unit"])
        # 2b) 摘要为单卡双折线（lines 嵌套：上传紫 #a78bfa / 下载青 #22d3ee）
        self.assertEqual(len(stats["summary"]), 1)
        lines = {ln["speed_key"]: ln for ln in stats["summary"][0]["lines"]}
        self.assertEqual(lines["upload"]["color"], "#a78bfa")
        self.assertEqual(lines["download"]["color"], "#22d3ee")
        self.assertTrue(lines["upload"]["points"])
        self.assertTrue(lines["download"]["points"])
        # 3) 云端归档累计：当天有 1 条 done（仍按归档任务计数）
        self.assertEqual(by_label["云端归档累计"]["value"], "1")

    def test_disk_sample_records_and_replays_real_value(self):
        """磁盘占用样本应真实落盘并按天回放。"""
        import datetime as _dt
        from services import task_service as ts
        orig = ts._DISK_HISTORY_FILE
        tmpf = os.path.join(tempfile.mkdtemp(prefix="tg_disk_hist_"), ".hist.json")
        ts._DISK_HISTORY_FILE = tmpf
        try:
            ts._record_disk_sample(42.5)
            days = [_dt.date.today() - _dt.timedelta(days=d) for d in range(2, -1, -1)]
            series = ts._disk_trend(days, 0.0)
            self.assertEqual(len(series), 3)
            self.assertEqual(series[-1], 42.5, "最后一天应为刚记录的真实采样")
            # 更早的日期没有样本，沿用当天值而非凭空造 0
            self.assertEqual(series[0], 42.5)
        finally:
            ts._DISK_HISTORY_FILE = orig
            shutil.rmtree(os.path.dirname(tmpf), ignore_errors=True)

    # ------------------------------------------------------------------
    # 已归档收纳文件夹：默认只显未归档，已归档收进可展开文件夹
    # ------------------------------------------------------------------
    def _two_rows(self):
        return [
            {"_unique_id": "u-unarch", "filename": "unarch.mp4", "local_path": __file__,
             "_size_bytes": 10, "_download_status": "completed", "status": "downloaded",
             "_telegram_id": "1", "_file_id": 1},
            {"_unique_id": "u-arch", "filename": "arch.mp4", "local_path": __file__,
             "_size_bytes": 20, "_download_status": "completed", "status": "downloaded",
             "_telegram_id": "1", "_file_id": 2},
        ]

    def _with_archived_job(self, fn):
        job = {"id": "cls-job-1", "unique_id": "u-arch", "filename": "arch.mp4",
               "state": "done", "remote_path": "/onedrive/x/arch.mp4",
               "remote_dir": "/onedrive/x", "size_bytes": 20,
               "created_at": 1.0, "archived_at": 1.0, "delete_local": False}
        bridge_server._ARCHIVE_JOBS[job["id"]] = job
        try:
            with patch("bridge_server.tasks_all", new=AsyncMock(return_value=self._two_rows())):
                return fn()
        finally:
            bridge_server._ARCHIVE_JOBS.pop(job["id"], None)

    def test_local_page_has_no_archived_folder(self):
        """本地在存页不应再出现「已归档收纳文件夹」（该交互已按用户要求回退）。"""
        def body():
            return self.client.get("/library/local", cookies=self.cookies).text
        html = self._with_archived_job(body)

        for f in ("unarch.mp4", "arch.mp4"):
            self.assertIn(f, html, "本地页仍应平铺展示文件")
        self.assertNotIn('<details class="archived-folder">', html,
                         "本地页的已归档文件夹已回退，不应再出现")

    def test_cloud_page_groups_by_drive_folder(self):
        """云端归档页按「网盘」折叠为可展开文件夹，默认收起。"""
        rows = [
            self._row("onedrive", fname="a.mp4"),
            self._row("onedrive", fname="b.mp4"),
            self._row("google", fname="c.mp4"),
        ]
        html = self._render_cloud(
            rows, {"ok": True, "mounts": ["google", "onedrive"], "message": ""})

        self.assertEqual(html.count('data-drive-group='), 2, "应按网盘切出 2 个分组")
        self.assertIn('data-drive-group="onedrive"', html)
        self.assertIn('data-drive-group="google"', html)
        # 文件夹必须默认收起（无 open 属性）
        self.assertNotIn('<details class="archived-folder" open', html)
        # 组头应带数量与体积
        self.assertIn("2 个", html)
        self.assertIn("合计", html)

    def test_cloud_drive_folder_shows_missing_badge(self):
        """分组内若有云端失效条目，组头要给出可见提示。"""
        rows = [
            self._row("onedrive", fname="ok.mp4"),
            self._row("onedrive", status="missing", fname="gone.mp4"),
        ]
        html = self._render_cloud(
            rows, {"ok": True, "mounts": ["onedrive"], "message": ""})
        self.assertIn("失效 1", html, "组头应提示该盘有失效条目")

    def test_ctx_fallback_keeps_request_key(self):
        """_ctx 降级路径必须带 request，否则后端不可达时整页 500。"""
        import asyncio as _aio
        from services.task_service import _ctx

        with patch("services.task_service._session_state",
                   new=AsyncMock(side_effect=RuntimeError("backend down"))):
            ctx = _aio.run(_ctx(None, "library-local", "library", "local"))
        self.assertIn("request", ctx, "降级上下文缺少 request 会导致 TemplateResponse 抛错")
        self.assertEqual(ctx["session_state"], "warn")


if __name__ == "__main__":
    unittest.main()
