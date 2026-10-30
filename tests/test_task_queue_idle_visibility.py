# -*- coding: utf-8 -*-
"""
tests/test_task_queue_idle_visibility.py
=======================================
回归测试：「昨天提交了 100 个下载，任务队列里只记录了 10 条」。

线上实测（34vps，data.db 2013 行 / /api/files 482 条媒体记录）：
  * Java 后端在**归档成功后删除本地文件**时，会把 downloadStatus 重置为
    'idle' 并清空 local_path —— 461 条已归档 uid 在库里全是 idle；
  * tasks_all 的 _TASK_STATUSES 只认 downloading/paused/completed/error，
    472 条 idle 被整条过滤，队列只剩 10 条 completed；
  * 昨天归档的 95 条全部是 idle，于是「提交的任务全没了」。

本文件锁定三条契约：
1. idle 记录只要拿得出「被处理过」的证据（归档记录 / 已下载字节 / 本地路径），
   就必须进任务队列，并按归档状态还原成 archived / upload / failed。
2. 纯 idle 且无任何证据的记录（浏览时被索引进来的频道媒体）不得进队列，
   否则真正的任务会被几百条从未下载的记录淹没。
3. 「今日完成」只数今天，不能因为 idle 回归就变成历史累加器。
"""
import asyncio
import datetime as _dt
import time
import unittest
from unittest.mock import AsyncMock, patch

import bridge_server
from services.task_service import (
    _TASK_STATUSES, _is_real_task, _to_task, _count_done_today, _build_tasks,
)


class TestIdleStatusAccepted(unittest.TestCase):
    """idle 必须被承认成一种任务状态。"""

    def test_idle_is_in_task_statuses(self):
        self.assertIn("idle", _TASK_STATUSES,
                      "漏掉 idle 会让归档删本地后的任务整条从队列消失")


class _ArchiveIndexStub:
    """_archive_index_snapshot 的最小替身（不以 Test 开头，避免 pytest 误收集）。"""

    def __init__(self, jobs):
        self._jobs = jobs

    def latest_of(self, uid):
        return self._jobs.get(str(uid or ""))


class TestRealTaskPredicate(unittest.TestCase):
    """idle 记录进队列的门槛：必须拿得出被处理过的证据。"""

    def test_idle_with_archive_job_is_task(self):
        rec = {"downloadStatus": "idle", "uniqueId": "u1"}
        idx = _ArchiveIndexStub({"u1": {"state": "done", "remote_path": "/d/a.mp4"}})
        self.assertTrue(_is_real_task(rec, idx),
                        "归档记录证明它下载并归档过，不能当作从未下载")

    def test_idle_with_partial_download_is_task(self):
        rec = {"downloadStatus": "idle", "uniqueId": "u2", "downloadedSize": 4096}
        self.assertTrue(_is_real_task(rec, _ArchiveIndexStub({})))

    def test_idle_with_local_path_is_task(self):
        rec = {"downloadStatus": "idle", "uniqueId": "u3",
               "localPath": "/app/data/account/x/videos/a.mp4"}
        self.assertTrue(_is_real_task(rec, _ArchiveIndexStub({})))

    def test_bare_idle_is_not_a_task(self):
        rec = {"downloadStatus": "idle", "uniqueId": "u4",
               "downloadedSize": 0, "localPath": None}
        self.assertFalse(_is_real_task(rec, _ArchiveIndexStub({})),
                         "浏览时被索引进来的频道媒体不是任务")

    def test_non_idle_always_accepted(self):
        for st in ("downloading", "paused", "completed", "error"):
            rec = {"downloadStatus": st, "uniqueId": "ux"}
            self.assertTrue(_is_real_task(rec, _ArchiveIndexStub({})), st)


class TestIdleStatusMapping(unittest.TestCase):
    """idle + 归档记录要还原成正确的展示状态。"""

    def _task(self, arch_state):
        rec = {"downloadStatus": "idle", "uniqueId": "u1", "size": 2048,
               "messageId": 5, "chatId": 7, "fileName": "a.mp4"}
        idx = _ArchiveIndexStub({"u1": {"state": arch_state,
                                       "remote_path": "/onedrive/d/a.mp4"}})
        return _to_task(rec, 1, {}, idx)

    def test_done_archive_becomes_archived(self):
        t = self._task("done")
        self.assertEqual(t["status"], "archived")
        self.assertEqual(t["cloud_path"], "/onedrive/d/a.mp4")

    def test_uploading_archive_becomes_upload(self):
        self.assertEqual(self._task("uploading")["status"], "upload")

    def test_failed_archive_becomes_failed(self):
        self.assertEqual(self._task("failed")["status"], "failed")

    def test_idle_without_archive_is_pending(self):
        rec = {"downloadStatus": "idle", "uniqueId": "u9", "size": 2048,
               "messageId": 5, "chatId": 7, "fileName": "a.mp4"}
        t = _to_task(rec, 1, {}, _ArchiveIndexStub({}))
        self.assertEqual(t["status"], "pending")


class TestBuildTasksIncludesIdle(unittest.TestCase):
    """端到端：482 条记录里 472 条 idle，队列不能再只剩 10 条。"""

    def _run(self, files, arch_jobs):
        async def fake_info(force=False):
            return {"files": files, "count": len(files), "nextFromMessageId": 0}

        async def fake_titles():
            return {}

        with patch.object(bridge_server, "BACKEND") as be, \
                patch.object(bridge_server, "_chat_title_map", new=fake_titles), \
                patch.object(bridge_server, "_archive_index_snapshot",
                             new=lambda: _ArchiveIndexStub(arch_jobs)):
            be.list_all_files_page_info = fake_info
            be._unwrap_files = lambda raw: raw.get("files") or []
            return asyncio.run(_build_tasks(force=True))

    def test_archived_idle_records_stay_visible(self):
        files, jobs = [], {}
        for i in range(20):
            uid = f"done{i}"
            files.append({"downloadStatus": "idle", "uniqueId": uid, "size": 1024,
                          "messageId": i + 1, "chatId": 1, "fileName": f"v{i}.mp4"})
            jobs[uid] = {"state": "done", "remote_path": f"/onedrive/d/v{i}.mp4"}
        files.append({"downloadStatus": "completed", "uniqueId": "live1", "size": 2048,
                      "messageId": 99, "chatId": 1, "fileName": "live.mp4"})

        tasks = self._run(files, jobs)
        self.assertEqual(len(tasks), 21,
                         "20 条已归档 idle + 1 条 completed 都必须进队列")
        self.assertEqual(sum(1 for t in tasks if t["status"] == "archived"), 20)

    def test_unsubmitted_noise_stays_out(self):
        files, jobs = [], {}
        for i in range(30):
            files.append({"downloadStatus": "idle", "uniqueId": f"noise{i}",
                          "size": 1024, "downloadedSize": 0, "localPath": None,
                          "messageId": i + 1, "chatId": 1, "fileName": f"n{i}.mp4"})
        files.append({"downloadStatus": "idle", "uniqueId": "real", "size": 4096,
                      "downloadedSize": 4096, "messageId": 500, "chatId": 1,
                      "fileName": "real.mp4"})
        jobs["real"] = {"state": "done", "remote_path": "/onedrive/d/real.mp4"}

        tasks = self._run(files, jobs)
        self.assertEqual(len(tasks), 1, "从未提交过的频道媒体不得混进任务队列")
        self.assertEqual(tasks[0]["_unique_id"], "real")


class TestDoneTodayIsTodayOnly(unittest.TestCase):
    """「今日完成」必须只数今天，不能是历史累加器。"""

    def _ts_today(self, hour=10):
        now = _dt.datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)
        return now.timestamp()

    def test_counts_only_today(self):
        today = self._ts_today()
        old = today - 5 * 86400
        tasks = [
            {"status": "archived", "_archived_at": today * 1000},
            {"status": "downloaded", "_date_ts": today},
            {"status": "archived", "_archived_at": old * 1000},
            {"status": "archived", "_date_ts": old},
        ]
        self.assertEqual(_count_done_today(tasks), 2)

    def test_missing_timestamps_are_not_counted(self):
        tasks = [{"status": "archived"}, {"status": "downloaded", "_date_ts": 0}]
        self.assertEqual(_count_done_today(tasks), 0,
                         "没有时间戳不能算成今天完成")

    def test_ignores_non_done_statuses(self):
        today = self._ts_today()
        tasks = [
            {"status": "download", "_date_ts": today},
            {"status": "pending", "_date_ts": today},
            {"status": "failed", "_date_ts": today},
        ]
        self.assertEqual(_count_done_today(tasks), 0)


if __name__ == "__main__":
    unittest.main()
