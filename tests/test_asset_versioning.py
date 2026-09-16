# -*- coding: utf-8 -*-
"""回归测试：静态资源版本戳必须跟随文件内容变化。

背景（用户可见的历史缺陷）：
base.html 里把 CSS/JS 的版本参数写成手写固定字符串
（`main.css?v=20260910_v4`），而静态文件响应带
`Cache-Control: public, max-age=86400`。于是改了 CSS/JS 却忘了改那个字符串时，
浏览器会继续用这条 URL 的旧缓存，**用户刷新也看不到改动**，报「改了没生效」。
实测：main.css 加了 .cap-btn 样式并重启服务，服务端返回的 CSS 已含新样式，
但 URL 仍是 ?v=20260910_v4 → 页面毫无变化。

修法：core/templates.py 的 asset_v() 按文件内容算 sha256 前 10 位，
内容一变 URL 就变，缓存自动失效，无需人工维护版本号。
"""
import hashlib
import os
import re
import sys
import tempfile
import unittest

os.environ.setdefault("TG_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import STATIC_DIR
from core.templates import _asset_version, _asset_versions


class TestAssetVersioning(unittest.TestCase):
    def test_version_matches_file_content_hash(self):
        """版本戳必须等于该文件内容的 sha256 前 10 位。"""
        for rel, key in (("css/main.css", "main_css"),
                         ("css/tokens.css", "tokens_css"),
                         ("js/app.js", "app_js")):
            expect = hashlib.sha256(
                open(os.path.join(STATIC_DIR, rel), "rb").read()).hexdigest()[:10]
            self.assertEqual(_asset_versions()[key], expect,
                             f"{rel} 的版本戳应等于其内容哈希")

    def test_version_changes_when_content_changes(self):
        """内容变化 → 版本戳必须变化（否则缓存不会失效）。"""
        a = _asset_version("css/main.css")
        b = hashlib.sha256(b"different-content").hexdigest()[:10]
        self.assertNotEqual(a, b)
        # 同一文件重复调用必须稳定（否则每请求都变，缓存全失效）
        self.assertEqual(a, _asset_version("css/main.css"))

    def test_missing_file_falls_back(self):
        """文件缺失不能抛异常（渲染不该被静态资源拖垮）。"""
        self.assertEqual(_asset_version("css/__nope__.css"), "0")

    def test_base_template_uses_asset_v_not_hardcoded(self):
        """base.html 必须用 asset_v()，不得回退到手写固定版本号。"""
        tpl = open(os.path.join(os.path.dirname(STATIC_DIR), "templates", "base.html"),
                   encoding="utf-8").read()
        self.assertIn("asset_v()", tpl)
        for fname in ("main.css", "tokens.css", "app.js"):
            # 不得出现 ?v=<写死的日期串>
            self.assertIsNone(
                re.search(re.escape(fname) + r'\?v=\d{8}', tpl),
                f"{fname} 仍在使用手写固定版本号，缓存会失效不了")
            self.assertIn(f'{{{{ _v.', tpl)

    def test_rendered_page_has_content_hash(self):
        """真实页面里 CSS 的 v= 必须是内容哈希而非日期串。

        用线上服务实拉（登录后 GET /browse），比在测试里拼 base.html 的
        渲染上下文更可靠 —— base.html 依赖大量运行时变量。
        """
        import json
        import urllib.request
        import urllib.error
        import urllib.parse
        import http.cookiejar

        creds_path = os.path.join(os.environ.get("TG_DATA_DIR", ""), ".backend_creds")
        if not os.path.exists(creds_path):
            self.skipTest("本地无 .backend_creds，跳过线上实拉（模板静态检查已覆盖）")

        creds = json.load(open(creds_path))
        jar = http.cookiejar.CookieJar()

        class _NR(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        op = urllib.request.build_opener(_NR, urllib.request.HTTPCookieProcessor(jar))
        data = urllib.parse.urlencode(
            {"username": creds["username"], "password": creds["password"]}).encode()
        try:
            op.open(urllib.request.Request("http://127.0.0.1:8000/login", data=data,
                                           method="POST"), timeout=15)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 303)
        ck = "; ".join(f"{c.name}={c.value}" for c in jar)
        html = urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:8000/browse", headers={"Cookie": ck}), timeout=30
        ).read().decode()

        m = re.search(r'/static/css/main\.css\?v=([0-9a-f]{10})', html)
        self.assertIsNotNone(m, "页面里 main.css 的 v= 应是 10 位内容哈希")
        self.assertEqual(m.group(1), _asset_versions()["main_css"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
