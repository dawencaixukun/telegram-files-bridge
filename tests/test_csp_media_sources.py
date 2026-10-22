# -*- coding: utf-8 -*-
"""
tests/test_csp_media_sources.py — CSP 必须放行跨域缩略图与播放直链

回归背景：安全头一度只允许 `img-src 'self' data:`，且完全没有 media-src。
OpenList 把云盘原生缩略图（OneDrive 的 *.mediap.svc.ms 等）和播放直链
（*personalcontent.com / CDN）直接交给浏览器加载，跨域资源全部被浏览器
静默拦截 —— 浏览页所有卡片回落占位图，点播放黑屏且无错误提示
（media 报 "Media load rejected by URL safety check"）。
"""
import unittest

import bridge_server
from core.auth import _CSP_MEDIA_SCHEMES
from fastapi.testclient import TestClient


class CspMediaSourcesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(bridge_server.app)
        cls.csp = cls.client.get("/login").headers.get("content-security-policy", "")

    def _directive(self, name: str) -> str:
        return next(
            (d.strip() for d in self.csp.split(";") if d.strip().startswith(name)),
            "",
        )

    def test_header_present(self):
        self.assertTrue(self.csp, "缺少 Content-Security-Policy 头")

    def test_img_src_allows_cross_origin(self):
        d = self._directive("img-src")
        self.assertTrue(d, "CSP 缺少 img-src 指令")
        self.assertIn("'self'", d)
        self.assertIn("data:", d)
        self.assertIn("https:", d, "跨域 https 缩略图会被浏览器拦截")

    def test_media_src_present_and_allows_streams(self):
        d = self._directive("media-src")
        self.assertTrue(
            d,
            "CSP 缺少 media-src：在线播放直链会回落 default-src 'self' 被拦",
        )
        self.assertIn("https:", d)
        self.assertIn("blob:", d)

    def test_media_schemes_constant_matches_header(self):
        for scheme in _CSP_MEDIA_SCHEMES.split():
            self.assertIn(scheme, self.csp, f"{scheme} 未写进 CSP")

    def test_still_blocks_frames_and_forms(self):
        """放开媒体源不能顺带松掉防点击劫持与表单沙箱。"""
        self.assertIn("frame-ancestors 'none'", self.csp)
        self.assertIn("base-uri 'self'", self.csp)
        self.assertIn("form-action 'self'", self.csp)


if __name__ == "__main__":
    unittest.main()
