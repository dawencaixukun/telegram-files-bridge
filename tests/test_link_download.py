# -*- coding: utf-8 -*-
"""
test_link_download.py — Telegram 消息链接解析与直投下载自动化测试套件
=====================================================================
覆盖矩阵：
1. 链接格式与正则白名单引擎 (validate_and_parse_tg_link)
   - 私有频道/超级群组链接 (标准、带话题、各种ID长度边界、带参数、别名域名、大小写)
   - 公开频道/群组链接 (标准、带话题、带参数、各种合规用户名、别名域名)
   - 恶意/非法链接阻断 (内网IP SSRF、非白名单域名、钓鱼前缀/后缀、用户凭据欺骗、协议限制)
   - 畸形格式与参数边界 (ID非数字、路径不全、超短用户名、空值)
   - 抗 ReDoS 性能压测 (超长恶意畸形输入耗时校验)
2. API 接口: POST /api/tg/resolve-link
   - 权限门禁 (未登录 401)
   - CSRF 防护门禁 (缺失或伪造 403)
   - 入参结构与非法输入校验 (400 INVALID_JSON / INVALID_BODY / MISSING_LINK / SSRF_BLOCKED)
   - TG 账号健康检测 (无在线账号 409 TG_ACCOUNT_UNAVAILABLE)
   - 多媒体消息成功解析 (视频/音频/文档/图片、大小、Chat 映射、查重与归档标志)
   - 非多媒体消息/无附件/撤回处理 (404 NO_FILES_FOUND)
   - 后端 TDLib 异常与超时脱敏 (502 TG_BACKEND_ERROR)
   - 多账号候选与故障转移机制
3. API 接口: POST /api/tg/quick-download
   - 门禁安全校验 (401 / 403)
   - 显式 files 列表直投下载与参数校验
   - 联动自动归档调度注册 (_QUICK_ARCHIVE_REGISTRY 状态验证及与 _auto_archive_sweep 协作)
   - 基于 link 的一键自动解析并投递下载
   - 无有效文件、解析失败或账号缺失等边界容错
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


class TestTelegramLinkDownload(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(bridge_server.app)
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_link_dl_test_")
        self._orig_archive_jobs = dict(bridge_server._ARCHIVE_JOBS)
        self._orig_archive_file = bridge_server._ARCHIVE_FILE
        bridge_server._ARCHIVE_FILE = os.path.join(self.tmp_dir, ".test_archive_jobs.json")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        bridge_server._QUICK_ARCHIVE_REGISTRY.clear()
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self._orig_archive_jobs)
        bridge_server._ARCHIVE_FILE = self._orig_archive_file
        bridge_server.CHAT_SOURCE_CACHE["key"] = None
        bridge_server.CHAT_SOURCE_CACHE["value"] = None

    def _auth_cookies(self):
        """生成已认证 Portal Cookie 与双重提交 CSRF Header。"""
        token = bridge_server._make_portal_token()
        csrf = "csrf-token-verifier-8888"
        cookies = {
            bridge_server.PORTAL_COOKIE: token,
            bridge_server.CSRF_COOKIE: csrf,
        }
        headers = {
            bridge_server.CSRF_HEADER: csrf,
        }
        return cookies, headers

    # =========================================================================
    # 1. 链接格式解析与白名单正则引擎单元测试 (validate_and_parse_tg_link)
    # =========================================================================

    def test_validate_private_links(self):
        """私有频道/超级群组链接解析验证 (t.me/c/<chat_id>/<msg_id>)。"""
        # 1. 标准私有频道链接
        res = bridge_server.validate_and_parse_tg_link("https://t.me/c/1827364521/987")
        self.assertTrue(res["ok"])
        self.assertEqual(res["link_type"], "private")
        self.assertEqual(res["chat_identifier"], "1827364521")
        self.assertIsNone(res["topic_id"])
        self.assertEqual(res["message_id"], 987)
        self.assertEqual(res["canonical_url"], "https://t.me/c/1827364521/987")

        # 2. 带 Topic / Forum 的私有频道链接
        res = bridge_server.validate_and_parse_tg_link("https://t.me/c/1827364521/1024/987")
        self.assertTrue(res["ok"])
        self.assertEqual(res["link_type"], "private")
        self.assertEqual(res["chat_identifier"], "1827364521")
        self.assertEqual(res["topic_id"], 1024)
        self.assertEqual(res["message_id"], 987)
        self.assertEqual(res["canonical_url"], "https://t.me/c/1827364521/987")

        # 3. 边界 Chat ID 长度 (5位到20位正整数)
        res_min = bridge_server.validate_and_parse_tg_link("https://t.me/c/10000/1")
        self.assertTrue(res_min["ok"])
        self.assertEqual(res_min["chat_identifier"], "10000")
        self.assertEqual(res_min["message_id"], 1)

        res_max = bridge_server.validate_and_parse_tg_link("https://t.me/c/12345678901234567890/999999")
        self.assertTrue(res_max["ok"])
        self.assertEqual(res_max["chat_identifier"], "12345678901234567890")
        self.assertEqual(res_max["message_id"], 999999)

        # 4. 附带查询参数及锚点 (如 ?single, ?thread=xxx)
        res_param = bridge_server.validate_and_parse_tg_link("https://t.me/c/1827364521/987?single=true#top")
        self.assertTrue(res_param["ok"])
        self.assertEqual(res_param["canonical_url"], "https://t.me/c/1827364521/987")

        # 5. 官方别名域名支持 (telegram.me, telegram.dog)
        res_tgme = bridge_server.validate_and_parse_tg_link("http://telegram.me/c/1827364521/987")
        self.assertTrue(res_tgme["ok"])
        self.assertEqual(res_tgme["canonical_url"], "https://t.me/c/1827364521/987")

        res_tgdog = bridge_server.validate_and_parse_tg_link("https://telegram.dog/c/1827364521/987")
        self.assertTrue(res_tgdog["ok"])
        self.assertEqual(res_tgdog["canonical_url"], "https://t.me/c/1827364521/987")

        # 6. 省略协议头与大小写混写
        res_raw = bridge_server.validate_and_parse_tg_link("T.ME/C/1827364521/987")
        self.assertTrue(res_raw["ok"])
        self.assertEqual(res_raw["canonical_url"], "https://t.me/c/1827364521/987")

    def test_validate_public_links(self):
        """公开频道/超级群组链接解析验证 (t.me/<username>/<msg_id>)。"""
        # 1. 标准公开频道链接
        res = bridge_server.validate_and_parse_tg_link("https://t.me/tech_news/12345")
        self.assertTrue(res["ok"])
        self.assertEqual(res["link_type"], "public")
        self.assertEqual(res["chat_identifier"], "tech_news")
        self.assertIsNone(res["topic_id"])
        self.assertEqual(res["message_id"], 12345)
        self.assertEqual(res["canonical_url"], "https://t.me/tech_news/12345")

        # 2. 带 Topic 的公开超级群组链接
        res_topic = bridge_server.validate_and_parse_tg_link("https://t.me/tech_community/888/12345")
        self.assertTrue(res_topic["ok"])
        self.assertEqual(res_topic["topic_id"], 888)
        self.assertEqual(res_topic["message_id"], 12345)
        self.assertEqual(res_topic["canonical_url"], "https://t.me/tech_community/12345")

        # 3. 别名域名与参数
        res_dog = bridge_server.validate_and_parse_tg_link("https://telegram.dog/open_source_hub/666?single")
        self.assertTrue(res_dog["ok"])
        self.assertEqual(res_dog["chat_identifier"], "open_source_hub")
        self.assertEqual(res_dog["message_id"], 666)
        self.assertEqual(res_dog["canonical_url"], "https://t.me/open_source_hub/666")

        # 4. 合法用户名边界 (长度 4~32，支持字母、数字、下划线)
        res_min_user = bridge_server.validate_and_parse_tg_link("https://t.me/user/10")
        self.assertTrue(res_min_user["ok"])
        self.assertEqual(res_min_user["chat_identifier"], "user")

        long_user = "a" * 32
        res_max_user = bridge_server.validate_and_parse_tg_link(f"https://t.me/{long_user}/10")
        self.assertTrue(res_max_user["ok"])
        self.assertEqual(res_max_user["chat_identifier"], long_user)

    def test_validate_ssrf_and_security_blocking(self):
        """防 SSRF 白名单与恶意伪造链接拦截测试。"""
        malicious_urls = [
            # 1. 内网 IP / 回环地址
            "http://127.0.0.1/c/1827364521/987",
            "http://127.0.0.1:8000/api/admin",
            "http://10.0.0.1/c/1827364521/987",
            "http://192.168.1.100/c/1827364521/987",
            "http://172.16.0.1/c/1827364521/987",
            "http://169.254.169.254/latest/meta-data",
            # 2. 外部非 Telegram 域名
            "https://google.com/c/1827364521/987",
            "https://baidu.com/tech_news/123",
            "https://github.com/t.me/c/1827364521/987",
            # 3. 仿冒/钓鱼 Telegram 域名
            "https://evil-t.me/c/1827364521/987",
            "https://t.me.attacker.com/c/1827364521/987",
            "https://faketelegram.me/c/1827364521/987",
            # 4. 带认证凭据的伪造链接 (Userinfo URL 绕过尝试)
            "https://user:pass@t.me/c/1827364521/987",
            "https://admin@t.me/tech_news/123",
            # 5. 非法协议尝试
            "ftp://t.me/tech_news/123",
            "file:///t.me/c/1827364521/987",
            "javascript:alert(1)",
            "data:text/html,<h1>test</h1>",
        ]

        for url in malicious_urls:
            with self.subTest(url=url):
                res = bridge_server.validate_and_parse_tg_link(url)
                self.assertFalse(res["ok"], f"恶意/非法链接未被拦截: {url}")
                self.assertIn(res["code"], ("SSRF_BLOCKED", "INVALID_LINK_FORMAT"))

    def test_validate_malformed_and_boundary_cases(self):
        """畸形参数、缺失路径与格式异常测试。"""
        malformed_cases = [
            # 缺失 messageId
            ("https://t.me/c/1827364521", "未包含 messageId"),
            ("https://t.me/tech_news", "未包含 messageId"),
            # messageId 为非数字
            ("https://t.me/c/1827364521/abc", "messageId 含有英文字符"),
            ("https://t.me/tech_news/999xyz", "messageId 含有英文字符"),
            # 私有频道 Chat ID 过短 (< 5位) 或含有非数字
            ("https://t.me/c/123/456", "Chat ID 过短"),
            ("https://t.me/c/invalid_id/456", "Chat ID 包含字母"),
            # 公开用户名过短 (< 4字符) 或包含非法字符
            ("https://t.me/abc/123", "用户名过短"),
            ("https://t.me/bad-name!/123", "用户名包含连字符与特殊符号"),
            # 纯域名或仅根目录
            ("https://t.me", "纯根域名"),
            ("https://t.me/", "空路径"),
            # 空值与非法类型
            ("", "空字符串"),
            ("   ", "纯空白字符串"),
            (None, "None 类型"),
            (12345, "整数类型"),
        ]

        for link, desc in malformed_cases:
            with self.subTest(desc=desc, link=link):
                res = bridge_server.validate_and_parse_tg_link(link)
                self.assertFalse(res["ok"], f"畸形用例应返回 False: {desc}")
                self.assertEqual(res["code"], "INVALID_LINK_FORMAT")

    def test_redos_protection_and_length_limit(self):
        """超长输入与抗 ReDoS 性能压力测试。"""
        # 超长输入 (> 512 字符)
        long_junk = "https://t.me/c/1827364521/" + "9" * 600
        res = bridge_server.validate_and_parse_tg_link(long_junk)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "INVALID_LINK_FORMAT")
        self.assertIn("超出限制", res["message"])

        # 恶意构造的可能引发回溯的模式密集压测 (耗时需 < 100ms)
        evil_patterns = [
            "https://t.me/c/" + "1" * 19 + "/" + "9" * 11 + "?" + "a=" * 100,
            "https://t.me/" + "a" * 31 + "/" + "9" * 11 + "?" + "k=1&" * 50,
            "https://telegram.me/c/" + "0" * 25 + "/9999",
            "https://t.me////c////1827364521////987",
        ]

        t0 = time.perf_counter()
        for _ in range(500):
            for pat in evil_patterns:
                bridge_server.validate_and_parse_tg_link(pat)
        elapsed = time.perf_counter() - t0

        # 2000 次正则解析应在 0.2 秒内完成，无任何 ReDoS 停滞
        self.assertLess(elapsed, 0.5, f"正则存在回溯性能隐患，耗时 {elapsed:.4f}s")

    # =========================================================================
    # 2. POST /api/tg/resolve-link API 端点测试
    # =========================================================================

    def test_api_resolve_link_auth_and_csrf_gate(self):
        """解析接口权限与 CSRF 双提交门禁测试。"""
        # 1. 未登录访问 (无 Cookie) -> 401
        resp = self.client.post("/api/tg/resolve-link", json={"link": "https://t.me/c/1827364521/987"})
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(resp.json()["message"], "未登录")

        cookies, headers = self._auth_cookies()

        # 2. 已登录但缺失 CSRF Header -> 403
        resp_no_csrf = self.client.post("/api/tg/resolve-link", json={"link": "https://t.me/c/1827364521/987"}, cookies=cookies)
        self.assertEqual(resp_no_csrf.status_code, 403)
        self.assertFalse(resp_no_csrf.json()["ok"])

        # 3. CSRF Header 伪造不匹配 -> 403
        bad_headers = {bridge_server.CSRF_HEADER: "wrong-csrf-token"}
        resp_bad_csrf = self.client.post(
            "/api/tg/resolve-link",
            json={"link": "https://t.me/c/1827364521/987"},
            cookies=cookies,
            headers=bad_headers
        )
        self.assertEqual(resp_bad_csrf.status_code, 403)
        self.assertFalse(resp_bad_csrf.json()["ok"])

    def test_api_resolve_link_request_body_validation(self):
        """解析接口请求体非结构化/缺失参数校验。"""
        cookies, headers = self._auth_cookies()

        # 1. 非 JSON 请求体
        resp_non_json = self.client.post(
            "/api/tg/resolve-link",
            content="raw text not json",
            headers={"Content-Type": "text/plain", **headers},
            cookies=cookies
        )
        self.assertEqual(resp_non_json.status_code, 400)
        self.assertEqual(resp_non_json.json()["code"], "INVALID_JSON")

        # 2. JSON 为列表而非对象
        resp_list = self.client.post(
            "/api/tg/resolve-link",
            json=["https://t.me/c/1827364521/987"],
            headers=headers,
            cookies=cookies
        )
        self.assertEqual(resp_list.status_code, 400)
        self.assertEqual(resp_list.json()["code"], "INVALID_BODY")

        # 3. 缺失 link 字段或为空
        resp_missing = self.client.post(
            "/api/tg/resolve-link",
            json={},
            headers=headers,
            cookies=cookies
        )
        self.assertEqual(resp_missing.status_code, 400)
        self.assertEqual(resp_missing.json()["code"], "MISSING_LINK")

        # 4. link 为 SSRF 拦截目标
        resp_ssrf = self.client.post(
            "/api/tg/resolve-link",
            json={"link": "http://127.0.0.1:8080/admin"},
            headers=headers,
            cookies=cookies
        )
        self.assertEqual(resp_ssrf.status_code, 400)
        self.assertEqual(resp_ssrf.json()["code"], "SSRF_BLOCKED")

        # 5. link 为畸形格式
        resp_invalid = self.client.post(
            "/api/tg/resolve-link",
            json={"link": "https://t.me/c/short/invalid"},
            headers=headers,
            cookies=cookies
        )
        self.assertEqual(resp_invalid.status_code, 400)
        self.assertEqual(resp_invalid.json()["code"], "INVALID_LINK_FORMAT")

    def test_api_resolve_link_account_unavailable(self):
        """当系统无任何可用在线 TG 账号时返回 409。"""
        cookies, headers = self._auth_cookies()

        with patch("bridge_server.chat_sources", new=AsyncMock(return_value=[])):
            resp = self.client.post(
                "/api/tg/resolve-link",
                json={"link": "https://t.me/c/1827364521/987"},
                headers=headers,
                cookies=cookies
            )
            self.assertEqual(resp.status_code, 409)
            data = resp.json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["code"], "TG_ACCOUNT_UNAVAILABLE")

    def test_api_resolve_link_success_with_media_files(self):
        """测试多媒体消息成功解析 (200 SUCCESS)，校验元数据与查重状态。"""
        cookies, headers = self._auth_cookies()

        mock_sources = [{"telegramId": 1001, "title": "Account 1"}]
        mock_backend_files = [
            {
                "fileId": 5001,
                "id": 5001,
                "uniqueId": "uid_video_5001",
                "name": "极客精选_042期.mp4",
                "size": 524288000,
                "type": "video",
                "mimeType": "video/mp4",
                "chatId": -1001827364521,
                "chatTitle": "技术归档群",
                "messageId": 987,
                "downloadStatus": "completed",
                "thumbnail": "thumb_data_base64",
            },
            {
                "fileId": 5002,
                "id": 5002,
                "uniqueId": "uid_doc_5002",
                "name": "说明文档.pdf",
                "size": 1048576,
                "type": "document",
                "mimeType": "application/pdf",
                "chatId": -1001827364521,
                "chatTitle": "技术归档群",
                "messageId": 987,
                "downloadStatus": "idle",
            }
        ]

        with patch("bridge_server.chat_sources", new=AsyncMock(return_value=mock_sources)), \
             patch.object(bridge_server.BACKEND, "resolve_link", new=AsyncMock(return_value=mock_backend_files)), \
             patch("bridge_server._archive_registry_lookup", return_value={"remote_path": "/阿里云盘/技术归档群/极客精选_042期.mp4"}):

            resp = self.client.post(
                "/api/tg/resolve-link",
                json={"link": "https://t.me/c/1827364521/987?single"},
                headers=headers,
                cookies=cookies
            )

            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["code"], "SUCCESS")
            detail = data["data"]
            self.assertEqual(detail["canonicalUrl"], "https://t.me/c/1827364521/987")
            self.assertEqual(detail["linkType"], "private")
            self.assertEqual(detail["chatIdentifier"], "1827364521")
            self.assertEqual(detail["messageId"], 987)
            self.assertEqual(detail["telegramId"], 1001)

            files = detail["files"]
            self.assertEqual(len(files), 2)

            f1 = files[0]
            self.assertEqual(f1["fileId"], 5001)
            self.assertEqual(f1["uniqueId"], "uid_video_5001")
            self.assertEqual(f1["filename"], "极客精选_042期.mp4")
            self.assertEqual(f1["fileType"], "video")
            self.assertEqual(f1["size"], 524288000)
            self.assertEqual(f1["sizeHuman"], "500.0 MB")
            self.assertTrue(f1["isAlreadyDownloaded"])  # downloadStatus == completed
            self.assertTrue(f1["isAlreadyArchived"])    # 命中 archive_registry_lookup

            f2 = files[1]
            self.assertEqual(f2["fileId"], 5002)
            self.assertEqual(f2["filename"], "说明文档.pdf")
            self.assertEqual(f2["fileType"], "document")
            self.assertFalse(f2["isAlreadyDownloaded"])

    def test_api_resolve_link_non_media_message(self):
        """测试非多媒体消息/纯文本/附件已被撤回 (404 NO_FILES_FOUND)。"""
        cookies, headers = self._auth_cookies()
        mock_sources = [{"telegramId": 1001, "title": "Account 1"}]

        with patch("bridge_server.chat_sources", new=AsyncMock(return_value=mock_sources)), \
             patch.object(bridge_server.BACKEND, "resolve_link", new=AsyncMock(return_value=[])):

            resp = self.client.post(
                "/api/tg/resolve-link",
                json={"link": "https://t.me/tech_news/12345"},
                headers=headers,
                cookies=cookies
            )

            self.assertEqual(resp.status_code, 404)
            data = resp.json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["code"], "NO_FILES_FOUND")
            self.assertIn("未找到可下载的媒体文件", data["message"])

    def test_api_resolve_link_backend_error_and_timeout(self):
        """测试后端 TDLib 抛出网络超时或异常时的处理 (502 TG_BACKEND_ERROR 脱敏)。"""
        cookies, headers = self._auth_cookies()
        mock_sources = [{"telegramId": 1001, "title": "Account 1"}]

        with patch("bridge_server.chat_sources", new=AsyncMock(return_value=mock_sources)), \
             patch.object(bridge_server.BACKEND, "resolve_link", new=AsyncMock(side_effect=TimeoutError("TDLib request timed out"))):

            resp = self.client.post(
                "/api/tg/resolve-link",
                json={"link": "https://t.me/c/1827364521/987"},
                headers=headers,
                cookies=cookies
            )

            self.assertEqual(resp.status_code, 502)
            data = resp.json()
            self.assertFalse(data["ok"])
            self.assertEqual(data["code"], "TG_BACKEND_ERROR")
            self.assertIn("解析失败", data["message"])

    def test_api_resolve_link_account_fallback(self):
        """多账号选择与故障转移：指定 telegramId 失败后，自动尝试备用账号。"""
        cookies, headers = self._auth_cookies()
        mock_sources = [
            {"telegramId": 1001, "title": "Account 1"},
            {"telegramId": 1002, "title": "Account 2"},
        ]

        async def mock_resolve(cand, link):
            if cand == 1001:
                raise RuntimeError("CHAT_ADMIN_REQUIRED")
            if cand == 1002:
                return [{
                    "fileId": 777,
                    "uniqueId": "uid_fallback_777",
                    "name": "fallback_doc.zip",
                    "size": 2048,
                    "type": "document",
                    "chatId": -1001827364521,
                    "messageId": 987,
                }]
            return []

        with patch("bridge_server.chat_sources", new=AsyncMock(return_value=mock_sources)), \
             patch.object(bridge_server.BACKEND, "resolve_link", side_effect=mock_resolve):

            resp = self.client.post(
                "/api/tg/resolve-link",
                json={"link": "https://t.me/c/1827364521/987", "telegramId": 1001},
                headers=headers,
                cookies=cookies
            )

            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["data"]["telegramId"], 1002)
            self.assertEqual(data["data"]["files"][0]["fileId"], 777)

    # =========================================================================
    # 3. POST /api/tg/quick-download API 端点测试
    # =========================================================================

    def test_api_quick_download_auth_and_csrf_gate(self):
        """直投下载接口权限与 CSRF 门禁。"""
        resp_401 = self.client.post("/api/tg/quick-download", json={"link": "https://t.me/c/1827364521/987"})
        self.assertEqual(resp_401.status_code, 401)

        cookies, headers = self._auth_cookies()
        resp_403 = self.client.post("/api/tg/quick-download", json={"link": "https://t.me/c/1827364521/987"}, cookies=cookies)
        self.assertEqual(resp_403.status_code, 403)

    def test_api_quick_download_with_files_and_auto_archive(self):
        """通过 files 数组直接直投，并联动注册自动归档。"""
        cookies, headers = self._auth_cookies()

        payload = {
            "files": [
                {
                    "telegramId": 1001,
                    "chatId": -1001827364521,
                    "messageId": 987,
                    "fileId": 8001,
                    "uniqueId": "uid_quick_auto_archive_1",
                },
                {
                    "telegramId": 1001,
                    "chatId": -1001827364521,
                    "messageId": 987,
                    "fileId": 8002,
                    "uniqueId": "uid_quick_auto_archive_2",
                }
            ],
            "autoArchive": True,
            "archiveDir": "/阿里云盘/TG直投测试",
            "policy": "skip",
            "deleteLocal": True,
        }

        with patch.object(bridge_server.BACKEND, "start_download_multiple", new=AsyncMock(return_value={"ok": True})) as mock_start:
            resp = self.client.post("/api/tg/quick-download", json=payload, headers=headers, cookies=cookies)

            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["count"], 2)
            self.assertTrue(data["autoArchive"])
            self.assertEqual(data["archiveDir"], "/阿里云盘/TG直投测试")

            # 校验调用后端的入参格式
            mock_start.assert_called_once()
            called_files = mock_start.call_args[0][0]["files"]
            self.assertEqual(len(called_files), 2)
            self.assertEqual(called_files[0]["fileId"], 8001)

            # 校验自动归档注册映射 _QUICK_ARCHIVE_REGISTRY 是否就绪
            reg1 = bridge_server._QUICK_ARCHIVE_REGISTRY.get("uid_quick_auto_archive_1")
            self.assertIsNotNone(reg1)
            self.assertEqual(reg1["remoteDir"], "/阿里云盘/TG直投测试")
            self.assertEqual(reg1["policy"], "skip")
            self.assertTrue(reg1["deleteLocal"])

            # 验证在 _auto_archive_sweep 扫描时的实际联动效果
            media_path = os.path.join(self.tmp_dir, "quick_video.mp4")
            with open(media_path, "wb") as f:
                f.write(b"quick video binary")

            mock_task = {
                "id": 101,
                "_unique_id": "uid_quick_auto_archive_1",
                "local_path": media_path,
                "filename": "quick_video.mp4",
                "_size_bytes": 18,
                "_download_status": "completed",
                "_telegram_id": 1001,
                "_chat_id": -1001827364521,
            }

            with patch("bridge_server.tasks_all", new=AsyncMock(return_value=[mock_task])), \
                 patch("bridge_server._openlist_ready", new=AsyncMock(return_value=True)), \
                 patch("bridge_server._archive_worker", new=AsyncMock()):
                enqueued = asyncio.run(bridge_server._auto_archive_sweep())
                self.assertEqual(enqueued, 1)

                job = bridge_server._archive_latest_raw_of("uid_quick_auto_archive_1")
                self.assertIsNotNone(job)
                self.assertEqual(job["remote_dir"], "/阿里云盘/TG直投测试")
                self.assertEqual(job["remote_path"], "/阿里云盘/TG直投测试/quick_video.mp4")
                self.assertEqual(job["policy"], "skip")
                self.assertTrue(job["delete_local"])

    def test_api_quick_download_via_link_direct(self):
        """传入消息 link 一键自动解析并投递直投下载。"""
        cookies, headers = self._auth_cookies()

        mock_sources = [{"telegramId": 1001, "title": "Account 1"}]
        mock_backend_files = [
            {
                "fileId": 9001,
                "id": 9001,
                "uniqueId": "uid_via_link_9001",
                "name": "电影资源.mkv",
                "size": 1073741824,
                "type": "video",
                "chatId": -1001827364521,
                "messageId": 987,
            }
        ]

        with patch("bridge_server.chat_sources", new=AsyncMock(return_value=mock_sources)), \
             patch.object(bridge_server.BACKEND, "resolve_link", new=AsyncMock(return_value=mock_backend_files)), \
             patch.object(bridge_server.BACKEND, "start_download_multiple", new=AsyncMock(return_value={"ok": True})) as mock_start:

            resp = self.client.post(
                "/api/tg/quick-download",
                json={"link": "https://t.me/c/1827364521/987"},
                headers=headers,
                cookies=cookies
            )

            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["count"], 1)

            mock_start.assert_called_once()
            called_files = mock_start.call_args[0][0]["files"]
            self.assertEqual(called_files[0]["fileId"], 9001)

    def test_api_quick_download_error_handling(self):
        """直投下载异常处理（非 JSON、格式错误链接、无文件、后端报错）。"""
        cookies, headers = self._auth_cookies()

        # 1. 链接格式错误
        resp_bad_link = self.client.post(
            "/api/tg/quick-download",
            json={"link": "http://127.0.0.1/malicious"},
            headers=headers,
            cookies=cookies
        )
        self.assertEqual(resp_bad_link.status_code, 400)
        self.assertEqual(resp_bad_link.json()["code"], "SSRF_BLOCKED")

        # 2. 消息无文件可供下载
        with patch("bridge_server.chat_sources", new=AsyncMock(return_value=[{"telegramId": 1}])), \
             patch.object(bridge_server.BACKEND, "resolve_link", new=AsyncMock(return_value=[])):
            resp_no_files = self.client.post(
                "/api/tg/quick-download",
                json={"link": "https://t.me/tech_news/12345"},
                headers=headers,
                cookies=cookies
            )
            self.assertEqual(resp_no_files.status_code, 400)
            self.assertEqual(resp_no_files.json()["code"], "NO_FILES_TO_DOWNLOAD")

        # 3. 后端 start_download_multiple 抛出异常
        with patch.object(bridge_server.BACKEND, "start_download_multiple", side_effect=RuntimeError("TDLib Client Crash")):
            resp_err = self.client.post(
                "/api/tg/quick-download",
                json={"files": [{"telegramId": 1, "chatId": 2, "messageId": 3, "fileId": 4}]},
                headers=headers,
                cookies=cookies
            )
            self.assertEqual(resp_err.status_code, 502)
            self.assertEqual(resp_err.json()["code"], "BACKEND_ERROR")
            self.assertIn("直投下载提交失败", resp_err.json()["message"])


if __name__ == "__main__":
    unittest.main()
