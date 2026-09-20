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
    def test_no_template_x_if_inside_svg(self):
        """svg 内不得使用 <template x-if>：SVG 命名空间的 template 没有 .content，
        Alpine 的 x-if 会抛 "Cannot read properties of undefined (reading 'cloneNode')"，
        图标永远渲染不出来。改用两个 svg + x-show 切换（实测等价且无错）。
        真实缺陷：submit.html 校验列表的 ✓/✗ 图标（旧版页面即带此 bug）。
        """
        svg_open = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)
        for name, path in _iter_templates():
            with open(path, "r", encoding="utf-8") as fh:
                html = fh.read()
            for m in svg_open.finditer(html):
                close = html.find("</svg>", m.end())
                if close == -1:
                    continue
                inner = html[m.end():close]
                if re.search(r"<template\b[^>]*x-if", inner, re.IGNORECASE):
                    self.fail("%s: <svg> 内出现 <template x-if>（SVG 命名空间下没有 "
                              ".content，Alpine x-if 必然抛 cloneNode 错误，图标渲染不出）。"
                              "改用两个 svg + x-show。" % name)

    def test_m3u8_delete_binding_is_booleanized(self):
        """定点回归这条真实缺陷：删除按钮必须显式布尔化，且不挂 per-task 临时标记。"""
        path = os.path.join(TEMPLATES_DIR, "tasks.html")
        with open(path, "r", encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn('@click="remove(t)"', html, "任务页应有删除按钮")
        m = re.search(r':disabled="([^"]*)"[^>]*@click="remove\(t\)"', html)
        self.assertIsNotNone(m, "删除按钮应带 :disabled 绑定")
        expr = m.group(1)
        self.assertTrue(
            "!!" in expr or "Boolean(" in expr,
            "删除按钮的 :disabled 必须显式布尔化（原始 undefined 会被 Alpine 当成"
            "「设置该属性」→ 按钮永久灰掉），当前表达式：%s" % expr)
        self.assertNotIn("t.deleting", expr,
                         "不要再把临时标记挂在任务对象上：列表刷新会整体替换对象，标记随之丢失")

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
