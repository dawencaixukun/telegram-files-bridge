# -*- coding: utf-8 -*-
"""
tests/test_archive_delete_local_regression.py
=============================================
回归测试：「归档之后本地文件没删除」

覆盖三条根因链路：
1. POST /archive/start 未显式携带 deleteLocal 时，必须回落到设置页全局开关
   _ARCHIVE_CONFIG["deleteLocal"]（与 /api/tg/quick-download 的口径一致），
   否则用户已在设置里开启「归档成功后自动删除本地文件」，手动归档仍会保留本地文件。
2. 归档弹窗复选框的默认值必须跟随服务端全局开关（前端 JS 契约），
   同时显式传 false 时仍以 body 为准，不能被全局开关反向覆盖。
3. 删除本地失败必须暴露真实结果：任务视图带 localDeleted / localDeleteError，
   前端按钮文案以 localDeleted（真实结果）而非 deleteLocal（请求意图）为准。
"""
import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
import bridge_server


class TestArchiveDeleteLocalRegression(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(bridge_server.app)
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_del_reg_")
        self._orig_jobs = dict(bridge_server._ARCHIVE_JOBS)
        self._orig_file = bridge_server._ARCHIVE_FILE
        self._orig_delete_local = bridge_server._ARCHIVE_CONFIG.get("deleteLocal")
        bridge_server._ARCHIVE_FILE = os.path.join(self.tmp_dir, ".test_archive_jobs.json")
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._DELETED_LOCAL_UIDS.clear()
        bridge_server._reset_flood_wait()
        self.media = os.path.join(self.tmp_dir, "regress_movie.mp4")
        with open(self.media, "wb") as f:
            f.write(b"x" * 2048)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self._orig_jobs)
        bridge_server._ARCHIVE_FILE = self._orig_file
        bridge_server._ARCHIVE_CONFIG["deleteLocal"] = self._orig_delete_local
        bridge_server._DELETED_LOCAL_UIDS.clear()
        bridge_server._reset_flood_wait()

    def _auth(self):
        token = bridge_server._make_portal_token()
        csrf = "test-csrf-token-12345"
        return (
            {bridge_server.PORTAL_COOKIE: token, bridge_server.CSRF_COOKIE: csrf},
            {bridge_server.CSRF_HEADER: csrf},
        )

    def _mock_task(self, uid="uid-del-reg-1"):
        return {
            "_unique_id": uid,
            "local_path": self.media,
            "filename": "regress_movie.mp4",
            "_size_bytes": 2048,
            "_download_status": "completed",
            "_telegram_id": "1",
            "_file_id": 901,
        }

    # ------------------------------------------------------------------
    # 1. 后端兜底：body 未给 deleteLocal 时回落全局配置
    # ------------------------------------------------------------------
    def test_start_falls_back_to_global_delete_local_when_body_omits_it(self):
        cookies, headers = self._auth()
        bridge_server._ARCHIVE_CONFIG["deleteLocal"] = True
        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=[self._mock_task()])):
            resp = self.client.post(
                "/archive/start",
                json={"uniqueIds": ["uid-del-reg-1"], "remoteDir": "/阿里云盘/tg-archive"},
                cookies=cookies,
                headers=headers,
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"], data.get("message"))
        self.assertTrue(
            data["jobs"][0]["deleteLocal"],
            "body 未携带 deleteLocal 时必须回落到全局开关 True",
        )

    def test_start_respects_explicit_false_over_global_true(self):
        """全局开启但用户在本弹窗显式取消勾选，必须以请求为准（防反向覆盖）。"""
        cookies, headers = self._auth()
        bridge_server._ARCHIVE_CONFIG["deleteLocal"] = True
        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=[self._mock_task()])):
            resp = self.client.post(
                "/archive/start",
                json={
                    "uniqueIds": ["uid-del-reg-1"],
                    "remoteDir": "/阿里云盘/tg-archive",
                    "deleteLocal": False,
                },
                cookies=cookies,
                headers=headers,
            )
        data = resp.json()
        self.assertTrue(data["ok"], data.get("message"))
        self.assertFalse(data["jobs"][0]["deleteLocal"])

    def test_start_explicit_true_still_wins_when_global_off(self):
        cookies, headers = self._auth()
        bridge_server._ARCHIVE_CONFIG["deleteLocal"] = False
        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=[self._mock_task()])):
            resp = self.client.post(
                "/archive/start",
                json={
                    "uniqueIds": ["uid-del-reg-1"],
                    "remoteDir": "/阿里云盘/tg-archive",
                    "deleteLocal": True,
                },
                cookies=cookies,
                headers=headers,
            )
        data = resp.json()
        self.assertTrue(data["ok"], data.get("message"))
        self.assertTrue(data["jobs"][0]["deleteLocal"])

    # ------------------------------------------------------------------
    # 2. 端到端：delete_local 为真时归档完成后磁盘文件确实消失
    # ------------------------------------------------------------------
    def test_archive_worker_actually_removes_local_file(self):
        self.assertTrue(os.path.exists(self.media))
        job = {
            "id": "job-del-reg-e2e",
            "unique_id": "uid-del-reg-1",
            "filename": "regress_movie.mp4",
            "size_bytes": 2048,
            "local_path": self.media,
            "remote_dir": "/阿里云盘/tg-archive",
            "remote_path": "/阿里云盘/tg-archive/regress_movie.mp4",
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

        async def fake_stat(tok, path):
            # 模拟云端已落盘且大小与本地一致（新校验要求上传后回查真实 size）
            return {"size": 2048, "raw": {"size": 2048}}

        async def run_worker():
            with patch("bridge_server._openlist_token", new=AsyncMock(return_value="tk")), \
                 patch("bridge_server._openlist_mkdir_tree", new=AsyncMock()), \
                 patch("bridge_server._openlist_put_once", new=AsyncMock()), \
                 patch("bridge_server._openlist_stat", new=fake_stat), \
                 patch.object(bridge_server.BACKEND, "remove_file", new=AsyncMock(return_value={"ok": True})):
                await bridge_server._archive_worker(job)

        asyncio.run(run_worker())

        self.assertEqual(job["state"], "done")
        self.assertTrue(job.get("local_deleted"), "任务应记录 local_deleted=True")
        self.assertFalse(os.path.exists(self.media), "归档成功后本地文件必须已删除")
        self.assertIn("uid-del-reg-1", bridge_server._DELETED_LOCAL_UIDS)

    # ------------------------------------------------------------------
    # 3. 删除失败必须可见（可诊断），且不谎报已删除
    # ------------------------------------------------------------------
    def test_delete_failure_is_surfaced_not_faked(self):
        """把本地路径换成一个受限后缀，触发 _safe_delete_local_path 的拒删分支。"""
        blocked = os.path.join(self.tmp_dir, "blocked.md")
        with open(blocked, "wb") as f:
            f.write(b"y" * 64)
        job = {
            "id": "job-del-reg-fail",
            "unique_id": "uid-del-reg-blocked",
            "filename": "blocked.md",
            "size_bytes": 64,
            "local_path": blocked,
            "remote_dir": "/阿里云盘/tg-archive",
            "remote_path": "/阿里云盘/tg-archive/blocked.md",
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

        async def fake_stat2(tok, path):
            return {"size": 64, "raw": {"size": 64}}

        async def run_worker():
            with patch("bridge_server._openlist_token", new=AsyncMock(return_value="tk")), \
                 patch("bridge_server._openlist_mkdir_tree", new=AsyncMock()), \
                 patch("bridge_server._openlist_put_once", new=AsyncMock()), \
                 patch("bridge_server._openlist_stat", new=fake_stat2), \
                 patch.object(bridge_server.BACKEND, "remove_file", new=AsyncMock(return_value={"ok": True})):
                await bridge_server._archive_worker(job)

        asyncio.run(run_worker())

        self.assertEqual(job["state"], "done")
        self.assertTrue(os.path.exists(blocked), "受保护后缀不应被删除")
        self.assertFalse(job.get("local_deleted"))
        self.assertTrue(job.get("local_delete_error"), "删除失败原因必须被记录")

        public = bridge_server._archive_public(job)
        self.assertFalse(public["localDeleted"])
        self.assertTrue(public["deleteLocal"], "请求意图仍为 True")
        self.assertTrue(public["localDeleteError"], "失败原因必须透出给前端")

    # ------------------------------------------------------------------
    # 4. 前端契约：弹窗复选框跟随服务端全局开关、按钮以真实结果为准
    # ------------------------------------------------------------------
    def test_frontend_modal_seeds_checkbox_from_server_config(self):
        with open("static/js/app.js", "r", encoding="utf-8") as f:
            js = f.read()
        self.assertIn("d.config.deleteLocal", js, "弹窗回填应读取服务端归档配置")
        self.assertIn("/archive/config", js)
        # 按钮文案必须基于真实结果 localDeleted，而非请求意图 deleteLocal
        self.assertIn("本地待清理", js)
        self.assertIn("localDeleteError", js)

    # ------------------------------------------------------------------
    # 5. 数据丢失红线：0 字节云端残片绝不能被判成功并删除本地
    # ------------------------------------------------------------------
    def _mkjob(self, size_bytes, policy="skip", remote_path="/onedrive/yello/a.mp4"):
        return {
            "id": "job-zero-" + policy,
            "unique_id": "uid-zero-1",
            "filename": "a.mp4",
            "size_bytes": size_bytes,
            "local_path": self.media,
            "remote_dir": "/onedrive/yello",
            "remote_path": remote_path,
            "policy": policy,
            "delete_local": True,
            "state": "queued",
            "progress": 0,
            "error": "",
            "created_at": 0.0,
            "updated_at": 0.0,
            "archived_at": 0.0,
        }

    def test_skip_policy_rejects_zero_byte_cloud_stub(self):
        """【数据丢失红线】云端只有 0 字节残片时，skip 策略不得判成功，更不得删本地。

        历史事故：上传被取消后 OneDrive 留下 0 字节空壳，旧逻辑仅判「文件是否存在」，
        于是判 done 并按 deleteLocal 删掉本地 —— 本地没了、云端是空文件，真实数据丢失
        （线上实测 7 个文件受害，最大一个 2.3 GB）。
        """
        job = self._mkjob(2048, policy="skip")
        bridge_server._ARCHIVE_JOBS[job["id"]] = job

        async def stat_zero(tok, path):
            return {"size": 0, "raw": {"size": 0}}

        async def stat_ok(tok, path):
            return {"size": 2048, "raw": {"size": 2048}}

        async def run(policy, stat_fn):
            j = self._mkjob(2048, policy=policy)
            bridge_server._ARCHIVE_JOBS[j["id"]] = j
            with patch("bridge_server._openlist_token", new=AsyncMock(return_value="tk")), \
                 patch("bridge_server._openlist_mkdir_tree", new=AsyncMock()), \
                 patch("bridge_server._openlist_put_once", new=AsyncMock()), \
                 patch("bridge_server._openlist_stat", new=stat_fn), \
                 patch.object(bridge_server.BACKEND, "remove_file", new=AsyncMock(return_value={"ok": True})):
                await bridge_server._archive_worker(j)
            return j

        # 1) 云端 0 字节：不得被当成已完成，且本地文件必须原样保留
        j1 = asyncio.run(run("skip", stat_zero))
        self.assertNotEqual(j1["state"], "done", "0 字节云端残片绝不能被判为归档成功")
        self.assertTrue(os.path.exists(self.media), "误判成功会删掉本地文件，此处必须保留")
        self.assertFalse(j1.get("local_deleted"))

        # 2) 云端大小与本地一致：正常跳过并允许删本地
        j2 = asyncio.run(run("skip", stat_ok))
        self.assertEqual(j2["state"], "done")
        self.assertFalse(os.path.exists(self.media), "确认云端完整时才允许删本地")

    def test_upload_200_but_size_mismatch_is_not_success(self):
        """上传接口返回 200 但落盘大小不符时，必须判失败且不删本地。"""
        with open(self.media, "wb") as f:
            f.write(b"x" * 2048)
        job = self._mkjob(2048, policy="overwrite")
        bridge_server._ARCHIVE_JOBS[job["id"]] = job

        async def stat_short(tok, path):
            return {"size": 10, "raw": {"size": 10}}

        async def run_worker():
            with patch("bridge_server._openlist_token", new=AsyncMock(return_value="tk")), \
                 patch("bridge_server._openlist_mkdir_tree", new=AsyncMock()), \
                 patch("bridge_server._openlist_put_once", new=AsyncMock()), \
                 patch("bridge_server._openlist_stat", new=stat_short):
                await bridge_server._archive_worker(job)

        asyncio.run(run_worker())
        self.assertEqual(job["state"], "failed")
        self.assertIn("大小核验不一致", job.get("error", ""))
        self.assertTrue(os.path.exists(self.media), "核验失败时绝不能删本地文件")

    def test_missing_local_but_complete_cloud_marks_done(self):
        """本地已删但云端有完整副本时，应判为已归档（消除重复入队造成的假失败）。"""
        os.remove(self.media)
        job = self._mkjob(2048, policy="overwrite")
        bridge_server._ARCHIVE_JOBS[job["id"]] = job

        async def stat_ok(tok, path):
            return {"size": 2048, "raw": {"size": 2048}}

        async def run_worker():
            with patch("bridge_server._openlist_token", new=AsyncMock(return_value="tk")), \
                 patch("bridge_server._openlist_mkdir_tree", new=AsyncMock()), \
                 patch("bridge_server._openlist_put_once", new=AsyncMock()), \
                 patch("bridge_server._openlist_stat", new=stat_ok):
                await bridge_server._archive_worker(job)

        asyncio.run(run_worker())
        self.assertEqual(job["state"], "done", "云端已有完整副本时应判成功，而非长期挂在失败列表")
        self.assertEqual(job.get("remote_size"), 2048)

    def test_missing_local_and_missing_cloud_still_fails(self):
        """本地和云端都没有完整副本时，必须如实报失败（不得假装成功）。"""
        os.remove(self.media)
        job = self._mkjob(2048, policy="overwrite")
        bridge_server._ARCHIVE_JOBS[job["id"]] = job

        async def stat_none(tok, path):
            return None

        async def run_worker():
            with patch("bridge_server._openlist_token", new=AsyncMock(return_value="tk")), \
                 patch("bridge_server._openlist_mkdir_tree", new=AsyncMock()), \
                 patch("bridge_server._openlist_stat", new=stat_none):
                await bridge_server._archive_worker(job)

        asyncio.run(run_worker())
        self.assertEqual(job["state"], "failed")
        self.assertIn("云端没有完整副本", job.get("error", ""))

    def test_frontend_omits_delete_local_when_user_never_chose(self):
        """用户从未在弹窗内选择过时，提交体不携带 deleteLocal（交后端回落全局）。"""
        with open("static/js/app.js", "r", encoding="utf-8") as f:
            js = f.read()
        self.assertIn("archDelLocalExplicit", js)
        self.assertIn("if (archDelLocalExplicit) archBody.deleteLocal = deleteLocal;", js)
        self.assertNotIn("Object.assign", js, "app.js 保持 ES5 风格，勿引入新语法")

    def test_modal_checkbox_records_explicit_choice(self):
        with open("templates/partials/_archive_modal.html", "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("__archDelLocalTouch(this)", html, "勾选变更需记录为用户主动选择")


if __name__ == "__main__":
    unittest.main()
