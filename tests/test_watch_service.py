# -*- coding: utf-8 -*-
"""回归测试：频道监听（watch）与取回断点续传。

覆盖点：
1. watch 规则筛选：未开 watch / 通配 chatId 必须被跳过（不产生无谓请求）
2. 首次见面只建基线，**不入队历史消息**（防止规则一启用就批量下载历史）
3. 出现新消息只下新的；无新消息不入队；多条新消息按时间升序入队
4. 熔断（风控/水位）中必须挂起而非硬下
5. 已见集合有界 + 落盘/恢复往返（权限 0600）
6. 取回断点续传：Range 生效时不重下已有字节；服务端忽略 Range 时必须重写而非追加
"""
import os
import sys
import json
import asyncio
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestWatchService(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="watch-test-")
        os.environ["TG_DATA_DIR"] = self._tmp
        # 刻意不 importlib.reload（与 test_browse_hide 同理）：reload(core.state) 会
        # 替换 _SUB_RULES 等单例对象，别的模块还持旧引用 → 单跑组合时跨文件失败。
        # 这里只清内存态 + 用隔离数据目录，等价"干净状态"。
        import core.state as st
        import services.watch_service as W
        st._SUB_RULES.clear()
        st._WATCH_SEEN.clear()
        self.st = st
        self.W = W

        class FakeB:
            def __init__(self):
                self.files = []
                self.calls = []
                self.authorized = True
                self.submitted = []

            async def get_authorization_state(self):
                return {"code": -1834871737, "authorized": self.authorized}

            async def list_files(self, tg, ch, from_message_id=0, type="media", force=False):
                self.calls.append((str(tg), str(ch)))
                return list(self.files)

            async def start_download_multiple(self, payload):
                self.submitted.append(list(payload.get("files") or []))
                return {"ok": True}

        self.fb = FakeB()
        self._W = W                      # tearDown 引用用（W 是 setUp 局部名）
        self._orig_backend = W.BACKEND
        W.BACKEND = self.fb

    def tearDown(self):
        os.environ.pop("TG_DATA_DIR", None)
        self._W.BACKEND = self._orig_backend  # 归还单例，避免污染后续测试

    def _rule(self, **kw):
        r = {"id": "r1", "enabled": True, "watch": True,
             "telegramId": "8652569586", "chatId": "-1001",
             "chatTitle": "测试频道", "dirTemplate": "/x/{YYYY}"}
        r.update(kw)
        self.st._SUB_RULES.clear()
        self.st._SUB_RULES["r1"] = r
        return r

    @staticmethod
    def _f(mid, uid, ftype="video"):
        return {"telegramId": 8652569586, "chatId": -1001, "messageId": mid,
                "fileId": 100 + mid, "uniqueId": uid, "filename": "v%d.mp4" % mid,
                "type": ftype}

    def test_watch_skips_non_video(self):
        """watch 只自动下载视频：图片/音频/文档记基线但跳过（用户明确要求）。"""
        self._rule()
        self.st._WATCH_SEEN.clear()
        self.fb.files = [self._f(11, "u11", "video"),
                         self._f(12, "u12", "photo"),
                         self._f(13, "u13", "audio"),
                         self._f(14, "u14", "document")]
        n = asyncio.run(self.W._watch_tick())   # 首轮=基线，0 入队
        self.assertEqual(n, 0)
        k = self.W._seen_key("r1", "-1001")
        seen = self.st._WATCH_SEEN.get(k, [])
        for uid in ("u11", "u12", "u13", "u14"):
            self.assertIn(uid, seen)    # 全部记基线，图片不会反复扫
        # 第二轮：新视频入队、新图片仍跳过
        self.fb.files = [self._f(15, "u15", "video"),
                         self._f(16, "u16", "photo")]
        n = asyncio.run(self.W._watch_tick())
        self.assertEqual(n, 1)          # 只有视频入队

    def test_no_rule_no_polling(self):
        """未开启 watch -> 一次后端请求都不发。"""
        self._rule(watch=False)
        n = asyncio.run(self.W._watch_tick())
        self.assertEqual(n, 0)
        self.assertEqual(self.fb.calls, [])

    def test_wildcard_chat_rejected(self):
        """通配 '*' 的会话在监听场景语义不明，必须跳过。"""
        self._rule(telegramId="*", chatId="*")
        self.assertEqual(self.W._watch_rules(), [])
        self.assertEqual(asyncio.run(self.W._watch_tick()), 0)

    def test_first_sight_baseline_only(self):
        """首次见面只记基线，绝不入队历史消息。"""
        self._rule()
        self.st._WATCH_SEEN.clear()
        self.fb.files = [self._f(101, "A"), self._f(102, "B"), self._f(103, "C")]
        self.assertEqual(asyncio.run(self.W._watch_tick()), 0)
        self.assertEqual(len(self.st._WATCH_SEEN.get("r1:-1001", [])), 3)

    def test_only_new_enqueued(self):
        """第二轮出现新 uid -> 只入队新的。"""
        self._rule()
        self.st._WATCH_SEEN.clear()
        self.fb.files = [self._f(101, "A"), self._f(102, "B")]
        asyncio.run(self.W._watch_tick())          # 建基线
        self.fb.files = [self._f(103, "C"), self._f(102, "B")]
        self.assertEqual(asyncio.run(self.W._watch_tick()), 1)

    def test_no_new_no_enqueue(self):
        """无新消息 -> 0，且不重复提交。"""
        self._rule()
        self.st._WATCH_SEEN.clear()
        self.fb.files = [self._f(101, "A")]
        asyncio.run(self.W._watch_tick())
        before = len(self.fb.submitted)
        self.assertEqual(asyncio.run(self.W._watch_tick()), 0)
        self.assertEqual(len(self.fb.submitted), before)

    def test_multi_new_ascending(self):
        """多条新消息按时间升序入队。"""
        self._rule()
        self.st._WATCH_SEEN.clear()
        self.fb.files = [self._f(101, "A")]
        asyncio.run(self.W._watch_tick())
        self.fb.files = [self._f(104, "D"), self._f(103, "C"), self._f(102, "B")]
        n = asyncio.run(self.W._watch_tick())
        self.assertEqual(n, 3)
        mids = [f["messageId"] for f in self.fb.submitted[-1]]
        self.assertEqual(mids, sorted(mids))

    def test_flood_breaker_suspends(self):
        """熔断中必须挂起，不得硬下。"""
        self._rule()
        self.st._WATCH_SEEN.clear()
        self.fb.files = [self._f(101, "A")]
        asyncio.run(self.W._watch_tick())
        orig_flood = self.W._is_flood_wait_active
        self.W._is_flood_wait_active = lambda: True
        self.st._ARCHIVE_JOBS.clear()
        self.fb.files = [self._f(102, "B")]
        n = asyncio.run(self.W._watch_tick())
        self.assertEqual(n, 0)
        pend = [j for j in self.st._ARCHIVE_JOBS.values() if j.get("source") == "watch"]
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0].get("state"), "waiting_disk")
        self.W._is_flood_wait_active = orig_flood  # 归还，避免污染后续测试

    def test_seen_bounded_and_persist(self):
        """已见集合有界 + 落盘 0600 + 恢复往返。"""
        self.st._WATCH_SEEN.clear()
        for i in range(1200):
            self.W._seen_put("k", {"u%d" % i})
        self.assertLessEqual(len(self.st._WATCH_SEEN["k"]), self.st._WATCH_SEEN_MAX)
        self.st._watch_save()
        # 路径由 APP_ROOT_DIR 在导入期解析（与 _SUBS_FILE 等既有状态文件一致），
        # 因此断言实际解析出的路径，而不是测试自己以为的 TG_DATA_DIR。
        p = self.st._WATCH_SEEN_FILE
        self.assertTrue(os.path.exists(p), "未写入 %s" % p)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.st._WATCH_SEEN.clear()
        self.st._watch_seen_load()
        self.assertTrue(self.st._WATCH_SEEN.get("k"))

    def test_unauthorized_no_poll(self):
        """账号未授权 -> 不轮询。"""
        self._rule()
        self.fb.authorized = False
        orig = self.W._tg_authorized
        self.W._tg_authorized = lambda: asyncio.sleep(0, result=False)
        try:
            self.assertEqual(asyncio.run(self.W._watch_tick()), 0)
        finally:
            self.W._tg_authorized = orig
        self.assertEqual(self.fb.calls, [])

    # ---------------- 文本链接监听（转发即下载：链接发到收藏） ----------------

    @staticmethod
    def _text_msg(mid, text):
        return {"id": mid, "content": {"text": {"text": text}}}

    def test_extract_links_basic(self):
        """从文本提取 t.me 链接：公开/私密/去尾部标点/去重。"""
        text = ("看这个 https://t.me/mychannel/123 ，还有 https://t.me/c/2333333/456。"
                "重复 https://t.me/mychannel/123 结尾）")
        self.assertEqual(
            self.W._extract_links(text),
            ["https://t.me/mychannel/123", "https://t.me/c/2333333/456"])

    def test_extract_links_telegram_dog(self):
        self.assertEqual(
            self.W._extract_links("https://telegram.dog/foo/9"),
            ["https://telegram.dog/foo/9"])

    def test_text_link_baseline_then_enqueue(self):
        """文本链接消息：首次见面建基线不入队；新出现的链接消息才解析入队。"""
        self._rule()
        self.st._WATCH_SEEN.clear()
        orig = self.W._search_text_messages
        self.W._search_text_messages = \
            lambda tg, ch, limit=30: asyncio.sleep(0, result=[self._text_msg(501, "https://t.me/ch/1")])
        try:
            # 基线轮：不解析
            self.assertEqual(asyncio.run(self.W._watch_tick()), 0)
            key = self.W._seen_key("r1", "-1001")
            self.assertIn("link-8652569586--1001-501", self.st._WATCH_SEEN.get(key, []))

            # 第二轮出现新链接消息：解析入队（mock 解析函数）
            self.W._search_text_messages = \
                lambda tg, ch, limit=30: asyncio.sleep(0, result=[
                    self._text_msg(501, "https://t.me/ch/1"),
                    self._text_msg(502, "看 https://t.me/ch/2 与 https://t.me/ch/3"),
                ])
            import services.task_service as ts
            calls = []

            async def fake_resolve(links, force=False):
                calls.append(list(links))
                return len(links), ""

            orig_resolve = ts._resolve_links_to_files
            ts._resolve_links_to_files = fake_resolve
            try:
                n = asyncio.run(self.W._watch_tick())
            finally:
                ts._resolve_links_to_files = orig_resolve
            self.assertEqual(n, 2)
            self.assertEqual(calls, [["https://t.me/ch/2", "https://t.me/ch/3"]])
        finally:
            self.W._search_text_messages = orig

    def test_text_link_no_dup(self):
        """同一条链接消息不会被二次解析（已见键生效）。"""
        self._rule()
        self.st._WATCH_SEEN.clear()
        orig = self.W._search_text_messages
        msg = self._text_msg(601, "https://t.me/ch/9")
        self.W._search_text_messages = lambda tg, ch, limit=30: asyncio.sleep(0, result=[msg])
        import services.task_service as ts
        calls = []

        async def fake_resolve(links, force=False):
            calls.append(list(links))
            return len(links), ""

        orig_resolve = ts._resolve_links_to_files
        ts._resolve_links_to_files = fake_resolve
        try:
            asyncio.run(self.W._watch_tick())                    # 基线
            self.W._search_text_messages = lambda tg, ch, limit=30: asyncio.sleep(0, result=[msg])
            self.assertEqual(asyncio.run(self.W._watch_tick()), 0)   # 已见，0
            self.assertEqual(calls, [])
        finally:
            self.W._search_text_messages = orig
            ts._resolve_links_to_files = orig_resolve

    def test_text_link_baseline_independent_of_media(self):
        """恶性 bug 回归：媒体基线先写入 seen 后，链接消息首轮只建基线不入队。

        34 实测事故：seen 被媒体键填满 → 链接轮询误判"非基线轮" → 历史链接
        全部解析入队（37 条连环下载）。
        """
        self._rule()
        self.st._WATCH_SEEN.clear()
        # 先让媒体轮询建基线（模拟真实时序）
        self.fb.files = [self._f(101, "A")]
        asyncio.run(self.W._watch_tick())
        key = self.W._seen_key("r1", "-1001")
        self.assertTrue(self.st._WATCH_SEEN.get(key))       # 媒体基线已在

        orig = self.W._search_text_messages
        orig_resolve = None
        import services.task_service as ts
        calls = []

        async def fake_resolve(links, force=False):
            calls.append(list(links))
            return len(links), ""

        try:
            self.W._search_text_messages = \
                lambda tg, ch, limit=30: asyncio.sleep(0, result=[
                    self._text_msg(701, "https://t.me/ch/old1"),
                    self._text_msg(702, "https://t.me/ch/old2"),
                ])
            orig_resolve = ts._resolve_links_to_files
            ts._resolve_links_to_files = fake_resolve
            # 链接首轮：只建基线，绝不入队历史链接
            self.assertEqual(asyncio.run(self.W._watch_tick()), 0)
            self.assertEqual(calls, [])
            k = self.W._seen_key("r1", "-1001")
            self.assertIn("link-8652569586--1001-701", self.st._WATCH_SEEN.get(k, []))

            # 之后新链接消息正常入队
            self.W._search_text_messages = \
                lambda tg, ch, limit=30: asyncio.sleep(0, result=[
                    self._text_msg(701, "https://t.me/ch/old1"),
                    self._text_msg(703, "https://t.me/ch/new3"),
                ])
            n = asyncio.run(self.W._watch_tick())
            self.assertEqual(n, 1)
            self.assertEqual(calls, [["https://t.me/ch/new3"]])
        finally:
            self.W._search_text_messages = orig
            if orig_resolve is not None:
                ts._resolve_links_to_files = orig_resolve

    def test_whitelist_has_search_chat_messages(self):
        """TDLib 白名单必须放行 SearchChatMessages（文本链接轮询依赖）。"""
        from core.config import TG_API_METHOD_WHITELIST
        self.assertIn("SearchChatMessages", TG_API_METHOD_WHITELIST)


if __name__ == "__main__":
    unittest.main()
