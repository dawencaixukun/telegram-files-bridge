# -*- coding: utf-8 -*-
"""回归测试：OpenList 改名自愈。

用户在 OpenList 网页改名后，归档记录里的旧 remote_path 失效。
自愈策略：按【同目录 + 相同文件大小】重定位 ——
  * 唯一匹配 -> 返回新路径
  * 多个同大小 -> 无法确定，返回 None（绝不静默指错文件）
  * 0 个匹配 / 列目录失败 / size 未知 -> None
  * 收藏/旧名本身在候选里要排除
"""
import os
import sys
import asyncio
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.openlist_service import _openlist_relocate_after_rename


class TestRenameRelocate(unittest.TestCase):
    def setUp(self):
        self._tok = "tok"
        self._orig_list = None
        import services.openlist_service as ol
        self.ol = ol

    def tearDown(self):
        if self._orig_list is not None:
            self.ol.openlist_list_files = self._orig_list
            self._orig_list = None

    def _mock_dir(self, files, ok=True):
        async def fake_list(path="/"):
            if not ok:
                return {"ok": False, "message": "boom"}
            return {"ok": True, "files": files}
        self._orig_list = self.ol.openlist_list_files
        self.ol.openlist_list_files = fake_list

    def test_unique_size_match(self):
        """唯一同大小 -> 返回新路径。"""
        self._mock_dir([
            {"name": "old.mp4", "size": 111},           # 旧名（会被排除）
            {"name": "renamed.mp4", "size": 111},       # 改名后的它
            {"name": "other.mp4", "size": 222},
        ])
        r = asyncio.run(_openlist_relocate_after_rename("tok", "/onedrive/d/old.mp4", 111))
        self.assertEqual(r, "/onedrive/d/renamed.mp4")

    def test_multiple_same_size_returns_none(self):
        """多个同大小 -> 无法唯一确定，宁可失败。"""
        self._mock_dir([
            {"name": "a.mp4", "size": 111},
            {"name": "b.mp4", "size": 111},
        ])
        r = asyncio.run(_openlist_relocate_after_rename("tok", "/onedrive/d/old.mp4", 111))
        self.assertIsNone(r)

    def test_no_candidate_returns_none(self):
        """没有同大小文件 -> None。"""
        self._mock_dir([{"name": "x.mp4", "size": 999}])
        r = asyncio.run(_openlist_relocate_after_rename("tok", "/onedrive/d/old.mp4", 111))
        self.assertIsNone(r)

    def test_unknown_size_returns_none(self):
        """size 未知 -> 放弃（没有可靠依据）。"""
        self._mock_dir([{"name": "renamed.mp4", "size": 111}])
        r = asyncio.run(_openlist_relocate_after_rename("tok", "/onedrive/d/old.mp4", 0))
        self.assertIsNone(r)

    def test_dir_list_failure_returns_none(self):
        """列目录失败 -> None，不抛异常。"""
        self._mock_dir([], ok=False)
        r = asyncio.run(_openlist_relocate_after_rename("tok", "/onedrive/d/old.mp4", 111))
        self.assertIsNone(r)

    def test_root_path_returns_none(self):
        """无父目录（根路径）-> None。"""
        r = asyncio.run(_openlist_relocate_after_rename("tok", "file.mp4", 111))
        self.assertIsNone(r)


if __name__ == "__main__":
    unittest.main()
