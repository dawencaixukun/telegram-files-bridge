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
             patch.object(core_backend, "_tg_state_of", AsyncMock(return_value=TG_WAIT_CODE)), \
             patch.object(core_backend, "_tg_code_length", AsyncMock(return_value=6)):
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
             patch.object(core_backend, "_tg_state_of", AsyncMock(return_value=TG_WAIT_CODE)), \
             patch.object(core_backend, "_tg_code_length", AsyncMock(return_value=6)):
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


class TestCodeLength(unittest.TestCase):
    """验证码位数不固定（5 位 / 6 位）：后端读取 TDLib codeInfo，前端动态渲染格数。

    历史缺陷：登录向导写死 5 格 + 满 5 位自动提交 —— Telegram 现在常见发 6 位码，
    第 6 位根本输不进去，输到第 5 位就被截断提交，报「验证码错误」，
    多账号登录时反复踩坑（用户实测反馈）。
    """

    def setUp(self):
        core_backend._TG_WIZARD_SESSION = None

    @staticmethod
    def _rows(state):
        return [{"id": "c1", "lastAuthorizationState": state}]

    def _run(self, state):
        with patch.object(core_backend.BACKEND, "list_telegrams_raw",
                          new=AsyncMock(return_value=self._rows(state))):
            return asyncio.run(core_backend._tg_code_length("c1"))

    def test_reads_camel_case_code_info(self):
        got = self._run({"constructor": TG_WAIT_CODE,
                         "codeInfo": {"codeLength": 6, "phoneCodeHash": "x"}})
        self.assertEqual(got, 6)

    def test_reads_snake_case_code_info(self):
        got = self._run({"constructor": TG_WAIT_CODE,
                         "code_info": {"code_length": 5}})
        self.assertEqual(got, 5)

    def test_missing_code_info_returns_none(self):
        """旧后端不序列化 codeInfo：返回 None，前端退回 5/6 自适应，绝不崩。"""
        self.assertIsNone(self._run({"constructor": TG_WAIT_CODE}))

    def test_out_of_range_length_rejected(self):
        self.assertIsNone(self._run({"constructor": TG_WAIT_CODE,
                                     "codeInfo": {"codeLength": 99}}))

    def test_step1_returns_code_length(self):
        """step1 的 WAIT_CODE 响应必须带上 codeLength 字段（可为 None）。"""
        second = _FakeSession("second")
        core_backend._TG_WIZARD_SESSION = second
        client = get_test_client()
        token = bridge_server._make_portal_token()
        csrf = "csrf-code-length"
        client.cookies.set(bridge_server.PORTAL_COOKIE, token)
        client.cookies.set(bridge_server.CSRF_COOKIE, csrf)
        with patch.object(core_backend, "new_backend_session", lambda: second), \
             patch.object(core_backend, "_tg_wait_state",
                          AsyncMock(return_value=TG_WAIT_CODE)), \
             patch.object(core_backend, "_tg_state_of", AsyncMock(return_value=TG_WAIT_CODE)), \
             patch("routers.system._tg_code_length", AsyncMock(return_value=6)):
            r = client.post("/tg-login/step1",
                            json={"country": "+86", "phone": "13800000000"},
                            headers={"X-CSRF-Token": csrf})
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body.get("codeLength"), 6)


class TestOtpTemplateDynamic(unittest.TestCase):
    """前端模板：格数必须由 otpLen 驱动，不得再写死 5。"""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.html = open(os.path.join(root, "templates", "tg_login.html"),
                         encoding="utf-8").read()

    def test_box_count_is_dynamic(self):
        self.assertIn("x-for=\"i in otpLen\"", self.html,
                      "OTP 格数必须由 otpLen 驱动（写死 5 会吞掉 6 位码的第 6 位）")
        self.assertNotIn("x-for=\"i in 5\"", self.html,
                         "不得再出现写死的 5 格循环")

    def test_state_has_otp_len_and_six_slots(self):
        self.assertIn("otpLen: 6,", self.html, "默认应为 6 格（Telegram 当前常见位数）")
        self.assertIn("otp: ['', '', '', '', '', '']", self.html,
                      "otp 数组必须能容纳 6 位")

    def test_auto_submit_uses_dynamic_length(self):
        self.assertIn("this.otp.join('').length === this.otpLen", self.html,
                      "自动提交必须按当前格数判断，而非固定 5")
        self.assertNotIn("this.otp.join('').length === 5", self.html,
                         "不得残留固定 5 位的自动提交判断")

    def test_manual_length_switch_available(self):
        self.assertIn("setOtpLen", self.html,
                      "后端取不到权威位数时，用户必须能手动切换 5/6 位")

    def test_paste_autodetects_length(self):
        self.assertIn("digits.length === 5 && this.otpLen === 6", self.html,
                      "粘贴 5 位码时须自动收窄到 5 格（否则末位丢失）")
        self.assertIn("digits.length === 6 && this.otpLen === 5", self.html,
                      "粘贴 6 位码时须自动扩到 6 格")

    def test_first_box_accepts_autofill(self):
        self.assertIn(":maxlength=\"i === 1 ? otpLen : 1\"", self.html,
                      "首格须放开 maxlength 以承接浏览器 one-time-code 自动填充")


if __name__ == "__main__":
    unittest.main()
