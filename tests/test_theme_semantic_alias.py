"""浅色主题下的浮层底色回归测试。

历史 bug：浏览页的工具条下拉写了 `background: var(--bg-1, #111827)`，
但 tokens.css 里从未定义过 `--bg-1`，于是兜底的深色 `#111827` 生效。
深色主题下看不出来（本来就是要深底），但用户切到浅色主题后，
下拉面板成了一整块纯黑矩形，深灰文字叠在黑底上几乎不可见 ——
用户描述为「都是发黑」。

本用例锁定：--bg-1/--bg-2/--bg-3 必须在两个主题里都有定义，
且模板里不得再出现带写死深色兜底的 var(--bg-*)。
"""
import pathlib
import re
import unittest

APP = pathlib.Path(__file__).resolve().parent.parent
TOKENS = (APP / "static/css/tokens.css").read_text(encoding="utf-8")
TEMPLATES = sorted((APP / "templates").glob("*.html"))


class TestThemeSemanticAlias(unittest.TestCase):
    def test_bg_aliases_defined_in_both_themes(self):
        dark_start = TOKENS.index(":root")
        light_start = TOKENS.index('[data-theme="light"]')
        dark_block = TOKENS[dark_start:light_start]
        light_block = TOKENS[light_start:]
        for token in ("--bg-1", "--bg-2", "--bg-3"):
            self.assertIn(f"{token}:", dark_block, f"暗色主题缺少 {token} 定义")
            self.assertIn(f"{token}:", light_block, f"浅色主题缺少 {token} 定义")

    def test_bg1_points_at_popover_surface(self):
        # 下拉必须落在「近乎实底」的浮层色上，不能是半透明玻璃色（会双重曝光）
        matches = re.findall(r"--bg-1:\s*var\((--[a-z0-9-]+)\)", TOKENS)
        self.assertEqual(len(matches), 2, f"应有两处 --bg-1 定义，实际 {matches}")
        for var in matches:
            self.assertEqual(var, "--surface-popover")

    def test_no_hardcoded_dark_fallbacks(self):
        # 反例：var(--bg-1, #111827) / var(--bg-2, #123) 之类写死深色的兜底
        pattern = re.compile(r"var\(--bg-[123]\s*,\s*#[0-9a-fA-F]{3,8}\)")
        offenders = []
        for path in TEMPLATES:
            text = path.read_text(encoding="utf-8")
            for m in pattern.finditer(text):
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line} {m.group(0)}")
        self.assertEqual(offenders, [], "模板里存在写死深色的兜底：" + "; ".join(offenders))

    def test_popover_token_differs_between_themes(self):
        dark_value = re.search(r"--surface-popover:\s*([^;]+);", TOKENS)
        light_value = re.search(
            r'\[data-theme="light"\][\s\S]*?--surface-popover:\s*([^;]+);', TOKENS
        )
        self.assertIsNotNone(dark_value)
        self.assertIsNotNone(light_value)
        self.assertNotEqual(
            dark_value.group(1).strip(),
            light_value.group(1).strip(),
            "浮层底色在浅色主题下必须与暗色不同，否则会出现黑块",
        )


if __name__ == "__main__":
    unittest.main()
