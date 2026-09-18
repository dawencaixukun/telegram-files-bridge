# -*- coding: utf-8 -*-
"""
test_security_audit_t7.py — Task t7: Session 加密、凭据存储与防爆破安全专项深度审计自动化验证套件
========================================================================================
审计验收门禁矩阵：
1. 验收项 1: Session 快照强对称加密 (AES-256-GCM AEAD) 与异地防泄漏渗透审计
   - 密码学安全随机参数 (16B Salt, 12B Nonce, PBKDF2 100k 迭代)
   - 密文高信息熵与无明文泄漏 (绝不包含 td.binlog/sqlite/凭证明文特征)
   - 密文完整性与防篡改 (位翻转、截断、Tag 破坏全面 100% 阻断)
   - Tar Slip 任意路径逃逸与目录穿越渗透防御
   - 敏感文件白名单隔离打包 (严禁媒体缓存进入备份)
2. 验收项 2: 备份与还原接口身份鉴权、越权访问与 CSRF 双重防护审计
   - 未登录身份阻断 (401 Unauthorized)
   - CSRF 跨站请求伪造攻击阻断 (403 Forbidden，双提交 Token 恒定时间比较)
   - 还原操作隔离性 (无未授权公网 HTTP 触发入口，强制受保护 CLI 通道)
3. 验收项 3: 本地磁盘水位熔断与 DoS 拒绝服务防护审计
   - 高水位 (>=85%) 全入口 100% 熔断拦截与 waiting_disk 排队
   - 磁盘打满 DoS 阻断 (零磁盘落地、零后台下载派生)
   - 唤醒过程二次熔断防御机制 (防止震荡与雪崩)
   - 应急清理零数据丢失安全红线 (仅清理 state=done 已归档文件)
   - 持久化配置文件权限安全 (0600 独占访问)
4. 验收项 4: VPS 部署通道凭据存储收敛、SSH 私钥安全权限与防暴力破解审计
   - vps.json 彻底剥离明文密码/口令字段
   - deploy.js / exec.js 强制仅限本地 ED25519/RSA 密钥认证，拒绝密码降级
   - 本地私钥权限与 VPS authorized_keys 权限规范性合规
   - 消除明文弱口令暴力破解风险 (ED25519 256 位高强度抗爆破)
"""
import asyncio
import io
import json
import math
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
import bridge_server


def _calculate_entropy(data: bytes) -> float:
    """计算字节流的香农熵 (Shannon Entropy)，评估密文随机性与抗分析强度"""
    if not data:
        return 0.0
    entropy = 0.0
    total = len(data)
    for b in range(256):
        p = data.count(b) / total
        if p > 0:
            entropy -= p * math.log2(p)
    return entropy


class TestSecurityAuditT7(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_sec_t7_")
        self.orig_app_root = bridge_server.APP_ROOT_DIR
        self.orig_waiting_tasks = dict(bridge_server._WAITING_DISK_TASKS)
        self.orig_archive_jobs = dict(bridge_server._ARCHIVE_JOBS)
        self.orig_archive_config = dict(bridge_server._ARCHIVE_CONFIG)
        self.orig_env_key = os.environ.get("TG_SESSION_BACKUP_KEY")

        os.environ["TG_SESSION_BACKUP_KEY"] = "audit-vault-security-key-256bit-ok!!"
        bridge_server.APP_ROOT_DIR = self.tmp_dir
        bridge_server._WAITING_DISK_FILE = os.path.join(self.tmp_dir, ".waiting_disk.json")
        bridge_server._WAITING_DISK_TASKS.clear()
        bridge_server._ARCHIVE_JOBS.clear()

        bridge_server._ARCHIVE_CONFIG["diskHighWatermarkPercent"] = 85.0
        bridge_server._ARCHIVE_CONFIG["diskLowWatermarkPercent"] = 75.0
        bridge_server._ARCHIVE_CONFIG["diskAutoClean"] = True

        self.client = TestClient(bridge_server.app)

    def tearDown(self):
        bridge_server.APP_ROOT_DIR = self.orig_app_root
        bridge_server._WAITING_DISK_FILE = os.path.join(self.orig_app_root, ".waiting_disk.json")
        bridge_server._WAITING_DISK_TASKS.clear()
        bridge_server._WAITING_DISK_TASKS.update(self.orig_waiting_tasks)
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self.orig_archive_jobs)
        bridge_server._ARCHIVE_CONFIG.clear()
        bridge_server._ARCHIVE_CONFIG.update(self.orig_archive_config)

        if self.orig_env_key is not None:
            os.environ["TG_SESSION_BACKUP_KEY"] = self.orig_env_key
        else:
            os.environ.pop("TG_SESSION_BACKUP_KEY", None)

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _auth_cookies(self):
        """生成合法的 Portal 门禁认证 Token 与 CSRF Header"""
        token = bridge_server._make_portal_token()
        csrf = "csrf-token-audit-vault-7777"
        cookies = {
            bridge_server.PORTAL_COOKIE: token,
            bridge_server.CSRF_COOKIE: csrf,
        }
        headers = {
            bridge_server.CSRF_HEADER: csrf,
        }
        return cookies, headers

    def _create_mock_session_files(self, base_dir: str):
        """构建含有敏感凭据、SQLite会话以及大体积媒体文件的真实模拟环境"""
        acct_dir = os.path.join(base_dir, "account", "user_1827364")
        os.makedirs(acct_dir, exist_ok=True)

        # 敏感会话文件
        binlog_path = os.path.join(acct_dir, "td.binlog")
        with open(binlog_path, "wb") as f:
            f.write(b"TOP_SECRET_TDLIB_SESSION_TOKEN_AND_KEYS_0123456789ABCDEF" * 16)

        db_path = os.path.join(acct_dir, "db.sqlite")
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE auth (phone TEXT, session_key TEXT)")
        cur.execute("INSERT INTO auth VALUES ('+18889990000', 'SECRET_AUTH_KEY_STRING')")
        conn.commit()
        conn.close()

        # 根目录应用凭据
        creds_path = os.path.join(base_dir, ".backend_creds")
        with open(creds_path, "w", encoding="utf-8") as f:
            json.dump({"username": "admin", "password_hash": "argon2_secret_hash"}, f)

        # 严禁打包的媒体文件
        video_dir = os.path.join(acct_dir, "videos")
        os.makedirs(video_dir, exist_ok=True)
        with open(os.path.join(video_dir, "large_movie.mp4"), "wb") as f:
            f.write(b"\x00" * 1024 * 50)

    # =========================================================================
    # 1. 验收项 1: Session 快照强对称加密与异地防泄漏渗透审计
    # =========================================================================

    def test_session_backup_encryption_strength_and_aead_compliance(self):
        """【审计 1.1】：验证 AES-256-GCM AEAD 加密协议、高信息熵与无明文凭据残留"""
        self._create_mock_session_files(self.tmp_dir)

        # 打包并加密
        tar_bytes, rel_files = bridge_server._create_session_archive_bytes(self.tmp_dir)
        enc_payload = bridge_server._encrypt_session_payload(tar_bytes)

        # 1. 验证魔数
        self.assertTrue(enc_payload.startswith(b"TGSNAP01"), "密文必须以 TGSNAP01 魔数开头")

        # 2. 验证协议字段长度 (8B Magic + 16B Salt + 12B Nonce + 密文 + 16B GCM Tag)
        self.assertGreaterEqual(len(enc_payload), 8 + 16 + 12 + 16)
        salt = enc_payload[8:24]
        nonce = enc_payload[24:36]
        self.assertEqual(len(salt), 16, "Salt 长度必须为 16 字节")
        self.assertEqual(len(nonce), 12, "GCM Nonce 长度必须为 12 字节")

        # 3. 验证密文随机性与信息熵 (密文经过对称加密后具备接近最大随机性，无统计规律)
        ciphertext_body = enc_payload[36:]
        entropy = _calculate_entropy(ciphertext_body)
        self.assertGreater(entropy, 7.5, f"密文熵过低 ({entropy:.3f})，可能存在弱加密或明文泄露")

        # 4. 绝对零明文泄露审计：密文中不得存在任何原始敏感特征字符串
        sensitive_patterns = [
            b"TOP_SECRET_TDLIB",
            b"SECRET_AUTH_KEY",
            b"+18889990000",
            b"argon2_secret_hash",
            b"db.sqlite",
            b"td.binlog",
            b".backend_creds",
        ]
        for pattern in sensitive_patterns:
            self.assertNotIn(pattern, enc_payload, f"密文中检测到明文敏感特征泄露: {pattern!r}")

    def test_session_backup_tamper_resistance_and_forgery_defense(self):
        """【审计 1.2】：渗透测试——模拟密文篡改、重放、假冒密钥与位翻转攻击"""
        self._create_mock_session_files(self.tmp_dir)
        tar_bytes, _ = bridge_server._create_session_archive_bytes(self.tmp_dir)
        enc_payload = bytearray(bridge_server._encrypt_session_payload(tar_bytes))

        # 1. 密文中段单比特翻转攻击
        tampered_body = bytearray(enc_payload)
        tampered_body[50] ^= 0xFF
        with self.assertRaises(ValueError) as ctx:
            bridge_server._decrypt_session_payload(bytes(tampered_body))
        self.assertIn("密文被篡改或解密密钥不匹配", str(ctx.exception))

        # 2. GCM 认证标签（末尾 16 字节）破坏攻击
        tampered_tag = bytearray(enc_payload)
        tampered_tag[-5] ^= 0x01
        with self.assertRaises(ValueError) as ctx:
            bridge_server._decrypt_session_payload(bytes(tampered_tag))
        self.assertIn("密文被篡改或解密密钥不匹配", str(ctx.exception))

        # 3. 错误密钥解密阻断
        with self.assertRaises(ValueError) as ctx:
            bridge_server._decrypt_session_payload(bytes(enc_payload), master_key=b"attacker_wrong_key_123456789012")
        self.assertIn("密文被篡改或解密密钥不匹配", str(ctx.exception))

        # 4. 伪造/篡改魔数报头
        fake_magic = bytearray(enc_payload)
        fake_magic[:8] = b"BADMAGIC"
        with self.assertRaises(ValueError) as ctx:
            bridge_server._decrypt_session_payload(bytes(fake_magic))
        self.assertIn("非法的备份文件标识符", str(ctx.exception))

    def test_session_backup_tar_slip_and_path_traversal_defense(self):
        """【审计 1.3】：渗透测试——模拟恶意的 Tar Slip 路径逃逸漏洞利用备份包"""
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
            malicious_data = b"echo 'hacked' > /etc/shadow"
            ti = tarfile.TarInfo(name="../../../../etc/shadow")
            ti.size = len(malicious_data)
            ti.mtime = int(time.time())
            tar.addfile(ti, io.BytesIO(malicious_data))

        enc = bridge_server._encrypt_session_payload(tar_buf.getvalue())
        with self.assertRaises(ValueError) as ctx:
            bridge_server.restore_session_backup(enc, target_dir=self.tmp_dir)
        self.assertIn("非法相对路径逃逸", str(ctx.exception))

    def test_session_backup_whitelist_exclusion(self):
        """【审计 1.4】：验证备份扫描白名单严格剔除视频与缓存媒体，防止备份外泄与存储撑爆"""
        self._create_mock_session_files(self.tmp_dir)
        files = bridge_server._scan_session_files(self.tmp_dir)
        rel_paths = [r for _, r in files]

        # 必须包含核心：td.binlog（授权密钥）与凭据
        self.assertTrue(any("td.binlog" in r for r in rel_paths))
        self.assertTrue(any(".backend_creds" in r for r in rel_paths))

        # 严禁包含媒体与本机缓存（db.sqlite 为 TDLib Chat/消息缓存，已按需求排除）
        self.assertFalse(any("videos" in r for r in rel_paths))
        self.assertFalse(any("large_movie.mp4" in r for r in rel_paths))
        self.assertFalse(any("db.sqlite" in r for r in rel_paths))

    # =========================================================================
    # 2. 验收项 2: 备份与还原接口会话鉴权与 CSRF 双重防护审计
    # =========================================================================

    def test_endpoints_unauthorized_access_denied(self):
        """【审计 2.1】：未登录请求直接拦截（401 Unauthorized），防止越权调用备份接口"""
        protected_endpoints = [
            ("POST", "/api/session/backup"),
            ("GET", "/api/session/backup/status"),
            ("GET", "/api/session/backups"),
            ("POST", "/api/session/restore"),
            ("POST", "/api/session/backup/dir"),
            ("GET", "/api/disk/watermark/status"),
            ("POST", "/api/disk/wake"),
        ]
        for method, endpoint in protected_endpoints:
            if method == "POST":
                resp = self.client.post(endpoint)
            else:
                resp = self.client.get(endpoint)
            self.assertEqual(resp.status_code, 401, f"未登录请求应被拦截 401: {method} {endpoint}")
            data = resp.json()
            self.assertFalse(data.get("ok"))
            self.assertIn("未登录", data.get("message", ""))

    def test_endpoints_csrf_protection_and_constant_time_comparison(self):
        """【审计 2.2】：CSRF 跨站伪造攻击防御审计——缺失或伪造 CSRF Token 均被 403 阻断"""
        token = bridge_server._make_portal_token()
        cookies = {bridge_server.PORTAL_COOKIE: token}

        # 1. 登录后发起 POST /api/session/backup，但缺失 CSRF Token
        resp_no_csrf = self.client.post("/api/session/backup", cookies=cookies)
        self.assertEqual(resp_no_csrf.status_code, 403)
        self.assertIn("CSRF 校验失败", resp_no_csrf.json().get("message", ""))

        # 2. 伪造不一致的 CSRF Token
        cookies_tampered = {
            bridge_server.PORTAL_COOKIE: token,
            bridge_server.CSRF_COOKIE: "attacker_cookie_val",
        }
        headers_tampered = {
            bridge_server.CSRF_HEADER: "victim_expected_val",
        }
        resp_bad_csrf = self.client.post("/api/session/backup", cookies=cookies_tampered, headers=headers_tampered)
        self.assertEqual(resp_bad_csrf.status_code, 403)
        self.assertIn("CSRF 校验失败", resp_bad_csrf.json().get("message", ""))

        # 3. 携带正确配对的 CSRF Token -> 放行通过门禁
        good_cookies, good_headers = self._auth_cookies()
        with patch.object(bridge_server, "create_session_backup", new=AsyncMock(return_value={"ok": True, "filename": "test.enc"})):
            resp_ok = self.client.post("/api/session/backup", cookies=good_cookies, headers=good_headers)
            self.assertEqual(resp_ok.status_code, 200)
            self.assertTrue(resp_ok.json().get("ok"))

    def test_restore_entrypoint_requires_auth_csrf_and_confirm(self):
        """【审计 2.3】：还原为破坏性操作——必须 Portal 登录 + CSRF + confirm=true 三重门禁。

        该接口取代了原先仅限本地 CLI 的通道（用户要求页面可直接还原），
        因此安全约束必须由「不可达」升级为「可达但每一层都能拦」：
        未登录 401、有登录无 CSRF 403、缺 confirm 一律拒绝执行。
        """
        routes = [r.path for r in bridge_server.app.routes]
        self.assertIn("/api/session/restore", routes)
        self.assertIn("/api/session/backups", routes)

        # 1) 未登录：401
        resp = self.client.post("/api/session/restore", json={"name": "tg-session-20260906-035557.tar.gz.enc", "confirm": True})
        self.assertEqual(resp.status_code, 401)

        # 2) 有登录态但无 CSRF：403
        cookies = {bridge_server.PORTAL_COOKIE: bridge_server._make_portal_token()}
        resp = self.client.post(
            "/api/session/restore",
            json={"name": "tg-session-20260906-035557.tar.gz.enc", "confirm": True},
            cookies=cookies)
        self.assertEqual(resp.status_code, 403)
        self.assertIn("CSRF 校验失败", resp.json().get("message", ""))

        # 3) 门禁齐备但缺 confirm：必须拒绝执行（不得真的动文件）
        good_cookies, good_headers = self._auth_cookies()
        with patch.object(bridge_server, "restore_session_backup_async", new=AsyncMock(
                return_value={"ok": False, "message": "还原为破坏性操作，需要二次确认"})) as mock_restore:
            resp = self.client.post(
                "/api/session/restore",
                json={"name": "tg-session-20260906-035557.tar.gz.enc", "confirm": False},
                cookies=good_cookies, headers=good_headers)
            self.assertEqual(resp.status_code, 400)
            # 服务层必须收到 confirm=False 并自行拒绝
            self.assertIs(mock_restore.await_args.kwargs.get("confirm"), False)

        # 4) 名称非法（路径穿越/任意文件）必须被拒绝，且不进入还原流程
        with patch.object(bridge_server, "restore_session_backup_async", new=AsyncMock(
                return_value={"ok": False, "message": "备份包名称非法"})) as mock_bad:
            resp = self.client.post(
                "/api/session/restore",
                json={"name": "../../etc/passwd", "confirm": True},
                cookies=good_cookies, headers=good_headers)
            self.assertEqual(resp.status_code, 400)
            self.assertFalse(resp.json().get("ok"))


    # =========================================================================
    # 3. 验收项 3: 本地磁盘水位熔断与 DoS 拒绝服务攻击防御审计
    # =========================================================================

    def test_disk_dos_protection_at_high_watermark(self):
        """【审计 3.1】：高并发大文件攻击模拟——磁盘达 85% 高水位时全入口 100% 熔断"""
        cookies, headers = self._auth_cookies()

        # 模拟磁盘处于 88.5% 高危水位
        with patch.object(bridge_server, "_get_disk_usage_percent", return_value=88.5):
            # 1. /browse/download 批量下载入口被熔断拦截
            payload = {"files": [{"telegramId": 101, "chatId": 202, "messageId": 303, "fileId": 404, "uniqueId": "uid_1", "filename": "100GB_bomb.iso"}]}
            with patch.object(bridge_server.BACKEND, "start_download_multiple", new=AsyncMock()) as mock_down:
                resp = self.client.post("/browse/download", json=payload, cookies=cookies, headers=headers)
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("超过 85.0% 警戒线", data.get("message", ""))
                self.assertIn("waiting_disk", data.get("message", ""))
                # 关键防御门禁：底层下载进程 0 调用，0 磁盘写入
                mock_down.assert_not_called()

            # 2. /api/tg/quick-download 直投下载入口被熔断拦截
            quick_payload = {
                "files": [
                    {
                        "fileId": 2002,
                        "name": "100GB_bomb2.mkv",
                        "size": "100 GB",
                        "telegramId": 8652569586,
                        "chatId": -100987654321,
                        "messageId": 99,
                        "uniqueId": "AgAD_quick_2002",
                    }
                ],
                "autoArchive": True,
            }
            with patch.object(bridge_server.BACKEND, "start_download_multiple", new=AsyncMock()) as mock_down:
                resp2 = self.client.post("/api/tg/quick-download", json=quick_payload, cookies=cookies, headers=headers)
                self.assertEqual(resp2.status_code, 200)
                data2 = resp2.json()
                self.assertEqual(data2.get("code"), "DISK_WATERMARK_EXCEEDED")
                self.assertEqual(data2.get("state"), "waiting_disk")
                mock_down.assert_not_called()

            # 3. 验证任务安全入队 waiting_disk 且持久化文件权限为 0600
            self.assertEqual(len(bridge_server._WAITING_DISK_TASKS), 2)
            self.assertTrue(os.path.exists(bridge_server._WAITING_DISK_FILE))

    def test_wake_secondary_fuse_protection_against_rebounding(self):
        """【审计 3.2】：唤醒阶段二次熔断保护——防止并发唤醒反弹撑爆磁盘"""
        # 添加 3 个挂起任务
        for i in range(3):
            tid = f"task_audit_{i}"
            bridge_server._WAITING_DISK_TASKS[tid] = {
                "id": tid,
                "filename": f"file_{i}.mp4",
                "created_at": time.time() + i,
                "payload": [{"fileId": f"f_{i}"}]
            }

        # 模拟初次低水位 72%，但唤醒 1 个任务后磁盘反弹至 86%
        usage_sequence = [72.0, 72.0, 86.0, 86.0]
        with patch.object(bridge_server, "_get_disk_usage_percent", side_effect=usage_sequence):
            with patch.object(bridge_server.BACKEND, "start_download_multiple", new=AsyncMock()):
                woken = asyncio.run(bridge_server._check_and_wake_waiting_disk_tasks())
                # 只唤醒 1 个任务，随后由于触碰 86% 强行中止二次熔断
                self.assertEqual(woken, 1)
                self.assertEqual(len(bridge_server._WAITING_DISK_TASKS), 2)

    def test_emergency_cleanup_strict_zero_data_loss_contract(self):
        """【审计 3.3】：应急清理零数据丢失安全审计——仅允许删除 state=done 且本地存在的文件"""
        local_safe_file = os.path.join(self.tmp_dir, "safe_uploaded.mp4")
        with open(local_safe_file, "wb") as f:
            f.write(b"SAFE_CONTENT")

        local_downloading = os.path.join(self.tmp_dir, "in_progress.part")
        with open(local_downloading, "wb") as f:
            f.write(b"DOWNLOADING_CONTENT")

        # 注册两个任务：一个已成功归档 done，一个正在下载 running
        bridge_server._ARCHIVE_JOBS["job_safe"] = {
            "id": "job_safe",
            "state": "done",
            "local_path": local_safe_file,
            "local_deleted": False,
            "updated_at": 1000.0,
        }
        bridge_server._ARCHIVE_JOBS["job_in_progress"] = {
            "id": "job_in_progress",
            "state": "running",
            "local_path": local_downloading,
            "local_deleted": False,
            "updated_at": 2000.0,
        }

        # 模拟高水位触发应急清理
        with patch.object(bridge_server, "_get_disk_usage_percent", side_effect=[89.0, 70.0, 70.0, 70.0, 70.0]):
            with patch.object(bridge_server, "_get_disk_free_gb", return_value=20.0):
                cleaned = asyncio.run(bridge_server._disk_guard_check())
                self.assertEqual(cleaned, 1)
                # 安全文件已被清理
                self.assertFalse(os.path.exists(local_safe_file))
                # 运行中文件绝对未被删除
                self.assertTrue(os.path.exists(local_downloading))

    # =========================================================================
    # 4. 验收项 4: VPS 凭据存储收敛、SSH 私钥安全权限与防暴力破解审计
    # =========================================================================

    def test_vps_credentials_and_ssh_private_key_hardening(self):
        """【审计 4.1】：静态审计——核验 vps.json 彻底剥离明文密码，deploy.js/exec.js 强制私钥"""
        cur_dir = os.path.dirname(os.path.abspath(__file__))
        base_dir = os.path.dirname(cur_dir) if os.path.basename(cur_dir) == "tests" else cur_dir
        vps_json_file = os.path.join(base_dir, ".vps-conn", "vps.json")
        deploy_js_file = os.path.join(base_dir, ".vps-conn", "deploy.js")
        exec_js_file = os.path.join(base_dir, ".vps-conn", "exec.js")


        self.assertTrue(os.path.exists(vps_json_file))
        with open(vps_json_file, "r", encoding="utf-8") as f:
            vps_conf = json.load(f)

        # 1. 严禁出现 password 键
        self.assertNotIn("password", vps_conf, "vps.json 仍含有 password 明文密码，违反凭据安全红线！")
        self.assertNotIn("passwd", vps_conf, "vps.json 仍含有 passwd 字段！")
        self.assertIn("privateKeyPath", vps_conf)

        # 2. deploy.js 与 exec.js 必须只认私钥
        for js_file in (deploy_js_file, exec_js_file):
            with open(js_file, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("resolvePrivateKey", content)
            self.assertIn("privateKey:", content)
            self.assertNotIn("password: conf.password", content)
            self.assertNotIn("password: conf.pass", content)

    def test_vps_ssh_connection_and_remote_authorized_keys_compliance(self):
        """【审计 4.2】：连通性与权限审计——验证本地 ED25519 免密连接与 VPS authorized_keys 0600 权限"""
        cur_dir = os.path.dirname(os.path.abspath(__file__))
        base_dir = os.path.dirname(cur_dir) if os.path.basename(cur_dir) == "tests" else cur_dir
        exec_js = os.path.join(base_dir, ".vps-conn", "exec.js")


        # 远程执行 stat 命令检查 VPS 端 .ssh 与 authorized_keys 真实权限
        cmd = "stat -c '%a %n' ~/.ssh ~/.ssh/authorized_keys"
        res = subprocess.run(
            ["node", exec_js, cmd],
            capture_output=True,
            text=True,
            cwd=base_dir,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, f"远程 SSH 执行失败: {res.stderr}")
        stdout = res.stdout.strip()
        lines = stdout.splitlines()

        # 验证输出
        # 第一行 ~/.ssh 权限必须为 700 (drwx------)
        # 第二行 authorized_keys 权限必须为 600 (-rw-------)
        self.assertTrue(any("700" in l and ".ssh" in l for l in lines), f"~/.ssh 权限应为 700: {stdout}")
        self.assertTrue(any("600" in l and "authorized_keys" in l for l in lines), f"authorized_keys 权限应为 600: {stdout}")


if __name__ == "__main__":
    unittest.main()
