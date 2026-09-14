# -*- coding: utf-8 -*-
"""回归测试：侧栏会话隐藏（黑名单语义）。

语义（用户明确要求）：开关关 = 该会话立即从侧栏消失；开 = 显示。
集合为空 = 显示全部。收藏是硬性标准项：永不被隐藏、恒排第一。
"""
import os
import sys
import asyncio
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestBlacklistHide(unittest.TestCase):
    def setUp(self):
        # 刻意不做 importlib.reload：discover 模式下其它测试已导入全套模块，
        # reload 会打散共享单例（实测引发 37 处跨模块失败）。
        # 这里直接重置被测服务的内存态，等价于"干净的黑名单"。
        os.environ["TG_DATA_DIR"] = tempfile.mkdtemp(prefix="hide-test-")
        import services.browse_service as bs
        bs._BROWSE_PINS_LOADED = True
        bs._BROWSE_PINS = set()
        self.bs = bs

        class FakeB:
            async def list_telegrams(self, force=False):
                return [{"telegramId": "9", "name": "DONK小"}]

            async def list_chats(self, tg, force=False):
                return [
                    {"chatId": "9", "title": "me"},                      # 收藏（saved）
                    {"chatId": "-100A", "title": "全能搜索"},
                    {"chatId": "-100B", "title": "NodeSeek官方频道"},
                    {"chatId": "-100C", "title": "奶昔论坛交流群"},
                ]

        self._orig_backend = self.bs.BACKEND
        self.bs.BACKEND = FakeB()

    def tearDown(self):
        os.environ.pop("TG_DATA_DIR", None)
        self.bs._BROWSE_PINS = set()
        self.bs.BACKEND = self._orig_backend  # 归还单例，避免污染后续测试

    def _titles(self, tree):
        return [c["title"] for a in tree for c in a["chats"]]

    def test_empty_set_shows_all(self):
        """黑名单为空 = 显示全部。"""
        tree = asyncio.run(self.bs._browse_tree())
        ts = self._titles(tree)
        self.assertEqual(len(ts), 4)
        self.assertEqual(ts[0], "收藏 (Saved Messages)")

    def test_hidden_chat_disappears(self):
        """关掉谁谁就消失：加入黑名单即从侧栏移除。"""
        self.bs._BROWSE_PINS = {"-100A"}          # 隐藏「全能搜索」
        tree = asyncio.run(self.bs._browse_tree())
        ts = self._titles(tree)
        self.assertNotIn("全能搜索", ts)
        self.assertEqual(len(ts), 3)

    def test_saved_never_hidden(self):
        """收藏是硬性标准项：即使 id 被塞进黑名单也不隐藏，且恒第一。"""
        self.bs._BROWSE_PINS = {"9", "-100B"}     # 连收藏一起塞进去
        tree = asyncio.run(self.bs._browse_tree())
        ts = self._titles(tree)
        self.assertIn("收藏 (Saved Messages)", ts)
        self.assertEqual(ts[0], "收藏 (Saved Messages)")
        self.assertNotIn("NodeSeek官方频道", ts)

    def test_toggle_roundtrip(self):
        """隐藏 -> 恢复显示：集合增减往返。"""
        self.bs._BROWSE_PINS = {"-100A"}
        self.assertEqual(
            self.bs._browse_pin_apply("-100A", True), True)   # 显示（移出黑名单）
        tree = asyncio.run(self.bs._browse_tree())
        self.assertIn("全能搜索", self._titles(tree))
        self.assertEqual(
            self.bs._browse_pin_apply("-100A", False), False)  # 隐藏（加入黑名单）
        tree = asyncio.run(self.bs._browse_tree())
        self.assertNotIn("全能搜索", self._titles(tree))

    def test_apply_many_bulk(self):
        """批量隐藏/显示：单次落盘语义正确。"""
        n = self.bs._browse_pins_apply_many(["-100A", "-100B"], False)  # 隐藏两个
        self.assertEqual(n, 2)
        tree = asyncio.run(self.bs._browse_tree())
        ts = self._titles(tree)
        self.assertEqual(ts, ["收藏 (Saved Messages)", "奶昔论坛交流群"])
        n2 = self.bs._browse_pins_apply_many(["-100A"], True)           # 恢复一个
        self.assertEqual(n2, 1)
        tree = asyncio.run(self.bs._browse_tree())
        self.assertIn("全能搜索", self._titles(tree))
        self.assertNotIn("NodeSeek官方频道", self._titles(tree))

    def test_full_tree_unfiltered(self):
        """full=True 永远返回全部（管理面板列候选用）。"""
        self.bs._BROWSE_PINS = {"-100A", "-100B", "-100C"}
        tree = asyncio.run(self.bs._browse_tree(full=True))
        self.assertEqual(len(self._titles(tree)), 4)

    def test_clear_restores_all(self):
        """一键清空黑名单 = 全部恢复显示。"""
        self.bs._BROWSE_PINS = {"-100A", "-100B"}
        removed = self.bs._browse_pins_clear()
        self.assertEqual(removed, 2)
        tree = asyncio.run(self.bs._browse_tree())
        self.assertEqual(len(self._titles(tree)), 4)


if __name__ == "__main__":
    unittest.main()
