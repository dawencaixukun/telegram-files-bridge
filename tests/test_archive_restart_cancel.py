# -*- coding: utf-8 -*-
"""
tests/test_archive_restart_cancel.py
===================================
回归测试：「本地在存的文件一直没归档成功，是不是并发问题？」

线上实测答案：不是并发。是**进程退出把在传任务取消后，写成了「用户已取消」**，
这些记录就再也不会自动重试，文件一直留在本地。

证据（.archive_jobs.json，30 条「用户已取消」）：
  * 其中 9 组的 updated_at 精确到秒相同（09:16:05 x2、03:10:24 x2、
    03:11:10 x2、03:14:30 x2 …），成对出现正好等于并发槽位数
    —— 这是 bridge_server shutdown 的批量 task.cancel()，不是人手点击；
  * 这 5 个 uid 没有任何 done 记录，本地文件却都还在（「本地在存」里显示
    未归档），云端也没有副本。

本文件锁定三条契约：
1. 用户取消 vs 进程退出中断必须区分：后者回 queued + resume_pending，
   可被自动续跑；前者保持 cancelled，不再自动重试。
2. 历史误标可自愈（_archive_heal_restart_cancelled），但不得误改真人取消。
3. /archive/cancel 必须在 cancel() 之前打 user_cancelled 标记。
"""
import asyncio
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bridge_server
from services.archive_service import _archive_heal_restart_cancelled


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tg_rc_")
        self._orig_jobs = dict(bridge_server._ARCHIVE_JOBS)
        self._orig_file = bridge_server._ARCHIVE_FILE
        bridge_server._ARCHIVE_FILE = os.path.join(self.tmp, ".archive_jobs.json")
        bridge_server._ARCHIVE_JOBS.clear()
        self.media = os.path.join(self.tmp, "v.mp4")
        with open(self.media, "wb") as f:
            f.write(b"x" * 2048)

    def tearDown(self):
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self._orig_jobs)
        bridge_server._ARCHIVE_FILE = self._orig_file
        shutil.rmtree(self.tmp, ignore_errors=True)


def _job(jid, state="uploading", **kw):
    base = {
        "id": jid, "unique_id": "u" + jid, "filename": jid + ".mp4",
        "local_path": "", "remote_dir": "/onedrive/yello",
        "remote_path": "/onedrive/yello/" + jid + ".mp4",
        "size_bytes": 2048, "state": state, "progress": 0, "error": "",
        "created_at": time.time(), "updated_at": time.time(),
        "archived_at": 0.0,
    }
    base.update(kw)
    return base


class TestShutdownCancelIsNotUserCancel(_Base):
    """进程退出取消 → 回 queued 待续跑，绝不写「用户已取消」。"""

    def test_shutdown_cancel_becomes_resumable(self):
        job = _job("restart1", local_path=self.media)
        bridge_server._ARCHIVE_JOBS[job["id"]] = job

        # 模拟 worker 正在 trying 上传时被 shutdown 打断
        async def fake_token():
            raise asyncio.CancelledError()

        with patch.object(bridge_server, "_openlist_token", new=fake_token), \
                patch.object(bridge_server, "_archive_save", new=lambda: None), \
                patch.object(bridge_server, "notify_archive_failed", new=MagicMock()):
            try:
                asyncio.run(bridge_server._archive_worker(job))
            except asyncio.CancelledError:
                pass

        self.assertEqual(job["state"], "queued",
                         "进程退出中断必须回到 queued，才能被自动续跑")
        self.assertTrue(job.get("resume_pending"))
        self.assertTrue(job.get("interrupted_by_restart"))
        self.assertNotEqual(job["error"], "用户已取消",
                            "重启不能伪装成用户取消，否则永远不会自动重试")

    def test_user_cancel_stays_cancelled(self):
        job = _job("uc1", state="cancelled", local_path=self.media,
                   error="用户已取消", user_cancelled=True)
        bridge_server._ARCHIVE_JOBS[job["id"]] = job

        async def boom():
            raise asyncio.CancelledError()

        with patch.object(bridge_server, "_openlist_token", new=boom), \
                patch.object(bridge_server, "_archive_save", new=lambda: None):
            try:
                asyncio.run(bridge_server._archive_worker(job))
            except asyncio.CancelledError:
                pass

        self.assertEqual(job["state"], "cancelled", "用户取消必须保持取消")
        self.assertFalse(job.get("resume_pending"))


class TestHealRestartMislabel(_Base):
    """历史「用户已取消」记录的自愈：只改该改的。"""

    def test_heals_restart_mislabel(self):
        job = _job("old1", state="cancelled", local_path=self.media,
                   error="用户已取消")
        bridge_server._ARCHIVE_JOBS[job["id"]] = job
        n = _archive_heal_restart_cancelled()
        self.assertEqual(n, 1)
        self.assertEqual(job["state"], "queued")
        self.assertTrue(job.get("resume_pending"))

    def test_does_not_heal_real_user_cancel(self):
        job = _job("real1", state="cancelled", local_path=self.media,
                   error="用户已取消", user_cancelled=True)
        bridge_server._ARCHIVE_JOBS[job["id"]] = job
        self.assertEqual(_archive_heal_restart_cancelled(), 0)
        self.assertEqual(job["state"], "cancelled")

    def test_does_not_heal_when_already_archived(self):
        """同一 uid 已有 done 记录 → 那条 cancelled 只是历史噪音，不动它。"""
        done = _job("d1", state="done", unique_id="same-uid",
                    remote_path="/onedrive/yello/a.mp4")
        old = _job("o1", state="cancelled", unique_id="same-uid",
                   local_path=self.media, error="用户已取消")
        bridge_server._ARCHIVE_JOBS[done["id"]] = done
        bridge_server._ARCHIVE_JOBS[old["id"]] = old
        self.assertEqual(_archive_heal_restart_cancelled(), 0)
        self.assertEqual(old["state"], "cancelled")

    def test_does_not_heal_when_local_file_gone(self):
        """本地文件没了就没法重传，改状态只会制造假待办。"""
        job = _job("gone1", state="cancelled",
                   local_path=os.path.join(self.tmp, "missing.mp4"),
                   error="用户已取消")
        bridge_server._ARCHIVE_JOBS[job["id"]] = job
        self.assertEqual(_archive_heal_restart_cancelled(), 0)
        self.assertEqual(job["state"], "cancelled")

    def test_does_not_heal_superseded(self):
        job = _job("sup1", state="cancelled", local_path=self.media,
                   error="用户已取消", superseded_by_done=True)
        bridge_server._ARCHIVE_JOBS[job["id"]] = job
        self.assertEqual(_archive_heal_restart_cancelled(), 0)


class TestCancelEndpointMarksUserCancel(unittest.TestCase):
    """/archive/cancel 必须先打标记再 cancel，否则 worker 分不清来源。"""

    def test_source_marks_before_cancel(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "routers", "archive.py"), encoding="utf-8") as f:
            src = f.read()
        seg = src[src.index("async def archive_cancel"):]
        seg = seg[:seg.index("@router.get(\"/api/archive/failed\"")] if \
            '@router.get("/api/archive/failed"' in seg else seg[:2000]
        mark = seg.index("user_cancelled")
        cancel = seg.index("task.cancel()")
        self.assertLess(mark, cancel,
                        "user_cancelled 标记必须在 task.cancel() 之前写入")


if __name__ == "__main__":
    unittest.main()
