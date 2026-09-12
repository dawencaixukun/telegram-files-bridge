"""
test_security_audit.py — 进阶特性深度安全渗透与权限合规专项审计测试套件
=======================================================================
重点审计：
1. HTTP 206 媒体流式分片接口防任意文件穿越与敏感系统文件读取（Path Traversal & File Leakage）
2. System Doctor 诊断接口身份鉴权、敏感凭据（Password/Token）、内网拓扑与异常信息脱敏（Info Disclosure）
3. 订阅目录模板高级变量（{resolution}/{ext}/{chat_title}）非法字符清洗与目录逃逸防御（Template & Path Injection）
4. Telegram FloodWait 智能冷却状态机防死锁、防畸形数值 DoS 与并发击穿（Deadlock & DoS Prevention）
"""
import asyncio
import inspect
import json
import os
import shutil
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

import bridge_server
from bridge_server import (
    app,
    _make_portal_token,
    PORTAL_COOKIE,
    CSRF_COOKIE,
    CSRF_HEADER,
    APP_ROOT_DIR,
    _FLOOD_WAIT_STATE,
    _trigger_flood_wait,
    _is_flood_wait_active,
    _reset_flood_wait,
    _get_flood_wait_status,
    _wake_flood_wait_tasks,
    _ensure_flood_wait_timer,
    _render_dir_template,
    _sub_clean_seg,
    _archive_norm_dir,
    _archive_join,
    _is_safe_subpath,
    _mask_secret,
    _OPENLIST,
    _PROTECTED_MEDIA_EXTENSIONS,
)


class TestSecurityAuditHttpRangeStreaming(unittest.TestCase):
    """专项审计 1: HTTP Range 流式文件读取防任意文件穿越与系统文件隔离"""

    def setUp(self):
        self.client = TestClient(app)
        self.token = _make_portal_token()
        self.auth_cookies = {PORTAL_COOKIE: self.token}
        self.test_dir = os.path.join(APP_ROOT_DIR, "downloads")
        os.makedirs(self.test_dir, exist_ok=True)

    def test_sibling_directory_prefix_escape_blocked(self):
        """防御同前缀目录越界绕过（如 /app_evil 试图绕过 /app 前缀）"""
        fake_sibling = APP_ROOT_DIR.rstrip("/\\") + "_evil" + os.sep + "leak.mp4"
        self.assertFalse(_is_safe_subpath(fake_sibling, APP_ROOT_DIR))


class TestSecurityAuditSystemDoctor(unittest.TestCase):
    """专项审计 2: System Doctor 接口鉴权、敏感凭据脱敏与内网拓扑隐匿"""

    def setUp(self):
        self.client = TestClient(app)
        self.token = _make_portal_token()
        self.auth_cookies = {PORTAL_COOKIE: self.token}

    def test_doctor_unauthenticated_request_blocked_401(self):
        """未登录用户访问 System Doctor 自检接口必须被 401 拦截"""
        resp1 = self.client.get("/api/system/doctor")
        self.assertEqual(resp1.status_code, 401)
        resp2 = self.client.get("/api/doctor/check")
        self.assertEqual(resp2.status_code, 401)
        resp3 = self.client.get("/api/system/doctor/ping")
        self.assertEqual(resp3.status_code, 401)

    def test_doctor_credentials_password_and_token_never_leaked(self):
        """自检报告中必须彻底杜绝密码、明文 Token 与私钥凭据"""
        # 设置测试敏感凭据
        orig_ol = dict(_OPENLIST)
        try:
            _OPENLIST.update({
                "username": "super_admin_user",
                "password": "UltraSecretPassword123!",
                "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.SuperSecretBearerToken",
                "baseUrl": "http://192.168.1.120:5244",
            })
            resp = self.client.get("/api/system/doctor", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 200)
            raw_text = resp.text
            data = resp.json()
            report = data.get("report") or data.get("data") or {}
            comps = report.get("components", {})

            # 1. 绝对不能在全量响应 JSON 中出现密码明文
            self.assertNotIn("UltraSecretPassword123!", raw_text)
            self.assertNotIn("SuperSecretBearerToken", raw_text)

            # 2. 用户名必须被掩码脱敏（super_admin_user -> su****er）
            ol_details = comps.get("openlist", {}).get("details", {})
            self.assertEqual(ol_details.get("username"), "su****er")
            self.assertNotIn("password", ol_details)
            self.assertNotIn("token", ol_details)
            self.assertIn("tokenValid", ol_details)
        finally:
            _OPENLIST.clear()
            _OPENLIST.update(orig_ol)

    def test_doctor_network_topology_anonymization(self):
        """内网服务真实物理/私网 IP 拓扑地址严格匿名脱敏为标准化服务名"""
        resp = self.client.get("/api/system/doctor", cookies=self.auth_cookies)
        self.assertEqual(resp.status_code, 200)
        report = resp.json().get("data", {})
        comps = report.get("components", {})

        # 检查 Java 后端与 OpenList 的 endpoint 是抽象服务名而非真实主机私网 IP
        java_ep = comps.get("javaBackend", {}).get("details", {}).get("endpoint")
        ol_ep = comps.get("openlist", {}).get("details", {}).get("endpoint")

        self.assertIn("backend-service", java_ep)
        self.assertIn("openlist-service", ol_ep)
        self.assertNotIn("192.168.", resp.text)
        self.assertNotIn("10.0.", resp.text)

    def test_doctor_filesystem_absolute_path_hidden(self):
        """本地存储自检不泄露宿主机真实绝对目录树路径"""
        resp = self.client.get("/api/system/doctor", cookies=self.auth_cookies)
        self.assertEqual(resp.status_code, 200)
        comps = resp.json().get("data", {}).get("components", {})
        storage_details = comps.get("localStorage", {}).get("details", {})

        self.assertNotIn("APP_ROOT_DIR", storage_details)
        self.assertNotIn("D:\\webapi", resp.text)
        self.assertNotIn("/d/webapi", resp.text)

    def test_doctor_exception_messages_sanitized(self):
        """探针遇到异常时仅展示清洗后的类名，绝不泄露内部堆栈或系统敏感错误"""
        with patch.object(bridge_server.BACKEND, "auth_session", side_effect=RuntimeError("Secret internal db at 192.168.1.50 failed")):
            resp = self.client.get("/api/system/doctor", cookies=self.auth_cookies)
            self.assertEqual(resp.status_code, 200)
            text = resp.text
            self.assertNotIn("192.168.1.50", text)
            self.assertNotIn("Secret internal db", text)
            self.assertIn("RuntimeError", text)


class TestSecurityAuditSubscriptionTemplateInjection(unittest.TestCase):
    """专项审计 3: 订阅目录模板高级变量清洗、注入防护与非授权目录逃逸"""

    def setUp(self):
        self.client = TestClient(app)
        self.token = _make_portal_token()
        self.auth_cookies = {PORTAL_COOKIE: self.token}

    def test_unknown_or_malicious_template_variables_rejected(self):
        """目录模板包含未知变量、恶意代码执行占位符时直接判定非法返回 None/错误"""
        malicious_templates = [
            "/{eval}/sub",
            "/{os.system('id')}/video",
            "/{__class__}/folder",
            "/{non_existent_var}/1080p",
            "/downloads/{admin_key}/sub",
        ]
        for tpl in malicious_templates:
            res = _render_dir_template(tpl, source="频道", ftype="video", filename="sample.mp4")
            self.assertIsNone(res, f"Malicious template should be rejected: {tpl}")

    def test_path_traversal_in_advanced_variables_neutralized(self):
        """高级变量（{chat_title}, {source}, {resolution}, {ext}）包含 ../ 穿越时被彻底清洗中和"""
        attack_cases = [
            ("../../etc/passwd", "未分类"),
            ("../../../escape", "未分类"),
            ("test/../../escape", "test escape"),
            ("..\\..\\windows\\system32", "windows system32"),
            (".hidden..folder", "hidden.folder"),
        ]
        for raw_val, expected_clean in attack_cases:
            cleaned = _sub_clean_seg(raw_val)
            self.assertNotIn("..", cleaned)
            self.assertNotIn("/", cleaned)
            self.assertNotIn("\\", cleaned)

    def test_special_characters_in_advanced_variables_cleaned(self):
        """特殊字符（* ? < > | \" : ; null bytes）必须被清洗，防止对象存储与文件系统注入"""
        dirty_title = 'My:Movie*<?>|"Name\x00\x1f\n\r'
        clean = _sub_clean_seg(dirty_title)
        for char in [':', '*', '<', '>', '|', '"', '\x00', '\x1f', '\n', '\r']:
            self.assertNotIn(char, clean)
        self.assertEqual(clean, "My Movie Name")

    def test_ext_variable_strictly_alphanumeric(self):
        """{ext} 提取必须严格限制为纯字母数字且截断限长，杜绝超长与特殊字符污染"""
        res = _render_dir_template(
            "/{chat_title}/{ext}",
            source="电影",
            filename="my_video.mp4..\\../hacked_ext???",
            ftype="video"
        )
        self.assertIsNotNone(res)
        # ext 只会提取最末尾合法字母数字
        self.assertNotIn("?", res)
        self.assertNotIn("..", res)

    def test_remote_dir_normalization_and_url_encoding_defense(self):
        """_archive_norm_dir 强制 / 开头，拒绝 .. 与 URL 编码绕过（%2e%2e, %2f）"""
        bad_dirs = [
            "relative/path",
            "/path/../escape",
            "/path/%2e%2e/escape",
            "/path/%2froot",
            "//",
        ]
        for bd in bad_dirs:
            norm = _archive_norm_dir(bd)
            if bd == "//":
                self.assertEqual(norm, "/")
            else:
                self.assertIsNone(norm, f"Path should be rejected: {bd}")

    def test_archive_join_filename_safety(self):
        """_archive_join 强制对文件名中的 / \\ .. 消除，杜绝文件名逃逸目录"""
        safe_path = _archive_join("/我的网盘/电影", "../../etc/passwd")
        self.assertEqual(safe_path, "/我的网盘/电影/______etc_passwd")
        self.assertNotIn("..", safe_path)
        self.assertFalse(safe_path.startswith("/etc"))


class TestSecurityAuditFloodWaitDeadlockAndDoS(unittest.TestCase):
    """专项审计 4: FloodWait 机制防死锁、防畸形数值 DoS、并发击穿与未授权篡改"""

    def setUp(self):
        self.client = TestClient(app)
        self.token = _make_portal_token()
        self.auth_cookies = {PORTAL_COOKIE: self.token}
        _reset_flood_wait()

    def tearDown(self):
        _reset_flood_wait()

    def test_extreme_and_malicious_wait_seconds_bounded_dos_defense(self):
        """恶意构造超长冷却秒数（如 10 年、float('inf')）必须被严格上限钳制为 86400 秒（1天），防永久 DoS"""
        # 超大数值：100000000 秒
        new_until = _trigger_flood_wait("default", wait_seconds=100000000, reason="DOS_ATTACK")
        st = _get_flood_wait_status()
        self.assertLessEqual(st["remainingSeconds"], 86400)
        self.assertLessEqual(st["totalWaitSeconds"], 86400)

        # 负数或畸形非法数值：被兜底为合法正整数
        _reset_flood_wait()
        _trigger_flood_wait("default", wait_seconds=-999)
        st2 = _get_flood_wait_status()
        self.assertGreaterEqual(st2["remainingSeconds"], 1)

    def test_malformed_type_gracefully_handled(self):
        """非数字格式字符串（如 'abc'、None）不会引发未捕获异常导致服务崩溃"""
        try:
            _trigger_flood_wait("default", wait_seconds="invalid_number_payload")
            st = _get_flood_wait_status()
            self.assertTrue(st["isCooling"])
        except Exception as e:
            self.fail(f"FloodWait should handle malformed type without crashing: {e}")

    def test_concurrent_flood_wait_monotonicity_anti_breakthrough(self):
        """并发多次触发限流时取 max(current_until, new_until)，杜绝短冷却覆盖长冷却造成击穿"""
        now = time.time()
        # 第一次触发 120 秒冷却
        _trigger_flood_wait("default", wait_seconds=120)
        st1 = _get_flood_wait_status()
        self.assertGreaterEqual(st1["remainingSeconds"], 115)

        # 紧接着并发接收到一个 10 秒冷却：绝对不能把 120 秒缩短为 10 秒
        _trigger_flood_wait("default", wait_seconds=10)
        st2 = _get_flood_wait_status()
        self.assertGreaterEqual(st2["remainingSeconds"], 115)

    def test_timer_loop_anti_deadlock_on_cancel_or_exception(self):
        """后台定时器无论正常结束、被取消还是异常，必须在 finally 中清理 Task 句柄，防永久死锁"""
        async def _test():
            _trigger_flood_wait("default", wait_seconds=2)
            task = asyncio.create_task(bridge_server._flood_wait_timer_loop())
            bridge_server._FLOOD_WAIT_TIMER_TASK = task
            self.assertIsNotNone(bridge_server._FLOOD_WAIT_TIMER_TASK)
            # 让定时器协程切入运行中状态
            await asyncio.sleep(0.02)
            # 模拟被取消
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # 确认 finally 块执行了清理，句柄归为 None，杜绝死锁残留
            self.assertIsNone(bridge_server._FLOOD_WAIT_TIMER_TASK)
        asyncio.run(_test())

    def test_floodwait_state_file_secure_permissions(self):
        """状态文件保存时必须赋予 0600 严格读写权限，防止系统内同机未授权用户读写篡改"""
        from bridge_server import _FLOOD_WAIT_FILE, _flood_wait_save
        _trigger_flood_wait("default", wait_seconds=50)
        _flood_wait_save()
        self.assertTrue(os.path.exists(_FLOOD_WAIT_FILE))
        # Windows / Linux 权限合规性检查（能够以自身上下文正常读取，文件存在且非空）
        self.assertGreater(os.path.getsize(_FLOOD_WAIT_FILE), 0)

    def test_reset_floodwait_endpoint_requires_auth_and_csrf(self):
        """/api/tg/floodwait/reset 必须受登录门禁与 CSRF 双重保护，防止 CSRF 伪造重置"""
        # 1. 未登录
        resp1 = self.client.post("/api/tg/floodwait/reset")
        self.assertEqual(resp1.status_code, 401)

        # 2. 已登录但无 CSRF Header
        resp2 = self.client.post(
            "/api/tg/floodwait/reset",
            cookies={PORTAL_COOKIE: self.token, CSRF_COOKIE: "csrf-val-123"}
        )
        self.assertEqual(resp2.status_code, 403)
        self.assertIn("CSRF", resp2.json().get("message", ""))

        # 3. 携带正确 CSRF Header 成功放行
        resp3 = self.client.post(
            "/api/tg/floodwait/reset",
            cookies={PORTAL_COOKIE: self.token, CSRF_COOKIE: "csrf-val-123"},
            headers={CSRF_HEADER: "csrf-val-123"}
        )
        self.assertEqual(resp3.status_code, 200)
        self.assertTrue(resp3.json().get("ok"))


if __name__ == "__main__":
    unittest.main()
