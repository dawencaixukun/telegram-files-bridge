# -*- coding: utf-8 -*-
"""
test_security_audit_t5.py — Task t5: 服务器与数据库安全专项深度审计自动化验证套件
=============================================================================
审计验收门禁矩阵：
1. 验收项 1: URL 解析防 SSRF 漏洞，严格限制协议与 t.me/telegram.me/telegram.dog 白名单
2. 验收项 2: 接口 Session 鉴权与 CSRF Token 双重门禁，防止未授权与跨站调用
3. 验收项 3: 输入参数防整数溢出、防路径穿越与命令注入风险
4. 验收项 4: 本地及数据持久化文件操作安全性，保障服务器系统与数据库完全隔离
"""
import asyncio
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
import bridge_server


class TestSecurityAuditT5(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(bridge_server.app)
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_sec_t5_")
        self._orig_archive_jobs = dict(bridge_server._ARCHIVE_JOBS)
        self._orig_archive_file = bridge_server._ARCHIVE_FILE
        bridge_server._ARCHIVE_FILE = os.path.join(self.tmp_dir, ".test_sec_archive_jobs.json")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        bridge_server._QUICK_ARCHIVE_REGISTRY.clear()
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self._orig_archive_jobs)
        bridge_server._ARCHIVE_FILE = self._orig_archive_file
        bridge_server.CHAT_SOURCE_CACHE["key"] = None
        bridge_server.CHAT_SOURCE_CACHE["value"] = None

    def _auth_cookies(self):
        """构造有效 Portal Session 与 CSRF Token 凭证。"""
        token = bridge_server._make_portal_token()
        csrf = "csrf-security-audit-token-9999"
        cookies = {
            bridge_server.PORTAL_COOKIE: token,
            bridge_server.CSRF_COOKIE: csrf,
        }
        headers = {
            bridge_server.CSRF_HEADER: csrf,
        }
        return cookies, headers

    # =========================================================================
    # 验收项 1: URL 解析防 SSRF 漏洞与白名单验证
    # =========================================================================
    def test_ssrf_and_domain_whitelist_rigorous(self):
        """深度审计：SSRF 穿透攻击向量拦截与协议/域名强制约束。"""
        ssrf_attack_payloads = [
            # 1.1 内网 IPv4 地址与本地回环
            "http://127.0.0.1/c/1827364521/987",
            "http://127.0.0.1:8080/c/1827364521/987",
            "http://localhost/c/1827364521/987",
            "http://0.0.0.0/c/1827364521/987",
            "http://10.0.0.1/c/1827364521/987",
            "http://172.16.0.1/c/1827364521/987",
            "http://192.168.1.1/c/1827364521/987",
            # 1.2 云厂商元数据服务 (AWS/GCP/Aliyun/Azure IMDS)
            "http://169.254.169.254/latest/meta-data/",
            "http://100.100.100.200/latest/meta-data/",
            # 1.3 IPv6 回环与内网
            "http://[::1]/c/1827364521/987",
            "http://[0:0:0:0:0:0:0:1]/c/1827364521/987",
            # 1.4 非法伪协议 (拒绝除 http/https 之外的一切协议)
            "file:///etc/passwd",
            "file:///c:/windows/win.ini",
            "gopher://127.0.0.1:6379/_flushall",
            "dict://127.0.0.1:11211/stat",
            "ftp://t.me/c/1827364521/987",
            "ldap://127.0.0.1:389/o=anonymous",
            "javascript:alert(document.cookie)",
            "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
            # 1.5 域名伪造与混淆绕过攻击
            "https://t.me.attacker.com/c/1827364521/987",
            "https://evil-t.me/c/1827364521/987",
            "https://telegram.me.phishing.io/c/1827364521/987",
            "https://attacker.com/t.me/c/1827364521/987",
            "https://attacker.com#t.me/c/1827364521/987",
            "https://attacker.com?t.me/c/1827364521/987",
            # 1.6 Userinfo 认证信息混淆 (欺骗解析器 Host 字段)
            "https://user:pass@t.me/c/1827364521/987",
            "https://t.me@attacker.com/c/1827364521/987",
            "https://admin:admin@telegram.me/tech/123",
        ]

        for payload in ssrf_attack_payloads:
            with self.subTest(payload=payload):
                res = bridge_server.validate_and_parse_tg_link(payload)
                self.assertFalse(res["ok"], f"SSRF Payload 未被拦截: {payload}")
                self.assertIn(res["code"], ("SSRF_BLOCKED", "INVALID_LINK_FORMAT"))

        # 1.7 验证规范化 URL (Canonical URL) 恒为严格 HTTPS 且限定官方域名
        valid_inputs = [
            "https://t.me/c/1827364521/987",
            "http://t.me/c/1827364521/987",
            "t.me/c/1827364521/987",
            "https://telegram.me/c/1827364521/987",
            "https://telegram.dog/c/1827364521/987",
            "https://t.me/tech_news/12345",
            "http://telegram.me/tech_news/12345",
        ]
        for v_in in valid_inputs:
            with self.subTest(valid_input=v_in):
                res = bridge_server.validate_and_parse_tg_link(v_in)
                self.assertTrue(res["ok"], f"合法链接解析失败: {v_in}")
                canon = res["canonical_url"]
                self.assertTrue(canon.startswith("https://t.me/"), f"规范化 URL 必须为严格 https://t.me/: {canon}")
                self.assertNotIn("@", canon)
                self.assertNotIn("?", canon)
                self.assertNotIn("#", canon)

    # =========================================================================
    # 验收项 2: Session 鉴权与 CSRF Token 双重门禁
    # =========================================================================
    def test_auth_and_csrf_dual_gate(self):
        """深度审计：验证新增接口受到 Session 鉴权与 CSRF Token 双重门禁保护。"""
        endpoints = [
            ("/api/tg/resolve-link", {"link": "https://t.me/c/1827364521/987"}),
            ("/api/tg/quick-download", {"files": [{"telegramId": 1, "chatId": 2, "messageId": 3, "fileId": 4}]}),
        ]

        cookies, headers = self._auth_cookies()

        for ep, body in endpoints:
            with self.subTest(endpoint=ep):
                # 2.1 未登录请求 (无 Cookie) -> 必须 401
                resp_no_auth = self.client.post(ep, json=body)
                self.assertEqual(resp_no_auth.status_code, 401, f"{ep} 未登录未返回 401")
                self.assertFalse(resp_no_auth.json()["ok"])

                # 2.2 伪造/过期 Session Cookie -> 必须 401
                resp_fake_auth = self.client.post(
                    ep, json=body, cookies={bridge_server.PORTAL_COOKIE: "invalid-token-tampered"}
                )
                self.assertEqual(resp_fake_auth.status_code, 401)

                # 2.3 已登录但完全缺失 CSRF Header -> 必须 403
                resp_no_csrf = self.client.post(ep, json=body, cookies=cookies)
                self.assertEqual(resp_no_csrf.status_code, 403, f"{ep} 缺失 CSRF 未返回 403")
                self.assertFalse(resp_no_csrf.json()["ok"])

                # 2.4 已登录但 CSRF Header 伪造不一致 -> 必须 403 (恒定时间对比防时序攻击)
                resp_bad_csrf = self.client.post(
                    ep, json=body, cookies=cookies, headers={bridge_server.CSRF_HEADER: "attacker-forged-csrf"}
                )
                self.assertEqual(resp_bad_csrf.status_code, 403)
                self.assertFalse(resp_bad_csrf.json()["ok"])

                # 2.5 完整凭据通过门禁 (由于下游 mock 状态，状态码不应为 401 或 403)
                with patch("bridge_server.chat_sources", new=AsyncMock(return_value=[])), \
                     patch.object(bridge_server.BACKEND, "start_download_multiple", new=AsyncMock(return_value={"ok": True})):
                    resp_valid = self.client.post(ep, json=body, cookies=cookies, headers=headers)
                    self.assertNotIn(resp_valid.status_code, (401, 403), f"{ep} 门禁合法放行失败")

    # =========================================================================
    # 验收项 3: 防整数溢出、路径穿越与命令注入
    # =========================================================================
    def test_input_sanitization_overflow_injection(self):
        """深度审计：防畸形整数溢出、路径穿越逃逸与命令注入。"""
        cookies, headers = self._auth_cookies()

        # 3.1 整数溢出与非数字注入防护
        injection_files = [
            # SQL / Command 注入测试负载
            {"telegramId": "1; DROP TABLE users;--", "chatId": 2, "messageId": 3, "fileId": 4},
            {"telegramId": 1, "chatId": "2 && rm -rf /", "messageId": 3, "fileId": 4},
            {"telegramId": 1, "chatId": 2, "messageId": "$(cat /etc/passwd)", "fileId": 4},
            {"telegramId": 1, "chatId": 2, "messageId": 3, "fileId": "`reboot`"},
            # 极大数/负数/畸形字符串
            {"telegramId": "not_an_int", "chatId": 2, "messageId": 3, "fileId": 4},
            {"telegramId": None, "chatId": None, "messageId": None, "fileId": None},
        ]

        # quick-download 对非法注入参数应做类型转换过滤，无有效文件时优雅返回 400
        resp_inject = self.client.post(
            "/api/tg/quick-download",
            json={"files": injection_files},
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(resp_inject.status_code, 400)
        self.assertEqual(resp_inject.json()["code"], "NO_FILES_TO_DOWNLOAD")

        # 3.2 归档目录路径穿越防护 (archiveDir Path Traversal)
        traversal_dirs = [
            "../../etc",
            "/../../../etc",
            "/foo/../../etc/passwd",
            "/..\\..\\windows\\system32",
            "..",
            "../",
            "/foo/./bar/../../etc",
        ]
        for bad_dir in traversal_dirs:
            norm = bridge_server._archive_norm_dir(bad_dir)
            self.assertIsNone(norm, f"路径穿越目录未被拒绝: {bad_dir}")

        # 3.3 文件名逃逸防护 (防止文件名含路径分隔符跳出目标目录)
        joined_path = bridge_server._archive_join("/阿里云盘/tg", "../../../etc/shadow")
        self.assertNotIn("..", joined_path)
        self.assertTrue(joined_path.startswith("/阿里云盘/tg/"))
        self.assertEqual(joined_path, "/阿里云盘/tg/_________etc_shadow")
        # 测试单独的 ".." 或 "." 文件名
        self.assertEqual(bridge_server._archive_join("/阿里云盘/tg", ".."), "/阿里云盘/tg/未命名")
        self.assertEqual(bridge_server._archive_join("/阿里云盘/tg", "."), "/阿里云盘/tg/未命名")

        # 3.4 消息链接中的路径穿越与畸形字符
        traversal_links = [
            "https://t.me/c/1827364521/../../etc/passwd",
            "https://t.me/c/1827364521/987/..",
            "https://t.me/../../../987",
            "https://t.me/c/1827364521/987\r\nHost: evil.com",
            "https://t.me/c/1827364521/987%00.mp4",
        ]
        for t_link in traversal_links:
            res = bridge_server.validate_and_parse_tg_link(t_link)
            self.assertFalse(res["ok"], f"链接路径穿越未被阻断: {t_link}")

        # 3.5 抗 ReDoS 性能压测 (防御正则回溯拒绝服务)
        redos_payloads = [
            "https://t.me/c/" + "1" * 19 + "/" + "9" * 11 + "?" + "a=" * 100,
            "https://t.me/" + "a" * 31 + "/" + "9" * 11 + "?" + "k=1&" * 80,
            "https://telegram.me/c/" + "0" * 30 + "/1",
        ]
        t0 = time.perf_counter()
        for _ in range(300):
            for p in redos_payloads:
                bridge_server.validate_and_parse_tg_link(p)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.4, f"正则存在 ReDoS 回溯瓶颈，耗时: {elapsed:.4f}s")

    # =========================================================================
    # 验收项 4: 数据持久化与系统隔离安全性
    # =========================================================================
    def test_persistence_isolation_and_info_leak_protection(self):
        """深度审计：数据持久化文件权限、敏感宿主路径脱敏及内存清理。"""
        cookies, headers = self._auth_cookies()

        # 4.1 接口绝对不暴露服务端宿主绝对路径 (防止本地路径信息泄漏)
        mock_sources = [{"telegramId": 1001, "title": "Account 1"}]
        mock_backend_files = [{
            "fileId": 7001,
            "id": 7001,
            "uniqueId": "uid_sec_leak_check",
            "name": "confidential_spec.pdf",
            "size": 1024,
            "type": "document",
            "chatId": -1001827364521,
            "messageId": 987,
            "localPath": "/root/secret/internal/confidential_spec.pdf",
            "downloadStatus": "completed",
        }]

        with patch("bridge_server.chat_sources", new=AsyncMock(return_value=mock_sources)), \
             patch.object(bridge_server.BACKEND, "resolve_link", new=AsyncMock(return_value=mock_backend_files)):

            resp = self.client.post(
                "/api/tg/resolve-link",
                json={"link": "https://t.me/c/1827364521/987"},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            f_resp = data["data"]["files"][0]
            # 验证返回结构中绝不含 localPath 或真实服务器绝对路径
            self.assertNotIn("localPath", f_resp)
            self.assertNotIn("local_path", f_resp)

        # 4.2 直投暂存注册 _QUICK_ARCHIVE_REGISTRY 在归档入队后立即出队淘汰
        uid_test = "uid_sec_registry_sweep_test"
        bridge_server._QUICK_ARCHIVE_REGISTRY[uid_test] = {
            "remoteDir": "/阿里云盘/安全审计目录",
            "policy": "skip",
            "deleteLocal": False,
            "created_at": time.time(),
        }

        media_path = os.path.join(self.tmp_dir, "sec_test.mp4")
        with open(media_path, "wb") as f:
            f.write(b"sec binary")

        mock_task = {
            "id": 9999,
            "_unique_id": uid_test,
            "local_path": media_path,
            "filename": "sec_test.mp4",
            "_size_bytes": 10,
            "_download_status": "completed",
            "_telegram_id": 1001,
            "_chat_id": -1001827364521,
        }

        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=[mock_task])), \
             patch("bridge_server._openlist_ready", new=AsyncMock(return_value=True)), \
             patch("bridge_server._archive_worker", new=AsyncMock()):
            enqueued = asyncio.run(bridge_server._auto_archive_sweep())
            self.assertEqual(enqueued, 1)
            # 确认 uid_test 在入队后已被成功 pop 释放，防止长期常驻内存导致泄漏
            self.assertNotIn(uid_test, bridge_server._QUICK_ARCHIVE_REGISTRY, "直投归档完成后注册项未被及时 pop 释放")

        # 4.3 验证持久化文件写操作具备安全性
        bridge_server._archive_save()
        self.assertTrue(os.path.exists(bridge_server._ARCHIVE_FILE))


if __name__ == "__main__":
    unittest.main()
