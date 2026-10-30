# -*- coding: utf-8 -*-
"""
tests/test_browse_manage_modal_layering.py — 管理会话弹窗的层叠与关闭通道

回归背景（手机端实测）：`#browseManageBackdrop` 写在 #content 内，而 #content
带 `view-transition-name`，形成独立层叠上下文 → 弹窗的 z-index:90 只在该上下文
内生效；会话侧栏被搬到 body 下（z-index:85）后把弹窗整体压住。表现为
「点管理会话唤不出弹窗 / 弹窗被会话列表盖住且关不掉」。

修复要点（本测试锁定）：
1. 弹窗加载后立即被 reparent 到 document.body 直下（脱离 #content）；
2. 打开弹窗时先收起会话侧栏，避免两层叠着抢点击；
3. 提供点遮罩空白处 / Esc 两条关闭通道；
4. CSS 里弹窗 z-index 高于侧栏(85)，小屏整屏可滚且头部/底部按钮吸顶吸底。
"""
import os
import unittest

import bridge_server
from core.auth import _make_portal_token
from core.config import PORTAL_COOKIE, CSRF_COOKIE
from fastapi.testclient import TestClient


class BrowseManageModalLayeringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(bridge_server.app)
        csrf = "probe-csrf"
        resp = cls.client.get(
            "/browse",
            cookies={PORTAL_COOKIE: _make_portal_token(), CSRF_COOKIE: csrf},
        )
        cls.status = resp.status_code
        cls.html = resp.text

    def test_page_renders(self):
        self.assertEqual(self.status, 200)
        self.assertIn('id="browseManageBackdrop"', self.html, "管理会话弹窗容器丢失")

    def test_modal_is_reparented_out_of_content(self):
        self.assertIn(
            "__browseReparentManage",
            self.html,
            "缺少把弹窗搬到 body 的修复函数：z-index 会被 #content 层叠上下文吞掉",
        )
        self.assertRegex(
            self.html,
            r"if \(bd && bd\.parentElement !== document\.body\) document\.body\.appendChild\(bd\);",
            "reparent 实现缺失：弹窗仍在 #content 内",
        )
        # 立即执行一次，首屏就应已在 body 下
        self.assertIn("window.__browseReparentManage();", self.html)

    def test_open_manage_closes_side_panel_first(self):
        i = self.html.find("window.__browseOpenManage = function")
        self.assertGreater(i, 0, "找不到 __browseOpenManage")
        body = self.html[i:i + 700]
        self.assertIn(
            "__browseToggleSidePanel(true)",
            body,
            "打开管理弹窗时没有先收起会话侧栏：手机会被侧栏压住",
        )

    def test_close_channels_exist(self):
        # 点遮罩空白处关闭
        self.assertIn("e.target === bd", self.html, "缺少点遮罩关闭通道")
        # Esc 关闭
        self.assertIn("e.key !== 'Escape'", self.html, "缺少 Esc 关闭通道")
        # 显式关闭函数仍然存在（头部 × 与「完成」按钮用）
        self.assertIn("window.__browseCloseManage = function", self.html)

    def test_css_layering_and_mobile_scroll(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "css", "main.css"), encoding="utf-8") as f:
            css = f.read()
        i = css.find("#browseManageBackdrop {")
        self.assertGreater(i, 0, "缺少 #browseManageBackdrop 的层叠修复样式")
        block = css[i:i + 260]
        self.assertIn("z-index: 120", block, "弹窗 z-index 未压过会话侧栏(85)")
        self.assertIn("overflow-y: auto", block, "弹窗不可滚动：手机上底部按钮会出屏")


if __name__ == "__main__":
    unittest.main()
