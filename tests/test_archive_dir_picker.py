"""归档目录选择器（设置页）移动端形态回归测试。

背景（用户两轮反馈）：
1) 「默认归档位置」的选择面板在手机上向下展开时，底栏被顶出视口，
   页面底部还有一个 position:sticky 的「保存」悬浮按钮压在面板上，
   点「使用此目录」会命中保存按钮。
2) 「返回上一级」用了字符 ⮬（某些安卓字体渲染成方框乱码）+ 内联 rotate，
   用户看到的是乱码图标。

修法：手机端改成贴底抽屉（fixed + 容器去 backdrop-filter 解除包含块），
桌面端保持输入框下方下拉；返回上一级只用标准 chevron 图标。

本用例锁定模板结构与关键 CSS，避免后续被改回「向下展开 + 字符图标」。
"""
import pathlib
import re
import unittest

APP = pathlib.Path(__file__).resolve().parent.parent
SETTINGS = (APP / "templates/settings.html").read_text(encoding="utf-8")
MAIN_CSS = (APP / "static/css/main.css").read_text(encoding="utf-8")


class TestArchiveDirPicker(unittest.TestCase):
    def _dropdown_block(self) -> str:
        start = SETTINGS.index('class="arch-dropdown"')
        return SETTINGS[start : start + 4000]

    def test_no_mojibake_up_icon(self):
        # ⮬ 会被部分安卓字体渲染成方框；返回箭头应只用 svg chevron
        self.assertNotIn("⮬", SETTINGS)
        self.assertIn('class="arch-up-icon"', SETTINGS)

    def test_up_icon_is_rotated_chevron(self):
        css = MAIN_CSS
        self.assertIn(".arch-up-row .arch-up-icon", css)
        block = css[css.index(".arch-up-row .arch-up-icon"):]
        block = block[: block.index("}")]
        self.assertIn("rotate(180deg)", block)

    def test_scrim_element_and_click_to_close(self):
        self.assertIn('class="arch-dd-scrim"', SETTINGS)
        # 点遮罩关闭
        idx = SETTINGS.index('class="arch-dd-scrim"')
        line = SETTINGS[idx : idx + 200]
        self.assertIn('@click="dirOpen = false"', line)

    def test_footer_shows_current_path(self):
        self.assertIn('class="arch-drop-cur"', SETTINGS)
        self.assertIn("使用此目录", SETTINGS)

    def test_mobile_drawer_css(self):
        css = MAIN_CSS
        mobile = css[css.index("@media (max-width: 640px)", css.index(".arch-dropdown")):]
        block = mobile[: mobile.index(".arch-crumb { padding: 12px 16px")]
        self.assertIn("position: fixed", block)
        self.assertIn("bottom: 0", block)
        # 解除祖先 backdrop-filter 造成的包含块，否则 fixed 相对卡片定位
        self.assertIn(".card:has(.arch-dir-field)", css)
        self.assertIn("backdrop-filter: none", css)

    def test_no_fixed_pixel_list_height(self):
        # 历史缺陷：写死 max-height:220px，手机上一屏装不下
        self.assertNotIn('style="max-height:220px', SETTINGS)
        self.assertNotIn("max-height: 220px", MAIN_CSS)

    def test_scrim_hidden_on_desktop(self):
        # 基态 display:none，只在手机 media 内改为 block
        self.assertIn(".arch-dd-scrim { display: none; }", MAIN_CSS)
        mobile_idx = MAIN_CSS.index("@media (max-width: 640px)", MAIN_CSS.index(".arch-dropdown"))
        after = MAIN_CSS[mobile_idx:]
        self.assertIn(".arch-dd-scrim {", after)
        scrim_block = after[after.index(".arch-dd-scrim {"):]
        scrim_block = scrim_block[: scrim_block.index("}")]
        self.assertIn("display: block", scrim_block)


if __name__ == "__main__":
    unittest.main()
