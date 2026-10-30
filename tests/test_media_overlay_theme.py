# -*- coding: utf-8 -*-
"""回归测试：缩略图浮层标签必须跟随深浅色主题（「白天主题黑黑一块」）。

历史：浏览页 / 云端归档卡片缩略图上的网盘标识浮层，在浅色主题下曾
呈现为深色矩形（「黑黑一块」）。该浮层现已按用户要求整体移除（网盘归属
由卡片上的云端路径首段体现），本用例继续守护剩余浮层标签
（时长角标 dur-badge / 复选框 card-check）的主题正确性。

根因（两处叠加）：
1. 底色写死 rgba(15, 23, 42, .85) / rgba(10, 12, 20, .68) 这类深色遮罩，
   不随 data-theme 变化 → 浅色页面上就是一块黑；
2. 文字色写 var(--accent)，而 tokens.css 里从未定义过 --accent，
   于是回退成继承来的 --text-1 —— 浅色主题下是深字。深字 + 深底 = 全黑。

修复：新增 --media-chip-{bg,bg-hover,border,text,shadow} 主题令牌
（暗色=深遮罩白字；浅色=半透明白底深字），网盘标识 / 时长角标 /
复选框浮层全部改走这些令牌。

本用例锁定：
- 令牌在两个主题里都有定义且取值不同；
- 浮层标签的 CSS 里不再出现写死的深色遮罩底；
- 浮层标签不得再引用未定义的 --accent。
"""
import pathlib
import re
import unittest

APP = pathlib.Path(__file__).resolve().parent.parent
TOKENS = (APP / "static/css/tokens.css").read_text(encoding="utf-8")
MAIN_CSS = (APP / "static/css/main.css").read_text(encoding="utf-8")
TEMPLATES = sorted((APP / "templates").glob("*.html"))

CHIP_TOKENS = (
    "--media-chip-bg",
    "--media-chip-bg-hover",
    "--media-chip-border",
    "--media-chip-text",
    "--media-chip-shadow",
)


def _theme_blocks():
    dark_start = TOKENS.index(":root")
    light_start = TOKENS.index('[data-theme="light"]')
    return TOKENS[dark_start:light_start], TOKENS[light_start:]


def _overlay_blocks():
    """收集所有「浮在缩略图上」的标签样式块：网盘标识 / 时长 / 复选框。"""
    sources = [("main.css", MAIN_CSS)] + [(p.name, p.read_text(encoding="utf-8")) for p in TEMPLATES]
    pattern = re.compile(
        r"\.(?:cloud-drive-(?:badge|float)|dur-badge|card-check)[^{}]*\{([^{}]*)\}"
    )
    for name, text in sources:
        for m in pattern.finditer(text):
            yield name, text[: m.start()].count("\n") + 1, m.group(1)


class TestMediaOverlayTheme(unittest.TestCase):
    def test_chip_tokens_defined_in_both_themes(self):
        dark_block, light_block = _theme_blocks()
        for token in CHIP_TOKENS:
            self.assertIn(f"{token}:", dark_block, f"暗色主题缺少 {token} 定义")
            self.assertIn(f"{token}:", light_block, f"浅色主题缺少 {token} 定义")

    def test_chip_tokens_differ_between_themes(self):
        dark_block, light_block = _theme_blocks()
        for token in ("--media-chip-bg", "--media-chip-text"):
            dark_val = re.search(rf"{token}:\s*([^;]+);", dark_block).group(1).strip()
            light_val = re.search(rf"{token}:\s*([^;]+);", light_block).group(1).strip()
            self.assertNotEqual(
                dark_val, light_val,
                f"{token} 在浅色主题下必须与暗色不同，否则浮层标签就是一块不随主题变色的黑",
            )

    def test_no_hardcoded_dark_scrim_in_overlays(self):
        # 反例：background: rgba(15, 23, 42, .85) / rgba(10, 12, 20, .68)
        hardcoded = re.compile(r"background(?:-color)?:\s*rgba\(\s*(?:1[0-9]|[0-9]|2[0-9])\s*,")
        offenders = []
        for name, line, body in _overlay_blocks():
            for decl in body.split(";"):
                if hardcoded.match(decl.strip()):
                    offenders.append(f"{name}:{line} {decl.strip()}")
        self.assertEqual(
            offenders, [],
            "缩略图浮层标签仍有写死的深色遮罩底（浅色主题会变黑块）：" + "; ".join(offenders),
        )

    def test_overlays_use_chip_tokens(self):
        text = "".join(body for _, _, body in _overlay_blocks())
        self.assertIn("var(--media-chip-bg", text, "浮层标签底色未走 --media-chip-bg 令牌")
        self.assertIn("var(--media-chip-text", text, "浮层标签文字色未走 --media-chip-text 令牌")

    def test_no_undefined_accent_var_in_overlays(self):
        # --accent 在 tokens.css 里不存在，浅色主题下会被继承成深色文字
        for name, line, body in _overlay_blocks():
            self.assertNotIn(
                "var(--accent)", body,
                f"{name}:{line} 浮层标签引用了未定义的 --accent（浅色主题下文字会回退成深色）",
            )


class TestNoUndefinedThemeVars(unittest.TestCase):
    """模板里引用的颜色变量必须真的在 tokens.css / main.css 里定义过。

    这条是上面那个 bug 的通用护栏：写一个不存在的变量名不会报错，
    只会在浅色主题下悄悄回退成继承色，肉眼才发现「字看不出来」。
    """

    def test_template_color_vars_exist(self):
        defined = set(re.findall(r"(--[A-Za-z0-9_-]+)\s*:", TOKENS + MAIN_CSS))
        # 模板内自定义的局部变量（在各自的 <style> 里定义）也要算进去
        template_text = "".join(p.read_text(encoding="utf-8") for p in TEMPLATES)
        defined |= set(re.findall(r"(--[A-Za-z0-9_-]+)\s*:", template_text))

        missing = {}
        for path in TEMPLATES:
            for m in re.finditer(r"var\(\s*(--[A-Za-z0-9_-]+)", path.read_text(encoding="utf-8")):
                name = m.group(1)
                if name not in defined:
                    missing.setdefault(name, set()).add(path.name)
        # --accent 是历史遗留的目标名；颜色相关的变量必须收敛，非颜色变量允许局部兜底
        color_missing = {
            k: v for k, v in missing.items()
            if k.startswith(("--brand", "--text", "--bg-", "--border", "--surface", "--accent"))
        }
        self.assertEqual(
            color_missing, {},
            "模板引用了未定义的颜色变量：" + repr(color_missing),
        )


if __name__ == "__main__":
    unittest.main()
