# -*- coding: utf-8 -*-
"""回归测试：转发即下载（会话管理面板一键监听）。

覆盖点：
1. 开启 = 建 watch=True 规则；重复开启幂等（不重复建规则）
2. 已有普通订阅规则的会话：只翻 watch 开关，保留原目录模板
3. 关闭 = watch 置 False（规则保留）；从未开启时关闭幂等成功
4. watch-rules GET 只回 watch 规则，key 形如 "tg:chat"
5. 缺 tg/chat 返回 400
6. watch=True 的规则确实进入 _watch_rules()（watch_service 会轮询它）
"""
import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient


class TestWatchFromManage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="watch-manage-test-")
        os.environ["TG_DATA_DIR"] = self._tmp
        import bridge_server as appmod
        self.app = appmod
        # 不做 importlib.reload：与 test_browse_hide.py 同理，reload 会打散
        # 跨模块共享单例。这里只清内存规则集，等价于干净的规则状态。
        appmod._SUB_RULES.clear()
        self.client = TestClient(appmod.app)

        token = appmod._make_portal_token()
        self.csrf = "csrf-watch-manage-test"
        self.cookies = {
            appmod.PORTAL_COOKIE: token,
            appmod.CSRF_COOKIE: self.csrf,
        }
        self.headers = {appmod.CSRF_HEADER: self.csrf}
        self.app._SUB_RULES.clear()

    def tearDown(self):
        os.environ.pop("TG_DATA_DIR", None)
        self.app._SUB_RULES.clear()

    def _set(self, tg="100", chat="-100999", title="收藏 (Saved Messages)", on=True):
        return self.client.post(
            "/browse/watch-rules",
            json={"tg": tg, "chat": chat, "title": title, "on": on},
            cookies=self.cookies, headers=self.headers)

    def test_on_creates_watch_rule(self):
        r = self._set()
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["ok"], d)
        rules = list(self.app._SUB_RULES.values())
        self.assertEqual(len(rules), 1)
        self.assertTrue(rules[0]["watch"])
        self.assertTrue(rules[0]["enabled"])
        self.assertEqual(str(rules[0]["chatId"]), "-100999")
        self.assertIn("tg-archive", rules[0]["dirTemplate"])

    def test_on_twice_idempotent(self):
        self._set()
        self._set()
        rules = [r for r in self.app._SUB_RULES.values() if str(r.get("chatId")) == "-100999"]
        self.assertEqual(len(rules), 1)

    def test_on_existing_rule_flips_watch_keeps_template(self):
        rid = "existing-rule"
        self.app._SUB_RULES[rid] = {
            "id": rid, "telegramId": "100", "chatId": "-100999",
            "chatTitle": "电影频道", "enabled": True, "priority": 5,
            "dirTemplate": "/电影/{chat_title}", "deleteLocal": False,
            "policy": "skip", "watch": False, "created_at": 0.0,
            "stats": {},
        }
        r = self._set()
        self.assertTrue(r.json()["ok"])
        rule = self.app._SUB_RULES[rid]
        self.assertTrue(rule["watch"])
        self.assertEqual(rule["dirTemplate"], "/电影/{chat_title}")  # 模板不动
        self.assertFalse(rule["deleteLocal"])                        # 其它配置不动

    def test_off_keeps_rule_disables_watch(self):
        self._set()
        r = self._set(on=False)
        self.assertTrue(r.json()["ok"])
        rules = list(self.app._SUB_RULES.values())
        self.assertEqual(len(rules), 1)      # 规则保留（归档配置不丢）
        self.assertFalse(rules[0]["watch"])

    def test_off_without_rule_is_ok(self):
        r = self._set(on=False)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(len(self.app._SUB_RULES), 0)

    def test_get_lists_only_watch_rules(self):
        self.app._SUB_RULES["plain"] = {
            "id": "plain", "telegramId": "100", "chatId": "-100111",
            "chatTitle": "普通订阅", "enabled": True, "watch": False,
            "dirTemplate": "/x", "stats": {},
        }
        self._set()
        r = self.client.get("/browse/watch-rules", cookies=self.cookies)
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual(list(d["rules"].keys()), ["100:-100999"])

    def test_missing_ids_rejected(self):
        r = self.client.post(
            "/browse/watch-rules", json={"tg": "", "chat": "", "on": True},
            cookies=self.cookies, headers=self.headers)
        self.assertEqual(r.status_code, 400)

    def test_watch_rule_reaches_watch_service(self):
        """开出来的规则必须被 watch_service 视为监听对象（否则白开）。"""
        self._set()
        from services.watch_service import _watch_rules
        rules = _watch_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(str(rules[0]["chatId"]), "-100999")


if __name__ == "__main__":
    unittest.main()
