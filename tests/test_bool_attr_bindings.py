# -*- coding: utf-8 -*-
"""回归测试：模板里的布尔属性绑定不得裸引用「x-for 迭代对象」的字段。

背景（用户可见的真实缺陷，2026-09-20 修复）：
tasks.html 的删除按钮写成 `:disabled="t.deleting"`，但 `deleting` 从未被初始化
（后端 `_m3u8_public` 也不返回该字段），于是 Alpine 求值得到 `undefined`。
Chrome 无头 + 真实 vendor/alpinejs.min.js 实测矩阵：

    :disabled="undefined"  → 元素上**写入了 disabled**（永久灰掉）
    :disabled="null"       → 不写属性
    :disabled="false"      → 不写属性
    :disabled="0" / ""     → 写入 disabled（falsy 也会被当作「设置属性」）
    :disabled="!!undefined"→ 不写属性（布尔化后安全）

用户现象：任务页取消后只剩「重试」，「删除」键点不动。线上日志佐证——
部署至今从未出现过一次 `POST /api/m3u8/delete`，按钮被禁用，点击根本没发出请求。
subscriptions.html 的 `r._saving` 是同一类问题（优先级/目录/策略/删除按钮全被锁死）。

本测试做静态检查，只针对**危险面**：绑定表达式里裸引用（未经 `!!`/`!`/`Boolean()`/
比较/`||`/`&&`/三元）了 x-for 迭代变量的字段。这些对象来自服务端 JSON，组件里
没有初始化声明，取值可能是 undefined。组件自身声明为 false 的字段
（busy/loading/saving…）不在检查范围，它们始终有值。

之所以用静态检查而非渲染测试：跑真实 Alpine 需要浏览器，单测环境没有。
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import TEMPLATES_DIR  # noqa: E402

# 布尔属性在 HTML 里「存在即真」，绑定到 undefined 会被误置。
_BOOL_ATTRS = ("disabled", "checked", "required", "readonly", "open", "selected",
               "autofocus", "hidden", "multiple", "novalidate", "muted", "loop",
               "controls", "autoplay", "itemscope", "ismap", "reversed", "async",
               "defer", "allowfullscreen", "playsinline")

_ATTR_RE = re.compile(r':(%s)\s*=\s*"([^"]*)"' % "|".join(_BOOL_ATTRS))
_FOR_RE = re.compile(r'x-for\s*=\s*"\s*\(?([A-Za-z_$][\w$]*)')
# 已布尔化 / 结果必为布尔的运算符：undefined 不可能原样透传到属性
_SAFE_RE = re.compile(r"!!|Boolean\(|===|!==|==|!=|\|\||&&|\?")


def _iter_templates():
    for name in sorted(os.listdir(TEMPLATES_DIR)):
        if name.endswith(".html"):
            yield name, os.path.join(TEMPLATES_DIR, name)
    partials = os.path.join(TEMPLATES_DIR, "partials")
    if os.path.isdir(partials):
        for name in sorted(os.listdir(partials)):
            if name.endswith(".html"):
                yield "partials/" + name, os.path.join(partials, name)


class TestBooleanAttrBindings(unittest.TestCase):
    """`x-bind` 布尔属性不得裸绑定 x-for 迭代对象的字段（否则元素被永久禁用）。"""

    def test_no_bare_binding_to_iterated_field(self):
        bad = []
        for name, path in _iter_templates():
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
            # 先收集该模板里所有 x-for 的迭代变量名（如 t、r、j、it）
            iter_vars = set()
            for line in lines:
                for m in _FOR_RE.finditer(line):
                    iter_vars.add(m.group(1))
            if not iter_vars:
                continue
            for lineno, line in enumerate(lines, 1):
                for attr, expr in _ATTR_RE.findall(line):
                    expr = expr.strip()
                    if _SAFE_RE.search(expr) or expr.startswith("!"):
                        continue
                    for var in iter_vars:
                        if re.search(r"\b%s\s*[.\[]" % re.escape(var), expr):
                            bad.append((name, lineno, attr, expr))
                            break

        if bad:
            msg = "\n".join("  %s:%d  :%s=\"%s\"" % b for b in bad)
            self.fail(
                "布尔属性裸绑定了 x-for 迭代对象的字段。该字段来自服务端 JSON、"
                "组件未初始化，求值为 undefined 时 Alpine 仍会写入该属性，"
                "元素被永久禁用/勾选（真实缺陷：取消后「删除」键点不动）。"
                "请显式布尔化，例如 :disabled=\"!!t.deleting\"：\n" + msg)

    def test_m3u8_delete_binding_is_booleanized(self):
        """定点回归：m3u8 删除按钮已随 VPS 任务表重构移除（操作列=重试/取消/详情），
        本用例锁定新形态：任务表不得再出现裸绑定的删除按钮。"""
        path = os.path.join(TEMPLATES_DIR, "tasks.html")
        partial = os.path.join(TEMPLATES_DIR, "partials", "_tasks_table.html")
        for f in (path, partial):
            if not os.path.exists(f):
                continue
            with open(f, "r", encoding="utf-8") as fh:
                html = fh.read()
            self.assertNotIn('@click="remove(t)"', html,
                             "任务表已无 m3u8 删除按钮（VPS 重构后移除），不得回潮")

    def test_subscriptions_saving_binding_is_booleanized(self):
        """同类缺陷定点回归：订阅规则行的 :disabled 不得裸用 r._saving。"""
        path = os.path.join(TEMPLATES_DIR, "subscriptions.html")
        with open(path, "r", encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn(':_saving', html.replace('r._saving', ':_saving'),
                      "订阅页应仍使用 _saving 做保存中标记")
        for attr, expr in _ATTR_RE.findall(html):
            if "_saving" in expr:
                self.assertIn("!!", expr,
                              "r._saving 未初始化时是 undefined，必须写成 !!r._saving，"
                              "当前表达式：%s" % expr)


if __name__ == "__main__":
    unittest.main()
