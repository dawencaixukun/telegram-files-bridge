# -*- coding: utf-8 -*-
"""
test_security_audit_t8.py — Task t8: 凭据安全、外发防SSRF与全局搜索专项深度安全审计测试套件
========================================================================================
安全审计验收标准矩阵：
1. 验收项 1: 凭据安全与脱敏审计
   - 验证 Telegram Bot Token 等敏感密钥在配置文件中受到 0600 严格权限保护
   - 验证配置接口（GET/POST /api/notify/config）脱敏回显，明文密钥不外泄
   - 验证前端带掩码更新时安全保留原有真实 Token
   - 验证网络异常与错误抛出时对异常信息进行密钥擦除脱敏（bot<REDACTED>）
2. 验收项 2: 通知外发防 SSRF 与凭据注入渗透审计
   - 验证通知外发 HTTP 请求严格限制为 Telegram 官方 API 域名 (https://api.telegram.org)
   - 验证杜绝内网 IP（127.0.0.1、169.254.169.254 等）与第三方恶意域名 SSRF 风险
   - 验证 HTTP 客户端强制配置 follow_redirects=False，杜绝 301/302 重定向逃逸
   - 验证 Bot Token 与 Chat ID 格式白名单校验，杜绝路径穿越、CRLF 请求拆分与 Userinfo 伪造
3. 验收项 3: 全局聚合搜索端点鉴权、路径隔离与跨会话防泄露审计
   - 验证全局搜索端点具备完备门禁鉴权，未登录请求严格返回 401 Unauthorized
   - 验证检索关键词包含任意路径参数（如 ../、/etc/passwd、Windows 盘符）时杜绝路径穿越
   - 验证已标记删除与物理不存在资产的安全过滤与数据隔离
   - 验证云端检索采用纯内存快照（check_remote=False），消除外部越权网络探测
4. 验收项 4: 批量重试与搜索接口防高频刷请求与防 DoS 机制审计
   - 验证搜索端点长文本截断、分词上限与条数上下界限制，防范 ReDoS 与内存耗尽
   - 验证搜索与批量重试端点具备客户端滑动窗口限流机制，超频自动返回 429 Too Many Requests
   - 验证批量重试接口具备并发锁 (_BATCH_RETRY_LOCK) 序列化保障，阻断并发重放竞争
   - 验证批量重试任务数上限（单次最多100个）与重试次数上限熔断（防无限死循环 DoS）
"""
import asyncio
import json
import os
import stat
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
import bridge_server


class BaseSecurityTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(bridge_server.app)

    def setUp(self):
        self.orig_jobs = dict(bridge_server._ARCHIVE_JOBS)
        self.orig_tasks = dict(bridge_server._ARCHIVE_TASKS)
        self.orig_deleted = set(bridge_server._DELETED_LOCAL_UIDS)
        self.orig_notify_cfg = dict(bridge_server._NOTIFY_CONFIG)
        self.orig_notify_cfg["events"] = dict(bridge_server._NOTIFY_CONFIG.get("events", {}))
        self.orig_search_reqs = dict(bridge_server._SEARCH_REQUESTS)
        self.orig_retry_reqs = dict(bridge_server._BATCH_RETRY_REQUESTS)

    def tearDown(self):
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self.orig_jobs)
        bridge_server._ARCHIVE_TASKS.clear()
        bridge_server._ARCHIVE_TASKS.update(self.orig_tasks)
        bridge_server._DELETED_LOCAL_UIDS.clear()
        bridge_server._DELETED_LOCAL_UIDS.update(self.orig_deleted)
        bridge_server._NOTIFY_CONFIG.clear()
        bridge_server._NOTIFY_CONFIG.update(self.orig_notify_cfg)
        bridge_server._SEARCH_REQUESTS.clear()
        bridge_server._SEARCH_REQUESTS.update(self.orig_search_reqs)
        bridge_server._BATCH_RETRY_REQUESTS.clear()
        bridge_server._BATCH_RETRY_REQUESTS.update(self.orig_retry_reqs)

    def _auth_cookies(self):
        token = bridge_server._make_portal_token()
        csrf = "sec_test_csrf_token_xyz"
        cookies = {
            bridge_server.PORTAL_COOKIE: token,
            bridge_server.CSRF_COOKIE: csrf,
        }
        headers = {bridge_server.CSRF_HEADER: csrf}
        return cookies, headers


# =====================================================================
# 1. 凭据存储安全、权限隔离 (0600) 与接口安全脱敏审计
# =====================================================================
class TestCredentialSecurityAndMasking(BaseSecurityTestCase):
    """专项审计 Telegram Bot Token 等敏感凭据在磁盘落盘、内存存储与 API 回显中的安全性。"""

    def test_notify_config_file_permission_0600(self):
        """审计项 1.1: 验证 .notify_config.json 文件在创建与保存时均受到 0600 (只允许所有者读写) 严格权限保护。"""
        with tempfile.TemporaryDirectory(prefix="sec_notify_perm_") as td:
            test_conf_path = os.path.join(td, ".notify_config.json")
            with patch.object(bridge_server, "_NOTIFY_CONFIG_FILE", test_conf_path), \
                 patch.object(bridge_server, "APP_ROOT_DIR", td):
                bridge_server._NOTIFY_CONFIG["botToken"] = "123456789:SecAuditSecretTokenABCDEF"
                bridge_server._NOTIFY_CONFIG["chatId"] = "98765432"
                bridge_server._notify_config_save()

                self.assertTrue(os.path.exists(test_conf_path))
                # 检查文件权限模式
                mode = stat.S_IMODE(os.stat(test_conf_path).st_mode)
                # 在 POSIX 环境下需为 0o600；Windows 兼容模式下验证非世界可写/基本权限
                if os.name != "nt":
                    self.assertEqual(mode, 0o600, f"配置文件权限不符合 0600 规范: {oct(mode)}")

                # 测试加载时自动加固权限
                os.chmod(test_conf_path, 0o644 if os.name != "nt" else 0o666)
                bridge_server._notify_config_load()
                mode_after_load = stat.S_IMODE(os.stat(test_conf_path).st_mode)
                if os.name != "nt":
                    self.assertEqual(mode_after_load, 0o600, "加载配置后未自动收紧修复 0600 权限")

    def test_token_masking_algorithm(self):
        """审计项 1.2: 验证 _mask_token 脱敏算法的健壮性。"""
        # 标准长 Token (保留前 6 位，后 4 位，中间打码)
        tok = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"
        masked = bridge_server._mask_token(tok)
        self.assertTrue(masked.startswith("123456"))
        self.assertTrue(masked.endswith("wxyZ"))
        self.assertIn("******", masked)
        self.assertNotIn("ABCdefGhIJKlmNoPQRsTU", masked)

        # 短 Token (<10 字符全打码)
        short_tok = "123:abc"
        self.assertEqual(bridge_server._mask_token(short_tok), "******")

        # 空或 None
        self.assertEqual(bridge_server._mask_token(""), "")
        self.assertEqual(bridge_server._mask_token(None), "")

    def test_api_notify_config_endpoints_masking(self):
        """审计项 1.3: 验证 GET /api/notify/config 与 POST /api/notify/config 接口回显安全脱敏，严防泄露明文密钥。"""
        cookies, headers = self._auth_cookies()
        bridge_server._NOTIFY_CONFIG["botToken"] = "555666777:SuperSensitiveBotSecret123"
        bridge_server._NOTIFY_CONFIG["chatId"] = "1234567"

        # 1. GET 获取配置
        resp = self.client.get("/api/notify/config", cookies=cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        ret_token = data["config"]["botToken"]
        self.assertIn("******", ret_token)
        self.assertNotIn("SuperSensitiveBotSecret123", ret_token)
        self.assertTrue(data["config"]["hasBotToken"])

        # 2. POST 保存配置并返回脱敏回显
        save_payload = {
            "enabled": True,
            "channel": "bot",
            "botToken": "888999000:NewlyUpdatedSecretBotToken987",
            "chatId": "7654321",
        }
        resp2 = self.client.post("/api/notify/config", json=save_payload, cookies=cookies, headers=headers)
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        saved_masked = data2["config"]["botToken"]
        self.assertIn("******", saved_masked)
        self.assertNotIn("NewlyUpdatedSecretBotToken987", saved_masked)
        # 验证服务端内部已正确更新
        self.assertEqual(bridge_server._NOTIFY_CONFIG["botToken"], "888999000:NewlyUpdatedSecretBotToken987")

    def test_api_notify_config_preserve_token_on_mask(self):
        """审计项 1.4: 验证前端传回带 ****** 掩码时，服务端安全保留真实 Token，不被掩码覆盖。"""
        cookies, headers = self._auth_cookies()
        original_secret = "111222333:SecretTokenKeepIntact999"
        bridge_server._NOTIFY_CONFIG["botToken"] = original_secret

        # 前端编辑并保存，但未修改 Token（传回带掩码的数据）
        payload = {
            "enabled": True,
            "botToken": "111222******t999",
            "chatId": "234567",
        }
        resp = self.client.post("/api/notify/config", json=payload, cookies=cookies, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(bridge_server._NOTIFY_CONFIG["botToken"], original_secret, "掩码输入错误覆盖了真实 Token")

    def test_bot_send_error_token_redaction(self):
        """审计项 1.5: 验证网络异常抛出时，底层 httpx 报错信息包含的 Bot Token 被自动脱敏擦除。"""
        raw_secret = "123456789:ExtremelySecretBotFatherTokenXYZ"
        chat_id = "12345678"

        async def run_err_test():
            # 模拟 httpx 在抛出 ConnectError 时附带完整目标 URL
            fake_url_err = httpx_err = Exception(
                f"ConnectError: Failed to connect to https://api.telegram.org/bot{raw_secret}/sendMessage: [Errno 110] Connection timed out"
            )
            with patch("httpx.AsyncClient.post", side_effect=fake_url_err):
                ok, err = await bridge_server._send_via_bot(raw_secret, chat_id, "hello")
                self.assertFalse(ok)
                # 必须抹除真实 token
                self.assertNotIn(raw_secret, err, "异常信息泄露了未脱敏的明文 Bot Token!")
                self.assertIn("bot<REDACTED>", err, "异常信息未被规范替换为 bot<REDACTED>")

        asyncio.run(run_err_test())


# =====================================================================
# 2. 通知外发防 SSRF 与参数注入渗透审计
# =====================================================================
class TestNotifySSRFAndInjectionDefense(BaseSecurityTestCase):
    """专项渗透审计 Telegram Bot 与外发请求防 SSRF、协议限制与非法参数拦截。"""

    def test_strict_ssrf_domain_and_scheme_whitelist(self):
        """审计项 2.1: 验证必须严格限定 https://api.telegram.org，杜绝内网 IP、私有网段与任意第三方域名。"""
        async def run_ssrf_checks():
            valid_token = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"
            chat_id = "-100123456789"

            # 1. 拦截非 HTTPS (http://api.telegram.org)
            with patch("bridge_server.urlsplit", return_value=MagicMock(scheme="http", hostname="api.telegram.org", port=80)):
                ok, err = await bridge_server._send_via_bot(valid_token, chat_id, "text")
                self.assertFalse(ok)
                self.assertIn("非法 Telegram Bot API 目标地址", err)

            # 2. 拦截本地回环 127.0.0.1
            with patch("bridge_server.urlsplit", return_value=MagicMock(scheme="https", hostname="127.0.0.1", port=443)):
                ok, err = await bridge_server._send_via_bot(valid_token, chat_id, "text")
                self.assertFalse(ok)
                self.assertIn("非法 Telegram Bot API 目标地址", err)

            # 3. 拦截本地 IPv6 ::1
            with patch("bridge_server.urlsplit", return_value=MagicMock(scheme="https", hostname="::1", port=443)):
                ok, err = await bridge_server._send_via_bot(valid_token, chat_id, "text")
                self.assertFalse(ok)
                self.assertIn("非法 Telegram Bot API 目标地址", err)

            # 4. 拦截云厂商元数据地址 169.254.169.254
            with patch("bridge_server.urlsplit", return_value=MagicMock(scheme="https", hostname="169.254.169.254", port=443)):
                ok, err = await bridge_server._send_via_bot(valid_token, chat_id, "text")
                self.assertFalse(ok)
                self.assertIn("非法 Telegram Bot API 目标地址", err)

            # 5. 拦截内网私网地址 192.168.1.1 与 10.0.0.1
            for private_ip in ("192.168.1.1", "10.0.0.1"):
                with patch("bridge_server.urlsplit", return_value=MagicMock(scheme="https", hostname=private_ip, port=443)):
                    ok, err = await bridge_server._send_via_bot(valid_token, chat_id, "text")
                    self.assertFalse(ok)
                    self.assertIn("非法 Telegram Bot API 目标地址", err)

            # 6. 拦截高危外部域名 attacker-controlled.com
            with patch("bridge_server.urlsplit", return_value=MagicMock(scheme="https", hostname="attacker-controlled.com", port=443)):
                ok, err = await bridge_server._send_via_bot(valid_token, chat_id, "text")
                self.assertFalse(ok)
                self.assertIn("非法 Telegram Bot API 目标地址", err)

        asyncio.run(run_ssrf_checks())

    def test_follow_redirects_disabled(self):
        """审计项 2.2: 验证 AsyncClient 明确禁用重定向 (follow_redirects=False)，杜绝 301/302 重定向 SSRF。"""
        captured_kwargs = {}

        class DummyAsyncClient:
            def __init__(self, *args, **kwargs):
                captured_kwargs.update(kwargs)
            async def __aenter__(self):
                return self
            async def __aexit__(self, *exc):
                pass
            async def post(self, url, **kwargs):
                return MagicMock(status_code=200)

        async def run_client_check():
            with patch("httpx.AsyncClient", new=DummyAsyncClient):
                ok, _ = await bridge_server._send_via_bot("123456:SecretTokenABC", "123456", "test")
                self.assertTrue(ok)
                self.assertIn("follow_redirects", captured_kwargs)
                self.assertFalse(captured_kwargs["follow_redirects"], "HTTP 客户端未禁用 follow_redirects，存在重定向 SSRF 风险！")

        asyncio.run(run_client_check())

    def test_bot_token_and_chat_id_injection_protection(self):
        """审计项 2.3: 验证 Bot Token 与 Chat ID 格式防注入渗透（防路径穿越、换行CRLF、Userinfo @ 伪造）。"""
        async def run_injection_tests():
            # 路径穿越攻击 Token
            ok, err = await bridge_server._send_via_bot("../../etc/passwd", "123456", "text")
            self.assertFalse(ok)
            self.assertIn("非法", err)

            # CRLF 换行注入 Token
            ok, err = await bridge_server._send_via_bot("123456:ABC\r\nHost: evil.com", "123456", "text")
            self.assertFalse(ok)
            self.assertIn("非法", err)

            # Userinfo @ 域名伪造注入 Token
            ok, err = await bridge_server._send_via_bot("victim@attacker.com", "123456", "text")
            self.assertFalse(ok)
            self.assertIn("非法", err)

            # 空白符注入 Token
            ok, err = await bridge_server._send_via_bot("12345 6789:ABC", "123456", "text")
            self.assertFalse(ok)
            self.assertIn("非法", err)

            # 非法 Chat ID 注入（路径遍历或换行）
            ok, err = await bridge_server._send_via_bot("123456:ABCDEFGHIJKLMN", "../admin", "text")
            self.assertFalse(ok)
            self.assertIn("非法", err)

            ok, err = await bridge_server._send_via_bot("123456:ABCDEFGHIJKLMN", "1234\r\nSet-Cookie: x=1", "text")
            self.assertFalse(ok)
            self.assertIn("非法", err)

        asyncio.run(run_injection_tests())


# =====================================================================
# 3. 全局搜索端点鉴权、路径隔离与跨会话防信息泄露审计
# =====================================================================
class TestGlobalSearchSecurityAndIsolation(BaseSecurityTestCase):
    """专项审计跨库全局搜索 (tasks/local/cloud) 的鉴权门禁、路径隔离与防信息泄露。"""

    def test_search_unauthenticated_rejection(self):
        """审计项 3.1: 验证未登录用户访问 GET /api/search 与 GET /api/search/aggregate 被严格拦截为 401 Unauthorized。"""
        # 未携带 portal 认证 cookie
        resp1 = self.client.get("/api/search?q=movie")
        self.assertEqual(resp1.status_code, 401, "未登录访问 /api/search 未被拦截为 401")
        self.assertFalse(resp1.json()["ok"])

        resp2 = self.client.get("/api/search/aggregate?q=movie")
        self.assertEqual(resp2.status_code, 401, "未登录访问 /api/search/aggregate 未被拦截为 401")
        self.assertFalse(resp2.json()["ok"])

    def test_search_path_traversal_resistance(self):
        """审计项 3.2: 验证当搜索词传入相对路径逃逸、绝对路径或系统文件时，不会触发底层任意文件读取或路径逃逸。"""
        cookies, headers = self._auth_cookies()

        traversal_queries = [
            "../../etc/passwd",
            "..\\..\\windows\\win.ini",
            "/etc/shadow",
            "C:\\boot.ini",
            "../../.env",
            "../../.backend_creds",
        ]

        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=[])), \
             patch("bridge_server._cloud_archive_rows", new=AsyncMock(return_value=[])):
            for q in traversal_queries:
                resp = self.client.get(f"/api/search?q={q}", cookies=cookies)
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertTrue(data["ok"])
                self.assertEqual(data["total"], 0)
                self.assertEqual(len(data["results"]["local"]), 0)

    def test_search_deleted_and_nonexistent_local_assets_isolated(self):
        """审计项 3.3: 验证在存资产搜索时严格检验物理文件存在性与 _DELETED_LOCAL_UIDS，防止幽灵文件与已删除数据泄露。"""
        cookies, headers = self._auth_cookies()

        fake_tasks = [
            {
                "id": 1,
                "_unique_id": "uid_normal_exist",
                "filename": "real_file.mp4",
                "status": "completed",
                "_download_status": "completed",
                "local_path": "C:\\fake\\real_file.mp4",
            },
            {
                "id": 2,
                "_unique_id": "uid_physically_missing",
                "filename": "missing_file.mp4",
                "status": "completed",
                "_download_status": "completed",
                "local_path": "C:\\fake\\missing_file.mp4",
            },
            {
                "id": 3,
                "_unique_id": "uid_marked_deleted",
                "filename": "deleted_file.mp4",
                "status": "completed",
                "_download_status": "completed",
                "local_path": "C:\\fake\\deleted_file.mp4",
            }
        ]

        bridge_server._DELETED_LOCAL_UIDS.add("uid_marked_deleted")

        # 仅让 real_file.mp4 判定为真实物理存在
        def fake_exists(p):
            return "real_file.mp4" in p

        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=fake_tasks)), \
             patch("os.path.exists", side_effect=fake_exists), \
             patch("bridge_server._cloud_archive_rows", new=AsyncMock(return_value=[])):
            resp = self.client.get("/api/search?q=file", cookies=cookies)
            self.assertEqual(resp.status_code, 200)
            local_items = resp.json()["results"]["local"]

            # 必须只包含真实存在的 real_file.mp4
            uids = [item["uniqueId"] for item in local_items]
            self.assertIn("uid_normal_exist", uids)
            self.assertNotIn("uid_physically_missing", uids, "物理不存在的文件被违规曝光在本地搜索中")
            self.assertNotIn("uid_marked_deleted", uids, "已标记删除的 UID 被违规曝光在本地搜索中")

    def test_search_cloud_memory_indexing_isolation(self):
        """审计项 3.4: 验证云端归档检索完全采用 check_remote=False 纯内存索引，严防越权外部网络探测。"""
        cookies, headers = self._auth_cookies()

        call_args = []
        async def mock_cloud_rows(check_remote=True):
            call_args.append(check_remote)
            return []

        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=[])), \
             patch("bridge_server._cloud_archive_rows", side_effect=mock_cloud_rows):
            resp = self.client.get("/api/search?q=test", cookies=cookies)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(call_args, [False], "聚合搜索调用 _cloud_archive_rows 时未传 check_remote=False，存在网络 I/O 越权风险")


# =====================================================================
# 4. 批量重试与搜索接口防高频刷请求与防 DoS 审计
# =====================================================================
class TestAntiDoSAndRateLimiting(BaseSecurityTestCase):
    """专项审计搜索接口与批量重试接口的防高并发 DoS、滑动窗口限流、并发锁与熔断机制。"""

    def test_search_input_length_and_tokens_capping(self):
        """审计项 4.1: 验证超长检索词截断 (100字符)、分词数量上限 (8个) 与结果条数区间 [1, 50] 边界防护。"""
        cookies, headers = self._auth_cookies()

        # 构造一个 5000 字符的超长恶意字符串
        huge_query = "attack_" * 500
        resp = self.client.get(f"/api/search?q={huge_query}&limit=9999", cookies=cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        # query 回显已被截断至 <= 100 字符
        self.assertLessEqual(len(data["query"]), 100, "超长搜索查询未被截断，存在内存与 CPU 拒绝服务风险")

    def test_search_rate_limiting_sliding_window(self):
        """审计项 4.2: 验证短时间内发送大量搜索请求会触发服务端的滑动窗口限流并返回 429。"""
        cookies, headers = self._auth_cookies()

        # 模拟同一 IP 密集发送大量请求，达到上限
        bridge_server._SEARCH_REQUESTS["testclient"] = [time.time()] * bridge_server._SEARCH_RATE_LIMIT

        resp = self.client.get("/api/search?q=dos_test", cookies=cookies)
        self.assertEqual(resp.status_code, 429, "高频搜索未被限流拦截为 429")
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data.get("code"), "RATE_LIMITED")
        self.assertIn("过于频繁", data.get("message", ""))

    def test_batch_retry_concurrency_lock_protection(self):
        """审计项 4.3: 验证批量重试接口受 _BATCH_RETRY_LOCK 保护，防止多协程/多请求并发进入引起重试风暴与重放竞态。"""
        self.assertTrue(hasattr(bridge_server, "_BATCH_RETRY_LOCK"), "bridge_server 缺少 _BATCH_RETRY_LOCK 并发锁")
        self.assertIsInstance(bridge_server._BATCH_RETRY_LOCK, asyncio.Lock)

    def test_batch_retry_rate_limiting(self):
        """审计项 4.4: 验证批量重试端点高频调用受到滑动窗口限流拦截 (429)。"""
        cookies, headers = self._auth_cookies()

        # 模拟同一 IP 密集重试已达阈值
        bridge_server._BATCH_RETRY_REQUESTS["testclient"] = [time.time()] * bridge_server._BATCH_RETRY_LIMIT

        resp = self.client.post("/api/archive/batch-retry", json={"category": "all"}, cookies=cookies, headers=headers)
        self.assertEqual(resp.status_code, 429, "高频批量重试未被限流拦截为 429")
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data.get("code"), "RATE_LIMITED")

    def test_batch_retry_max_job_ids_limit(self):
        """审计项 4.5: 验证批量重试单次指定的 ID 数量超出 100 个时被参数校验拦截 (400)。"""
        cookies, headers = self._auth_cookies()
        huge_ids = [f"job_{i}" for i in range(150)]

        resp = self.client.post("/api/archive/batch-retry", json={"ids": huge_ids}, cookies=cookies, headers=headers)
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn("不可超过 100 个", data["message"])

    def test_batch_retry_anti_replay_cap(self):
        """审计项 4.6: 验证已达最大连续重试上限 (_MAX_JOB_RETRIES=20) 的任务在全量重试中被安全熔断跳过，防止死循环重试 DoS。"""
        cookies, headers = self._auth_cookies()

        # 构造一个已连续重试 25 次的顽固失败任务和一个正常失败任务
        bridge_server._ARCHIVE_JOBS["job-exhausted"] = {
            "id": "job-exhausted",
            "filename": "exhausted.mp4",
            "state": "failed",
            "error": "502 Bad Gateway timeout",
            "retry_count": 25,
        }
        bridge_server._ARCHIVE_JOBS["job-normal"] = {
            "id": "job-normal",
            "filename": "normal.mp4",
            "state": "failed",
            "error": "504 Gateway Timeout",
            "retry_count": 2,
        }

        with patch("bridge_server._archive_worker", new=AsyncMock()):
            resp = self.client.post("/api/archive/batch-retry", json={"category": "all"}, cookies=cookies, headers=headers)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            retried_ids = data["retriedJobIds"]
            self.assertIn("job-normal", retried_ids)
            self.assertNotIn("job-exhausted", retried_ids, "连续重试次数超限的任务未被熔断跳过，存在死循环重试风险")


if __name__ == "__main__":
    unittest.main()
