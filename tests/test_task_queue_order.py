# -*- coding: utf-8 -*-
"""回归测试：任务队列必须「进行中优先、已归档沉底」。

历史缺陷（用户可见）：tasks_all() 直接沿用后端返回顺序（本质是数据库返回序），
一条正在下载的任务会被几十条历史「已归档」记录淹没，用户必须滚屏才能找到
真正在跑的任务。

约束：
- 进行中（下载/上传/校验）必须在最前；
- 已归档、隔离必须沉到最后；
- 同优先级按时间倒序（新的在前）；
- 排序必须稳定，重复刷新行序不抖动。
"""
import time
import unittest

from services.task_service import _task_sort_key, tasks_all, _TASK_RANK
import core.state as _state_mod


def _mk(filename, status, age_sec, uid):
    return {
        "filename": filename,
        "status": status,
        "id": uid,
        "_unique_id": uid,
        "_date_ts": time.time() - age_sec,
    }


class TestTaskQueueOrdering(unittest.TestCase):
    def _sorted(self, tasks):
        return sorted(tasks, key=_task_sort_key)

    def test_active_tasks_come_first(self):
        """下载/上传/校验必须排在任何终态任务之前。"""
        tasks = [
            _mk("老归档.mkv", "archived", 90000, "a1"),
            _mk("下载中.mkv", "download", 60, "a2"),
            _mk("归档中.mkv", "upload", 120, "a3"),
            _mk("新归档.mkv", "archived", 300, "a4"),
            _mk("校验中.mkv", "verify", 30, "a5"),
        ]
        order = [t["status"] for t in self._sorted(tasks)]
        active = {"download", "upload", "verify"}
        # 前 3 行必须全是进行中
        self.assertEqual(set(order[:3]), active, "进行中任务必须占据最前 3 行")
        self.assertNotIn("archived", order[:3])

    def test_archived_and_isolated_sink_to_bottom(self):
        """已归档与隔离必须沉到最后，不能插在进行中任务前面。"""
        tasks = [
            _mk("归档1.mkv", "archived", 10, "b1"),
            _mk("下载中.mkv", "download", 5000, "b2"),
            _mk("隔离.mkv", "isolated", 20, "b3"),
            _mk("归档2.mkv", "archived", 30, "b4"),
        ]
        order = [t["status"] for t in self._sorted(tasks)]
        self.assertEqual(order[0], "download", "下载中必须排第一（哪怕它最旧）")
        self.assertEqual(order[-1], "isolated", "隔离必须沉到最底")

    def test_newer_first_within_same_rank(self):
        """同优先级内按时间倒序：新的在前。"""
        tasks = [
            _mk("旧归档.mkv", "archived", 90000, "c1"),
            _mk("新归档.mkv", "archived", 100, "c2"),
        ]
        out = self._sorted(tasks)
        self.assertEqual(out[0]["filename"], "新归档.mkv")

    def test_failed_before_downloaded_before_archived(self):
        """层次：失败(需注意) → 已下载(可操作) → 已归档(历史)。"""
        tasks = [
            _mk("归档.mkv", "archived", 10, "d1"),
            _mk("已下载.mkv", "downloaded", 20, "d2"),
            _mk("失败.mkv", "failed", 30, "d3"),
        ]
        order = [t["status"] for t in self._sorted(tasks)]
        self.assertEqual(order, ["failed", "downloaded", "archived"])

    def test_sort_is_stable_across_repeats(self):
        """排序稳定：同样输入反复排序，行序不得抖动。"""
        tasks = [
            _mk("A.mkv", "archived", 10, "e1"),
            _mk("B.mkv", "archived", 10, "e2"),
            _mk("C.mkv", "download", 10, "e3"),
        ]
        first = [t["id"] for t in self._sorted(tasks)]
        for _ in range(5):
            self.assertEqual([t["id"] for t in self._sorted(tasks)], first)

    def test_unknown_status_does_not_crash(self):
        """未知状态不能让排序抛异常，且应排在已归档之后。"""
        tasks = [
            _mk("怪状态.mkv", "some_new_status", 10, "f1"),
            _mk("归档.mkv", "archived", 10, "f2"),
        ]
        order = [t["status"] for t in self._sorted(tasks)]
        self.assertEqual(order[0], "archived")
        self.assertEqual(order[-1], "some_new_status")

    def test_missing_date_ts_is_safe(self):
        """缺 _date_ts（如挂起任务）不能抛异常。"""
        t = {"filename": "x.mkv", "status": "waiting_disk", "id": "g1"}
        key = _task_sort_key(t)
        self.assertEqual(key[0], _TASK_RANK["waiting_disk"])
        self.assertEqual(key[1], 0.0)

    def test_tasks_all_returns_sorted(self):
        """集成：tasks_all() 的返回值必须是排好序的（进行中在最前）。"""
        raw = [
            _mk("老归档.mkv", "archived", 90000, "h1"),
            _mk("下载中.mkv", "download", 9999, "h2"),
        ]

        async def run():
            from unittest.mock import AsyncMock, patch
            import bridge_server
            with patch("bridge_server._build_tasks", new=AsyncMock(return_value=list(raw))):
                return await tasks_all(force=True)

        import asyncio
        out = asyncio.run(run())
        self.assertTrue(out, "tasks_all 不应返回空")
        self.assertEqual(out[0]["status"], "download",
                         "tasks_all() 首行必须是进行中的任务")

    def test_rank_is_consistent_with_documented_order(self):
        """优先级表必须满足：进行中 < 挂起/待处理 < 失败 < 已下载 < 已归档 < 隔离。"""
        self.assertLess(_TASK_RANK["download"], _TASK_RANK["waiting_disk"])
        self.assertLess(_TASK_RANK["upload"], _TASK_RANK["pending"])
        self.assertLess(_TASK_RANK["pending"], _TASK_RANK["failed"])
        self.assertLess(_TASK_RANK["failed"], _TASK_RANK["downloaded"])
        self.assertLess(_TASK_RANK["downloaded"], _TASK_RANK["archived"])
        self.assertLess(_TASK_RANK["archived"], _TASK_RANK["isolated"])


if __name__ == "__main__":
    unittest.main()
