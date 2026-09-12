# -*- coding: utf-8 -*-
import asyncio
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

import bridge_server
import preview_server


class TestLibraryEnhancements(unittest.TestCase):
    def setUp(self):
        self.bridge_client = TestClient(bridge_server.app)
        self.preview_client = TestClient(preview_server.app)
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_test_")
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
        csrf = "test-csrf-token-12345"
        return {
            bridge_server.PORTAL_COOKIE: token,
            bridge_server.CSRF_COOKIE: csrf,
        }, {
            bridge_server.CSRF_HEADER: csrf,
        }

    # ==========================================================
    # 1. 路径安全测试 (Path Traversal & Dangerous file deletion)
    # ==========================================================
    def test_safe_delete_security(self):
        async def run_test():
            # 1.1 拒绝受保护的代码和配置文件
            py_file = os.path.join(self.tmp_dir, "hack.py")
            with open(py_file, "w") as f:
                f.write("print(1)")
            ok, err = await bridge_server._safe_delete_local_path(py_file)
            self.assertFalse(ok)
            self.assertIn("受保护", err)

            json_file = os.path.join(self.tmp_dir, "config.json")
            with open(json_file, "w") as f:
                f.write("{}")
            ok, err = await bridge_server._safe_delete_local_path(json_file)
            self.assertFalse(ok)
            self.assertIn("受保护", err)

            # 1.2 拒绝点号开头的隐藏/系统内部文件
            dot_file = os.path.join(self.tmp_dir, ".bridge_secret")
            with open(dot_file, "w") as f:
                f.write("secret")
            ok, err = await bridge_server._safe_delete_local_path(dot_file)
            self.assertFalse(ok)
            self.assertIn("点号", err)

            # 1.3 拒绝目录
            dir_path = os.path.join(self.tmp_dir, "subdir")
            os.makedirs(dir_path, exist_ok=True)
            ok, err = await bridge_server._safe_delete_local_path(dir_path)
            self.assertFalse(ok)
            self.assertIn("不是普通文件", err)

            # 1.4 正常媒体文件允许删除
            media_file = os.path.join(self.tmp_dir, "video.mp4")
            with open(media_file, "wb") as f:
                f.write(b"video content")
            self.assertTrue(os.path.exists(media_file))
            ok, err = await bridge_server._safe_delete_local_path(media_file)
            self.assertTrue(ok)
            self.assertFalse(os.path.exists(media_file))

        asyncio.run(run_test())

    # ==========================================================
    # 2. OpenList 直达 URL 生成
    # ==========================================================
    def test_openlist_direct_url(self):
        cookies, headers = self._auth_cookies()
        # 2.1 单元测试函数
        url = bridge_server._openlist_direct_url("/阿里云盘/tg-archive/测试视频.mp4")
        self.assertIn("127.0.0.1:5244", url)
        self.assertIn("%E9%98%BF%E9%87%8C", url)
        self.assertIn("%E6%B5%8B%E8%AF%95", url)

        # 2.2 API 端点测试
        resp = self.bridge_client.get("/openlist/direct-url?path=/OneDrive/movie.mkv", cookies=cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["url"], "http://127.0.0.1:5244/OneDrive/movie.mkv")

        # 2.3 preview_server API 端点测试
        resp_pv = self.preview_client.get("/openlist/direct-url?path=/OneDrive/movie.mkv")
        self.assertEqual(resp_pv.status_code, 200)
        self.assertEqual(resp_pv.json()["url"], "http://127.0.0.1:5244/OneDrive/movie.mkv")

    # ==========================================================
    # 3. 门禁与 CSRF 校验测试
    # ==========================================================
    def test_auth_and_csrf_protection(self):
        # 3.1 未登录请求数据端点 -> 401 JSON
        resp = self.bridge_client.post("/library/local/delete", json={"uniqueId": "uid-1"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json(), {"ok": False, "message": "未登录"})

        resp = self.bridge_client.post("/library/cloud/delete", json={"remotePath": "/test"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json(), {"ok": False, "message": "未登录"})

        resp = self.bridge_client.post("/library/cloud/retrieve", json={"remotePath": "/test"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json(), {"ok": False, "message": "未登录"})

        # 3.2 登录但无 CSRF -> 403 Forbidden
        token = bridge_server._make_portal_token()
        resp = self.bridge_client.post(
            "/library/local/delete",
            json={"uniqueId": "uid-1"},
            cookies={bridge_server.PORTAL_COOKIE: token}
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("CSRF", resp.json()["message"])

        # 3.3 登录但 CSRF 不匹配 -> 403 Forbidden
        resp = self.bridge_client.post(
            "/library/local/delete",
            json={"uniqueId": "uid-1"},
            cookies={bridge_server.PORTAL_COOKIE: token, bridge_server.CSRF_COOKIE: "token-a"},
            headers={bridge_server.CSRF_HEADER: "token-b"}
        )
        self.assertEqual(resp.status_code, 403)

    # ==========================================================
    # 4. 本地文件删除接口 (单项 & 批量)
    # ==========================================================
    def test_library_local_delete(self):
        cookies, headers = self._auth_cookies()

        media1 = os.path.join(self.tmp_dir, "test1.mp4")
        media2 = os.path.join(self.tmp_dir, "test2.mp4")
        with open(media1, "wb") as f:
            f.write(b"content1")
        with open(media2, "wb") as f:
            f.write(b"content2")

        mock_tasks = [
            {"_unique_id": "uid-del-1", "local_path": media1, "filename": "test1.mp4", "_telegram_id": "1", "_file_id": 101},
            {"_unique_id": "uid-del-2", "local_path": media2, "filename": "test2.mp4", "_telegram_id": "1", "_file_id": 102},
        ]

        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=mock_tasks)), \
             patch.object(bridge_server.BACKEND, "remove_file", new=AsyncMock(return_value={"ok": True})):
            # 4.1 单项删除
            resp = self.bridge_client.post(
                "/library/local/delete",
                json={"uniqueId": "uid-del-1"},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["deleted"], 1)
            self.assertFalse(os.path.exists(media1))

            # 4.2 批量删除
            resp2 = self.bridge_client.post(
                "/library/local/delete",
                json={"uniqueIds": ["uid-del-2"]},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp2.status_code, 200)
            self.assertTrue(resp2.json()["ok"])
            self.assertFalse(os.path.exists(media2))

            # 4.3 别名接口 /api/local/delete
            resp3 = self.bridge_client.post(
                "/api/local/delete",
                json={"uniqueId": "uid-non-exist"},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp3.status_code, 200)
            self.assertFalse(resp3.json()["ok"])
            self.assertIn("找不到", resp3.json()["errors"][0]["message"])

        # 4.4 preview_server 本地删除测试
        resp_pv = self.preview_client.post("/library/local/delete", json={"uniqueIds": ["uid-000101", "uid-000102"]})
        self.assertEqual(resp_pv.status_code, 200)
        self.assertTrue(resp_pv.json()["ok"])
        self.assertEqual(resp_pv.json()["deleted"], 2)

    # ==========================================================
    # 5. 归档成功后自动删除本地文件 (delete_local) & 批量归档
    # ==========================================================
    def test_archive_with_delete_local(self):
        cookies, headers = self._auth_cookies()

        media = os.path.join(self.tmp_dir, "to_archive.mp4")
        with open(media, "wb") as f:
            f.write(b"x" * 1024)

        mock_task = {
            "_unique_id": "uid-arch-test-1",
            "local_path": media,
            "filename": "to_archive.mp4",
            "_size_bytes": 1024,
            "_download_status": "completed",
            "_telegram_id": "1",
            "_file_id": 201
        }

        # 5.1 POST /archive/start 和 /archive/batch 端点检查
        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=[mock_task])):
            resp = self.bridge_client.post(
                "/archive/start",
                json={
                    "uniqueIds": ["uid-arch-test-1"],
                    "remoteDir": "/阿里云盘/tg-archive",
                    "deleteLocal": True
                },
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["started"], 1)
            self.assertTrue(data["jobs"][0]["deleteLocal"])

        # 5.1.1 验证批量归档突破原 20 个限制（如已选 30 个文件）不报错且全部排队
        mock_tasks_30 = []
        uids_30 = []
        for i in range(30):
            uid_i = f"uid-arch-batch-{i}"
            uids_30.append(uid_i)
            mock_tasks_30.append({
                "_unique_id": uid_i,
                "local_path": media,
                "filename": f"video_{i:02d}.mp4",
                "_size_bytes": 1024,
                "_download_status": "completed",
                "_telegram_id": "1",
                "_file_id": 300 + i
            })
        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=mock_tasks_30)):
            resp_30 = self.bridge_client.post(
                "/archive/start",
                json={
                    "uniqueIds": uids_30,
                    "remoteDir": "/阿里云盘/tg-archive",
                    "deleteLocal": False
                },
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_30.status_code, 200)
            data_30 = resp_30.json()
            self.assertTrue(data_30["ok"], f"30个文件批量归档失败: {data_30.get('message')}")
            self.assertEqual(data_30["started"], 30)
            self.assertEqual(len(data_30["jobs"]), 30)

        # 5.2 验证归档成功后自动删除本地文件的执行体逻辑
        job = {
            "id": "test-job-del-local",
            "unique_id": "uid-arch-test-1",
            "filename": "to_archive.mp4",
            "size_bytes": 1024,
            "local_path": media,
            "remote_dir": "/阿里云盘/tg-archive",
            "remote_path": "/阿里云盘/tg-archive/to_archive.mp4",
            "policy": "overwrite",
            "delete_local": True,
            "state": "queued",
            "progress": 0,
            "error": "",
            "created_at": 0.0,
            "updated_at": 0.0,
            "archived_at": 0.0,
        }
        bridge_server._ARCHIVE_JOBS[job["id"]] = job

        async def fake_stat3(tok, path):
            # 新校验要求上传后回查云端真实大小；这里模拟已完整落盘
            return {"size": 1024, "raw": {"size": 1024}}

        async def run_worker():
            with patch("bridge_server._openlist_token", new=AsyncMock(return_value="fake-tok")), \
                 patch("bridge_server._openlist_mkdir_tree", new=AsyncMock()), \
                 patch("bridge_server._openlist_put_once", new=AsyncMock()), \
                 patch("bridge_server._openlist_stat", new=fake_stat3), \
                 patch.object(bridge_server.BACKEND, "remove_file", new=AsyncMock(return_value={"ok": True})):
                await bridge_server._archive_worker(job)

        asyncio.run(run_worker())

        self.assertEqual(job["state"], "done")
        self.assertTrue(job.get("local_deleted"))
        self.assertFalse(os.path.exists(media))

    # ==========================================================
    # 6. 云端删除 (POST /library/cloud/delete) & 清理失效记录
    # ==========================================================
    def test_cloud_delete_and_clear_missing(self):
        cookies, headers = self._auth_cookies()

        bridge_server._ARCHIVE_JOBS["job-c1"] = {
            "id": "job-c1",
            "unique_id": "uid-c1",
            "filename": "cloud1.mp4",
            "remote_dir": "/阿里云盘/tg-archive",
            "remote_path": "/阿里云盘/tg-archive/cloud1.mp4",
            "state": "done",
            "archived_at": 100.0,
            "created_at": 100.0,
        }
        bridge_server._ARCHIVE_JOBS["job-c2"] = {
            "id": "job-c2",
            "unique_id": "uid-c2",
            "filename": "cloud2.mp4",
            "remote_dir": "/阿里云盘/tg-archive",
            "remote_path": "/阿里云盘/tg-archive/cloud2.mp4",
            "state": "done",
            "archived_at": 101.0,
            "created_at": 101.0,
        }

        # 6.1 云端删除
        with patch("bridge_server._openlist_token", new=AsyncMock(return_value="fake-tok")), \
             patch.object(bridge_server._openlist_client, "post", new=AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"code": 200, "message": "success"}))):
            resp = self.bridge_client.post(
                "/library/cloud/delete",
                json={"remotePaths": ["/阿里云盘/tg-archive/cloud1.mp4"]},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["ok"])
            self.assertNotIn("job-c1", bridge_server._ARCHIVE_JOBS)
            self.assertIn("job-c2", bridge_server._ARCHIVE_JOBS)

        # 6.2 清理已失效记录
        with patch("bridge_server._cloud_archive_rows", new=AsyncMock(return_value=[
            {"id": "job-c2", "cloud_path": "/阿里云盘/tg-archive/cloud2.mp4", "status": "missing"}
        ])):
            resp = self.bridge_client.post(
                "/library/cloud/clear-missing",
                json={},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["ok"])
            self.assertEqual(resp.json()["cleared"], 1)
            self.assertNotIn("job-c2", bridge_server._ARCHIVE_JOBS)

        # 6.3 preview_server 对应端点测试
        resp_pv = self.preview_client.post("/library/cloud/delete", json={"remotePaths": ["/阿里云盘/tg-archive/纪录片/EP03.mp4"]})
        self.assertEqual(resp_pv.status_code, 200)
        self.assertTrue(resp_pv.json()["ok"])

        resp_pv_clear = self.preview_client.post("/library/cloud/clear-missing", json={})
        self.assertEqual(resp_pv_clear.status_code, 200)
        self.assertTrue(resp_pv_clear.json()["ok"])

    # ==========================================================
    # 7. 云端取回测试 (POST /library/cloud/retrieve)
    # ==========================================================
    def test_cloud_retrieve(self):
        cookies, headers = self._auth_cookies()

        target_file = os.path.join(self.tmp_dir, "ret_movie.mp4")
        bridge_server._ARCHIVE_JOBS["job-ret-1"] = {
            "id": "job-ret-1",
            "unique_id": "uid-ret-1",
            "filename": "ret_movie.mp4",
            "remote_dir": "/阿里云盘/tg-archive",
            "remote_path": "/阿里云盘/tg-archive/ret_movie.mp4",
            "local_path": target_file,
            "state": "done",
            "size_bytes": 100,
        }

        # 7.1 启动取回任务
        with patch("bridge_server._retrieve_worker", new=AsyncMock()):
            resp = self.bridge_client.post(
                "/library/cloud/retrieve",
                json={"remotePath": "/阿里云盘/tg-archive/ret_movie.mp4"},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            ret_job = data["job"]
            self.assertEqual(ret_job["filename"], "ret_movie.mp4")

            # 7.2 防重复测试 1：任务已在进行中 (queued/downloading)
            bridge_server._RETRIEVE_JOBS[ret_job["id"]]["state"] = "downloading"
            resp_dup = self.bridge_client.post(
                "/library/cloud/retrieve",
                json={"remotePath": "/阿里云盘/tg-archive/ret_movie.mp4"},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_dup.status_code, 200)
            self.assertTrue(resp_dup.json().get("alreadyRunning"))

            # 7.3 取消取回任务
            cancel_resp = self.bridge_client.post(
                "/library/cloud/retrieve/cancel",
                json={"jobId": ret_job["id"]},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(cancel_resp.status_code, 200)
            self.assertEqual(cancel_resp.json()["job"]["state"], "cancelled")

            # 7.4 防重复测试 2：本地文件已存在且未开启 overwrite
            with open(target_file, "wb") as f:
                f.write(b"already local content")
            resp_exists = self.bridge_client.post(
                "/library/cloud/retrieve",
                json={"remotePath": "/阿里云盘/tg-archive/ret_movie.mp4", "overwrite": False},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_exists.status_code, 200)
            self.assertTrue(resp_exists.json().get("alreadyExists"))

        # 7.5 查询取回状态
        st_resp = self.bridge_client.get(
            "/library/cloud/retrieve/status",
            cookies=cookies
        )
        self.assertEqual(st_resp.status_code, 200)
        self.assertTrue(st_resp.json()["ok"])

        # 7.6 验证 worker 下载写盘执行逻辑
        download_target = os.path.join(self.tmp_dir, "downloaded.mp4")
        ret_job_worker = {
            "id": "job-test-dl",
            "remote_path": "/阿里云盘/tg-archive/downloaded.mp4",
            "filename": "downloaded.mp4",
            "target_path": download_target,
            "state": "queued",
            "progress": 0,
            "size_bytes": 16,
            "downloaded_bytes": 0,
            "error": "",
            "created_at": 0.0,
            "updated_at": 0.0,
            "finished_at": 0.0,
        }

        class MockStreamResponse:
            status_code = 200
            headers = {"content-length": "16"}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def aiter_bytes(self, chunk_size=1024):
                yield b"hello world 1234"

        async def run_retrieve_worker():
            with patch("bridge_server._openlist_token", new=AsyncMock(return_value="fake-tok")), \
                 patch.object(bridge_server._openlist_client, "post", new=AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"code": 200, "data": {"url": "http://127.0.0.1:5244/d/test"}, "message": "success"}))), \
                 patch.object(bridge_server._openlist_upload_client, "stream", return_value=MockStreamResponse()):
                await bridge_server._retrieve_worker(ret_job_worker)

        asyncio.run(run_retrieve_worker())

        self.assertEqual(ret_job_worker["state"], "done")
        self.assertEqual(ret_job_worker["progress"], 100)
        self.assertTrue(os.path.exists(download_target))
        with open(download_target, "rb") as f:
            self.assertEqual(f.read(), b"hello world 1234")

        # 7.7 preview_server 取回测试
        pv_ret = self.preview_client.post("/library/cloud/retrieve", json={"remotePath": "/test.mp4"})
        self.assertEqual(pv_ret.status_code, 200)
        self.assertTrue(pv_ret.json()["ok"])

        # 7.8 preview_server 云端文件列表
        pv_files = self.preview_client.get("/library/cloud/files")
        self.assertEqual(pv_files.status_code, 200)
        self.assertTrue(pv_files.json()["ok"])
        self.assertGreater(len(pv_files.json()["files"]), 0)

    # ==========================================================
    # 8. 全局默认归档配置与自动转存 (下载完成自动归档到默认目录)
    # ==========================================================
    def test_default_archive_config_and_auto_sweep(self):
        cookies, headers = self._auth_cookies()

        # 8.1 GET /archive/config
        resp = self.bridge_client.get("/archive/config", cookies=cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertIn("config", data)

        # 8.2 POST /archive/config
        resp_post = self.bridge_client.post(
            "/archive/config",
            json={
                "autoArchive": True,
                "defaultDir": "/阿里云盘/自动归档默认目录",
                "policy": "skip",
                "deleteLocal": True,
            },
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(resp_post.status_code, 200)
        self.assertEqual(bridge_server._ARCHIVE_CONFIG["defaultDir"], "/阿里云盘/自动归档默认目录")
        self.assertTrue(bridge_server._ARCHIVE_CONFIG["autoArchive"])
        self.assertTrue(bridge_server._ARCHIVE_CONFIG["deleteLocal"])
        self.assertEqual(bridge_server._ARCHIVE_CONFIG["policy"], "skip")

        # 8.3 验证下载完成的任务在没有单独订阅规则时，也能按全局默认目录自动归档
        media = os.path.join(self.tmp_dir, "auto_default.mp4")
        with open(media, "wb") as f:
            f.write(b"auto content")

        mock_task = {
            "id": 888,
            "_unique_id": "uid-auto-default-test",
            "local_path": media,
            "filename": "auto_default.mp4",
            "_size_bytes": 100,
            "_download_status": "completed",
            "_telegram_id": "999",
            "_chat_id": "8888",
        }

        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=[mock_task])), \
             patch("bridge_server._openlist_ready", new=AsyncMock(return_value=True)), \
             patch("bridge_server._archive_worker", new=AsyncMock()):
            n = asyncio.run(bridge_server._auto_archive_sweep())
            self.assertEqual(n, 1)
            job = bridge_server._archive_latest_raw_of("uid-auto-default-test")
            self.assertIsNotNone(job)
            self.assertEqual(job["remote_dir"], "/阿里云盘/自动归档默认目录")
            self.assertEqual(job["remote_path"], "/阿里云盘/自动归档默认目录/auto_default.mp4")
            self.assertEqual(job["policy"], "skip")
            self.assertTrue(job["delete_local"])
            self.assertEqual(job["rule_id"], "default")

        # 8.4 POST /archive/sweep 端点测试
        with patch("bridge_server._openlist_ready", new=AsyncMock(return_value=True)), \
             patch("bridge_server._auto_archive_sweep", new=AsyncMock(return_value=0)):
            resp_sw = self.bridge_client.post("/archive/sweep", cookies=cookies, headers=headers)
            self.assertEqual(resp_sw.status_code, 200)
            self.assertTrue(resp_sw.json()["ok"])

    # ==========================================================
    # 9. 全局 uniqueId/指纹查重与资产智能关联测试 (Task t2)
    # ==========================================================
    def test_dedup_and_asset_association(self):
        cookies, headers = self._auth_cookies()

        # 准备测试文件
        local_media = os.path.join(self.tmp_dir, "local_in_stock.mp4")
        with open(local_media, "wb") as f:
            f.write(b"local media 123456")

        # 模拟任务列表
        mock_tasks = [
            {
                "id": 1001,
                "_unique_id": "uid-local-001",
                "uniqueId": "uid-local-001",
                "local_path": local_media,
                "filename": "local_in_stock.mp4",
                "_size_bytes": 18,
                "size": 18,
                "_download_status": "completed",
                "status": "completed",
                "_telegram_id": 1,
                "_chat_id": 100,
            },
            {
                "id": 1002,
                "_unique_id": "uid-downloading-002",
                "uniqueId": "uid-downloading-002",
                "local_path": "—",
                "filename": "in_progress.mp4",
                "_size_bytes": 1024,
                "size": 1024,
                "_download_status": "downloading",
                "status": "downloading",
                "progress": 45,
                "_telegram_id": 1,
                "_chat_id": 100,
            }
        ]

        # 模拟已归档至云端的任务
        bridge_server._ARCHIVE_JOBS["job-cloud-001"] = {
            "id": "job-cloud-001",
            "unique_id": "uid-cloud-001",
            "filename": "cloud_archived.mp4",
            "size_bytes": 2048,
            "remote_path": "/阿里云盘/Movies/cloud_archived.mp4",
            "state": "done",
            "archived_at": 1725280000.0,
            "created_at": 1725280000.0,
        }

        async def run_async_dedup_tests():
            # 9.1 _check_file_dedup 云端命中
            res_cloud = await bridge_server._check_file_dedup(
                unique_id="uid-cloud-001",
                filename="cloud_archived.mp4",
                size_bytes=2048,
                tasks_list=mock_tasks
            )
            self.assertTrue(res_cloud["duplicate"])
            self.assertEqual(res_cloud["duplicateType"], "cloud")
            self.assertIn("云端已于", res_cloud["message"])
            self.assertEqual(res_cloud["asset"]["cloudPath"], "/阿里云盘/Movies/cloud_archived.mp4")
            self.assertEqual(res_cloud["asset"]["drive"], "阿里云盘")
            self.assertTrue(any(a["type"] == "open_cloud" for a in res_cloud["actions"]))
            self.assertTrue(any(a["type"] == "force_download" for a in res_cloud["actions"]))

            # 9.2 _check_file_dedup 本地在存命中
            res_local = await bridge_server._check_file_dedup(
                unique_id="uid-local-001",
                filename="local_in_stock.mp4",
                size_bytes=18,
                tasks_list=mock_tasks
            )
            self.assertTrue(res_local["duplicate"])
            self.assertEqual(res_local["duplicateType"], "local")
            self.assertIn("本地在存", res_local["message"])
            self.assertEqual(res_local["asset"]["localPath"], local_media)
            self.assertTrue(res_local["asset"]["localExists"])
            self.assertTrue(any(a["type"] == "open_local" for a in res_local["actions"]))

            # 9.3 _check_file_dedup 任务队列命中
            res_task = await bridge_server._check_file_dedup(
                unique_id="uid-downloading-002",
                filename="in_progress.mp4",
                size_bytes=1024,
                tasks_list=mock_tasks
            )
            self.assertTrue(res_task["duplicate"])
            self.assertEqual(res_task["duplicateType"], "task")
            self.assertEqual(res_task["asset"]["taskId"], 1002)

            # 9.4 _check_file_dedup 未重复
            res_none = await bridge_server._check_file_dedup(
                unique_id="uid-new-file",
                filename="brand_new.mp4",
                size_bytes=4096,
                tasks_list=mock_tasks
            )
            self.assertFalse(res_none["duplicate"])
            self.assertEqual(res_none["duplicateType"], "none")

        asyncio.run(run_async_dedup_tests())

        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=mock_tasks)), \
             patch.object(bridge_server.BACKEND, "start_download_multiple", new=AsyncMock(return_value={"ok": True})):

            # 9.5 端点 POST /api/files/check-dedup 测试
            resp_chk = self.bridge_client.post(
                "/api/files/check-dedup",
                json={"uniqueId": "uid-cloud-001"},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_chk.status_code, 200)
            data_chk = resp_chk.json()
            self.assertTrue(data_chk["ok"])
            self.assertEqual(data_chk["data"]["duplicateType"], "cloud")

            # 9.6 /browse/download 拦截云端重复下载（无 force）
            resp_bd_cloud = self.bridge_client.post(
                "/browse/download",
                json={"files": [{
                    "telegramId": 1, "chatId": 100, "messageId": 10, "fileId": 101,
                    "uniqueId": "uid-cloud-001", "name": "cloud_archived.mp4", "size": 2048
                }]},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_bd_cloud.status_code, 200)
            data_bd_cloud = resp_bd_cloud.json()
            self.assertFalse(data_bd_cloud["ok"])
            self.assertEqual(data_bd_cloud["code"], "DUPLICATE_ASSET")
            self.assertEqual(data_bd_cloud["duplicateType"], "cloud")
            self.assertIn("云端已于", data_bd_cloud["message"])
            self.assertIn("asset", data_bd_cloud)

            # 9.7 /browse/download 拦截本地在存重复下载（无 force）
            resp_bd_local = self.bridge_client.post(
                "/browse/download",
                json={"files": [{
                    "telegramId": 1, "chatId": 100, "messageId": 11, "fileId": 102,
                    "uniqueId": "uid-local-001", "name": "local_in_stock.mp4", "size": 18
                }]},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_bd_local.status_code, 200)
            data_bd_local = resp_bd_local.json()
            self.assertFalse(data_bd_local["ok"])
            self.assertEqual(data_bd_local["code"], "DUPLICATE_ASSET")
            self.assertEqual(data_bd_local["duplicateType"], "local")
            self.assertIn("本地在存", data_bd_local["message"])

            # 9.8 /browse/download 携带 force: true 强制重新下载通过
            resp_bd_force = self.bridge_client.post(
                "/browse/download",
                json={
                    "force": True,
                    "files": [{
                        "telegramId": 1, "chatId": 100, "messageId": 10, "fileId": 101,
                        "uniqueId": "uid-cloud-001", "name": "cloud_archived.mp4", "size": 2048
                    }]
                },
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_bd_force.status_code, 200)
            data_bd_force = resp_bd_force.json()
            self.assertTrue(data_bd_force["ok"])
            self.assertEqual(data_bd_force["count"], 1)

            # 9.9 /api/tg/quick-download 拦截重复下载（返回 409 DUPLICATE_ASSET）
            resp_qd_dup = self.bridge_client.post(
                "/api/tg/quick-download",
                json={"files": [{
                    "telegramId": 1, "chatId": 100, "messageId": 10, "fileId": 101,
                    "uniqueId": "uid-cloud-001", "name": "cloud_archived.mp4", "size": 2048
                }]},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_qd_dup.status_code, 409)
            data_qd_dup = resp_qd_dup.json()
            self.assertFalse(data_qd_dup["ok"])
            self.assertEqual(data_qd_dup["code"], "DUPLICATE_ASSET")
            self.assertEqual(data_qd_dup["duplicateType"], "cloud")

            # 9.10 /api/tg/quick-download 携带 force: true 强制通过
            resp_qd_force = self.bridge_client.post(
                "/api/tg/quick-download",
                json={
                    "force": True,
                    "files": [{
                        "telegramId": 1, "chatId": 100, "messageId": 10, "fileId": 101,
                        "uniqueId": "uid-cloud-001", "name": "cloud_archived.mp4", "size": 2048
                    }]
                },
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_qd_force.status_code, 200)
            data_qd_force = resp_qd_force.json()
            self.assertTrue(data_qd_force["ok"])
            self.assertEqual(data_qd_force["count"], 1)

    # ==========================================================
    # 10. Telegram 消息通知外发引擎测试 (Task t3)
    # ==========================================================
    def test_telegram_notification_engine(self):
        cookies, headers = self._auth_cookies()

        # 10.1 读取与保存通知配置 (GET & POST /api/notify/config)
        resp_cfg = self.bridge_client.get("/api/notify/config", cookies=cookies)
        self.assertEqual(resp_cfg.status_code, 200)
        self.assertIn("config", resp_cfg.json())

        test_cfg = {
            "enabled": True,
            "channel": "both",
            "botToken": "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ",
            "chatId": "-100123456789",
            "minFileSizeMB": 50,
            "events": {
                "downloadCompleted": True,
                "archiveSuccess": True,
                "archiveFailed": True,
                "diskWatermarkAlert": True
            }
        }
        resp_post_cfg = self.bridge_client.post(
            "/api/notify/config",
            json=test_cfg,
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(resp_post_cfg.status_code, 200)
        saved_cfg = resp_post_cfg.json()["config"]
        self.assertTrue(saved_cfg["enabled"])
        self.assertEqual(saved_cfg["channel"], "both")
        self.assertEqual(saved_cfg["chatId"], "-100123456789")
        # Token 自动脱敏
        self.assertIn("******", saved_cfg["botToken"])
        self.assertTrue(saved_cfg["hasBotToken"])

        # 10.2 Telegram Bot 发送与防 SSRF 校验
        async def run_bot_tests():
            # 正常模拟发送成功
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"ok": True}))):
                ok, err = await bridge_server._send_via_bot(
                    "123456:ABC", "-100123", "<b>Hello</b>"
                )
                self.assertTrue(ok)
                self.assertEqual(err, "")

            # 防 SSRF 拦截：非 api.telegram.org 域名直接拒绝
            with patch("bridge_server.urlsplit", return_value=MagicMock(scheme="http", hostname="127.0.0.1")):
                ok, err = await bridge_server._send_via_bot(
                    "123456:ABC", "-100123", "<b>Hello</b>"
                )
                self.assertFalse(ok)
                self.assertIn("非法", err)

        asyncio.run(run_bot_tests())

        # 10.3 Saved Messages 发送测试
        async def run_saved_msg_tests():
            with patch("bridge_server.chat_sources", new=AsyncMock(return_value=[{"telegramId": 99999}])), \
                 patch("bridge_server.telegram_api_call", new=AsyncMock(return_value={"@type": "message"})):
                ok, err = await bridge_server._send_via_saved_messages("<b>Card Content</b>")
                self.assertTrue(ok)

        asyncio.run(run_saved_msg_tests())

        # 10.4 核心事件模型外发与排版卡片测试
        async def run_event_tests():
            dispatched = []

            async def mock_dispatch(event_type, html_content, **kwargs):
                dispatched.append((event_type, html_content))
                return {"ok": True}

            with patch("bridge_server._dispatch_notification", side_effect=mock_dispatch):
                # (a) 大文件下载完成事件 (<50MB 忽略，>=50MB 触发)
                small_task = {"filename": "small.txt", "_size_bytes": 1024 * 1024, "size": "1.0 MB"}
                bridge_server.notify_download_completed(small_task)
                self.assertEqual(len(dispatched), 0)

                big_task = {
                    "filename": "big_movie.mp4",
                    "_size_bytes": 100 * 1024 * 1024,
                    "size": "100.0 MB",
                    "source": "电影频道",
                    "local_path": "/app/data/big_movie.mp4"
                }
                bridge_server.notify_download_completed(big_task)
                await asyncio.sleep(0.01)
                self.assertEqual(len(dispatched), 1)
                self.assertEqual(dispatched[0][0], "downloadCompleted")
                self.assertIn("大文件下载完成", dispatched[0][1])
                self.assertIn("big_movie.mp4", dispatched[0][1])

                # (b) 归档成功事件
                arch_job_ok = {
                    "filename": "archived_video.mp4",
                    "size_bytes": 50000000,
                    "remote_path": "/阿里云盘/Movies/archived_video.mp4",
                    "created_at": time.time() - 30,
                    "archived_at": time.time(),
                }
                bridge_server.notify_archive_success(arch_job_ok)
                await asyncio.sleep(0.01)
                self.assertEqual(len(dispatched), 2)
                self.assertEqual(dispatched[1][0], "archiveSuccess")
                self.assertIn("网盘归档成功", dispatched[1][1])
                self.assertIn("阿里云盘", dispatched[1][1])

                # (c) 归档失败告警事件
                arch_job_err = {
                    "filename": "failed_video.mp4",
                    "remote_path": "/阿里云盘/Movies/failed_video.mp4",
                    "error": "RuntimeError: 401 Unauthorized token expired",
                }
                bridge_server.notify_archive_failed(arch_job_err)
                await asyncio.sleep(0.01)
                self.assertEqual(len(dispatched), 3)
                self.assertEqual(dispatched[2][0], "archiveFailed")
                self.assertIn("网盘归档失败告警", dispatched[2][1])
                self.assertIn("鉴权过期", dispatched[2][1])

                # (d) 磁盘水位熔断告警事件
                bridge_server._NOTIFY_LAST_DISK_ALERT = 0.0
                bridge_server.notify_disk_watermark_alert(88.5, 85.0, 10.2, 3)
                await asyncio.sleep(0.01)
                self.assertEqual(len(dispatched), 4)
                self.assertEqual(dispatched[3][0], "diskWatermarkAlert")
                self.assertIn("VPS 磁盘高水位熔断告警", dispatched[3][1])
                self.assertIn("88.5%", dispatched[3][1])

        asyncio.run(run_event_tests())

        # 10.5 一键测试推送 API (POST /api/notify/test)
        with patch("bridge_server._dispatch_notification", new=AsyncMock(return_value={"ok": True, "results": {"bot": True}})):
            resp_test = self.bridge_client.post(
                "/api/notify/test",
                json={"channel": "bot"},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_test.status_code, 200)
            self.assertTrue(resp_test.json()["ok"])
            self.assertIn("成功", resp_test.json()["message"])

    # ==========================================================
    # 11. 跨库全局聚合搜索测试 (Task t4)
    # ==========================================================
    def test_search_aggregate_cross_library(self):
        cookies, headers = self._auth_cookies()

        local_file = os.path.join(self.tmp_dir, "debian_server_2026.iso")
        with open(local_file, "wb") as f:
            f.write(b"iso content")

        mock_tasks = [
            {
                "id": 2001,
                "_unique_id": "uid-task-debian",
                "uniqueId": "uid-task-debian",
                "filename": "debian_live_installer.iso",
                "source": "Linux Mirrors",
                "status": "downloading",
                "progress": 68,
                "size": "1.2 GB",
                "_size_bytes": 1200000000,
                "_download_status": "downloading",
            },
            {
                "id": 2002,
                "_unique_id": "uid-local-debian",
                "uniqueId": "uid-local-debian",
                "filename": "debian_server_2026.iso",
                "source": "Debian Releases",
                "status": "completed",
                "progress": 100,
                "local_path": local_file,
                "size": "850 MB",
                "_size_bytes": 850000000,
                "_download_status": "completed",
            },
        ]

        bridge_server._ARCHIVE_JOBS["job-cloud-deb"] = {
            "id": "job-cloud-deb",
            "unique_id": "uid-cloud-debian",
            "filename": "debian_arm64_cloud.iso",
            "size_bytes": 600000000,
            "remote_path": "/阿里云盘/ISOs/debian_arm64_cloud.iso",
            "remote_dir": "/阿里云盘/ISOs",
            "state": "done",
            "archived_at": 1725280000.0,
            "created_at": 1725280000.0,
        }

        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=mock_tasks)):
            # 11.1 空查询返回 0 条
            resp_empty = self.bridge_client.get("/api/search", cookies=cookies)
            self.assertEqual(resp_empty.status_code, 200)
            self.assertEqual(resp_empty.json()["total"], 0)

            # 11.2 全局聚合检索 "debian"：同时命中任务、本地与云端三库
            resp_all = self.bridge_client.get("/api/search?q=debian", cookies=cookies)
            self.assertEqual(resp_all.status_code, 200)
            data = resp_all.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["query"], "debian")
            self.assertEqual(data["counts"]["tasks"], 1)
            self.assertEqual(data["counts"]["local"], 1)
            self.assertEqual(data["counts"]["cloud"], 1)
            self.assertEqual(data["total"], 3)

            # 校验任务项
            item_task = data["results"]["tasks"][0]
            self.assertEqual(item_task["id"], 2001)
            self.assertEqual(item_task["progress"], 68)
            self.assertEqual(item_task["status"], "downloading")

            # 校验本地在存项
            item_local = data["results"]["local"][0]
            self.assertEqual(item_local["uniqueId"], "uid-local-debian")
            self.assertEqual(item_local["localPath"], local_file)

            # 校验云端项
            item_cloud = data["results"]["cloud"][0]
            self.assertEqual(item_cloud["drive"], "阿里云盘")
            self.assertIn("127.0.0.1:5244", item_cloud["openlistUrl"])

            # 11.3 别名路由 GET /api/search/aggregate 验证
            resp_alias = self.bridge_client.get("/api/search/aggregate?q=arm64", cookies=cookies)
            self.assertEqual(resp_alias.status_code, 200)
            data_alias = resp_alias.json()
            self.assertEqual(data_alias["counts"]["cloud"], 1)
            self.assertEqual(data_alias["counts"]["tasks"], 0)
            self.assertEqual(data_alias["counts"]["local"], 0)

            # 11.4 多关键词空格 AND 匹配（"debian server"）
            resp_and = self.bridge_client.get("/api/search?q=debian%20server", cookies=cookies)
            self.assertEqual(resp_and.status_code, 200)
            self.assertEqual(resp_and.json()["counts"]["local"], 1)
            self.assertEqual(resp_and.json()["counts"]["tasks"], 0)

    # ==========================================================
    # 12. 归档失败智能归类与一键批量重试测试 (Task t5)
    # ==========================================================
    def test_archive_failure_diagnostics_and_batch_retry(self):
        cookies, headers = self._auth_cookies()

        # 12.1 错误模式分类器函数校验
        self.assertEqual(bridge_server._classify_archive_error("401 Unauthorized")[0], "token_expired")
        self.assertEqual(bridge_server._classify_archive_error("OpenListAuthErr")[0], "token_expired")
        self.assertEqual(bridge_server._classify_archive_error("quota exceeded / disk full")[0], "storage_full")
        self.assertEqual(bridge_server._classify_archive_error("409 Conflict already exists")[0], "conflict")
        self.assertEqual(bridge_server._classify_archive_error("readtimeout HTTPSConnectionPool Read timed out")[0], "timeout")
        self.assertEqual(bridge_server._classify_archive_error("unknown IO error")[0], "unknown")

        # 12.2 模拟注入各类型归档失败任务
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS["job-fail-auth"] = {
            "id": "job-fail-auth",
            "unique_id": "uid-fail-auth",
            "filename": "auth_err.mp4",
            "size_bytes": 100000,
            "remote_path": "/阿里云盘/auth_err.mp4",
            "state": "failed",
            "error": "RuntimeError: 401 token expired",
            "updated_at": 1725281000.0,
            "policy": "skip",
        }
        bridge_server._ARCHIVE_JOBS["job-fail-conflict"] = {
            "id": "job-fail-conflict",
            "unique_id": "uid-fail-conflict",
            "filename": "conflict.mp4",
            "size_bytes": 200000,
            "remote_path": "/阿里云盘/conflict.mp4",
            "state": "failed",
            "error": "already exists on cloud (409)",
            "updated_at": 1725281100.0,
            "policy": "skip",
        }
        bridge_server._ARCHIVE_JOBS["job-fail-timeout"] = {
            "id": "job-fail-timeout",
            "unique_id": "uid-fail-timeout",
            "filename": "timeout.mp4",
            "size_bytes": 300000,
            "remote_path": "/阿里云盘/timeout.mp4",
            "state": "failed",
            "error": "504 Gateway Timeout network error",
            "updated_at": 1725281200.0,
            "policy": "overwrite",
        }

        # 12.3 GET /api/archive/failed 聚合查询
        resp_failed = self.bridge_client.get("/api/archive/failed", cookies=cookies)
        self.assertEqual(resp_failed.status_code, 200)
        data_f = resp_failed.json()
        self.assertTrue(data_f["ok"])
        self.assertEqual(data_f["summary"]["total"], 3)
        self.assertEqual(data_f["summary"]["categories"]["token_expired"], 1)
        self.assertEqual(data_f["summary"]["categories"]["conflict"], 1)
        self.assertEqual(data_f["summary"]["categories"]["timeout"], 1)
        self.assertEqual(len(data_f["failedJobs"]), 3)

        # 12.4 别名路由 GET /api/archive/failed-summary
        resp_summary = self.bridge_client.get("/api/archive/failed-summary", cookies=cookies)
        self.assertEqual(resp_summary.status_code, 200)
        self.assertEqual(resp_summary.json()["summary"]["total"], 3)

        # 12.5 POST /api/archive/retry-failed 一键重试全部并自动刷新凭据与强制覆盖
        with patch("bridge_server._openlist_relogin", new=AsyncMock(return_value="new-token")) as mock_relogin, \
             patch("bridge_server._archive_worker", new=AsyncMock()) as mock_worker:
            resp_retry = self.bridge_client.post(
                "/api/archive/retry-failed",
                json={"category": "all", "forceOverwrite": True},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_retry.status_code, 200)
            data_r = resp_retry.json()
            self.assertTrue(data_r["ok"])
            self.assertEqual(data_r["retriedCount"], 3)
            mock_relogin.assert_awaited_once()

            # 验证任务状态已原子重置为 queued 且冲突任务的 policy 已更新为 overwrite
            job_auth = bridge_server._ARCHIVE_JOBS["job-fail-auth"]
            self.assertEqual(job_auth["state"], "queued")
            self.assertEqual(job_auth["error"], "")
            self.assertEqual(job_auth["retry_count"], 1)

            job_conflict = bridge_server._ARCHIVE_JOBS["job-fail-conflict"]
            self.assertEqual(job_conflict["state"], "queued")
            self.assertEqual(job_conflict["policy"], "overwrite")

        # 12.6 单项重试指定 jobId
        bridge_server._ARCHIVE_JOBS["job-fail-single"] = {
            "id": "job-fail-single",
            "unique_id": "uid-fail-single",
            "filename": "single.mp4",
            "size_bytes": 400000,
            "remote_path": "/阿里云盘/single.mp4",
            "state": "failed",
            "error": "502 Bad Gateway",
            "updated_at": 1725281300.0,
            "policy": "overwrite",
        }
        with patch("bridge_server._archive_worker", new=AsyncMock()):
            resp_single = self.bridge_client.post(
                "/api/archive/retry-failed",
                json={"jobIds": ["job-fail-single"]},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_single.status_code, 200)
            self.assertEqual(resp_single.json()["retriedCount"], 1)
            self.assertEqual(bridge_server._ARCHIVE_JOBS["job-fail-single"]["state"], "queued")

    # ==========================================================
    # 14. Telegram FloodWait 智能冷却、任务挂起与倒计时
    # ==========================================================
    def test_flood_wait_state_machine_and_api(self):
        cookies, headers = self._auth_cookies()

        # 14.1 异常正则与数字提取测试
        self.assertEqual(bridge_server._extract_flood_wait_seconds({"code": 420, "message": "FLOOD_WAIT_45"}), 45)
        self.assertEqual(bridge_server._extract_flood_wait_seconds({"error_code": 429, "parameters": {"retry_after": 60}}), 60)
        self.assertEqual(bridge_server._extract_flood_wait_seconds("Too Many Requests: retry after 25"), 25)
        self.assertEqual(bridge_server._extract_flood_wait_seconds(Exception("TDLib error: FLOOD_WAIT_15")), 15)
        self.assertIsNone(bridge_server._extract_flood_wait_seconds({"code": 200, "message": "OK"}))

        # 14.2 状态机触发与并发防击穿合并 (取更大到期时间)
        now = time.time()
        until1 = bridge_server._trigger_flood_wait("acc_test", 50, reason="FLOOD_WAIT_50")
        self.assertTrue(bridge_server._is_flood_wait_active("acc_test"))
        self.assertGreaterEqual(until1, now + 49)

        # 较短的限流不缩短已有的冷却时间
        until2 = bridge_server._trigger_flood_wait("acc_test", 20, reason="FLOOD_WAIT_20")
        self.assertEqual(until1, until2)

        # 较长的限流自动顺延
        until3 = bridge_server._trigger_flood_wait("acc_test", 100, reason="FLOOD_WAIT_100")
        self.assertGreater(until3, until1)

        # 14.3 API GET /api/tg/floodwait/status 接口测试
        resp_st = self.bridge_client.get("/api/tg/floodwait/status", cookies=cookies)
        self.assertEqual(resp_st.status_code, 200)
        data_st = resp_st.json()["data"]
        self.assertTrue(data_st["isCooling"])
        self.assertEqual(data_st["account"], "acc_test")
        self.assertGreater(data_st["remainingSeconds"], 0)
        self.assertIn("FLOOD_WAIT", data_st["reason"])

        # 14.4 冷却期间提交直投任务应安全自动挂起
        resp_qd = self.bridge_client.post(
            "/api/tg/quick-download",
            json={
                "force": True,
                "files": [{"fileId": 123, "chatId": -1001, "messageId": 456, "telegramId": 789, "uniqueId": "uid-flood-file", "filename": "movie.mp4", "size": 5000}]
            },
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(resp_qd.status_code, 200)
        data_qd = resp_qd.json()
        self.assertTrue(data_qd["ok"])
        self.assertEqual(data_qd["state"], "flood_wait")
        self.assertEqual(data_qd["code"], "FLOOD_WAIT_SUSPENDED")

        # 验证任务列表 tasks_all 包含挂起的风控任务
        async def check_tasks():
            tasks = await bridge_server.tasks_all(force=True)
            flood_tasks = [t for t in tasks if t.get("status") == "waiting_disk" and "风控" in t.get("error_msg", "")]
            self.assertGreaterEqual(len(flood_tasks), 1)
        asyncio.run(check_tasks())

        # 14.5 API POST /api/tg/floodwait/reset 强制重置
        resp_reset = self.bridge_client.post("/api/tg/floodwait/reset", json={}, cookies=cookies, headers=headers)
        self.assertEqual(resp_reset.status_code, 200)
        self.assertTrue(resp_reset.json()["ok"])
        self.assertFalse(bridge_server._is_flood_wait_active("acc_test"))

        # 再次查状态验证已恢复
        resp_st2 = self.bridge_client.get("/api/tg/floodwait/status", cookies=cookies)
        self.assertFalse(resp_st2.json()["data"]["isCooling"])
        self.assertEqual(resp_st2.json()["data"]["remainingSeconds"], 0)

    # ==========================================================
    # 15. System Doctor 系统健康与依赖一键自检 (Doctor Probes)
    # ==========================================================
    def test_system_doctor_check_and_probes(self):
        cookies, headers = self._auth_cookies()

        # 15.1 存活探测 GET /api/system/doctor/ping
        resp_ping = self.bridge_client.get("/api/system/doctor/ping", cookies=cookies)
        self.assertEqual(resp_ping.status_code, 200)
        self.assertTrue(resp_ping.json()["pong"])

        # 15.2 全链路自检 GET /api/system/doctor
        mock_session = {"authenticated": True, "userId": 10001}
        mock_tdlib = {"@type": "authorizationStateReady"}
        with patch.object(bridge_server.BACKEND, "auth_session", new=AsyncMock(return_value=mock_session)), \
             patch.object(bridge_server.BACKEND, "telegram_api", new=AsyncMock(return_value=mock_tdlib)), \
             patch("bridge_server._openlist_ready", new=AsyncMock(return_value=True)), \
             patch("bridge_server.openlist_dirs", new=AsyncMock(return_value={"ok": True, "dirs": [{"name": "AliDrive"}]})):
            start_t = time.perf_counter()
            resp_doc = self.bridge_client.get("/api/system/doctor", cookies=cookies)
            dur_sec = time.perf_counter() - start_t

            self.assertEqual(resp_doc.status_code, 200)
            # 严格验证在 3 秒内返回
            self.assertLess(dur_sec, 3.0)

            data = resp_doc.json()["data"]
            self.assertTrue(data["ok"])
            self.assertEqual(data["overallStatus"], "healthy")
            self.assertIn("javaBackend", data["components"])
            self.assertIn("tdlib", data["components"])
            self.assertIn("openlist", data["components"])
            self.assertIn("localStorage", data["components"])

            # 验证各组件状态与延迟
            java_c = data["components"]["javaBackend"]
            self.assertEqual(java_c["status"], "healthy")
            self.assertTrue(java_c["details"]["authenticated"])

            tdlib_c = data["components"]["tdlib"]
            self.assertEqual(tdlib_c["status"], "healthy")

            ol_c = data["components"]["openlist"]
            self.assertEqual(ol_c["status"], "healthy")
            self.assertEqual(ol_c["details"]["mountCount"], 1)

            storage_c = data["components"]["localStorage"]
            self.assertEqual(storage_c["status"], "healthy")
            self.assertTrue(storage_c["details"]["writable"])

        # 15.3 异常隔离与熔断测试（单个组件如 OpenList 故障，不影响其他组件且总耗时仍 < 3s）
        with patch.object(bridge_server.BACKEND, "auth_session", new=AsyncMock(return_value=mock_session)), \
             patch.object(bridge_server.BACKEND, "telegram_api", new=AsyncMock(return_value=mock_tdlib)), \
             patch("bridge_server._openlist_ready", new=AsyncMock(side_effect=TimeoutError("Connection timed out"))):
            start_t = time.perf_counter()
            resp_fail = self.bridge_client.get("/api/system/doctor", cookies=cookies)
            dur_sec = time.perf_counter() - start_t

            self.assertEqual(resp_fail.status_code, 200)
            self.assertLess(dur_sec, 3.0)
            data_fail = resp_fail.json()["data"]
            # 存在故障时整体状态标红
            self.assertEqual(data_fail["overallStatus"], "critical")
            self.assertEqual(data_fail["components"]["openlist"]["status"], "critical")
            # 其他组件仍然正常回报
            self.assertEqual(data_fail["components"]["javaBackend"]["status"], "healthy")
            self.assertEqual(data_fail["components"]["localStorage"]["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
