# -*- coding: utf-8 -*-
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

import bridge_server
import preview_server


class TestCloudBrowseAndTransfer(unittest.TestCase):
    def setUp(self):
        self.bridge_client = TestClient(bridge_server.app)
        self.preview_client = TestClient(preview_server.app)
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_test_browse_")
        self._orig_archive_jobs = dict(bridge_server._ARCHIVE_JOBS)
        self._orig_archive_file = bridge_server._ARCHIVE_FILE
        bridge_server._ARCHIVE_FILE = os.path.join(self.tmp_dir, ".test_archive_jobs.json")
        bridge_server._reset_flood_wait()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self._orig_archive_jobs)
        bridge_server._ARCHIVE_FILE = self._orig_archive_file
        bridge_server._reset_flood_wait()

    def _auth_cookies(self):
        token = bridge_server._make_portal_token()
        csrf = "test-csrf-token-browse-12345"
        return {
            bridge_server.PORTAL_COOKIE: token,
            bridge_server.CSRF_COOKIE: csrf,
        }, {
            bridge_server.CSRF_HEADER: csrf,
        }

    # ==========================================================
    # 1. /library/browse 与 /library/cloud 路由测试
    # ==========================================================
    def test_library_browse_route_and_elements(self):
        cookies, headers = self._auth_cookies()

        # 未登录请求 /library/browse 重定向到 /login
        unauth_resp = self.bridge_client.get("/library/browse", follow_redirects=False)
        self.assertIn(unauth_resp.status_code, (302, 401))

        # 已登录请求 /library/browse
        bridge_server._ARCHIVE_JOBS["job-test-card-1"] = {
            "id": "job-test-card-1",
            "state": "done",
            "filename": "EP01.mp4",
            "remote_path": "/onedrive/tg-archive/EP01.mp4",
            "remote_dir": "/onedrive/tg-archive",
            "size_bytes": 1024 * 1024 * 10,
            "drive": "onedrive",
            "status": "ok",
        }
        with patch.object(bridge_server, "openlist_mounts", new=AsyncMock(return_value={"ok": True, "mounts": ["onedrive", "wopan"]})), \
             patch("services.openlist_service._openlist_exists", new=AsyncMock(return_value=True)):
            resp = self.bridge_client.get("/library/browse", cookies=cookies)
            self.assertEqual(resp.status_code, 200)
            html = resp.text

            # 确认页面标题与导航栏包含「浏览」
            self.assertIn("浏览", html)
            self.assertTrue('href="/explorer"' in html or 'href="/library/browse"' in html)

            # 确认包含复制、移动按钮结构
            self.assertIn("data-cloud-copy-btn", html)
            self.assertIn("data-cloud-move-btn", html)
            self.assertIn("__copyCloudFile", html)
            self.assertIn("__moveCloudFile", html)

            # 确认包含实时进度条结构
            self.assertIn("cloud-transfer-progress", html)
            self.assertIn("data-transfer-bar", html)

            # 确认包含跨盘弹窗结构
            self.assertIn("showTransferModal", html)
            self.assertIn("executeTransfer", html)

        # 兼容请求 /library/cloud 依然 200 正常工作
        with patch.object(bridge_server, "openlist_mounts", new=AsyncMock(return_value={"ok": True, "mounts": ["onedrive"]})):
            resp_cloud = self.bridge_client.get("/library/cloud", cookies=cookies)
            self.assertEqual(resp_cloud.status_code, 200)

        # 预览服务同时支持 /library/browse
        resp_pv = self.preview_client.get("/library/browse")
        self.assertEqual(resp_pv.status_code, 200)
        self.assertIn("浏览", resp_pv.text)

    # ==========================================================
    # 2. GET /api/cloud/mounts 端点测试
    # ==========================================================
    def test_api_cloud_mounts(self):
        cookies, headers = self._auth_cookies()

        # 未登录拒绝
        unauth_resp = self.bridge_client.get("/api/cloud/mounts")
        self.assertEqual(unauth_resp.status_code, 401)

        # 正常返回挂载网盘列表
        with patch.object(bridge_server, "openlist_mounts", new=AsyncMock(return_value={"ok": True, "mounts": ["onedrive", "wopan", "quark"]})):
            resp = self.bridge_client.get("/api/cloud/mounts", cookies=cookies)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("mounts"), ["/onedrive", "/wopan", "/quark"])

        # 预览服务也支持 /api/cloud/mounts
        resp_pv = self.preview_client.get("/api/cloud/mounts")
        self.assertEqual(resp_pv.status_code, 200)
        self.assertTrue(resp_pv.json().get("ok"))

    # ==========================================================
    # 3. POST /api/cloud/copy 复制端点测试
    # ==========================================================
    def test_api_cloud_copy(self):
        cookies, headers = self._auth_cookies()

        # 3.1 参数校验
        r_empty = self.bridge_client.post("/api/cloud/copy", json={}, cookies=cookies, headers=headers)
        self.assertEqual(r_empty.status_code, 400)

        r_same = self.bridge_client.post(
            "/api/cloud/copy",
            json={"src_dir": "/onedrive", "dst_dir": "/onedrive", "names": ["test.mp4"]},
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(r_same.status_code, 400)
        self.assertIn("相同", r_same.json().get("message", ""))

        # 3.2 模拟 OpenList 成功调用
        mock_copy = AsyncMock(return_value={"ok": True, "code": 200, "message": "success", "data": {"tid": "123"}})
        with patch("services.openlist_service._openlist_fs_copy", new=mock_copy):
            r_ok = self.bridge_client.post(
                "/api/cloud/copy",
                json={"src_dir": "/onedrive", "dst_dir": "/wopan", "names": ["video1.mp4", "video2.mp4"]},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(r_ok.status_code, 200)
            d = r_ok.json()
            self.assertTrue(d.get("ok"))
            self.assertIn("成功提交复制任务", d.get("message"))
            self.assertEqual(d.get("src_dir"), "/onedrive")
            self.assertEqual(d.get("dst_dir"), "/wopan")
            self.assertEqual(d.get("names"), ["video1.mp4", "video2.mp4"])
            mock_copy.assert_awaited_once_with("/onedrive", "/wopan", ["video1.mp4", "video2.mp4"])

        # 3.3 支持单文件与 remotePath 格式
        mock_copy.reset_mock()
        with patch("services.openlist_service._openlist_fs_copy", new=mock_copy):
            r_single = self.bridge_client.post(
                "/api/cloud/copy",
                json={"remotePath": "/onedrive/movies/film.mkv", "dst_dir": "/wopan/movies"},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(r_single.status_code, 200)
            mock_copy.assert_awaited_once_with("/onedrive/movies", "/wopan/movies", ["film.mkv"])

        # 3.4 预览服务也支持 /api/cloud/copy
        r_pv = self.preview_client.post("/api/cloud/copy", json={"src_dir": "/a", "dst_dir": "/b", "names": ["f"]})
        self.assertEqual(r_pv.status_code, 200)
        self.assertTrue(r_pv.json().get("ok"))

    # ==========================================================
    # 4. POST /api/cloud/move 移动端点与记录回写测试
    # ==========================================================
    def test_api_cloud_move_and_archive_sync(self):
        cookies, headers = self._auth_cookies()

        # 4.1 参数校验：缺少 dst_dir
        r_nodst = self.bridge_client.post(
            "/api/cloud/move",
            json={"src_dir": "/onedrive", "names": ["test.mp4"]},
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(r_nodst.status_code, 400)

        # 4.2 准备已归档任务记录
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS["job-mov-1"] = {
            "id": "job-mov-1",
            "state": "done",
            "filename": "mov1.mp4",
            "remote_path": "/onedrive/tg-archive/mov1.mp4",
            "remote_dir": "/onedrive/tg-archive",
            "size_bytes": 1024 * 1024 * 10,
        }

        # 4.3 模拟移动成功并验证归档记录同步回写
        mock_move = AsyncMock(return_value={"ok": True, "code": 200, "message": "success"})
        with patch("services.openlist_service._openlist_fs_move", new=mock_move):
            r_mov = self.bridge_client.post(
                "/api/cloud/move",
                json={"src_dir": "/onedrive/tg-archive", "dst_dir": "/wopan/videos", "names": ["mov1.mp4"]},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(r_mov.status_code, 200)
            res = r_mov.json()
            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("updatedJobs"), 1)

            # 验证 _ARCHIVE_JOBS 中已回写新路径与新目录
            j = bridge_server._ARCHIVE_JOBS["job-mov-1"]
            self.assertEqual(j.get("remote_path"), "/wopan/videos/mov1.mp4")
            self.assertEqual(j.get("remote_dir"), "/wopan/videos")

        # 4.4 预览服务也支持 /api/cloud/move
        r_pv = self.preview_client.post(
            "/api/cloud/move",
            json={"src_dir": "/阿里云盘", "dst_dir": "/Google Drive", "names": ["EP01.mp4"]}
        )
        self.assertEqual(r_pv.status_code, 200)
        self.assertTrue(r_pv.json().get("ok"))

    # ==========================================================
    # 5. 进度条轮询接口测试 (/api/admin/task/{kind}/undone)
    # ==========================================================
    def test_task_progress_polling_endpoint(self):
        cookies, headers = self._auth_cookies()

        # 模拟 OpenList 返回未完成任务
        mock_tasks = [
            {
                "id": "tid-copy-99",
                "name": "copy [test.mp4] to [/wopan]",
                "progress": 42.5,
                "state": 1,
                "status": "running"
            }
        ]
        with patch("services.openlist_service._openlist_tasks_undone", new=AsyncMock(return_value=mock_tasks)):
            r = self.bridge_client.get("/api/admin/task/copy/undone", cookies=cookies)
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data.get("code"), 200)
            self.assertEqual(len(data.get("data")), 1)
            self.assertEqual(data.get("data")[0]["progress"], 42.5)

        # 汇总任务查询 /api/cloud/tasks
        with patch("services.openlist_service._openlist_tasks_undone", new=AsyncMock(return_value=[])):
            r_tasks = self.bridge_client.get("/api/cloud/tasks", cookies=cookies)
            self.assertEqual(r_tasks.status_code, 200)
            self.assertTrue(r_tasks.json().get("ok"))

    # ==========================================================
    # 6. Telegram Bot 命令与通知测试
    # ==========================================================
    def test_bot_cloudtasks_and_notifications(self):
        import asyncio
        from services.bot_command_service import (
            build_cloud_tasks_reply,
            build_cloud_tasks_panel,
            build_st_reply,
            build_st_panel,
            BOT_COMMANDS
        )
        from services.notification_service import (
            notify_cloud_transfer_start,
            notify_cloud_transfer_completed,
            notify_cloud_transfer_failed
        )

        # 6.1 BOT_COMMANDS 包含 cloudtasks
        cmd_names = [c["command"] for c in BOT_COMMANDS]
        self.assertIn("cloudtasks", cmd_names)

        # 6.2 无任务时的 /cloudtasks 回复
        reply_empty = asyncio.run(build_cloud_tasks_reply())
        self.assertIn("暂无正在执行", reply_empty)

        # 6.3 注入任务后的 /cloudtasks 回复
        from services.openlist_service import _CLOUD_TRANSFER_TASKS
        _CLOUD_TRANSFER_TASKS["test_task_1"] = {
            "id": "test_task_1",
            "action": "copy",
            "names": ["电影.mkv"],
            "dst_dir": "/wopan",
            "progress": 68.0,
            "state": "running"
        }
        try:
            reply_with_task = asyncio.run(build_cloud_tasks_reply())
            self.assertIn("电影.mkv", reply_with_task)
            self.assertIn("68.0%", reply_with_task)

            # 6.4 /st 总览包含跨盘传输统计
            st_text = asyncio.run(build_st_reply())
            self.assertIn("跨盘传输", st_text)
            self.assertIn("/cloudtasks", st_text)

            # 6.5 /st 面板快捷键包含 act:cloud_tasks
            _, st_kb = asyncio.run(build_st_panel())
            cb_list = [btn["callback_data"] for row in st_kb.get("inline_keyboard", []) for btn in row]
            self.assertIn("act:cloud_tasks", cb_list)
        finally:
            _CLOUD_TRANSFER_TASKS.pop("test_task_1", None)

        # 6.6 通知函数触发不抛异常
        with patch("services.notification_service._spawn_notification") as mock_spawn:
            notify_cloud_transfer_start("copy", "/onedrive", "/wopan", ["file1.mp4"])
            mock_spawn.assert_called_once()
            args, _ = mock_spawn.call_args
            self.assertEqual(args[0], "cloudTransfer")
            self.assertIn("复制已提交", args[1])

        with patch("services.notification_service._spawn_notification") as mock_spawn_done:
            notify_cloud_transfer_completed("move", "/onedrive", "/wopan", ["file1.mp4"], elapsed=15)
            mock_spawn_done.assert_called_once()
            args, _ = mock_spawn_done.call_args
            self.assertEqual(args[0], "cloudTransfer")
            self.assertIn("移动完成", args[1])

        with patch("services.notification_service._spawn_notification") as mock_spawn_fail:
            notify_cloud_transfer_failed("copy", "/onedrive", "/wopan", ["file1.mp4"], error="空间不足")
            mock_spawn_fail.assert_called_once()
            args, _ = mock_spawn_fail.call_args
            self.assertEqual(args[0], "cloudTransfer")
            self.assertIn("失败原因", args[1])


if __name__ == "__main__":
    unittest.main()
