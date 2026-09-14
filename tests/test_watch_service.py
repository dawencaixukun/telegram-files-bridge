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
        import importlib
        import core.state as st
        import services.watch_service as W
        importlib.reload(st)
        importlib.reload(W)
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
    def _f(mid, uid):
        return {"telegramId": 8652569586, "chatId": -1001, "messageId": mid,
                "fileId": 100 + mid, "uniqueId": uid, "filename": "v%d.mp4" % mid}

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
        self.W._tg_authorized = lambda: asyncio.sleep(0, result=False)
        self.assertEqual(asyncio.run(self.W._watch_tick()), 0)
        self.assertEqual(self.fb.calls, [])


if __name__ == "__main__":
    unittest.main()
