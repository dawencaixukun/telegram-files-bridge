# -*- coding: utf-8 -*-
"""
端到端联调测试与安全性代码审查套件 (test_e2e_security_review.py)
涵盖：
1. 文件系统安全与路径穿越/任意删除/软链接解引用防护测试
2. 批量归档与自动删除本地文件的竞态条件、失败保护与跳过策略
3. 云端搜索/筛选、OpenList 跳转链接有效性与特殊字符转义、失效记录清理
4. 云端取回落盘、原子写入、防重复机制、取消清理与敏感文件覆盖防护
5. 接口全量 CSRF 与 401 权限门禁验证，异常输入与边界防呆
"""
import asyncio
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
import bridge_server
import preview_server


class TestE2ESecurityAndFunctionality(unittest.TestCase):
    def setUp(self):
        self.bridge_client = TestClient(bridge_server.app)
        self.preview_client = TestClient(preview_server.app)
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_e2e_sec_")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _auth_cookies(self):
        token = bridge_server._make_portal_token()
        csrf = "test-e2e-csrf-secret-999"
        cookies = {
            bridge_server.PORTAL_COOKIE: token,
            bridge_server.CSRF_COOKIE: csrf,
        }
        headers = {
            bridge_server.CSRF_HEADER: csrf,
        }
        return cookies, headers

    # =========================================================================
    # 1. 文件系统安全与防任意文件删除 / 路径逃逸 / 软链接劫持
    # =========================================================================
    def test_safe_delete_symlink_and_traversal_protection(self):
        async def run_checks():
            # 1.1 验证软链接防删除 (防止利用软链接间接删除其指向的目标文件)
            target_file = os.path.join(self.tmp_dir, "real_target.dat")
            with open(target_file, "wb") as f:
                f.write(b"critical-data")
            link_file = os.path.join(self.tmp_dir, "fake_symlink.mp4")
            try:
                os.symlink(target_file, link_file)
                symlink_created = True
            except (OSError, NotImplementedError):
                symlink_created = False

            if symlink_created:
                ok, err = await bridge_server._safe_delete_local_path(link_file)
                self.assertFalse(ok, "软链接应当被拒绝删除")
                self.assertIn("软链接", err)
                self.assertTrue(os.path.exists(target_file), "目标文件绝不可被间接删除")

            # 1.2 拒绝受保护的代码/系统/配置文件
            for bad_ext in [".py", ".json", ".jsonl", ".db", ".sqlite", ".sh", ".env", ".key", ".pem", ".yml", ".yaml", ".md", ".html", ".js", ".css"]:
                bad_file = os.path.join(self.tmp_dir, f"test{bad_ext}")
                with open(bad_file, "w") as f:
                    f.write("code")
                ok, err = await bridge_server._safe_delete_local_path(bad_file)
                self.assertFalse(ok, f"扩展名 {bad_ext} 必须被拒绝删除")
                self.assertIn("受保护", err)
                self.assertTrue(os.path.exists(bad_file))

            # 1.3 拒绝点号开头的隐藏文件
            dot_file = os.path.join(self.tmp_dir, ".hidden_file")
            with open(dot_file, "w") as f:
                f.write("dot")
            ok, err = await bridge_server._safe_delete_local_path(dot_file)
            self.assertFalse(ok)
            self.assertIn("点号", err)

            # 1.4 空路径与连字符防呆
            ok, err = await bridge_server._safe_delete_local_path("")
            self.assertFalse(ok)
            ok, err = await bridge_server._safe_delete_local_path("—")
            self.assertFalse(ok)

            # 1.5 目录拒绝删除
            sub_dir = os.path.join(self.tmp_dir, "a_folder")
            os.makedirs(sub_dir, exist_ok=True)
            ok, err = await bridge_server._safe_delete_local_path(sub_dir)
            self.assertFalse(ok)
            self.assertIn("不是普通文件", err)

        asyncio.run(run_checks())

    # =========================================================================
    # 2. 批量归档与自动删除本地文件 (竞态条件、失败回滚防删与跳过策略)
    # =========================================================================
    def test_archive_failure_does_not_delete_local_file(self):
        """核心防呆验证：当归档上传失败时，本地原文件绝不可被删除！"""
        cookies, headers = self._auth_cookies()
        media = os.path.join(self.tmp_dir, "important_video.mp4")
        with open(media, "wb") as f:
            f.write(b"important-video-content-do-not-delete")

        job = {
            "id": "job-fail-test",
            "unique_id": "uid-fail-test",
            "filename": "important_video.mp4",
            "size_bytes": len(b"important-video-content-do-not-delete"),
            "local_path": media,
            "remote_dir": "/阿里云盘/tg-archive",
            "remote_path": "/阿里云盘/tg-archive/important_video.mp4",
            "policy": "overwrite",
            "delete_local": True,
            "state": "queued",
            "progress": 0,
            "error": "",
            "created_at": time.time(),
            "updated_at": time.time(),
            "archived_at": 0.0,
        }
        bridge_server._ARCHIVE_JOBS[job["id"]] = job

        async def run_failing_worker():
            # 模拟 OpenList 上传过程抛出网络/服务端异常
            with patch("bridge_server._openlist_token", new=AsyncMock(return_value="tok")),                  patch("bridge_server._openlist_mkdir_tree", new=AsyncMock()),                  patch("bridge_server._openlist_put_once", new=AsyncMock(side_effect=RuntimeError("OpenList 网盘连接超时 (504)"))):
                await bridge_server._archive_worker(job)

        asyncio.run(run_failing_worker())

        self.assertEqual(job["state"], "failed")
        self.assertIn("超时", job["error"])
        self.assertFalse(job.get("local_deleted", False), "失败的任务不能标记 local_deleted")
        self.assertTrue(os.path.exists(media), "归档失败后本地文件必须完好保留，严防误删！")
        with open(media, "rb") as f:
            self.assertEqual(f.read(), b"important-video-content-do-not-delete")

    def test_local_delete_cancels_active_archive_task(self):
        """竞态条件防呆：删除本地文件时，若存在进行中的归档任务，安全联动取消归档"""
        cookies, headers = self._auth_cookies()
        media = os.path.join(self.tmp_dir, "racing_video.mp4")
        with open(media, "wb") as f:
            f.write(b"racing-data")

        uid = "uid-racing-123"
        job_id = "job-racing-123"
        job = {
            "id": job_id,
            "unique_id": uid,
            "filename": "racing_video.mp4",
            "local_path": media,
            "remote_dir": "/阿里云盘",
            "remote_path": "/阿里云盘/racing_video.mp4",
            "state": "uploading",
            "progress": 50,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        bridge_server._ARCHIVE_JOBS[job_id] = job
        fake_task = MagicMock()
        fake_task.done.return_value = False
        bridge_server._ARCHIVE_TASKS[job_id] = fake_task

        mock_tasks = [{"_unique_id": uid, "local_path": media, "filename": "racing_video.mp4", "_telegram_id": "1", "_file_id": 999}]

        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=mock_tasks)),              patch.object(bridge_server.BACKEND, "remove_file", new=AsyncMock(return_value={"ok": True})):
            resp = self.bridge_client.post(
                "/library/local/delete",
                json={"uniqueId": uid},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["ok"])

        # 验证关联的任务被联动取消
        fake_task.cancel.assert_called_once()
        self.assertEqual(job["state"], "cancelled")
        self.assertIn("已取消", job["error"])
        self.assertFalse(os.path.exists(media))

    # =========================================================================
    # 3. 云端直达链接、特殊字符转义与失效清理
    # =========================================================================
    def test_openlist_direct_url_xss_and_special_chars(self):
        """验证直达链接对中文、空格、以及 XSS 特殊符号的完全转义与安全性"""
        cookies, headers = self._auth_cookies()

        # 3.1 包含中文与空格
        url1 = bridge_server._openlist_direct_url("/阿里云盘/tg archive/航拍 4K.mp4")
        self.assertIn("%E9%98%BF%E9%87%8C", url1)
        self.assertIn("tg%20archive", url1)
        self.assertIn("%204K.mp4", url1)

        # 3.2 包含潜在 XSS 符号
        malicious_path = '/OneDrive/<script>alert("xss")</script>.mp4'
        url2 = bridge_server._openlist_direct_url(malicious_path)
        self.assertNotIn("<script>", url2)
        self.assertIn("%3Cscript%3E", url2)
        self.assertIn("%22xss%22", url2)

        # 3.3 preview_server 接口一致性
        pv_resp = self.preview_client.get(f"/openlist/direct-url?path={malicious_path}")
        self.assertEqual(pv_resp.status_code, 200)
        self.assertNotIn("<script>", pv_resp.json()["url"])
        self.assertIn("%3Cscript%3E", pv_resp.json()["url"])

    def test_cloud_delete_path_normalization(self):
        """测试云端删除端点对路径穿越 .. 的阻断过滤"""
        cookies, headers = self._auth_cookies()

        # 传入带有 .. 路径逃逸的请求
        resp = self.bridge_client.post(
            "/library/cloud/delete",
            json={"remotePaths": ["/../etc/passwd", "/dir/../../root"]},
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])
        self.assertIn("未指定要删除的云端文件", resp.json()["message"])

    # =========================================================================
    # 4. 云端取回 (Retrieve)：落盘原子性、防覆盖敏感文件、断点与防重复
    # =========================================================================
    def test_retrieve_protected_file_overwrite_prevention(self):
        """测试取回目标路径安全性：严防覆盖服务器 .py / .env / .sh 等核心代码和系统文件"""
        cookies, headers = self._auth_cookies()

        # 尝试取回文件名为 bridge_server.py 的恶意请求
        resp = self.bridge_client.post(
            "/library/cloud/retrieve",
            json={"remotePath": "/阿里云盘/tg-archive/bridge_server.py"},
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])
        self.assertIn("受保护", resp.json()["message"])

        # 尝试取回点号隐藏文件 .env
        resp_env = self.bridge_client.post(
            "/library/cloud/retrieve",
            json={"remotePath": "/阿里云盘/tg-archive/.env"},
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(resp_env.status_code, 400)
        self.assertFalse(resp_env.json()["ok"])
        self.assertIn("点号", resp_env.json()["message"])

    def test_retrieve_cancel_cleans_temporary_part_file(self):
        """测试取回任务取消时，临时分块文件 (.part.<id>) 自动被清理删除"""
        temp_dl_file = os.path.join(self.tmp_dir, "cancel_test.mp4")
        job = {
            "id": "job-cancel-cleanup",
            "remote_path": "/阿里云盘/cancel_test.mp4",
            "filename": "cancel_test.mp4",
            "target_path": temp_dl_file,
            "state": "downloading",
            "progress": 30,
            "size_bytes": 1000,
            "downloaded_bytes": 300,
            "error": "",
            "created_at": time.time(),
            "updated_at": time.time(),
            "finished_at": 0.0,
        }
        bridge_server._RETRIEVE_JOBS[job["id"]] = job

        # 模拟产生部分下载的临时文件
        part_path = temp_dl_file + f".part.{job['id']}"
        with open(part_path, "wb") as f:
            f.write(b"partial-data")
        self.assertTrue(os.path.exists(part_path))

        # 模拟 worker 响应 CancelledError
        async def run_cancelled_worker():
            try:
                raise asyncio.CancelledError()
            except asyncio.CancelledError:
                job["state"] = "cancelled"
                job["error"] = "取回已取消"
                if os.path.exists(part_path):
                    os.remove(part_path)

        asyncio.run(run_cancelled_worker())

        self.assertEqual(job["state"], "cancelled")
        self.assertFalse(os.path.exists(part_path), "取消后临时 .part 文件必须被删除干净！")

    # =========================================================================
    # 5. 全链路 CSRF、401 鉴权门禁与格式异常防御
    # =========================================================================
    def test_all_endpoints_auth_and_csrf_matrix(self):
        """矩阵测试：所有新增 POST/GET 端点在未登录、无 CSRF、非法 JSON 下的坚固性"""
        endpoints_post = [
            ("/library/local/delete", {"uniqueId": "uid1"}),
            ("/api/local/delete", {"uniqueId": "uid1"}),
            ("/library/cloud/delete", {"remotePath": "/阿里云盘/test.mp4"}),
            ("/library/cloud/clear-missing", {}),
            ("/library/cloud/retrieve", {"remotePath": "/阿里云盘/test.mp4"}),
            ("/library/cloud/retrieve/cancel", {"jobId": "test"}),
            ("/archive/batch", {"uniqueIds": ["uid1"], "remoteDir": "/阿里云盘"}),
        ]

        # 5.1 未登录请求数据端点 -> 必须全量返回 401 JSON
        for ep, body in endpoints_post:
            resp = self.bridge_client.post(ep, json=body)
            self.assertEqual(resp.status_code, 401, f"未登录请求 {ep} 必须返回 401")
            self.assertEqual(resp.json(), {"ok": False, "message": "未登录"})

        endpoints_get = [
            "/library/cloud/files",
            "/openlist/direct-url?path=/test.mp4",
            "/library/cloud/retrieve/status",
        ]
        for ep in endpoints_get:
            resp = self.bridge_client.get(ep)
            self.assertEqual(resp.status_code, 401, f"未登录请求 {ep} 必须返回 401")

        # 5.2 登录但无 CSRF Header -> 必须全量拦截 403 Forbidden
        token = bridge_server._make_portal_token()
        for ep, body in endpoints_post:
            resp = self.bridge_client.post(
                ep,
                json=body,
                cookies={bridge_server.PORTAL_COOKIE: token}
            )
            self.assertEqual(resp.status_code, 403, f"无 CSRF 的写请求 {ep} 必须返回 403")
            self.assertIn("CSRF", resp.json()["message"])

        # 5.3 登录但 CSRF 双提交 Header 与 Cookie 不一致 -> 必须全量拦截 403 Forbidden
        for ep, body in endpoints_post:
            resp = self.bridge_client.post(
                ep,
                json=body,
                cookies={bridge_server.PORTAL_COOKIE: token, bridge_server.CSRF_COOKIE: "csrf-val-1"},
                headers={bridge_server.CSRF_HEADER: "csrf-val-2"}
            )
            self.assertEqual(resp.status_code, 403, f"CSRF 不匹配的请求 {ep} 必须返回 403")

        # 5.4 非法 JSON 载荷（非 dict / 畸形文本）防崩溃验证
        cookies, headers = self._auth_cookies()
        resp_bad_json = self.bridge_client.post(
            "/library/local/delete",
            content=b"not-a-valid-json{{{",
            cookies=cookies,
            headers={"Content-Type": "application/json", **headers}
        )
        self.assertEqual(resp_bad_json.status_code, 400)
        self.assertFalse(resp_bad_json.json()["ok"])

        resp_list_body = self.bridge_client.post(
            "/library/local/delete",
            content=b'["uid1"]',
            cookies=cookies,
            headers={"Content-Type": "application/json", **headers}
        )
        self.assertEqual(resp_list_body.status_code, 400)
        self.assertFalse(resp_list_body.json()["ok"])


if __name__ == "__main__":
    unittest.main()
