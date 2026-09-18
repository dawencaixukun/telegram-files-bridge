# -*- coding: utf-8 -*-
"""渲染 _browse_files.html，检查下载按钮已改成胶囊（.cap-btn）。"""
import os, sys, tempfile, unittest

TMP = tempfile.mkdtemp(prefix="capbtn-")
os.environ["TG_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) or ".")

from test_browse_openlist_button import _render, _row


class TestCapBtn(unittest.TestCase):
    def test_download_button_is_capsule(self):
        """未下载卡片：下载按钮必须是 .cap-btn 胶囊（带图标 + 文字），
        不能再是 .icon-btn.xs 圆角方块。"""
        html = _render([_row("dl.mp4", False)])
        self.assertIn("cap-btn", html, "下载按钮应使用胶囊类 cap-btn")
        self.assertIn("cap-dl", html, "下载胶囊应带主操作色标记 cap-dl")
        self.assertNotIn("icon-btn xs", html, "不应再用圆角方块 .icon-btn.xs 做下载按钮")
        self.assertIn("<span>下载</span>", html, "胶囊内应有「下载」文字")

    def test_submitted_state_is_capsule(self):
        """提交后的「已提交」态必须也是胶囊（JS 生成的 HTML）。"""
        with open("templates/browse.html", encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('class="cap-btn" disabled', src,
                      "已提交态应复用 cap-btn 胶囊")
        self.assertNotIn('border-radius:var(--radius-sm);padding:2px 6px;">已提交',
                         src, "已提交态不应再用内联直角小块")

    def test_archived_openlist_still_pill_group(self):
        """回归：已归档卡片的 OpenList 胶囊不受影响（其他用例依赖此契约）。"""
        html = _render([_row("a.mp4", True, "/阿里云盘/tg/a.mp4", "http://x/a.mp4")])
        self.assertIn('class="pill-group"', html)
        self.assertIn("seg seg-right", html)
        self.assertIn("<span>OpenList</span>", html)
        self.assertNotIn("icon-btn xs", html)

    def test_capsule_matches_openlist_geometry(self):
        """胶囊几何必须与 .pill-group .seg 一致（圆角/内距/字号），否则同排会错位。"""
        with open("static/css/main.css", encoding="utf-8") as fh:
            css = fh.read()
        self.assertIn(".cap-btn {", css)
        self.assertIn("border-radius: var(--radius-pill)", css)
        # 取出 cap-btn 块，逐项比对 seg
        cap = css.split(".cap-btn {", 1)[1].split("}", 1)[0]
        for token in ("padding: 5px 10px", "font-size: 11px", "font-weight: 600", "white-space: nowrap"):
            self.assertIn(token, cap, f"cap-btn 应含 {token} 以与 seg 对齐")
        self.assertIn("gap: 4px", cap)

    def test_foot_row_no_rect_inline_styles(self):
        """底部操作区不应再有内联直角样式的小块按钮。"""
        html = _render([_row("x.mp4", False)])
        self.assertNotIn("border-radius:var(--radius-sm);padding:2px 6px", html)

    def test_local_present_card_renders_footer(self):
        """本地在存卡片必须渲染出底部按钮。

        历史缺陷：模板里 `{% elif f.is_downloaded or f.dl == 'completed' %}`
        连写两遍，第一个命中且体为空，第二个永不执行 —— 「本地在存」按钮成了
        死代码，该卡片底部整块渲染为空。修胶囊时一并删掉重复分支。
        """
        html = _render([_row("local.mp4", False, dl="completed")])
        self.assertIn("本地在存", html, "本地在存卡片应渲染出胶囊按钮")
        self.assertIn("cap-btn", html)
        # 底部操作区不能为空
        import re as _re
        m = _re.search(r'<div class="t-foot">(.*?)</div>\s*</div>', html, _re.S)
        self.assertTrue(m and m.group(1).strip(), "底部操作区不应为空")


if __name__ == "__main__":
    unittest.main(verbosity=2)
