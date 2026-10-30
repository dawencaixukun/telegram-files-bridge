# -*- coding: utf-8 -*-
"""
tests/test_stream_traffic.py
============================
在线播放流量记账回归（用户需求：浏览标签在线播放的流量计入累计下载/上传，
且上传下载流量对等记账）：

1. /openlist/stream 代理端点存在且需要登录（未登录 401 JSON，不 302）；
2. _stream_stats_add 累加并落盘，_stream_stats_load 重启恢复；
3. _dashboard_stats 的累计下载/累计上传 KPI 双侧同时计入播放字节（对等）。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 隔离数据目录（导入 bridge 前设置）
_TMP = tempfile.mkdtemp(prefix="stream-stats-")
os.environ["TG_DATA_DIR"] = _TMP

import bridge_server as B
import core.state as st


class TestStreamTraffic(unittest.TestCase):

    def setUp(self):
        st._STREAM_STATS.clear()
        st._STREAM_STATS.update({"total_bytes": 0, "updated_at": 0.0})
        self.client = B.TestClient(B.app) if hasattr(B, "TestClient") else None
        if self.client is None:
            from fastapi.testclient import TestClient
            self.client = TestClient(B.app)
        self.token = B._make_portal_token()
        self.cookies = {B.PORTAL_COOKIE: self.token}

    def test_stream_endpoint_requires_auth_401(self):
        """未登录访问播放代理必须 401 JSON（/openlist/* 契约），不得 302 回 HTML。"""
        r = self.client.get("/openlist/stream", params={"path": "/onedrive/x.mp4"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json().get("ok"), False)

    def test_stream_stats_add_and_persist(self):
        """累加记账 + 落盘 + 重载恢复（bridge 重启不清零）。"""
        st._stream_stats_add(1000)
        st._stream_stats_add(2500)
        self.assertEqual(st._STREAM_STATS["total_bytes"], 3500)
        self.assertTrue(os.path.exists(st._STREAM_STATS_FILE))
        # 模拟重启：清空内存后从盘恢复
        st._STREAM_STATS.clear()
        st._STREAM_STATS.update({"total_bytes": 0, "updated_at": 0.0})
        st._stream_stats_load()
        self.assertEqual(st._STREAM_STATS["total_bytes"], 3500)

    def test_kpi_counts_stream_bytes_on_both_sides(self):
        """播放字节必须同时进累计下载与累计上传（对等记账）。"""
        import asyncio
        from services.task_service import _dashboard_stats
        st._STREAM_STATS["total_bytes"] = 2 * 1024 ** 3   # 2 GiB
        stats = asyncio.run(_dashboard_stats(
            [], {"used_gb": 1.0, "total_gb": 10.0, "pct": 10.0}))
        kpi = [k for k in stats["kpis"] if k.get("label") == "累计下载 / 上传"][0]
        dl = kpi["items"][0]
        ul = kpi["items"][1]
        self.assertEqual((dl["value"], dl["unit"]), (ul["value"], ul["unit"]),
                         "双侧必须对等：同一播放字节同时计入下载与上传")
        # 2 GiB 播放 + 0 基线 = 2.0 GB（_fmt_total_bytes 用 1000 进制 GB）
        self.assertEqual(str(dl["value"]), "2.0")
        self.assertEqual(dl["unit"], "GB")

    def test_kpi_zero_stream_is_noop(self):
        """无播放流量时 KPI 逻辑不受影响（0 字节是 no-op）。"""
        import asyncio
        from services.task_service import _dashboard_stats
        stats = asyncio.run(_dashboard_stats(
            [], {"used_gb": 1.0, "total_gb": 10.0, "pct": 10.0}))
        kpi = [k for k in stats["kpis"] if k.get("label") == "累计下载 / 上传"][0]
        dl = kpi["items"][0]
        ul = kpi["items"][1]
        # 无归档/下载记录基线为 0：双侧同为 0
        self.assertEqual((dl["value"], dl["unit"]), (ul["value"], ul["unit"]))


if __name__ == "__main__":
    unittest.main()
