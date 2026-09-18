# -*- coding: utf-8 -*-
"""
tests/test_download_speed.py
====================================
任务队列下载速率实时显示回归测试：

1. _enrich_download_speed 用相邻两次采样的 downloadedSize 差值算真实速率；
2. 首轮采样（无基准）不显示速率，不瞎猜；
3. 字节倒退（任务重开）不产生负速率；
4. _fmt_speed 人类可读格式化（MB/s / KB/s / B/s）；
5. /partials/tasks 页面渲染：下载中任务显示速率 + data-refresh 轮询标记；
6. 队列空闲时 data-refresh="0"，前端据此停表，不空转打后端。

历史缺陷：任务页此前只有「已下载 x%」，速率只能靠用户手动刷新整页
两次心算差值；且页面完全没有轮询机制。
"""
import os
import sys
import time
import unittest
import asyncio
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.task_service as ts
import bridge_server
from fastapi.testclient import TestClient


def _mk_task(uid, dl_bytes, status="download"):
    """构造一个最小但字段完整的任务字典。"""
    return {
        "_unique_id": uid, "id": uid, "time": "09-10 15:00", "source": "ChatA",
        "msg_id": 1, "filename": "v.mp4", "size": "10 MB", "status": status,
        "loaded": "50%", "progress": 50, "progress_label": "已下载",
        "local_path": "—", "source_url": "", "error_msg": "", "stages": [],
        "_telegram_id": "1", "_dl_bytes": dl_bytes, "_dl_sampled_at": time.time(),
    }


class TestFmtSpeed(unittest.TestCase):
    def test_fmt_speed_units(self):
        self.assertEqual(ts._fmt_speed(0), "0 B/s")
        self.assertEqual(ts._fmt_speed(512), "512 B/s")
        self.assertEqual(ts._fmt_speed(2048), "2 KB/s")
        self.assertEqual(ts._fmt_speed(1024 * 1024), "1.0 MB/s")
        self.assertEqual(ts._fmt_speed(5.5 * 1024 * 1024), "5.5 MB/s")


class TestEnrichDownloadSpeed(unittest.TestCase):
    def setUp(self):
        ts._DL_SPEED_STATE.clear()

    def tearDown(self):
        ts._DL_SPEED_STATE.clear()

    def test_first_sample_has_no_speed(self):
        """首轮无基准：不显示速率，但快照必须落盘供下一轮差值。"""
        t = _mk_task("u1", 1_000_000)
        ts._enrich_download_speed([t])
        self.assertIsNone(t.get("speed"))
        self.assertIsNone(t.get("speed_label"))
        self.assertIn("u1", ts._DL_SPEED_STATE)

    def test_second_sample_computes_speed(self):
        """有基准且字节增长：速率 = Δbytes/Δt。"""
        ts._DL_SPEED_STATE["u1"] = {"bytes": 0, "at": time.time() - 5.0}
        t = _mk_task("u1", 5_000_000)
        ts._enrich_download_speed([t])
        self.assertIsNotNone(t["speed"])
        self.assertAlmostEqual(t["speed"], 1_000_000, delta=10_000)
        # 标签随 dt 抖动（976/977 KB/s 量级），只断言量级与单位正确
        self.assertRegex(t["speed_label"], r"^\d+(\.\d)? (KB|MB)/s$")

    def test_bytes_regression_no_negative_speed(self):
        """字节倒退（任务重开/重下）：不显示负速率。"""
        ts._DL_SPEED_STATE["u1"] = {"bytes": 9_000_000, "at": time.time() - 5.0}
        t = _mk_task("u1", 1_000_000)
        ts._enrich_download_speed([t])
        self.assertIsNone(t.get("speed"))
        # 倒退后基准应更新为最新值
        self.assertEqual(ts._DL_SPEED_STATE["u1"]["bytes"], 1_000_000)

    def test_non_download_status_skipped(self):
        """只有下载中的任务才算速率；状态切走时清下载快照防泄漏。

        注意：上传任务的 UL 基准保留（归档任务无精确字节数时不动基准，
        避免任务重开时差分出速率尖峰），只清理 DL。
        """
        t = _mk_task("u1", 1_000_000, status="upload")
        ts._DL_SPEED_STATE["u1"] = {"bytes": 1, "at": time.time() - 5.0}
        ts._enrich_download_speed([t])
        self.assertIsNone(t.get("speed"))
        self.assertNotIn("u1", ts._DL_SPEED_STATE)

    def test_download_cleans_upload_snapshot(self):
        """状态切回下载时清上传快照。"""
        t = _mk_task("u1", 1_000_000, status="download")
        ts._UL_SPEED_STATE["u1"] = {"bytes": 1, "at": time.time() - 5.0}
        ts._DL_SPEED_STATE.clear()
        ts._enrich_download_speed([t])
        self.assertNotIn("u1", ts._UL_SPEED_STATE)


class TestTasksPageSpeedRender(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(bridge_server.app)
        self.token = bridge_server._make_portal_token()
        self.cookies = {bridge_server.PORTAL_COOKIE: self.token}
        ts._DL_SPEED_STATE.clear()
        # 任务列表有全局缓存（CACHE_TTL=8s），测试间必须隔离，
        # 否则上一个用例缓存的空闲列表会让本用例直接命中缓存
        self._cache_patcher = patch.object(ts, "_TASKS_CACHE", {"expire": 0.0, "value": None})
        self._cache_patcher.start()
        self.addCleanup(self._cache_patcher.stop)

    def tearDown(self):
        ts._DL_SPEED_STATE.clear()

    def test_partial_tasks_shows_speed_and_refresh_flag(self):
        """下载中任务的速率渲染到页面，且容器带 data-refresh="1"。"""
        ts._DL_SPEED_STATE["live-1"] = {"bytes": 1_000_000, "at": time.time() - 5.0}
        with patch("bridge_server._build_tasks",
                   new=AsyncMock(return_value=[_mk_task("live-1", 6_000_000)])):
            r = self.client.get("/partials/tasks", cookies=self.cookies)
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertIn('data-refresh="1"', html, "有进行中任务时应标记自动刷新")
        self.assertRegex(html, r"(MB/s|KB/s)", "应渲染出人类可读速率")
        self.assertIn("tasksPollTimer", html, "应包含轮询定时器脚本")

    def test_partial_tasks_idle_no_refresh(self):
        """队列全空闲：data-refresh="0"，前端据此停表。"""
        idle = _mk_task("done-1", 0, status="archived")
        with patch("bridge_server._build_tasks",
                   new=AsyncMock(return_value=[idle])):
            r = self.client.get("/partials/tasks", cookies=self.cookies)
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-refresh="0"', r.text)

    def test_tasks_all_enriches_speed_before_cache(self):
        """tasks_all 必须在写缓存前富化速率——缓存命中路径不再补算。"""
        ts._DL_SPEED_STATE["live-1"] = {"bytes": 0, "at": time.time() - 5.0}
        async def run():
            with patch.object(ts, "_TASKS_CACHE", {"expire": 0.0, "value": None}), \
                 patch("bridge_server._build_tasks",
                       new=AsyncMock(return_value=[_mk_task("live-1", 5_000_000)])):
                out = await ts.tasks_all()
                self.assertIsNotNone(out[0].get("speed_label"),
                                     "回源后速率应已算出")
                return out[0]
        t = asyncio.run(run())
        # 标签随 dt 抖动（976/977 KB/s 量级），只断言量级与单位正确
        self.assertRegex(t["speed_label"], r"^\d+(\.\d)? (KB|MB)/s$")


if __name__ == "__main__":
    unittest.main()
