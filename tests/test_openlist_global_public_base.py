# -*- coding: utf-8 -*-
"""
tests/test_openlist_global_public_base.py
========================================
回归测试：OpenList 对外访问域名收敛为「设置里的全局配置」。

历史问题：域名散落在各页面的 localStorage 里，每个页面自带一个弹窗各存各的
（云端归档页、网盘浏览页、顶栏功能菜单各一套），于是出现
「这个页面配了、那个页面还是内网地址」，而且换浏览器/清缓存就丢。

本文件锁定四条契约：
1. 只有一个写入入口：POST /openlist/public-base，落 _ARCHIVE_CONFIG["publicBaseUrl"]。
2. 归一化严格：补 scheme、去尾斜杠；拒绝 userinfo / query / fragment / 非法 host
   （这个值会拼进前端跳转 URL，不能让用户塞任意串）。
3. 各页面模板不得再自建域名弹窗，只能读同一份全局值。
4. 服务端值优先于 localStorage：设置页改了域名，所有页面立即跟着变。
"""
import os
import re
import tempfile
import unittest

from fastapi.testclient import TestClient

import bridge_server
from core.config import _norm_public_base


class TestNormalizePublicBase(unittest.TestCase):
    """归一化必须严格，且不接受可被用来伪造跳转的组件。"""

    def test_bare_host_gets_http_scheme(self):
        self.assertEqual(_norm_public_base("pan.example.com"), "http://pan.example.com")

    def test_host_with_port(self):
        self.assertEqual(_norm_public_base("pan.example.com:5244"),
                         "http://pan.example.com:5244")

    def test_https_preserved(self):
        self.assertEqual(_norm_public_base("https://pan.example.com"),
                         "https://pan.example.com")

    def test_trailing_slash_stripped(self):
        self.assertEqual(_norm_public_base("https://pan.example.com/"),
                         "https://pan.example.com")

    def test_ip_address_allowed(self):
        self.assertEqual(_norm_public_base("1.2.3.4:5244"), "http://1.2.3.4:5244")

    def test_empty_means_clear(self):
        self.assertEqual(_norm_public_base(""), "")
        self.assertEqual(_norm_public_base("   "), "")

    def test_rejects_userinfo(self):
        self.assertEqual(_norm_public_base("http://user:pass@pan.example.com"), "",
                         "带 userinfo 的地址可用于伪造跳转，必须拒绝")

    def test_rejects_query_and_fragment(self):
        self.assertEqual(_norm_public_base("http://pan.example.com?a=1"), "")
        self.assertEqual(_norm_public_base("http://pan.example.com#x"), "")

    def test_rejects_non_http_scheme(self):
        self.assertEqual(_norm_public_base("ftp://pan.example.com"), "")
        self.assertEqual(_norm_public_base("javascript:alert(1)"), "")

    def test_rejects_illegal_host_chars(self):
        self.assertEqual(_norm_public_base("http://pa n.example.com"), "")


class TestGlobalEndpoint(unittest.TestCase):
    """写入只有一个入口，且落盘到归档配置。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tg_pubbase_")
        self._orig = bridge_server._ARCHIVE_CONFIG.get("publicBaseUrl")
        self._orig_file = bridge_server._ARCHIVE_CONFIG_FILE
        bridge_server._ARCHIVE_CONFIG_FILE = os.path.join(self.tmp, ".archive_config.json")
        self.client = TestClient(bridge_server.app)
        self.cookies = {bridge_server.PORTAL_COOKIE: bridge_server._make_portal_token(),
                        bridge_server.CSRF_COOKIE: "csrf-test"}
        self.headers = {"X-CSRF-Token": "csrf-test"}

    def tearDown(self):
        bridge_server._ARCHIVE_CONFIG["publicBaseUrl"] = self._orig
        bridge_server._ARCHIVE_CONFIG_FILE = self._orig_file

    def test_save_and_read_back(self):
        r = self.client.post("/openlist/public-base",
                             json={"publicBaseUrl": "pan.example.com:5244"},
                             cookies=self.cookies, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["publicBaseUrl"], "http://pan.example.com:5244")
        # 落到了全局归档配置里（所有页面读同一份）
        self.assertEqual(bridge_server._ARCHIVE_CONFIG["publicBaseUrl"],
                         "http://pan.example.com:5244")
        self.assertEqual(bridge_server._archive_config_public()["publicBaseUrl"],
                         "http://pan.example.com:5244")

    def test_clear(self):
        bridge_server._ARCHIVE_CONFIG["publicBaseUrl"] = "http://old.example.com"
        r = self.client.post("/openlist/public-base", json={"publicBaseUrl": ""},
                             cookies=self.cookies, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(bridge_server._ARCHIVE_CONFIG["publicBaseUrl"], "")

    def test_invalid_rejected_and_not_saved(self):
        bridge_server._ARCHIVE_CONFIG["publicBaseUrl"] = "http://keep.example.com"
        r = self.client.post("/openlist/public-base",
                             json={"publicBaseUrl": "http://user:p@evil.com"},
                             cookies=self.cookies, headers=self.headers)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(bridge_server._ARCHIVE_CONFIG["publicBaseUrl"],
                         "http://keep.example.com", "非法输入不得覆盖已有配置")

    def test_requires_auth(self):
        r = self.client.post("/openlist/public-base",
                             json={"publicBaseUrl": "pan.example.com"})
        self.assertIn(r.status_code, (401, 403), "未登录不得修改全局域名")


class TestTemplatesUseGlobalValue(unittest.TestCase):
    """各页面读同一份全局值，且不再自建域名弹窗。"""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.tpl = os.path.join(root, "templates")
        with open(os.path.join(self.tpl, "settings.html"), encoding="utf-8") as f:
            self.settings = f.read()

    def _read(self, name):
        with open(os.path.join(self.tpl, name), encoding="utf-8") as f:
            return f.read()

    def test_settings_has_global_field(self):
        self.assertIn('id="openlistPublicBase"', self.settings,
                      "设置页的 OpenList 卡片必须有全局域名输入框")
        self.assertIn("/openlist/public-base", self._read_js(),
                      "必须提交到唯一的全局写入接口")

    def _read_js(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "static", "js", "app.js"), encoding="utf-8") as f:
            return f.read()

    def test_child_pages_drop_own_domain_modal(self):
        for name in ("library_cloud.html", "explorer.html"):
            src = self._read(name)
            self.assertNotIn("openDomainModal", src,
                             f"{name} 不应再有自己的域名弹窗入口")
            self.assertNotIn('x-model="domainInput"', src,
                             f"{name} 不应再有自己的域名字段")

    def test_server_value_takes_precedence(self):
        """服务端值必须排在 localStorage 之前（否则设置页改了域名不生效）。"""
        pat = re.compile(
            r"\{\{\s*\(openlist_public_base or ''\)\s*\|\s*tojson\s*\}\}"
            r"\s*\|\|\s*localStorage\.getItem\('openlist_public_base'\)")
        for name in ("browse.html", "library_cloud.html", "explorer.html", "base.html"):
            src = self._read(name)
            self.assertRegex(src, pat,
                             f"{name} 的域名优先级不对：服务端值必须在 localStorage 之前")

    def test_base_menu_links_to_settings(self):
        src = self._read("base.html")
        self.assertIn('href="/settings#openlistPublicBase"', src,
                      "顶栏域名入口应直达设置页，而不是再开一个弹窗")


if __name__ == "__main__":
    unittest.main()
