# -*- coding: utf-8 -*-
"""回归测试：侧边栏「本地在存」徽标必须与页面列表同一口径。

用户报告：本地在存提示 5 个，进页面实际只有 2 个文件。

根因：徽标口径是 `status not in (archived, isolated)`，把
「下载中 / 失败 / 已下载但本地文件已删」全算成在存；而页面经过
`_is_local_in_stock` 严格门禁（磁盘真实存在才显示）。

修复：徽标改用同一口径 —— 下载完成 且 文件真实在磁盘 且 uid 未标记删除。
"""
import os
import tempfile
import unittest
import core.state as state
from services.task_service import _local_stock_exists, _to_task


def _rec(uid, name, dl_status="completed", local_path=""):
    return {"uniqueId": uid, "fileName": name, "size": 100,
            "downloadStatus": dl_status, "localPath": local_path,
            "date": 1757300000000}


class TestLocalStockBadge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="stock-badge-")
        cls.f1 = os.path.join(cls.tmp, "a.mkv")
        cls.f2 = os.path.join(cls.tmp, "b.mkv")
        for p in (cls.f1, cls.f2):
            with open(p, "wb") as f:
                f.write(b"x")

    def test_completed_and_on_disk_counts(self):
        """下载完成且文件在磁盘 → 计入。"""
        t = _to_task(_rec("U1", "a.mkv", local_path=self.f1), 1, {})
        self.assertTrue(_local_stock_exists(t))

    def test_downloading_with_real_file_counts(self):
        """下载中但文件已在盘（缓冲/续传）：与页面口径一致 → 计入。"""
        t = _to_task(_rec("U2", "b.mkv", dl_status="downloading",
                          local_path=self.f1), 1, {})
        self.assertTrue(_local_stock_exists(t))

    def test_failed_with_real_file_counts(self):
        """失败但文件在盘：与页面口径一致 → 计入（文件确实占着磁盘）。"""
        t = _to_task(_rec("U3", "c.mkv", dl_status="error",
                          local_path=self.f1), 1, {})
        self.assertTrue(_local_stock_exists(t))

    def test_file_deleted_not_counted(self):
        """已下载完成但本地文件已被删除（归档后清理）：不得计入。"""
        t = _to_task(_rec("U4", "d.mkv",
                          local_path=os.path.join(self.tmp, "不存在.mkv")), 1, {})
        self.assertFalse(_local_stock_exists(t))

    def test_deleted_uid_not_counted(self):
        """uid 已被标记本地删除：即使磁盘上碰巧有文件也不计入。"""
        saved = set(state._DELETED_LOCAL_UIDS)
        try:
            t = _to_task(_rec("U5", "e.mkv", local_path=self.f1), 1, {})
            state._DELETED_LOCAL_UIDS.add("U5")
            self.assertFalse(_local_stock_exists(t))
        finally:
            state._DELETED_LOCAL_UIDS.clear()
            state._DELETED_LOCAL_UIDS.update(saved)

    def test_no_local_path_not_counted(self):
        """无本地路径（如挂起/冷却任务）：不得计入，也不得抛异常。"""
        for lp in ("", "—", None):
            with self.subTest(local_path=lp):
                t = _to_task(_rec("U6", "f.mkv", local_path=lp), 1, {})
                self.assertFalse(_local_stock_exists(t))

    def test_badge_matches_page_gate(self):
        """端到端：同一批任务，徽标数与页面门禁过滤后的数必须一致。"""
        from services.task_service import _to_local_file_from_task
        records = [
            _rec("A1", "a.mkv", local_path=self.f1),
            _rec("A2", "b.mkv", local_path=self.f2),
            _rec("A3", "downloading.mkv", dl_status="downloading", local_path=self.f1),
            _rec("A4", "failed.mkv", dl_status="error"),
            _rec("A5", "gone.mkv", local_path=os.path.join(self.tmp, "x.mkv")),
        ]
        tasks = [_to_task(r, i + 1, {}) for i, r in enumerate(records)]

        # 新徽标口径（task_service._ctx 实际使用的表达式）
        badge = sum(1 for t in tasks if _local_stock_exists(t))

        # 页面门禁（library.py 真实调用的 _is_local_in_stock，而非手抄副本）
        from services.task_service import _dedup_files, _is_local_in_stock
        from services.archive_service import _enrich_archive
        files = [_to_local_file_from_task(t) for t in tasks]
        enriched = [_enrich_archive(f, False) for f in _dedup_files(files)]
        page_count = sum(1 for f in enriched if _is_local_in_stock(f))

        self.assertEqual(badge, page_count,
                         "徽标口径与页面口径不一致: %d vs %d" % (badge, page_count))
        self.assertEqual(badge, 3)


if __name__ == "__main__":
    unittest.main()
