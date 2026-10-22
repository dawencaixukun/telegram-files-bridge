# -*- coding: utf-8 -*-
"""
tests/test_tg_login_wizard.py
=============================
TG 登录向导的两个回归：

1. **向导必须用自己的后端会话**（不能借主会话 BACKEND）。
   后端 /telegram/create 是会话级改绑操作：在一个已链接已授权账号的会话上再
   create，会把该会话的 api 通道改绑到新建的未授权客户端（实测：绑定后 GetMe
   返回 code 401 Unauthorized），已授权账号随之从 api 通道失联。
   历史缺陷：`_tg_ensure_client()` 调的是 `BACKEND.telegram_create()`，而该
   方法在代码精简提交里被删掉 → step1 直接 AttributeError，向导整体不可用
   （VPS journalctl 实测：`TG step1 失败: 'BackendClient' object has no
   attribute 'telegram_create'`）。

2. **「添加一个账号之后无法添加第二个」**：同一个向导会话重复 create 是幂等的
   （后端返回同一 id），所以第二次登录会复用上一次的客户端，无法建新账号。
   `POST /tg-login/reset`（前端 ?fresh=1 / 完成页「再添加一个账号」）必须先
   丢弃旧会话，再建新客户端。
"""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tgwizard-")
os.environ["TG_DATA_DIR"] = _TMP

import bridge_server  # noqa: E402
import core.backend as core_backend  # noqa: E402
from core.config import TG_WAIT_CODE  # noqa: E402
from tests.conftest import get_test_client  # noqa: E402


class _FakeSession:
    """记录 create / api 调用次数的假后端会话。"""

    def __init__(self, name):
        self.name = name
        self.creates = 0
        self.api_calls = []
        self.closed = False

    async def telegram_create(self, proxy_name=None):
        self.creates += 1
        # 后端实测语义：同一会话内重复 create 是幂等的，返回同一个客户端
        return {"id": f"{self.name}-client", "lastState": None}

    async def telegram_api(self, method, params=None):
        self.api_calls.append((method, params))
        return {"code": f"{self.name}-{method}"}

    async def close(self):
        self.closed = True


class TestWizardSessionIsolation(unittest.TestCase):
    def setUp(self):
        core_backend._TG_WIZARD_SESSION = None

    def tearDown(self):
        core_backend._TG_WIZARD_SESSION = None

    def test_wizard_does_not_touch_main_session(self):
        """向导创建客户端时绝不能调用主会话 BACKEND.telegram_create。"""
        fake = _FakeSession("wiz")
        main_create = AsyncMock(side_effect=AssertionError("不得在主会话上 create"))
        with patch.object(core_backend, "new_backend_session", lambda: fake), \
             patch.object(core_backend.BACKEND, "telegram_create", main_create):
            r = asyncio.run(core_backend._tg_ensure_client())
        self.assertEqual(r["id"], "wiz-client")
        main_create.assert_not_called()
        self.assertIs(core_backend._TG_WIZARD_SESSION, fake)

    def test_wizard_api_goes_through_wizard_session(self):
        """api 透传必须落在向导会话上（主会话会打到别的账号 / 报 not link）。"""
        fake = _FakeSession("wiz")
        with patch.object(core_backend, "new_backend_session", lambda: fake):
            asyncio.run(core_backend._tg_ensure_client())
            asyncio.run(core_backend._tg_api("CheckAuthenticationCode", {"code": "12345"}))
        self.assertEqual(fake.api_calls, [("CheckAuthenticationCode", {"code": "12345"})])

    def test_tg_api_without_session_fails_loud(self):
        with self.assertRaises(RuntimeError):
            asyncio.run(core_backend._tg_api("CheckAuthenticationCode", {"code": "1"}))

    def test_reset_discards_session_so_second_account_gets_new_client(self):
        """「添加账号」：reset 之后必须建出第二个客户端，而不是复用第一个。"""
        first = _FakeSession("first")
        second = _FakeSession("second")
        created = []

        def _factory():
            s = first if not created else second
            created.append(s)
            return s

        with patch.object(core_backend, "new_backend_session", _factory):
            r1 = asyncio.run(core_backend._tg_ensure_client())
            # 未 reset：同一会话复用（幂等）
            r1b = asyncio.run(core_backend._tg_ensure_client())
            asyncio.run(core_backend._tg_wizard_close())
            # reset 之后：新会话 → 新客户端
            r2 = asyncio.run(core_backend._tg_ensure_client())
        self.assertEqual(r1["id"], "first-client")
        self.assertEqual(r1b["id"], "first-client")   # 幂等复用
        self.assertEqual(r2["id"], "second-client")   # 第二个账号
        self.assertNotEqual(r1["id"], r2["id"])
        self.assertTrue(first.closed)

    def test_broken_session_is_dropped(self):
        """会话失效时丢弃缓存，下次调用重建（不再永久卡死）。"""
        bad = _FakeSession("bad")
        bad.telegram_create = AsyncMock(side_effect=RuntimeError("cookie 过期"))
        good = _FakeSession("good")
        seq = [bad, good]

        with patch.object(core_backend, "new_backend_session", lambda: seq.pop(0)):
            with self.assertRaises(RuntimeError):
                asyncio.run(core_backend._tg_ensure_client())
            self.assertIsNone(core_backend._TG_WIZARD_SESSION)
            r = asyncio.run(core_backend._tg_ensure_client())
        self.assertEqual(r["id"], "good-client")


class TestWizardRoutes(unittest.TestCase):
    """路由层：step1 带 fresh 时先 reset，且 api 调用走向导会话。"""

    def setUp(self):
        core_backend._TG_WIZARD_SESSION = None
        self.client = get_test_client()
        self.token = bridge_server._make_portal_token()
        self.csrf = "csrf-secret-token-wizard"
        self.client.cookies.set(bridge_server.PORTAL_COOKIE, self.token)
        self.client.cookies.set(bridge_server.CSRF_COOKIE, self.csrf)

    def tearDown(self):
        core_backend._TG_WIZARD_SESSION = None

    def _post(self, url, body):
        return self.client.post(url, json=body, headers={"X-CSRF-Token": self.csrf})

    def test_reset_endpoint_releases_session(self):
        fake = _FakeSession("wiz")
        core_backend._TG_WIZARD_SESSION = fake
        r = self._post("/tg-login/reset", {})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertIsNone(core_backend._TG_WIZARD_SESSION)
        self.assertTrue(fake.closed)

    def test_step1_fresh_resets_before_create(self):
        """fresh=True：先丢弃旧会话 → 新客户端（第二个账号可建）。"""
        first = _FakeSession("first")
        second = _FakeSession("second")
        core_backend._TG_WIZARD_SESSION = first
        with patch.object(core_backend, "new_backend_session", lambda: second), \
             patch.object(core_backend, "_tg_wait_state",
                          AsyncMock(return_value=TG_WAIT_CODE)), \
             patch.object(core_backend, "_tg_state_of", AsyncMock(return_value=TG_WAIT_CODE)):
            r = self._post("/tg-login/step1", {"country": "+86", "phone": "13800000000", "fresh": True})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["telegramId"], "second-client")
        self.assertTrue(first.closed)
        self.assertEqual(second.api_calls[0][0], "SetAuthenticationPhoneNumber")
        self.assertEqual(second.api_calls[0][1]["phoneNumber"], "+8613800000000")

    def test_step1_without_fresh_reuses_wizard_session(self):
        first = _FakeSession("first")
        core_backend._TG_WIZARD_SESSION = first
        with patch.object(core_backend, "new_backend_session",
                          lambda: self.fail("不应新建会话")), \
             patch.object(core_backend, "_tg_wait_state",
                          AsyncMock(return_value=TG_WAIT_CODE)), \
             patch.object(core_backend, "_tg_state_of", AsyncMock(return_value=TG_WAIT_CODE)):
            r = self._post("/tg-login/step1", {"country": "+86", "phone": "13800000000"})
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["telegramId"], "first-client")


class TestTemplateWiring(unittest.TestCase):
    """模板/前端接线：状态样式不能再用 Alpine :style（会清空静态 style）。"""

    def test_no_alpine_style_on_wizard_elements(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "templates", "tg_login.html"), encoding="utf-8").read()
        self.assertNotIn(":style=", src,
                         "tg_login.html 不应再用 Alpine :style —— 它会整体覆盖静态 style 属性")
        self.assertIn("dd-caret", src)
        self.assertIn("tg-login/reset", src)
        self.assertIn("startNew", src)

    def test_css_defines_wizard_state_classes(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        css = open(os.path.join(root, "static", "css", "main.css"), encoding="utf-8").read()
        for cls in (".dd-caret", ".dd-caret.open", "button.input.focused", ".dd-item.is-active"):
            self.assertIn(cls, css, f"缺少 {cls} 样式定义")

    def test_account_page_has_add_account_entry(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        html = open(os.path.join(root, "templates", "account.html"), encoding="utf-8").read()
        self.assertIn("/tg-login?fresh=1", html)

    def test_country_picker_width_is_not_inline_fixed(self):
        """区号框宽度必须走 .country-picker + 窄屏媒体查询。

        历史缺陷：内联 `width:200px;flex-shrink:0` 在 390px 视口下把号码框挤到
        98px（实测），区号框比号码框还长。宽度规则必须可在媒体查询里覆盖，
        不能写死在 style 属性上。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        html = open(os.path.join(root, "templates", "tg_login.html"), encoding="utf-8").read()
        self.assertIn('class="country-picker"', html)
        self.assertNotIn("width:200px", html,
                         "区号框宽度不能写死在内联 style 上，否则窄屏无法收窄")

    def test_css_narrows_country_picker_on_mobile(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        css = open(os.path.join(root, "static", "css", "main.css"), encoding="utf-8").read()
        self.assertIn(".country-picker", css)
        # 必须存在针对窄屏的收窄规则
        import re as _re
        m = _re.search(r"@media \(max-width: 480px\) \{\s*\.country-picker \{[^}]*\}", css)
        self.assertIsNotNone(m, "缺少 .country-picker 的窄屏收窄媒体查询")
        self.assertIn("max-width: 132px", m.group(0) if m else "")


if __name__ == "__main__":
    unittest.main()
