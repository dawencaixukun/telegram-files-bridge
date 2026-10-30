# -*- coding: utf-8 -*-
"""
tests/test_archived_total_dedup.py
==================================
回归测试：「网盘里真有 919 条记录？」—— 仪表盘「云端归档累计」重复计数。

线上实测（34vps）：
  * .archive_jobs.json 里 state=done 的记录 495 条，去重 remote_path 后 456 个；
  * 后端任务列表里 status=archived 的 463 条，uid 全部都能在 done 里找到；
  * 卡片却显示 **919** = 456 + 463 —— 同一批文件被算了两遍。

根因：跨源去重失效。旧代码把「计数兜底集合」和「跨源去重集合」混成同一个
_archived_uids，而它只在 record **没有 remote_path** 时才写入 uid。
线上 495 条 done 全部带 remote_path，于是 _archived_uids 恒为空，
第二步的 `_uid in _archived_uids` 恒为 False，463 条已归档任务被当成
「归档日志里没有的新项」又加了一遍。

本文件锁定三条契约：
1. 同一批文件同时出现在归档日志和任务列表时，只算一次。
2. 只在归档日志里的、只在任务列表里的，各自都要算上（并集语义）。
3. 按 remote_path 去重：同一目标重复归档多次只算一个文件。
"""
import asyncio
import unittest
from unittest.mock import patch

import bridge_server
from services.task_service import _dashboard_stats


def _job(jid, path, uid, state="done"):
    return {"id": jid, "remote_path": path, "unique_id": uid,
            "state": state, "size_bytes": 1024, "archived_at": 1000.0}


def _task(uid, status="archived"):
    return {"status": status, "_unique_id": uid, "_size_bytes": 1024,
            "id": uid, "_date_ts": 1000.0, "filename": uid + ".mp4",
            "size": "1 KB", "source": "chat", "msg_id": "1",
            "local_path": "—", "cloud_path": "—", "_type": "video"}


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig = dict(bridge_server._ARCHIVE_JOBS)
        bridge_server._ARCHIVE_JOBS.clear()

    def tearDown(self):
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self._orig)

    def _total(self, tasks):
        stats = asyncio.run(_dashboard_stats(tasks, {"used_gb": 1.0}))
        for k in stats["kpis"]:
            if k.get("label") == "云端归档累计":
                return int(str(k["value"]).replace(",", ""))
        self.fail("KPI 里没有『云端归档累计』")


class TestArchivedTotalDedup(_Base):
    def test_same_files_in_both_sources_counted_once(self):
        """核心回归：456 日志 + 463 任务（同一批）不能算出 919。"""
        for i in range(6):
            bridge_server._ARCHIVE_JOBS[f"j{i}"] = _job(
                f"j{i}", f"/onedrive/yello/v{i}.mp4", f"u{i}")
        tasks = [_task(f"u{i}") for i in range(6)]
        self.assertEqual(self._total(tasks), 6,
                         "同一批文件在归档日志与任务列表各出现一次，只能算一次")

    def test_union_counts_each_source(self):
        """只在日志里的 + 只在任务里的，都要算上。"""
        bridge_server._ARCHIVE_JOBS["j1"] = _job("j1", "/d/a.mp4", "uA")
        bridge_server._ARCHIVE_JOBS["j2"] = _job("j2", "/d/b.mp4", "uB")
        tasks = [_task("uB"), _task("uC")]
        self.assertEqual(self._total(tasks), 3, "uA(日志) + uB(两边) + uC(任务) = 3")

    def test_dedupe_by_remote_path(self):
        """同一目标重复归档多次只算一个文件。"""
        bridge_server._ARCHIVE_JOBS["j1"] = _job("j1", "/d/same.mp4", "u1")
        bridge_server._ARCHIVE_JOBS["j2"] = _job("j2", "/d/same.mp4", "u2")
        self.assertEqual(self._total([]), 1)

    def test_path_case_insensitive(self):
        bridge_server._ARCHIVE_JOBS["j1"] = _job("j1", "/onedrive/Yello/A.mp4", "u1")
        bridge_server._ARCHIVE_JOBS["j2"] = _job("j2", "/onedrive/yello/a.mp4", "u2")
        self.assertEqual(self._total([]), 1, "路径去重应大小写不敏感")

    def test_not_done_jobs_ignored(self):
        bridge_server._ARCHIVE_JOBS["f1"] = _job("f1", "/d/x.mp4", "uX", state="failed")
        bridge_server._ARCHIVE_JOBS["c1"] = _job("c1", "/d/y.mp4", "uY", state="cancelled")
        self.assertEqual(self._total([]), 0)

    def test_downloaded_status_not_counted_as_archived(self):
        """只下载未归档的不该进「云端归档累计」。"""
        bridge_server._ARCHIVE_JOBS["j1"] = _job("j1", "/d/a.mp4", "uA")
        tasks = [_task("uA"), _task("uZ", status="downloaded")]
        self.assertEqual(self._total(tasks), 1)

    def test_legacy_record_without_path_falls_back_to_uid(self):
        """没有 remote_path 的老记录用 uid 兜底计数。"""
        bridge_server._ARCHIVE_JOBS["j1"] = _job("j1", "", "uOnlyUid")
        self.assertEqual(self._total([]), 1)


if __name__ == "__main__":
    unittest.main()
