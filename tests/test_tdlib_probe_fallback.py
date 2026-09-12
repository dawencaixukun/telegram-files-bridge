# -*- coding: utf-8 -*-
"""
tests/test_tdlib_probe_fallback.py
====================================
TDLib 会话探针双信号裁决回归测试：

历史缺陷（用户反馈「TDLib 会话一直异常」）：旧探针主信号
getAuthorizationState 一旦抛非超时异常（Java 后端 502/连接拒绝等
HTTP 层失败）就立即 raise 判 critical——哪怕 Telegram 会话实际在跑。
只要后端那个端点抖动，System Doctor 的 TDLib 项就永久异常。

修复后的裁决规则：
1. 主探针 authorizationStateReady → healthy（不变）；
2. 传输层失败（HTTPStatusError/TransportError/超时）不再一票判死，
   退回 list_telegrams 的 authorized 兜底二次确认；
3. 两路信号都拿不到授权确认才判 critical，报错带 HTTP 状态码；
4. TDLib 业务层 error 对象（如 Unauthorized）不算传输故障，交由账号列表裁决。
"""
import os
import sys
import unittest
import asyncio
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import bridge_server


def _raise_502(*args, **kwargs):
    """模拟 Java 后端 502 Bad Gateway（用户环境当时真实发生的故障）。"""
    req = httpx.Request("POST", "http://127.0.0.1:8123/api/telegram/api/getAuthorizationState")
    resp = httpx.Response(502, request=req)
    raise httpx.HTTPStatusError("502 Bad Gateway", request=req, response=resp)


class TestTdlibProbeFallback(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_ready_is_healthy(self):
        """主探针 Ready → healthy（原契约不回归）。"""
        bridge_server._reset_flood_wait()
        with patch.object(bridge_server.BACKEND, "telegram_api",
                          new=AsyncMock(return_value={"@type": "authorizationStateReady"})):
            r = self._run(bridge_server._doctor_probe_tdlib())
        self.assertEqual(r["status"], "healthy")
        self.assertFalse(r["details"]["isFloodWait"])

    def test_http_502_falls_back_to_authorized_account(self):
        """核心修复：主探针 502 但账号列表确认授权 → healthy（旧逻辑误判 critical）。"""
        with patch.object(bridge_server.BACKEND, "telegram_api", new=AsyncMock(side_effect=_raise_502)), \
             patch.object(bridge_server.BACKEND, "list_telegrams",
                          new=AsyncMock(return_value=[{"authorized": True, "phone": "+8610***0001", "telegramId": 1}])):
            r = self._run(bridge_server._doctor_probe_tdlib())
        self.assertEqual(r["status"], "healthy",
                         "传输层失败应退回账号列表兜底，不应一票判死")

    def test_http_502_with_no_fallback_is_critical_with_status_code(self):
        """两路信号都失败 → critical，且报错带 HTTP 状态码便于诊断。"""
        with patch.object(bridge_server.BACKEND, "telegram_api", new=AsyncMock(side_effect=_raise_502)), \
             patch.object(bridge_server.BACKEND, "list_telegrams", new=AsyncMock(side_effect=ConnectionRefusedError)):
            r = self._run(bridge_server._doctor_probe_tdlib())
        self.assertEqual(r["status"], "critical")
        self.assertIn("HTTP 502", r["message"], "报错应携带 HTTP 状态码")

    def test_transport_error_with_no_fallback_is_critical(self):
        """连接拒绝（TransportError）+ 列表失败 → critical。"""
        with patch.object(bridge_server.BACKEND, "telegram_api",
                          new=AsyncMock(side_effect=httpx.ConnectError("refused"))), \
             patch.object(bridge_server.BACKEND, "list_telegrams", new=AsyncMock(side_effect=ConnectionRefusedError)):
            r = self._run(bridge_server._doctor_probe_tdlib())
        self.assertEqual(r["status"], "critical")
        self.assertIn("ConnectError", r["message"])

    def test_tdlib_error_object_with_authorized_account_is_healthy(self):
        """TDLib 业务层 error 对象不算传输故障；账号列表确认授权 → healthy。"""
        with patch.object(bridge_server.BACKEND, "telegram_api",
                          new=AsyncMock(return_value={"@type": "error", "code": 401, "message": "Unauthorized"})), \
             patch.object(bridge_server.BACKEND, "list_telegrams",
                          new=AsyncMock(return_value=[{"authorized": True, "phone": "p1", "telegramId": 1}])):
            r = self._run(bridge_server._doctor_probe_tdlib())
        self.assertEqual(r["status"], "healthy")

    def test_timeout_falls_back_to_account_list(self):
        """主探针超时不判死，账号列表兜底。"""
        async def _timeout(*a, **kw):
            await asyncio.sleep(10)
        with patch.object(bridge_server.BACKEND, "telegram_api", new=AsyncMock(side_effect=_timeout)), \
             patch.object(bridge_server.BACKEND, "list_telegrams",
                          new=AsyncMock(return_value=[{"authorized": True, "phone": "p", "telegramId": 1}])):
            r = self._run(bridge_server._doctor_probe_tdlib())
        self.assertEqual(r["status"], "healthy", "超时应走账号列表兜底")

    def test_flood_wait_takes_precedence(self):
        """FloodWait 冷却中优先显示 warning（原契约不回归）。"""
        bridge_server._trigger_flood_wait("default", 30, reason="FLOOD_WAIT_30")
        try:
            with patch.object(bridge_server.BACKEND, "telegram_api",
                              new=AsyncMock(return_value={"@type": "authorizationStateReady"})):
                r = self._run(bridge_server._doctor_probe_tdlib())
            self.assertEqual(r["status"], "warning")
            self.assertTrue(r["details"]["isFloodWait"])
            self.assertIn("限流冷却", r["message"])
        finally:
            bridge_server._reset_flood_wait()

    def test_login_incomplete_is_warning(self):
        """无授权且无传输故障 → warning 登录未完成（原契约不回归）。"""
        bridge_server._reset_flood_wait()
        with patch.object(bridge_server.BACKEND, "telegram_api",
                          new=AsyncMock(return_value={"@type": "authorizationStateWaitPhoneNumber"})), \
             patch.object(bridge_server.BACKEND, "list_telegrams",
                          new=AsyncMock(return_value=[{"authorized": False, "lastAuthorizationState": {"@type": "x"}}])):
            r = self._run(bridge_server._doctor_probe_tdlib())
        self.assertEqual(r["status"], "warning")
        self.assertIn("登录未完成", r["message"])


if __name__ == "__main__":
    unittest.main()
