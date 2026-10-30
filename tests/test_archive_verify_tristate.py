# -*- coding: utf-8 -*-
"""
tests/test_archive_verify_tristate.py
=====================================
回归测试：「归档异常告警中心 53 条转存失败」三类假失败的根治。

线上真实构成（.archive_jobs.json 563 条，failed 54 条）：
  * 45 条 `bridge 重启导致上传中断` —— progress 全为 0，一个字节都没传，
    只是还在排队时进程被重启；旧 `_archive_load` 把 queued/uploading 一律写
    failed，制造了一批「什么都没发生」的告警。
  * 8 条 `上传后云端存在性核验失败` —— 文件其实已完整落盘（云端大小与本地
    字节数完全一致），只是 OneDrive 侧 /api/fs/get 从 0.6s 飙到 10~12s，
    第一次探测超时就判了失败。
  * 且这两类文案都不在分类器正则里，前端一律显示「❓ 未知异常」，
    用户看不到任何可执行信息。

本文件锁定四条修复契约：
1. 重启恢复：排队中(progress==0)的任务原样保留并标记 resume_pending，
   只有真的传了一半的才判失败。
2. 三态探测：超时是 unknown，绝不等于「云端不存在」，也不会被当成 0 字节。
3. 核验不重传：unverified 任务复查只查大小，绝不重新上传。
4. 假失败自愈：云端大小一致的历史 failed 自动改判 done。
"""
import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import bridge_server
from services.openlist_service import (
    STAT_OK, STAT_MISSING, STAT_UNKNOWN,
    _openlist_stat_ex, _openlist_verify_size,
)


class TestRestartResume(unittest.TestCase):
    """重启不再把排队中的任务判成失败。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tg_resume_")
        self._orig_jobs = dict(bridge_server._ARCHIVE_JOBS)
        self._orig_file = bridge_server._ARCHIVE_FILE
        bridge_server._ARCHIVE_FILE = os.path.join(self.tmp, ".archive_jobs.json")
        bridge_server._ARCHIVE_JOBS.clear()

    def tearDown(self):
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self._orig_jobs)
        bridge_server._ARCHIVE_FILE = self._orig_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _job(self, jid, state, progress):
        return {
            "id": jid, "unique_id": "uid-" + jid, "filename": jid + ".mp4",
            "size_bytes": 1024, "local_path": os.path.join(self.tmp, jid + ".mp4"),
            "remote_dir": "/onedrive/yello", "remote_path": "/onedrive/yello/" + jid + ".mp4",
            "policy": "overwrite", "delete_local": False,
            "state": state, "progress": progress, "error": "",
            "created_at": 1.0, "updated_at": 1.0, "archived_at": 0.0,
        }

    def test_queued_jobs_are_resumed_not_failed(self):
        """progress==0 的排队任务必须保留排队，不得写 failed。"""
        bridge_server._ARCHIVE_JOBS["q1"] = self._job("q1", "queued", 0)
        bridge_server._ARCHIVE_JOBS["q2"] = self._job("q2", "queued", 0)
        bridge_server._archive_load()

        for jid in ("q1", "q2"):
            j = bridge_server._ARCHIVE_JOBS[jid]
            self.assertEqual(j["state"], "queued", "排队中的任务不应被判失败")
            self.assertTrue(j.get("resume_pending"), "应打上待续跑标记")
            self.assertEqual(j.get("error") or "", "", "不应写入失败原因")

    def test_uploading_job_is_still_failed(self):
        """真的传了一半的任务仍要如实判失败（可重试），不能假装没事。"""
        bridge_server._ARCHIVE_JOBS["u1"] = self._job("u1", "uploading", 42)
        bridge_server._archive_load()
        j = bridge_server._ARCHIVE_JOBS["u1"]
        self.assertEqual(j["state"], "failed")
        self.assertIn("中断", j["error"])

    def test_resume_pending_requeues_after_startup(self):
        """_archive_resume_pending 会把 resume_pending 的任务重新交给 worker。"""
        bridge_server._ARCHIVE_JOBS["q3"] = self._job("q3", "queued", 0)
        self._job_resume = bridge_server._ARCHIVE_JOBS["q3"]
        bridge_server._ARCHIVE_JOBS["q3"]["resume_pending"] = True

        with patch.object(bridge_server, "_archive_worker", new=AsyncMock()) as worker:
            async def run():
                return await bridge_server._archive_resume_pending()
            n = asyncio.run(run())
            # 让 create_task 起的协程有机会被调度
            self.assertGreaterEqual(n, 1)
            self.assertFalse(bridge_server._ARCHIVE_JOBS["q3"].get("resume_pending"),
                             "续跑后必须清掉标记，否则会被反复重新入队")
            self.assertFalse(bridge_server._ARCHIVE_JOBS["q3"].get("error"))


class TestTriStateProbe(unittest.TestCase):
    """探测三态：unknown 绝不能被当成「不存在」或「0 字节」。"""

    def test_timeout_returns_unknown_not_missing(self):
        async def boom(*a, **k):
            raise TimeoutError("read timed out")
        with patch.object(bridge_server._openlist_client, "post", new=boom):
            async def run():
                return await _openlist_stat_ex("tk", "/onedrive/yello/a.mp4", attempts=1)
            code, size, _ = asyncio.run(run())
        self.assertEqual(code, STAT_UNKNOWN)
        self.assertEqual(size, 0)

    def test_missing_is_definitive_and_not_retried(self):
        """真实 OpenList 对不存在文件返回 code=500 + "object not found"。

        这类回答是确定性结论，重试只会白耗时间（线上每次探测 0.6~1s，重试三次
        就是三倍开销），必须一次判定并返回 missing。
        """
        calls = {"n": 0}

        class R:
            status_code = 200
            def json(self):
                return {"code": 500, "message": "object not found", "data": None}

        async def fake_post(*a, **k):
            calls["n"] += 1
            return R()

        with patch.object(bridge_server._openlist_client, "post", new=fake_post):
            async def run():
                return await _openlist_stat_ex("tk", "/x/a.mp4", attempts=3)
            code, _, _ = asyncio.run(run())

        self.assertEqual(code, STAT_MISSING)
        self.assertEqual(calls["n"], 1, "确定性结论不应重试")

    def test_verify_falls_back_to_directory_listing(self):
        """fs/get 查不清时用父目录列表兜底，能救回「其实已传完」的假失败。"""
        calls = {"n": 0}

        class GetFail:
            status_code = 200
            def json(self):
                return {"code": 500, "message": "boom", "data": {}}

        class ListOk:
            status_code = 200
            def json(self):
                return {"code": 200, "message": "success", "data": {
                    "content": [{"name": "a.mp4", "size": 2048, "is_dir": False}]}}

        async def fake_post(path, *a, **k):
            calls["n"] += 1
            return ListOk() if path == "/api/fs/list" else GetFail()

        with patch.object(bridge_server._openlist_client, "post", new=fake_post):
            async def run():
                return await _openlist_verify_size("tk", "/onedrive/yello/a.mp4", attempts=1)
            code, size = asyncio.run(run())
        self.assertEqual(code, STAT_OK, "目录列表已给出确定答案，不应再判 unknown")
        self.assertEqual(size, 2048)
        self.assertGreaterEqual(calls["n"], 2, "应当先 fs/get 再兜底 fs/list")


class TestVerifyOnlyResume(unittest.TestCase):
    """unverified 任务复查只核验，绝不重新上传。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tg_verify_")
        self._orig_jobs = dict(bridge_server._ARCHIVE_JOBS)
        self._orig_file = bridge_server._ARCHIVE_FILE
        bridge_server._ARCHIVE_FILE = os.path.join(self.tmp, ".archive_jobs.json")
        bridge_server._ARCHIVE_JOBS.clear()
        self.media = os.path.join(self.tmp, "v.mp4")
        with open(self.media, "wb") as f:
            f.write(b"y" * 2048)

    def tearDown(self):
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self._orig_jobs)
        bridge_server._ARCHIVE_FILE = self._orig_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _job(self):
        return {
            "id": "vj1", "unique_id": "uid-vj1", "filename": "v.mp4",
            "size_bytes": 2048, "local_path": self.media,
            "remote_dir": "/onedrive/yello", "remote_path": "/onedrive/yello/v.mp4",
            "policy": "overwrite", "delete_local": False,
            "state": "unverified", "progress": 99, "error": "",
            "verify_pending": True, "verify_attempts": 1,
            "created_at": 1.0, "updated_at": 1.0, "archived_at": 0.0,
        }

    def test_reverify_marks_done_without_uploads(self):
        job = self._job()
        bridge_server._ARCHIVE_JOBS["vj1"] = job
        put = AsyncMock()
        with patch("bridge_server._openlist_verify_size",
                   new=AsyncMock(return_value=(STAT_OK, 2048))), \
             patch("bridge_server._openlist_put_once", new=put), \
             patch("bridge_server.notify_archive_success", new=MagicMock()):
            async def run():
                return await bridge_server._archive_reverify_job(job, "tk")
            ok = asyncio.run(run())

        self.assertTrue(ok)
        self.assertEqual(job["state"], "done")
        self.assertEqual(job.get("remote_size"), 2048)
        self.assertFalse(job.get("verify_pending"))
        put.assert_not_called()

    def test_reverify_size_mismatch_fails_without_upload(self):
        job = self._job()
        bridge_server._ARCHIVE_JOBS["vj1"] = job
        put = AsyncMock()
        with patch("bridge_server._openlist_verify_size",
                   new=AsyncMock(return_value=(STAT_OK, 10))), \
             patch("bridge_server._openlist_put_once", new=put), \
             patch("bridge_server.notify_archive_failed", new=MagicMock()):
            async def run():
                return await bridge_server._archive_reverify_job(job, "tk")
            asyncio.run(run())

        self.assertEqual(job["state"], "failed")
        self.assertIn("大小核验不一致", job["error"])
        put.assert_not_called()

    def test_reverify_unknown_keeps_job_pending(self):
        """仍查不清时保持 unverified 等下一轮，不得判失败也不得重传。"""
        job = self._job()
        bridge_server._ARCHIVE_JOBS["vj1"] = job
        with patch("bridge_server._openlist_verify_size",
                   new=AsyncMock(return_value=(STAT_UNKNOWN, 0))):
            async def run():
                return await bridge_server._archive_reverify_job(job, "tk")
            asyncio.run(run())
        self.assertEqual(job["state"], "unverified")

    def test_reverify_gives_up_after_max_attempts(self):
        job = self._job()
        job["verify_attempts"] = 5
        bridge_server._ARCHIVE_JOBS["vj1"] = job
        with patch("bridge_server._openlist_verify_size",
                   new=AsyncMock(return_value=(STAT_UNKNOWN, 0))), \
             patch("bridge_server.notify_archive_failed", new=MagicMock()):
            async def run():
                return await bridge_server._archive_reverify_job(job, "tk")
            asyncio.run(run())
        self.assertEqual(job["state"], "failed")
        self.assertIn("无法确认", job["error"])


class TestFalseFailureHealing(unittest.TestCase):
    """历史「核验失败」假失败：云端其实完整时自动改判 done。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tg_heal_")
        self._orig_jobs = dict(bridge_server._ARCHIVE_JOBS)
        self._orig_file = bridge_server._ARCHIVE_FILE
        bridge_server._ARCHIVE_FILE = os.path.join(self.tmp, ".archive_jobs.json")
        bridge_server._ARCHIVE_JOBS.clear()

    def tearDown(self):
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self._orig_jobs)
        bridge_server._ARCHIVE_FILE = self._orig_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _failed(self, jid, size):
        return {
            "id": jid, "unique_id": "uid-" + jid, "filename": jid + ".mp4",
            "size_bytes": size, "local_path": "/gone/" + jid + ".mp4",
            "remote_dir": "/onedrive/yello", "remote_path": "/onedrive/yello/" + jid + ".mp4",
            "policy": "overwrite", "delete_local": True,
            "state": "failed", "progress": 99,
            "error": "上传后云端存在性核验失败，未确认归档完成",
            "created_at": 1.0, "updated_at": 1.0, "archived_at": 0.0,
        }

    def test_heal_marks_done_when_cloud_size_matches(self):
        bridge_server._ARCHIVE_JOBS["h1"] = self._failed("h1", 2048)
        with patch("bridge_server._openlist_token", new=AsyncMock(return_value="tk")), \
             patch("bridge_server._openlist_verify_size",
                   new=AsyncMock(return_value=(STAT_OK, 2048))):
            async def run():
                return await bridge_server._archive_heal_false_failures()
            healed = asyncio.run(run())

        self.assertEqual(healed, 1)
        j = bridge_server._ARCHIVE_JOBS["h1"]
        self.assertEqual(j["state"], "done")
        self.assertEqual(j["error"], "")
        self.assertTrue(j.get("healed_from_false_failure"))

    def test_heal_keeps_failed_when_cloud_incomplete(self):
        bridge_server._ARCHIVE_JOBS["h2"] = self._failed("h2", 2048)
        with patch("bridge_server._openlist_token", new=AsyncMock(return_value="tk")), \
             patch("bridge_server._openlist_verify_size",
                   new=AsyncMock(return_value=(STAT_OK, 0))):
            async def run():
                return await bridge_server._archive_heal_false_failures()
            healed = asyncio.run(run())
        self.assertEqual(healed, 0)
        self.assertEqual(bridge_server._ARCHIVE_JOBS["h2"]["state"], "failed",
                         "云端仍是 0 字节残片时绝不能改判成功")

    def test_heal_ignores_unknown_probe(self):
        bridge_server._ARCHIVE_JOBS["h3"] = self._failed("h3", 2048)
        with patch("bridge_server._openlist_token", new=AsyncMock(return_value="tk")), \
             patch("bridge_server._openlist_verify_size",
                   new=AsyncMock(return_value=(STAT_UNKNOWN, 0))):
            async def run():
                return await bridge_server._archive_heal_false_failures()
            healed = asyncio.run(run())
        self.assertEqual(healed, 0)
        self.assertEqual(bridge_server._ARCHIVE_JOBS["h3"]["state"], "failed")


class TestClassificationCoversRealMessages(unittest.TestCase):
    """线上两种真实失败文案必须落进明确分类，而不是 unknown。"""

    def test_restart_interrupted_is_classified(self):
        for msg in ("bridge 重启导致上传中断，可重试", "上传被中断（bridge 重启），可重试"):
            code, label, fix = bridge_server._classify_archive_error(msg)
            self.assertEqual(code, "interrupted", msg)
            self.assertNotEqual(label.strip("❓ "), "未知异常")
            self.assertTrue(fix)

    def test_verify_failure_is_classified(self):
        for msg in ("上传后云端存在性核验失败，未确认归档完成",
                    "上传已完成，但云端大小连续 5 次无法确认（OpenList 探测超时），本地文件已保留，可稍后重试"):
            code, _, fix = bridge_server._classify_archive_error(msg)
            self.assertEqual(code, "verify_pending", msg)
            self.assertTrue(fix)

    def test_new_categories_are_retryable(self):
        self.assertTrue(bridge_server._is_retryable_archive_error("bridge 重启导致上传中断，可重试"))
        self.assertTrue(bridge_server._is_retryable_archive_error("上传后云端存在性核验失败，未确认归档完成"))


class TestStaleFailureSupersede(unittest.TestCase):
    """同一文件后来归档成功时，旧失败记录必须从告警里消失。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tg_sup_")
        self._orig_jobs = dict(bridge_server._ARCHIVE_JOBS)
        self._orig_file = bridge_server._ARCHIVE_FILE
        bridge_server._ARCHIVE_FILE = os.path.join(self.tmp, ".archive_jobs.json")
        bridge_server._ARCHIVE_JOBS.clear()
        self.media = os.path.join(self.tmp, "s.mp4")
        with open(self.media, "wb") as f:
            f.write(b"z" * 512)

    def tearDown(self):
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self._orig_jobs)
        bridge_server._ARCHIVE_FILE = self._orig_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk(self, jid, state, uid):
        j = {
            "id": jid, "unique_id": uid, "filename": "s.mp4",
            "size_bytes": 512, "local_path": self.media,
            "remote_dir": "/onedrive/yello", "remote_path": "/onedrive/yello/s.mp4",
            "policy": "overwrite", "delete_local": True,
            "state": state, "progress": 0, "error": "",
            "created_at": float(len(jid)), "updated_at": 1.0, "archived_at": 0.0,
        }
        if state == "failed":
            j["error"] = "bridge 重启导致上传中断，可重试"
        bridge_server._ARCHIVE_JOBS[jid] = j
        return j

    def test_failed_superseded_when_same_uid_already_done(self):
        """线上 45 条里有 41 条属于这种情况：后来重试成功了，旧记录还挂着。"""
        self._mk("f1", "failed", "uid-A")
        self._mk("d1", "done", "uid-A")
        n = bridge_server._archive_supersede_stale_failures()
        self.assertEqual(n, 1)
        self.assertEqual(bridge_server._ARCHIVE_JOBS["f1"]["state"], "cancelled")
        self.assertTrue(bridge_server._ARCHIVE_JOBS["f1"].get("superseded_by_done"))
        self.assertEqual(bridge_server._ARCHIVE_JOBS["d1"]["state"], "done",
                         "已成功的记录不得被改动")

    def test_other_uid_failure_is_kept(self):
        """不同文件的失败绝不能因为别的文件成功就被收敛掉。"""
        self._mk("f2", "failed", "uid-B")
        self._mk("d2", "done", "uid-C")
        n = bridge_server._archive_supersede_stale_failures()
        self.assertEqual(n, 0)
        self.assertEqual(bridge_server._ARCHIVE_JOBS["f2"]["state"], "failed")

    def test_superseded_records_leave_the_alert_list(self):
        """收敛后 /api/archive/failed 不再返回它们（告警数真实下降）。"""
        self._mk("f3", "failed", "uid-D")
        self._mk("d3", "done", "uid-D")
        bridge_server._archive_supersede_stale_failures()
        client = TestClient(bridge_server.app)
        tok = bridge_server._make_portal_token()
        resp = client.get("/api/archive/failed",
                          cookies={bridge_server.PORTAL_COOKIE: tok})
        data = resp.json()
        ids = [x["id"] for x in (data.get("failedJobs") or [])]
        self.assertNotIn("f3", ids)


class TestParallelArchiveRace(unittest.TestCase):
    """同一文件被重复入队：后一个任务读到本地已被删时不能报成失败。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tg_race_")
        self._orig_jobs = dict(bridge_server._ARCHIVE_JOBS)
        self._orig_file = bridge_server._ARCHIVE_FILE
        bridge_server._ARCHIVE_FILE = os.path.join(self.tmp, ".archive_jobs.json")
        bridge_server._ARCHIVE_JOBS.clear()
        self.media = os.path.join(self.tmp, "race.mp4")
        with open(self.media, "wb") as f:
            f.write(b"r" * 1024)

    def tearDown(self):
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self._orig_jobs)
        bridge_server._ARCHIVE_FILE = self._orig_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _job(self):
        return {
            "id": "race1", "unique_id": "uid-race", "filename": "race.mp4",
            "size_bytes": 1024, "local_path": self.media,
            "remote_dir": "/onedrive/yello", "remote_path": "/onedrive/yello/race.mp4",
            "policy": "overwrite", "delete_local": True,
            "state": "queued", "progress": 0, "error": "",
            "created_at": 1.0, "updated_at": 1.0, "archived_at": 0.0,
        }

    def test_local_removed_mid_upload_but_cloud_complete_is_done(self):
        """实测线上 1 例：拿到锁时本地已被并行任务删掉，但云端早已完整。"""
        job = self._job()
        bridge_server._ARCHIVE_JOBS["race1"] = job
        with patch("bridge_server._openlist_token", new=AsyncMock(return_value="tk")), \
             patch("bridge_server._openlist_mkdir_tree", new=AsyncMock()), \
             patch("bridge_server._openlist_put_once",
                   new=AsyncMock(side_effect=FileNotFoundError("gone"))), \
             patch("bridge_server._openlist_stat_ex",
                   new=AsyncMock(return_value=(STAT_MISSING, 0, {}))), \
             patch("bridge_server._openlist_verify_size",
                   new=AsyncMock(return_value=(STAT_OK, 1024))), \
             patch("bridge_server.notify_archive_success", new=MagicMock()):
            async def run():
                await bridge_server._archive_worker(job)
            asyncio.run(run())

        self.assertEqual(job["state"], "done")
        self.assertEqual(job.get("remote_size"), 1024)
        self.assertTrue(job.get("local_deleted"))

    def test_local_removed_mid_upload_and_cloud_incomplete_fails(self):
        """云端也不完整时必须如实报错，不能假装成功。"""
        job = self._job()
        bridge_server._ARCHIVE_JOBS["race1"] = job
        with patch("bridge_server._openlist_token", new=AsyncMock(return_value="tk")), \
             patch("bridge_server._openlist_mkdir_tree", new=AsyncMock()), \
             patch("bridge_server._openlist_put_once",
                   new=AsyncMock(side_effect=FileNotFoundError("gone"))), \
             patch("bridge_server._openlist_stat_ex",
                   new=AsyncMock(return_value=(STAT_MISSING, 0, {}))), \
             patch("bridge_server._openlist_verify_size",
                   new=AsyncMock(return_value=(STAT_MISSING, 0))), \
             patch("bridge_server.notify_archive_failed", new=MagicMock()):
            async def run():
                await bridge_server._archive_worker(job)
            asyncio.run(run())

        self.assertEqual(job["state"], "failed")
        self.assertIn("被删除", job["error"])


if __name__ == "__main__":
    unittest.main()
