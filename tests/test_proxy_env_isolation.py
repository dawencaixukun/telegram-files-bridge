# -*- coding: utf-8 -*-
"""回归测试：本机回环 HTTP 客户端不得被环境变量代理劫持 + socks5h 兼容层。

背景（实测复现）：
- 本机 /root/.bashrc 里 clashctl 的 watch_proxy 会在**每个登录 shell**
  自动导出 ALL_PROXY=socks5h://127.0.0.1:7890。
- httpx 默认 trust_env=True，会读该变量。结果：
    1) OpenList(127.0.0.1:5244) / Java 后端(127.0.0.1:8123) 的回环请求
       被塞进代理；
    2) httpx 0.27 的 Proxy 只接受 http/https/socks5，遇到 socks5h:// 直接抛
         ValueError: Unknown scheme for proxy URL URL('socks5h://127.0.0.1:7890')
       —— 建 client 即抛，若发生在 import 期整个应用起不来
       （实测：带代理跑测试只跑起 93 个用例，68 errors）。
- 修法：绝环 client 显式 trust_env=False；且在 core/config 启动最早期把
  环境里的 socks5h:// 归一化成 httpx 认得的 socks5://。
"""
import os
import unittest
from unittest import mock


class TestSocksSchemeNormalization(unittest.TestCase):
    """core/config 的 socks5h:// → socks5:// 归一化。"""

    def test_normalize_rewrites_socks5h(self):
        from core.config import _normalize_socks_proxy_scheme
        env = {
            "ALL_PROXY": "socks5h://127.0.0.1:7890",
            "all_proxy": "socks5h://127.0.0.1:7890",
            "HTTPS_PROXY": "socks5h://user:pw@127.0.0.1:7890",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            _normalize_socks_proxy_scheme()
            self.assertEqual(os.environ["ALL_PROXY"], "socks5://127.0.0.1:7890")
            self.assertEqual(os.environ["all_proxy"], "socks5://127.0.0.1:7890")
            # 重写后不得丢掉认证信息
            self.assertEqual(os.environ["HTTPS_PROXY"], "socks5://user:pw@127.0.0.1:7890")

    def test_normalize_leaves_other_schemes_untouched(self):
        from core.config import _normalize_socks_proxy_scheme
        env = {"ALL_PROXY": "http://127.0.0.1:7890", "HTTP_PROXY": "socks5://x:1"}
        with mock.patch.dict(os.environ, env, clear=False):
            _normalize_socks_proxy_scheme()
            self.assertEqual(os.environ["ALL_PROXY"], "http://127.0.0.1:7890")
            self.assertEqual(os.environ["HTTP_PROXY"], "socks5://x:1")

    def test_httpx_accepts_normalized_scheme(self):
        """归一化后的 scheme 必须是 httpx 认得的（否则建 client 仍会抛）。"""
        import httpx
        from core.config import _normalize_socks_proxy_scheme
        with mock.patch.dict(os.environ, {"ALL_PROXY": "socks5h://127.0.0.1:7890"}, clear=False):
            _normalize_socks_proxy_scheme()
            try:
                c = httpx.AsyncClient(proxy=os.environ["ALL_PROXY"])
            except ValueError as e:  # pragma: no cover
                self.fail(f"归一化后 httpx 仍不接受该代理: {e}")
            else:
                import asyncio
                asyncio.run(c.aclose())
        # 保留 socksio 依赖（httpx 解析/建立 socks 连接需要）
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec("socksio"),
                             "缺少 socksio，httpx 无法建立 socks 代理连接")


class TestLocalLoopbackClientsIgnoreProxy(unittest.TestCase):
    def test_state_openlist_clients_have_trust_env_false(self):
        """core/state.py 的两个 OpenList 客户端必须 trust_env=False。"""
        from core import state
        for name in ("_openlist_client", "_openlist_upload_client"):
            c = getattr(state, name)
            self.assertFalse(getattr(c, "trust_env", True),
                             f"{name} 必须 trust_env=False，否则回环请求会走代理")

    def test_backend_client_has_trust_env_false(self):
        """core/backend.py 的 BackendClient 必须 trust_env=False（回环地址）。"""
        import asyncio
        from core.backend import BackendClient
        bc = BackendClient("http://127.0.0.1:8123/api")
        try:
            self.assertFalse(bc.client.trust_env,
                             "BackendClient 必须 trust_env=False（回环地址）")
        finally:
            asyncio.run(bc.client.aclose())

    def test_import_survives_socks_proxy_env(self):
        """环境里带 socks5h 代理时，import 应用必须不抛异常。"""
        with mock.patch.dict(os.environ, {"ALL_PROXY": "socks5h://127.0.0.1:7890"}, clear=False):
            import bridge_server  # noqa: F401  —— 能 import 即为通过
        self.assertIsNotNone(bridge_server.app)


if __name__ == "__main__":
    unittest.main(verbosity=2)
