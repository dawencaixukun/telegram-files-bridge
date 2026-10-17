# -*- coding: utf-8 -*-
"""
tests/test_drive_explorer.py — 针对 /explorer 原生网盘直连文件管理器与 API 的单元测试
================================================================================
覆盖：
1. /explorer 页面鉴权门禁（未登录 302 重定向到 /login）
2. /explorer 根路径展示所有已挂载网盘大卡片、常用网盘切换胶囊与 OpenList 跳转
3. /explorer 深层目录层级浏览、面包屑导航、视频缩略图与在线播放器结构
4. GET /api/explorer/list 目录数据拉取与关键词过滤
5. POST /api/explorer/rename 原地改名
6. POST /api/explorer/copy 跨盘复制与参数校验
7. POST /api/explorer/move 跨盘移动与参数校验
8. GET /api/explorer/stream-url 视频串流直链解析
9. GET /api/explorer/download 直链重定向下载
10. GET /api/explorer/tasks 实时跨盘传输任务进度轮询
11. preview_server 兼容性
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

import bridge_server
import preview_server
from core.auth import _make_portal_token, PORTAL_COOKIE, CSRF_COOKIE, CSRF_HEADER
from core.config import _ARCHIVE_FILE
from core.state import _reset_flood_wait


class TestDriveExplorer(unittest.TestCase):
    def setUp(self):
        self.bridge_client = TestClient(bridge_server.app)
        self.preview_client = TestClient(preview_server.app)
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_test_explorer_")
        self._orig_archive_file = _ARCHIVE_FILE
        self._reset_flood_wait = _reset_flood_wait
        _reset_flood_wait()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        _reset_flood_wait()

    def _auth_cookies(self):
        token = _make_portal_token()
        csrf = "csrf-token-explorer-test-xyz"
        return {
            PORTAL_COOKIE: token,
            CSRF_COOKIE: csrf,
        }, {
            CSRF_HEADER: csrf,
        }

    # -------------------------------------------------------------
    # 1. 页面路由与认证测试
    # -------------------------------------------------------------
    def test_explorer_page_auth_guard(self):
        """未登录用户访问 /explorer 应被 302 重定向到 /login。"""
        resp = self.bridge_client.get("/explorer", follow_redirects=False)
        self.assertIn(resp.status_code, (302, 401))

    def test_explorer_page_root_mounted_drives(self):
        """登录后访问 /explorer 根目录：展示所有已挂载网盘大卡片、快捷胶囊与导航高亮。"""
        cookies, _ = self._auth_cookies()

        mock_storages = [
            {"mount_path": "/onedrive", "name": "onedrive", "driver": "Onedrive", "display_name": "OneDrive", "status": "work", "status_label": "正常在线", "remark": "", "capacity_str": "就绪", "openlist_url": "http://127.0.0.1:5244/onedrive"},
            {"mount_path": "/wopan", "name": "wopan", "driver": "WoPan", "display_name": "沃家云盘", "status": "work", "status_label": "正常在线", "remark": "", "capacity_str": "就绪", "openlist_url": "http://127.0.0.1:5244/wopan"},
            {"mount_path": "/google", "name": "google", "driver": "GoogleDrive", "display_name": "Google Drive", "status": "work", "status_label": "正常在线", "remark": "", "capacity_str": "就绪", "openlist_url": "http://127.0.0.1:5244/google"},
            {"mount_path": "/quark", "name": "quark", "driver": "Quark", "display_name": "夸克网盘", "status": "work", "status_label": "正常在线", "remark": "", "capacity_str": "就绪", "openlist_url": "http://127.0.0.1:5244/quark"},
        ]
        with patch("services.openlist_service.openlist_storages", new=AsyncMock(return_value=mock_storages)), \
             patch("services.openlist_service._openlist_ready", new=AsyncMock(return_value=True)):
            resp = self.bridge_client.get("/explorer", cookies=cookies)
            self.assertEqual(resp.status_code, 200)
            html = resp.text

            # 页面标题与导航栏包含「浏览」
            self.assertIn("浏览 · TG 网盘文件管理器", html)
            self.assertIn('href="/explorer"', html)
            self.assertIn("全部网盘", html)

            # 网盘大卡片包含 OneDrive、沃家云盘、Google Drive、夸克网盘
            self.assertIn("OneDrive", html)
            self.assertIn("沃家云盘", html)
            self.assertIn("Google Drive", html)
            self.assertIn("夸克网盘", html)
            self.assertIn("进入网盘 →", html)

            # 常用网盘快速切换胶囊包含各网盘
            self.assertIn('class="explorer-capsules-bar"', html)
            self.assertIn("/explorer?path=/onedrive", html)
            self.assertIn("/explorer?path=/wopan", html)

            # 顶栏包含在 OpenList 打开、强制刷新缓存
            self.assertIn("__openExplorerInOpenList", html)
            self.assertIn("__refreshExplorerCache", html)
            self.assertIn("刷新缓存", html)

    def test_explorer_subfolder_and_video_player(self):
        """进入深层目录 /onedrive/yello：展示面包屑、视频缩略图、网盘徽章与视频播放器。"""
        cookies, _ = self._auth_cookies()

        mock_data = {
            "ok": True,
            "path": "/onedrive/yello",
            "parent_path": "/onedrive",
            "breadcrumbs": [
                {"name": "全部网盘", "path": "/", "is_last": False},
                {"name": "onedrive", "path": "/onedrive", "is_last": False},
                {"name": "yello", "path": "/onedrive/yello", "is_last": True},
            ],
            "entries": [
                {
                    "name": "clip.mp4",
                    "path": "/onedrive/yello/clip.mp4",
                    "is_dir": False,
                    "size": 1024 * 1024 * 50,
                    "size_human": "50.0 MB",
                    "modified": "2026-08-20 15:30",
                    "thumb": "https://thumb.example.com/clip.jpg",
                    "type": 2,
                    "is_video": True,
                    "ext": "MP4",
                    "drive": "onedrive",
                    "drive_label": "OneDrive",
                    "openlist_url": "http://127.0.0.1:5244/onedrive/yello/clip.mp4",
                },
                {
                    "name": "subfolder",
                    "path": "/onedrive/yello/subfolder",
                    "is_dir": True,
                    "size": 0,
                    "size_human": "—",
                    "modified": "2026-08-20 12:00",
                    "thumb": "",
                    "type": 1,
                    "is_video": False,
                    "ext": "DIR",
                    "drive": "onedrive",
                    "drive_label": "OneDrive",
                    "openlist_url": "http://127.0.0.1:5244/onedrive/yello/subfolder",
                }
            ],
            "dirs_count": 1,
            "files_count": 1,
            "total_size": 1024 * 1024 * 50,
            "total_size_human": "50.0 MB",
            "write": True,
            "message": "",
            "openlist_ready": True,
        }

        with patch("services.openlist_service.openlist_fs_list_all", new=AsyncMock(return_value=mock_data)), \
             patch("services.openlist_service._openlist_ready", new=AsyncMock(return_value=True)):
            resp = self.bridge_client.get("/explorer?path=/onedrive/yello", cookies=cookies)
            self.assertEqual(resp.status_code, 200)
            html = resp.text

            # 面包屑层级导航
            self.assertIn("全部网盘", html)
            self.assertIn("onedrive", html)
            self.assertIn("yello", html)

            # 视频卡片元素
            self.assertIn("clip.mp4", html)
            self.assertIn("https://thumb.example.com/clip.jpg", html)
            self.assertIn("[OneDrive]", html)
            self.assertIn("__playCloudVideo", html)
            self.assertIn("cloudVideoModal", html)
            self.assertIn("btnCopyStreamUrl", html)
            self.assertIn("btnOpenPotPlayer", html)
            self.assertIn("btnOpenVLC", html)

            # 操作栏按钮
            self.assertIn("data-cloud-copy-btn", html)
            self.assertIn("data-cloud-move-btn", html)
            self.assertIn("data-cloud-rename-btn", html)
            self.assertIn("/api/explorer/download?path=", html)
            self.assertIn("clip.mp4", html)

    # -------------------------------------------------------------
    # 2. API 接口测试
    # -------------------------------------------------------------
    def test_api_explorer_list(self):
        """GET /api/explorer/list 返回 JSON 目录列表与关键词搜索过滤。"""
        cookies, _ = self._auth_cookies()

        mock_res = {
            "ok": True,
            "path": "/onedrive",
            "entries": [
                {"name": "demo_video.mp4", "path": "/onedrive/demo_video.mp4", "is_dir": False},
                {"name": "notes.txt", "path": "/onedrive/notes.txt", "is_dir": False},
            ],
            "total": 2
        }
        with patch("services.openlist_service.openlist_fs_list_all", new=AsyncMock(return_value=mock_res)):
            # 全量
            r1 = self.bridge_client.get("/api/explorer/list?path=/onedrive", cookies=cookies)
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(len(r1.json().get("entries", [])), 2)

            # 搜索过滤
            r2 = self.bridge_client.get("/api/explorer/list?path=/onedrive&q=video", cookies=cookies)
            self.assertEqual(r2.status_code, 200)
            entries = r2.json().get("entries", [])
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["name"], "demo_video.mp4")

    def test_api_explorer_rename(self):
        """POST /api/explorer/rename 原地改名。"""
        cookies, headers = self._auth_cookies()

        # 参数缺失 400
        r_empty = self.bridge_client.post("/api/explorer/rename", json={}, cookies=cookies, headers=headers)
        self.assertEqual(r_empty.status_code, 400)

        # 模拟成功改名
        mock_rename = AsyncMock(return_value={"ok": True, "code": 200, "message": "已成功重命名为 new.mp4", "new_path": "/onedrive/new.mp4"})
        with patch("services.openlist_service.openlist_fs_rename_entry", new=mock_rename):
            r_ok = self.bridge_client.post(
                "/api/explorer/rename",
                json={"path": "/onedrive/old.mp4", "name": "new.mp4"},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(r_ok.status_code, 200)
            data = r_ok.json()
            self.assertTrue(data.get("ok"))
            self.assertIn("已成功重命名", data.get("message"))
            mock_rename.assert_awaited_once_with("/onedrive/old.mp4", "new.mp4")

    def test_api_explorer_copy(self):
        """POST /api/explorer/copy 跨盘复制。"""
        cookies, headers = self._auth_cookies()

        # 同目录直接拒绝
        r_same = self.bridge_client.post(
            "/api/explorer/copy",
            json={"src_dir": "/onedrive", "dst_dir": "/onedrive", "names": ["a.mp4"]},
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(r_same.status_code, 400)
        self.assertIn("相同", r_same.json().get("message", ""))

        # 成功调用
        mock_copy = AsyncMock(return_value={"ok": True, "code": 200, "message": "复制成功"})
        with patch("services.openlist_service._openlist_fs_copy", new=mock_copy):
            r_ok = self.bridge_client.post(
                "/api/explorer/copy",
                json={"src_dir": "/onedrive/yello", "dst_dir": "/wopan/movies", "names": ["clip.mp4"]},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(r_ok.status_code, 200)
            self.assertTrue(r_ok.json().get("ok"))
            mock_copy.assert_awaited_once_with("/onedrive/yello", "/wopan/movies", ["clip.mp4"])

    def test_api_explorer_move(self):
        """POST /api/explorer/move 跨盘移动。"""
        cookies, headers = self._auth_cookies()

        # 缺少 dst_dir
        r_nodst = self.bridge_client.post(
            "/api/explorer/move",
            json={"src_dir": "/onedrive", "names": ["a.mp4"]},
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(r_nodst.status_code, 400)

        # 成功调用
        mock_move = AsyncMock(return_value={"ok": True, "code": 200, "message": "移动成功"})
        with patch("services.openlist_service._openlist_fs_move", new=mock_move):
            r_ok = self.bridge_client.post(
                "/api/explorer/move",
                json={"src_path": "/onedrive/old.mp4", "dst_dir": "/google/archive"},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(r_ok.status_code, 200)
            self.assertTrue(r_ok.json().get("ok"))
            mock_move.assert_awaited_once_with("/onedrive", "/google/archive", ["old.mp4"])

    def test_api_explorer_stream_url_and_download(self):
        """GET /api/explorer/stream-url 与 /api/explorer/download。"""
        cookies, _ = self._auth_cookies()

        mock_stream = AsyncMock(return_value={
            "ok": True,
            "url": "https://cdn.example.com/stream/file.mp4",
            "directUrl": "http://127.0.0.1:5244/d/file.mp4",
            "path": "/onedrive/file.mp4",
            "filename": "file.mp4"
        })
        with patch("services.openlist_service.openlist_stream_url", new=mock_stream):
            # 获取播放串流
            r_stream = self.bridge_client.get("/api/explorer/stream-url?path=/onedrive/file.mp4", cookies=cookies)
            self.assertEqual(r_stream.status_code, 200)
            self.assertEqual(r_stream.json().get("url"), "https://cdn.example.com/stream/file.mp4")

            # 直链下载 302 重定向到 CDN
            r_dl = self.bridge_client.get("/api/explorer/download?path=/onedrive/file.mp4", cookies=cookies, follow_redirects=False)
            self.assertEqual(r_dl.status_code, 302)
            self.assertEqual(r_dl.headers.get("location"), "https://cdn.example.com/stream/file.mp4")

    def test_api_explorer_tasks_polling(self):
        """GET /api/explorer/tasks 进度条轮询接口。"""
        cookies, _ = self._auth_cookies()
        mock_tasks = {
            "ok": True,
            "copy": [],
            "move": [],
            "active_local": [{"id": "task_1", "action": "copy", "progress": 88.5, "state": "running"}],
            "total": 1
        }
        with patch("services.openlist_service.openlist_get_cloud_tasks", new=AsyncMock(return_value=mock_tasks)):
            resp = self.bridge_client.get("/api/explorer/tasks", cookies=cookies)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("total"), 1)
            self.assertEqual(data.get("active_local")[0]["progress"], 88.5)

    def test_preview_server_explorer_support(self):
        """预览服务支持 /explorer 与 /library/browse。"""
        resp_exp = self.preview_client.get("/explorer")
        self.assertEqual(resp_exp.status_code, 200)
        self.assertIn("浏览", resp_exp.text)
        self.assertIn("OneDrive", resp_exp.text)

        resp_sub = self.preview_client.get("/explorer?path=/onedrive")
        self.assertEqual(resp_sub.status_code, 200)
        self.assertIn("EP01.mp4", resp_sub.text)


if __name__ == "__main__":
    unittest.main()
