# -*- coding: utf-8 -*-
"""
tests/test_ext_m3u8.py — OmniSnag 扩展端 M3U8 远端下载契约测试套件
===================================================================
覆盖：
1. Token 鉴权（缺失/错误/正确）
2. resolve 探测（master 变体解析 / media 分片解析）
3. SSRF 拦截（127.0.0.1、169.254.169.254、10.0.0.1）
4. submit 提交与 URL 去重
5. tasks 看板列表与字段完整性
6. cancel 与 retry 的正常流程及 404 状态机约束
7. 全部网络出网与 ffmpeg 执行均采用 mock，不直连外网
"""

import asyncio
import os
import socket
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

import bridge_server as appmod
from services.m3u8_service import (
    _M3U8_TASKS,
    get_or_create_ext_token,
)


class TestExtM3U8Contract(unittest.TestCase):
    def setUp(self):
        # 确保每个测试运行在隔离干净的任务字典与有效 Token 下
        self.orig_tasks = dict(_M3U8_TASKS)
        _M3U8_TASKS.clear()
        self.token = get_or_create_ext_token()
        self.client = TestClient(appmod.app)
        self.headers = {"X-Ext-Token": self.token}

        # Mock socket.getaddrinfo 避免单测依赖真实公网 DNS
        self._orig_getaddrinfo = socket.getaddrinfo
        def _fake_getaddrinfo(host, port, *args, **kwargs):
            if host in ("127.0.0.1", "localhost"):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 0))]
            if host == "169.254.169.254":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port or 0))]
            if host == "10.0.0.1":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port or 0))]
            if host.startswith("192.168."):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port or 0))]
            if host.startswith("172.16."):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port or 0))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))]
        socket.getaddrinfo = _fake_getaddrinfo

    def tearDown(self):
        socket.getaddrinfo = self._orig_getaddrinfo
        _M3U8_TASKS.clear()
        _M3U8_TASKS.update(self.orig_tasks)

    # -----------------------------------------------------------------
    # 1. Token 鉴权测试
    # -----------------------------------------------------------------
    def test_auth_missing_token(self):
        """缺 Token 请求返回 401 JSON。"""
        resp = self.client.get("/api/ext/m3u8/tasks")
        self.assertEqual(resp.status_code, 401)
        data = resp.json()
        self.assertFalse(data.get("ok"))
        self.assertIn("Token", data.get("message", ""))

    def test_auth_invalid_token(self):
        """错误 Token 请求返回 401 JSON。"""
        resp = self.client.get("/api/ext/m3u8/tasks", headers={"X-Ext-Token": "invalid_wrong_token"})
        self.assertEqual(resp.status_code, 401)
        data = resp.json()
        self.assertFalse(data.get("ok"))
        self.assertIn("Token", data.get("message", ""))

    def test_auth_valid_token(self):
        """有效 Token 请求放行并返回 200。"""
        resp = self.client.get("/api/ext/m3u8/tasks", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("tasks", data)

    # -----------------------------------------------------------------
    # 2. SSRF 拦截测试
    # -----------------------------------------------------------------
    def test_ssrf_blocking(self):
        """私网、环回与链路本地地址被 SSRF 护栏拦截返回 400。"""
        targets = [
            "http://127.0.0.1/video.m3u8",
            "http://169.254.169.254/latest/meta-data",
            "http://10.0.0.1/live/stream.m3u8",
            "http://192.168.1.1/index.m3u8",
            "http://172.16.0.5/index.m3u8",
            "ftp://example.com/video.m3u8",
        ]
        for url in targets:
            # resolve 探测拦截
            resp = self.client.post("/api/ext/m3u8/resolve", json={"url": url}, headers=self.headers)
            self.assertEqual(resp.status_code, 400, f"URL {url} 应在 resolve 被拦截返回 400")
            self.assertFalse(resp.json().get("ok"))

            # submit 提交拦截
            resp_sub = self.client.post("/api/ext/m3u8/submit", json={"url": url}, headers=self.headers)
            self.assertEqual(resp_sub.status_code, 400, f"URL {url} 应在 submit 被拦截返回 400")
            self.assertFalse(resp_sub.json().get("ok"))

    # -----------------------------------------------------------------
    # 3. resolve 探测契约测试
    # -----------------------------------------------------------------
    @patch("httpx.AsyncClient.get")
    def test_resolve_master_playlist(self, mock_get):
        """master 主播放列表探测返回 kind: master 及变体列表。"""
        master_content = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1280000,RESOLUTION=1280x720,CODECS=\"avc1.4d401f\",NAME=\"720p\"\n"
            "720p/index.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2560000,RESOLUTION=1920x1080,CODECS=\"avc1.4d401f\",NAME=\"1080p\"\n"
            "1080p/index.m3u8\n"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = master_content
        mock_get.return_value = mock_resp

        resp = self.client.post(
            "/api/ext/m3u8/resolve",
            json={"url": "https://publichost.example.com/master.m3u8"},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("kind"), "master")
        variants = data.get("variants", [])
        self.assertEqual(len(variants), 2)
        self.assertEqual(variants[0]["name"], "720p")
        self.assertEqual(variants[0]["resolution"], "1280x720")
        self.assertEqual(variants[0]["bandwidth"], 1280000)
        self.assertEqual(variants[0]["url"], "https://publichost.example.com/720p/index.m3u8")
        self.assertEqual(variants[1]["name"], "1080p")
        self.assertEqual(variants[1]["bandwidth"], 2560000)

    @patch("httpx.AsyncClient.get")
    def test_resolve_media_playlist(self, mock_get):
        """media 播放列表探测返回 kind: media、分片数、时长及加密标识。"""
        media_content = (
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:2\n"
            "#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"\n"
            "#EXTINF:2.0,\n"
            "seg0.ts\n"
            "#EXTINF:2.5,\n"
            "seg1.ts\n"
            "#EXTINF:2.0,\n"
            "seg2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = media_content
        mock_get.return_value = mock_resp

        resp = self.client.post(
            "/api/ext/m3u8/resolve",
            json={"url": "https://publichost.example.com/media.m3u8"},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("kind"), "media")
        self.assertEqual(data.get("segments"), 3)
        self.assertAlmostEqual(data.get("duration_secs"), 6.5)
        self.assertTrue(data.get("encrypted"))
        self.assertFalse(data.get("init_segment"))

    # -----------------------------------------------------------------
    # 4. submit 提交与去重测试
    # -----------------------------------------------------------------
    @patch("services.m3u8_service._download_worker", new_callable=AsyncMock)
    def test_submit_and_duplicate(self, mock_worker):
        """首次提交创建任务，相同 URL 再次提交返回 duplicate: true 与同一 task_id。"""
        url = "https://publichost.example.com/sample_video.m3u8"
        resp1 = self.client.post(
            "/api/ext/m3u8/submit",
            json={"url": url, "title": "测试视频"},
            headers=self.headers,
        )
        self.assertEqual(resp1.status_code, 200)
        d1 = resp1.json()
        self.assertTrue(d1.get("ok"))
        self.assertFalse(d1.get("duplicate"))
        task_id = d1.get("task_id")
        self.assertTrue(task_id)
        self.assertEqual(d1.get("state"), "queued")

        # 再次提交同 URL
        resp2 = self.client.post(
            "/api/ext/m3u8/submit",
            json={"url": url, "title": "测试视频重发"},
            headers=self.headers,
        )
        self.assertEqual(resp2.status_code, 200)
        d2 = resp2.json()
        self.assertTrue(d2.get("ok"))
        self.assertTrue(d2.get("duplicate"))
        self.assertEqual(d2.get("task_id"), task_id)

    # -----------------------------------------------------------------
    # 5. tasks 字段完整性测试
    # -----------------------------------------------------------------
    def test_tasks_fields_completeness(self):
        """GET /api/ext/m3u8/tasks 返回的列表结构包含扩展端依赖的所有字段。"""
        _M3U8_TASKS["mock_tid_1"] = {
            "id": "mock_tid_1",
            "filename": "film.mp4",
            "state": "running",
            "total_segments": 100,
            "done_segments": 45,
            "downloaded_bytes": 50000000,
            "speed_bps": 12000000,
            "duration_secs": 200.0,
            "encrypted": True,
            "output_size": 0,
            "error": "",
            "updated_at": 1000.0,
        }

        resp = self.client.get("/api/ext/m3u8/tasks", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("active", data)
        self.assertEqual(data.get("active"), 1)
        tasks = data.get("tasks", [])
        self.assertEqual(len(tasks), 1)
        t = tasks[0]

        expected_fields = [
            "id", "filename", "state", "total_segments", "done_segments",
            "downloaded_bytes", "speed_bps", "duration_secs", "encrypted",
            "output_size", "error"
        ]
        for field in expected_fields:
            self.assertIn(field, t, f"任务对象缺少扩展所需字段: {field}")
        self.assertEqual(t["state"], "running")
        self.assertEqual(t["total_segments"], 100)
        self.assertEqual(t["done_segments"], 45)

    # -----------------------------------------------------------------
    # 6. cancel 与 retry 的 404 与正常分支测试
    # -----------------------------------------------------------------
    def test_cancel_and_retry_not_found(self):
        """不存在的 task_id 在 cancel 与 retry 均返回 404。"""
        resp_c = self.client.post(
            "/api/ext/m3u8/cancel",
            json={"task_id": "nonexistent_task_id"},
            headers=self.headers,
        )
        self.assertEqual(resp_c.status_code, 404)
        self.assertFalse(resp_c.json().get("ok"))

        resp_r = self.client.post(
            "/api/ext/m3u8/retry",
            json={"task_id": "nonexistent_task_id"},
            headers=self.headers,
        )
        self.assertEqual(resp_r.status_code, 404)
        self.assertFalse(resp_r.json().get("ok"))

    @patch("services.m3u8_service._download_worker", new_callable=AsyncMock)
    def test_cancel_and_retry_workflow(self, mock_worker):
        """创建任务 -> 取消 -> 重试状态流转完整验证。"""
        # 1. 提交排队
        url = "https://publichost.example.com/flow_video.m3u8"
        resp_sub = self.client.post(
            "/api/ext/m3u8/submit",
            json={"url": url, "title": "流转测试"},
            headers=self.headers,
        )
        task_id = resp_sub.json().get("task_id")

        # 2. 处于 queued 状态时重试不允许（返回 404）
        resp_bad_retry = self.client.post(
            "/api/ext/m3u8/retry",
            json={"task_id": task_id},
            headers=self.headers,
        )
        self.assertEqual(resp_bad_retry.status_code, 404)

        # 3. 取消任务
        resp_cancel = self.client.post(
            "/api/ext/m3u8/cancel",
            json={"task_id": task_id},
            headers=self.headers,
        )
        self.assertEqual(resp_cancel.status_code, 200)
        self.assertTrue(resp_cancel.json().get("ok"))

        # 查看状态已被标记为 cancelled
        t_data = _M3U8_TASKS.get(task_id)
        self.assertIsNotNone(t_data)
        assert t_data is not None
        self.assertEqual(t_data["state"], "cancelled")

        # 4. 已处于 cancelled 状态时再次取消不允许（返回 404）
        resp_cancel_again = self.client.post(
            "/api/ext/m3u8/cancel",
            json={"task_id": task_id},
            headers=self.headers,
        )
        self.assertEqual(resp_cancel_again.status_code, 404)

        # 5. 重试任务
        resp_retry = self.client.post(
            "/api/ext/m3u8/retry",
            json={"task_id": task_id},
            headers=self.headers,
        )
        self.assertEqual(resp_retry.status_code, 200)
        self.assertTrue(resp_retry.json().get("ok"))
        self.assertEqual(resp_retry.json().get("state"), "queued")
        self.assertEqual(_M3U8_TASKS[task_id]["state"], "queued")
