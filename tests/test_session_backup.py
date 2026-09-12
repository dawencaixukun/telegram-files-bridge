# -*- coding: utf-8 -*-
"""
test_session_backup.py — Telegram Session 异地加密冷备与秒级自愈还原自动化测试套件
================================================================================
测试目标与覆盖矩阵：
1. 会话文件白名单扫描与打包范围 (_scan_session_files & _create_session_archive_bytes):
   - 必须包含：TDLib 授权密钥 (account/*/td.binlog), 凭据与配置 (.backend_creds, .bridge_secret, 等)
   - 必须排除：db.sqlite 缓存、数 GB 的多媒体缓存 (videos, photos, documents, thumbnails, temp, downloads, logs, 历史 tar.gz 等)
   - 清单核验：自动生成并打包 session_backup_manifest.json 元数据清单
2. 强对称加密与 AEAD 认证封装 (_encrypt_session_payload & _decrypt_session_payload):
   - 协议特征：TGSNAP01 (8B Magic) + 16B Salt + 12B Nonce + AES-256-GCM 密文与 16B Tag
   - 正常加解密数据完整性一致性验证
   - 密文篡改拦截防御：任改 1 字节或损坏 Tag 时，AEAD 认证失败抛出 ValueError，杜绝脏数据还原
   - 密钥隔离：错误密钥解密必须直接拒绝抛出 ValueError
   - 格式校验：非 TGSNAP01 魔数、截断包头拒绝处理
3. 极端灾难模拟与 1 分钟秒级自愈还原演练 (restore_session_backup):
   - 模拟真实 TDLib 登录态核心文件（td.binlog 与 SQLite 数据库表数据）
   - 模拟灾难性损坏：破坏/清空本地 td.binlog 与 db.sqlite，删除凭据
   - 执行快速解密还原：高精度耗时统计，验证还原耗时远小于 1 分钟（实测 < 1 秒）
   - 还原后完整性验证：td.binlog 二进制与 SQLite 表数据及凭据 100% 无损恢复
   - 安全防御：Tar Slip / 路径穿越攻击检测与阻断 (../../evil)
   - 完整性门禁：解压不含 td.binlog 或 db.sqlite 时拒绝还原并报错
4. API 接口与状态指示灯:
   - 未登录 401 拦截与 CSRF 缺失 403 拦截
   - POST /api/session/backup 触发备份并返回标准包络
   - GET /api/session/backup/status 获取健康指示灯、耗时与快照元数据
5. CLI 命令行秒级自愈演练:
   - 验证 python bridge_server.py --restore-session <file> 能够正确解密并完成就地恢复
"""

import asyncio
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import bridge_server


class TestSessionBackupEngine(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_session_backup_test_")
        self.orig_app_root = bridge_server.APP_ROOT_DIR
        self.orig_env_key = os.environ.get("TG_SESSION_BACKUP_KEY")
        os.environ["TG_SESSION_BACKUP_KEY"] = "test-master-backup-key-32bytes-ok!"
        bridge_server.APP_ROOT_DIR = self.tmp_dir
        self.client = TestClient(bridge_server.app)

    def tearDown(self):
        bridge_server.APP_ROOT_DIR = self.orig_app_root
        if self.orig_env_key is not None:
            os.environ["TG_SESSION_BACKUP_KEY"] = self.orig_env_key
        else:
            os.environ.pop("TG_SESSION_BACKUP_KEY", None)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _auth_cookies(self):
        token = bridge_server._make_portal_token()
        csrf = "csrf-token-session-backup-8888"
        cookies = {
            bridge_server.PORTAL_COOKIE: token,
            bridge_server.CSRF_COOKIE: csrf,
        }
        headers = {
            bridge_server.CSRF_HEADER: csrf,
        }
        return cookies, headers

    def _setup_mock_session_environment(self, base_dir: str):
        """在测试目录构建真实的 TDLib 会话与配置环境，同时掺入大体积媒体文件与日志"""
        acct_dir = os.path.join(base_dir, "account", "8652569586")
        os.makedirs(acct_dir, exist_ok=True)

        # 1. 核心 TDLib 文件
        binlog_path = os.path.join(acct_dir, "td.binlog")
        binlog_data = b"\x00TDLIB_AUTH_SESSION_BINLOG_DATA_SAMPLE_MAGIC_KEY_0123456789" * 32
        with open(binlog_path, "wb") as f:
            f.write(binlog_data)

        sqlite_path = os.path.join(acct_dir, "db.sqlite")
        conn = sqlite3.connect(sqlite_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE session_info (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute("INSERT INTO session_info VALUES ('login_user', '8652569586')")
        cur.execute("INSERT INTO session_info VALUES ('auth_state', 'ready')")
        conn.commit()
        conn.close()

        wal_path = os.path.join(acct_dir, "db.sqlite-wal")
        with open(wal_path, "wb") as f:
            f.write(b"SQLITE_WAL_HEADER_SAMPLE_BYTES")

        # 2. 关键凭据与配置
        creds_path = os.path.join(base_dir, ".backend_creds")
        with open(creds_path, "w", encoding="utf-8") as f:
            json.dump({"username": "admin", "password_hash": "hash123"}, f)

        secret_path = os.path.join(base_dir, ".bridge_secret")
        with open(secret_path, "wb") as f:
            f.write(b"BRIDGE_SECRET_KEY_MATERIAL_32B_LONG")

        sub_path = os.path.join(base_dir, ".subscriptions.json")
        with open(sub_path, "w", encoding="utf-8") as f:
            json.dump([{"channel": "tech_news"}], f)

        # 3. 必须排除的多媒体缓存与无关文件
        for media_dir in ("videos", "photos", "documents", "thumbnails", "temp"):
            md = os.path.join(acct_dir, media_dir)
            os.makedirs(md, exist_ok=True)
            with open(os.path.join(md, "dummy_media.bin"), "wb") as f:
                f.write(b"MEDIA_DATA_THAT_MUST_BE_EXCLUDED" * 1024)

        dl_dir = os.path.join(base_dir, "downloads")
        os.makedirs(dl_dir, exist_ok=True)
        with open(os.path.join(dl_dir, "large_download.mp4"), "wb") as f:
            f.write(b"DOWNLOAD_CACHE" * 1024)

        logs_dir = os.path.join(base_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        with open(os.path.join(logs_dir, "app.log"), "w", encoding="utf-8") as f:
            f.write("LOG_INFO_TEST\n" * 100)

        return {
            "binlog_data": binlog_data,
            "binlog_path": binlog_path,
            "sqlite_path": sqlite_path,
            "creds_path": creds_path,
        }

    # =========================================================================
    # 1. 会话文件白名单扫描与打包范围
    # =========================================================================

    def test_scan_session_files_whitelist_inclusion_and_exclusion(self):
        """验证白名单扫描精准收敛核心凭证，严禁多媒体缓存进入备份包"""
        self._setup_mock_session_environment(self.tmp_dir)

        scanned = bridge_server._scan_session_files(self.tmp_dir)
        rel_paths = [rel for _, rel in scanned]

        # 验证核心包含项：只收授权密钥 td.binlog（db.sqlite 缓存已按需求排除）
        self.assertIn("account/8652569586/td.binlog", rel_paths)
        self.assertIn(".backend_creds", rel_paths)
        self.assertIn(".bridge_secret", rel_paths)
        self.assertIn(".subscriptions.json", rel_paths)

        # db.sqlite（TDLib Chat/消息缓存，实测 502 MB）不再纳入备份
        for rel in rel_paths:
            self.assertFalse(rel.startswith("account/8652569586/db.sqlite"),
                             f"db.sqlite 缓存不应被打包: {rel}")

        # 验证严禁排除项
        for rel in rel_paths:
            self.assertFalse(rel.startswith("account/8652569586/videos/"), f"媒体文件不应被打包: {rel}")
            self.assertFalse(rel.startswith("account/8652569586/photos/"), f"媒体文件不应被打包: {rel}")
            self.assertFalse(rel.startswith("account/8652569586/documents/"), f"媒体文件不应被打包: {rel}")
            self.assertFalse(rel.startswith("downloads/"), f"下载缓存不应被打包: {rel}")
            self.assertFalse(rel.startswith("logs/"), f"日志文件不应被打包: {rel}")

    def test_create_session_archive_manifest(self):
        """验证打包归档字节流并内嵌 session_backup_manifest.json 清单"""
        self._setup_mock_session_environment(self.tmp_dir)

        tar_bytes, rel_names = bridge_server._create_session_archive_bytes(self.tmp_dir)
        self.assertGreater(len(tar_bytes), 0)
        self.assertIn("session_backup_manifest.json", rel_names)

        # 解压核验清单内容
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
            names = tar.getnames()
            self.assertIn("session_backup_manifest.json", names)
            self.assertIn("account/8652569586/td.binlog", names)

            manifest_file = tar.extractfile("session_backup_manifest.json")
            self.assertIsNotNone(manifest_file)
            manifest = json.loads(manifest_file.read().decode("utf-8"))
            self.assertIn("backup_time", manifest)
            self.assertIn("files", manifest)
            self.assertIn("account/8652569586/td.binlog", manifest["files"])

    # =========================================================================
    # 2. AES-256-GCM AEAD 加解密与密文完整性校验
    # =========================================================================

    def test_encrypt_decrypt_roundtrip(self):
        """验证 AES-256-GCM 加密与解密完整往返"""
        raw_data = b"HELLO_TELEGRAM_SESSION_CRYPTO_VERIFICATION_PAYLOAD_1234567890" * 100
        enc_payload = bridge_server._encrypt_session_payload(raw_data)

        # 校验包头格式：TGSNAP01 (8B) + Salt (16B) + Nonce (12B) + Tag (16B) + Ciphertext
        self.assertTrue(enc_payload.startswith(b"TGSNAP01"))
        self.assertGreater(len(enc_payload), 8 + 16 + 12 + 16)

        # 解密并比对明文
        decrypted = bridge_server._decrypt_session_payload(enc_payload)
        self.assertEqual(decrypted, raw_data)

    def test_tampered_ciphertext_fails_aead_verification(self):
        """验证密文遭篡改（任何 1 个字节变化）时 AEAD 认证失败抛出异常"""
        raw_data = b"SENSITIVE_SESSION_DATA_MUST_NOT_BE_TAMPERED"
        enc_payload = bytearray(bridge_server._encrypt_session_payload(raw_data))

        # 篡改密文正文部分的任意 1 字节
        tamper_idx = len(enc_payload) - 5
        enc_payload[tamper_idx] ^= 0xFF

        with self.assertRaises(ValueError) as ctx:
            bridge_server._decrypt_session_payload(bytes(enc_payload))
        self.assertIn("校验失败", str(ctx.exception))

    def test_wrong_key_fails_decryption(self):
        """验证使用错误密钥解密时直接抛出异常拒绝还原"""
        raw_data = b"SECRET_SESSION_TOKEN_123456"
        key_a = b"KEY_A_MATERIAL_0123456789ABCDEF"
        key_b = b"KEY_B_MATERIAL_FEDCBA9876543210"

        enc_payload = bridge_server._encrypt_session_payload(raw_data, master_key=key_a)
        with self.assertRaises(ValueError) as ctx:
            bridge_server._decrypt_session_payload(enc_payload, master_key=key_b)
        self.assertIn("校验失败", str(ctx.exception))

    def test_invalid_magic_or_truncated_payload(self):
        """验证非 TGSNAP01 魔数或截断包头时被正确拦截"""
        with self.assertRaises(ValueError) as ctx:
            bridge_server._decrypt_session_payload(b"SHORT")
        self.assertIn("小于最小包头长度", str(ctx.exception))

        fake_payload = b"BADMAGIC" + b"\x00" * 60
        with self.assertRaises(ValueError) as ctx:
            bridge_server._decrypt_session_payload(fake_payload)
        self.assertIn("非法的备份文件标识符", str(ctx.exception))

    # =========================================================================
    # 3. 极端灾难模拟与 1 分钟秒级自愈还原演练
    # =========================================================================

    def test_session_corruption_and_fast_restoration_under_1_minute(self):
        """【核心验收条件 1】：模拟 Session 损坏，通过备份包在 1 分钟内解密还原并成功恢复 TDLib 登录态"""
        # 1. 建立初始健康会话
        env_info = self._setup_mock_session_environment(self.tmp_dir)
        orig_binlog = env_info["binlog_data"]
        binlog_path = env_info["binlog_path"]
        sqlite_path = env_info["sqlite_path"]
        creds_path = env_info["creds_path"]

        # 2. 执行备份打包与加密
        tar_bytes, _ = bridge_server._create_session_archive_bytes(self.tmp_dir)
        enc_payload = bridge_server._encrypt_session_payload(tar_bytes)
        backup_file = os.path.join(self.tmp_dir, "session-backups", "tg-session-disaster-test.tar.gz.enc")
        os.makedirs(os.path.dirname(backup_file), exist_ok=True)
        with open(backup_file, "wb") as f:
            f.write(enc_payload)

        # 3. 模拟极端灾难：Session 遭到严重破坏
        # 3.1 破坏 td.binlog 内容为非法垃圾字节
        with open(binlog_path, "wb") as f:
            f.write(b"CORRUPTED_GARBAGE_CRASH_DATA" * 50)
        # 3.2 删除 sqlite 缓存（该文件已不在备份范围内，灾难后由 TDLib 自动重建）
        if os.path.exists(sqlite_path):
            os.remove(sqlite_path)
        # 3.3 删除关键凭据
        if os.path.exists(creds_path):
            os.remove(creds_path)

        # 确认当前处于损坏不可用状态
        with open(binlog_path, "rb") as f:
            self.assertNotEqual(f.read(), orig_binlog)
        self.assertFalse(os.path.exists(sqlite_path))
        self.assertFalse(os.path.exists(creds_path))

        # 4. 执行灾难自愈还原并严格计时（要求 < 60 秒）
        t0 = time.perf_counter()
        res = bridge_server.restore_session_backup(backup_file, target_dir=self.tmp_dir)
        restore_duration = time.perf_counter() - t0

        # 验证性能：耗时必须远小于 60 秒（实测通常小于 0.2 秒）
        self.assertLess(restore_duration, 60.0, f"还原耗时 {restore_duration:.4f}s 超过了 1 分钟上限！")
        self.assertTrue(res.get("ok"))
        self.assertGreater(res.get("restored_count", 0), 0)

        # 5. 校验还原结果：TDLib 核心文件与凭据 100% 恢复
        # 5.1 td.binlog 完全一致
        with open(binlog_path, "rb") as f:
            restored_binlog = f.read()
        self.assertEqual(restored_binlog, orig_binlog)

        # 5.2 db.sqlite 缓存不再纳入备份，因此还原后不应恢复该文件
        #     （它是 TDLib 从 TG 服务器自动重建的 Chat/消息缓存）
        self.assertFalse(os.path.exists(sqlite_path),
                         "db.sqlite 缓存已排除在备份范围外，不应被还原")

        # 5.3 凭据文件成功恢复
        self.assertTrue(os.path.exists(creds_path))
        with open(creds_path, "r", encoding="utf-8") as f:
            creds = json.load(f)
            self.assertEqual(creds.get("username"), "admin")

    def test_restore_path_traversal_attack_blocked(self):
        """验证恶意备份包如果包含路径穿越（Tar Slip）会被拦截"""
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
            evil_data = b"MALICIOUS_OVERWRITE"
            ti = tarfile.TarInfo(name="../../etc/cron.d/evil_job")
            ti.size = len(evil_data)
            ti.mtime = int(time.time())
            tar.addfile(ti, io.BytesIO(evil_data))

        enc = bridge_server._encrypt_session_payload(tar_buf.getvalue())
        with self.assertRaises(ValueError) as ctx:
            bridge_server.restore_session_backup(enc, target_dir=self.tmp_dir)
        self.assertIn("非法相对路径逃逸", str(ctx.exception))

    def test_restore_missing_core_session_files_rejected(self):
        """验证缺少核心会话文件（无 binlog/sqlite/creds）的非法归档会被拒绝还原"""
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
            dummy_data = b"JUST_SOME_OTHER_FILE"
            ti = tarfile.TarInfo(name="other/notes.txt")
            ti.size = len(dummy_data)
            ti.mtime = int(time.time())
            tar.addfile(ti, io.BytesIO(dummy_data))

        enc = bridge_server._encrypt_session_payload(tar_buf.getvalue())
        with self.assertRaises(ValueError) as ctx:
            bridge_server.restore_session_backup(enc, target_dir=self.tmp_dir)
        self.assertIn("不含有效的 TDLib 会话核心文件", str(ctx.exception))

    def test_restore_nonexistent_file_raises_not_found(self):
        """验证还原不存在的备份文件抛出 FileNotFoundError"""
        with self.assertRaises(FileNotFoundError):
            bridge_server.restore_session_backup(os.path.join(self.tmp_dir, "not_exist.enc"))

    # =========================================================================
    # 4. API 接口与状态指示灯
    # =========================================================================

    def test_api_session_backup_auth_and_csrf_protection(self):
        """验证 /api/session/backup 接口必须具备 Portal 登录态与 CSRF 防护"""
        # 1. 未登录 401
        resp = self.client.post("/api/session/backup")
        self.assertEqual(resp.status_code, 401)

        # 2. 有登录态但无 CSRF 403
        cookies = {bridge_server.PORTAL_COOKIE: bridge_server._make_portal_token()}
        resp_csrf = self.client.post("/api/session/backup", cookies=cookies)
        self.assertEqual(resp_csrf.status_code, 403)

    def test_api_session_backup_execution_and_status(self):
        """验证 POST /api/session/backup 触发冷备以及 GET /api/session/backup/status 查询状态"""
        self._setup_mock_session_environment(self.tmp_dir)
        cookies, headers = self._auth_cookies()

        # Mock OpenList 上传为成功
        with patch.object(bridge_server, "_upload_session_backup_to_openlist", new_callable=AsyncMock) as mock_up:
            mock_up.return_value = (True, "/TG-Backups/tg-session-mock.tar.gz.enc")

            # 触发备份
            resp = self.client.post("/api/session/backup", cookies=cookies, headers=headers)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data.get("ok"))
            self.assertIn("tg-session-", data.get("filename", ""))
            self.assertGreater(data.get("size", 0), 0)
            self.assertEqual(data.get("remotePath"), "/TG-Backups/tg-session-mock.tar.gz.enc")

            # 查询备份状态
            resp_st = self.client.get("/api/session/backup/status", cookies=cookies)
            self.assertEqual(resp_st.status_code, 200)
            st_data = resp_st.json()
            self.assertTrue(st_data.get("ok"))
            self.assertEqual(st_data.get("status"), "ok")
            self.assertEqual(st_data.get("dotClass"), "ok")
            self.assertEqual(st_data.get("label"), "冷备正常")
            self.assertGreater(st_data.get("lastBackupTimestamp", 0), 0)

    # =========================================================================
    # 5. CLI 命令行秒级自愈演练
    # =========================================================================

    def test_cli_restore_session_command(self):
        """验证 python bridge_server.py --restore-session <file> 命令行执行"""
        env_info = self._setup_mock_session_environment(self.tmp_dir)
        binlog_path = env_info["binlog_path"]

        # 生成有效加密备份
        tar_bytes, _ = bridge_server._create_session_archive_bytes(self.tmp_dir)
        enc_payload = bridge_server._encrypt_session_payload(tar_bytes)
        enc_file = os.path.join(self.tmp_dir, "cli_test.tar.gz.enc")
        with open(enc_file, "wb") as f:
            f.write(enc_payload)

        # 模拟损坏
        with open(binlog_path, "wb") as f:
            f.write(b"CORRUPTED_FOR_CLI_TEST")

        # 调用 CLI 执行还原
        proc = subprocess.run(
            [sys.executable, "bridge_server.py", "--restore-session", enc_file],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.abspath(bridge_server.__file__)),
            env=dict(os.environ, TG_DATA_DIR=self.tmp_dir),
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"CLI 还原失败: {proc.stderr}")
        self.assertIn("[RESTORE OK]", proc.stdout)

    # ------------------------------------------------------------------
    # 本地冷备轮转：两种命名都要清理 + 总积上限兜底
    # ------------------------------------------------------------------
    def _write_backup(self, d, name, size, mtime):
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        os.utime(p, (mtime, mtime))
        return p

    def test_rotate_recognizes_both_backup_naming_schemes(self):
        """轮转必须同时识别 tg-session-*.tar.gz.enc 与 tg-sessions-*.tar.gz。

        历史 bug 只匹配前者，导致 backup-sessions.sh 产出的明文快照
        永远不会被清理（线上实测堆积 16.75 GB）。
        """
        from services.backup_service import _is_local_backup_name
        self.assertTrue(_is_local_backup_name("tg-session-20260906-035557.tar.gz.enc"))
        self.assertTrue(_is_local_backup_name("tg-sessions-20260905-033012.tar.gz"))
        self.assertTrue(_is_local_backup_name("tg-sessions-20260910-033024.tar.gz"))
        # 非备份文件不得误删
        self.assertFalse(_is_local_backup_name("data.db"))
        self.assertFalse(_is_local_backup_name(".archive_jobs.json"))
        self.assertFalse(_is_local_backup_name("logs.jsonl"))
        self.assertFalse(_is_local_backup_name("tg-session-backup-notes.txt"))

    def test_rotate_prunes_old_plain_snapshots(self):
        """只保留最新 keep 份，且旧命名（复数 + 明文）必须被删掉。"""
        from services.backup_service import _rotate_local_backups
        d = os.path.join(self.tmp_dir, "bk")
        os.makedirs(d, exist_ok=True)
        base = time.time() - 86400
        for i in range(6):
            self._write_backup(d, "tg-sessions-2026090%d-030000.tar.gz" % i, 1024, base + i)

        _rotate_local_backups(d, keep=3)

        left = sorted(os.listdir(d))
        self.assertEqual(len(left), 3, f"应只保留 3 份，实际: {left}")
        # 保留的必须是最新的三份（i=3,4,5）
        self.assertNotIn("tg-sessions-20260900-030000.tar.gz", left)
        self.assertNotIn("tg-sessions-20260901-030000.tar.gz", left)

    def test_rotate_enforces_total_size_cap(self):
        """即使份数未超上限，总积超限也要从最旧的开始删。"""
        import services.backup_service as bs
        d = os.path.join(self.tmp_dir, "bk2")
        os.makedirs(d, exist_ok=True)
        base = time.time() - 86400
        mb = 1024 * 1024
        # 3 份各 5MB，上限压到 12MB → 必须删掉最旧的一份
        for i in range(3):
            self._write_backup(d, "tg-session-2026090%d-030000.tar.gz.enc" % i, 5 * mb, base + i)

        orig_cap = bs._SESSION_BACKUP_MAX_TOTAL_BYTES
        bs._SESSION_BACKUP_MAX_TOTAL_BYTES = 12 * mb
        try:
            bs._rotate_local_backups(d, keep=5)
        finally:
            bs._SESSION_BACKUP_MAX_TOTAL_BYTES = orig_cap

        left = sorted(os.listdir(d))
        self.assertEqual(len(left), 2, f"总积超限应删最旧一份，实际保留: {left}")
        self.assertNotIn("tg-session-20260900-030000.tar.gz.enc", left)

    # ------------------------------------------------------------------
    # 备份范围红线：只备账号/环境状态，绝不打包 TG 媒体文件
    # ------------------------------------------------------------------
    def test_session_backup_excludes_media_files(self):
        """会话备份白名单不得收录 videos/（TG 下载的媒体本体）与 temp/ 临时文件。

        历史事故：app-data 树里 account/<id>/videos/ 存放 TG 下载的真实媒体
        （实测 7.6 GB，其中 49 条归档记录的 local_path 都指向该目录），
        任何「整目录打包」都会把它一并备份，单个包一度撑到 11.74 GB。
        """
        from services.backup_service import _scan_session_files

        base = os.path.join(self.tmp_dir, "appdata")
        acct = os.path.join(base, "account", "acct-1")
        for sub in ("videos", "temp", "thumbnails"):
            os.makedirs(os.path.join(acct, sub), exist_ok=True)

        # 应被备份的账号状态：仅 td.binlog（授权密钥本体）
        with open(os.path.join(acct, "td.binlog"), "wb") as f:
            f.write(b"session-auth-key")
        # 绝不能被备份：db.sqlite 缓存 + 媒体 + 临时文件
        with open(os.path.join(acct, "db.sqlite"), "wb") as f:
            f.write(b"x" * 8192)
        with open(os.path.join(acct, "db.sqlite-wal"), "wb") as f:
            f.write(b"x" * 512)
        with open(os.path.join(acct, "videos", "movie.mp4"), "wb") as f:
            f.write(b"x" * 4096)
        with open(os.path.join(acct, "temp", "transcode.tmp"), "wb") as f:
            f.write(b"x" * 512)
        # 顶层凭据
        with open(os.path.join(base, ".openlist_auth"), "wb") as f:
            f.write(b"auth")

        rels = {rel for _, rel in _scan_session_files(base)}

        self.assertTrue(any(r.endswith("td.binlog") for r in rels), "必须备份 td.binlog")
        self.assertTrue(any(r == ".openlist_auth" for r in rels), "必须备份凭据文件")

        leaked = [r for r in rels if "/videos/" in r or "/temp/" in r or "db.sqlite" in r]
        self.assertEqual(leaked, [], f"备份白名单不得包含媒体/临时文件，却收录了: {leaked}")


    # =========================================================================
    # 6. 一键还原：白名单精确落盘、格式自适应、容器停写编排
    # =========================================================================

    def _make_plain_snapshot(self, path, with_prefix=True, include_media=False):
        """构造每日脚本风格的明文 tg-sessions-*.tar.gz（整树以 app-data/ 为前缀）"""
        prefix = "app-data/" if with_prefix else ""
        with tarfile.open(path, mode="w:gz") as tar:
            def add(name, data, mode=0o644):
                ti = tarfile.TarInfo(name=prefix + name)
                ti.size = len(data)
                ti.mtime = int(time.time())
                ti.mode = mode
                tar.addfile(ti, io.BytesIO(data))

            add("account/8652569586/td.binlog", b"PLAIN_SNAPSHOT_TDLIB_AUTH_KEY")
            add("data.db", b"SQLITE_ADMIN_ACCOUNT_DB")
            add(".openlist_auth", b"{\"token\": \"x\"}")
            # 必须被排除：媒体缓存 / Chat 缓存 / 日志
            add("account/8652569586/db.sqlite", b"x" * 2048)
            if include_media:
                add("account/8652569586/videos/movie.mp4", b"x" * 4096)
            add("logs/api.log.0", b"noise" * 100)

    def test_restore_plain_snapshot_strips_appdata_prefix_and_skips_media(self):
        """每日脚本明文包：剥离 app-data/ 前缀落盘，且媒体/日志/db.sqlite 一律不还原"""
        from services.backup_service import restore_session_backup_plain
        env = self._setup_mock_session_environment(self.tmp_dir)
        snap = os.path.join(self.tmp_dir, "tg-sessions-20260910-033024.tar.gz")
        self._make_plain_snapshot(snap, with_prefix=True, include_media=True)

        target = os.path.join(self.tmp_dir, "restore_target")
        res = restore_session_backup_plain(snap, target_dir=target)
        self.assertTrue(res.get("ok"))

        # td.binlog 必须落在 account/<id>/ 下（前缀已剥离，而非 app-data/account/...）
        restored_binlog = os.path.join(target, "account", "8652569586", "td.binlog")
        self.assertTrue(os.path.exists(restored_binlog),
                        f"td.binlog 未按前缀剥离落盘，实际文件: {res.get('files')}")
        self.assertFalse(os.path.exists(os.path.join(target, "app-data")),
                         "不得生成 app-data/ 嵌套目录")
        # data.db（管理台登录库）与凭据必须还原
        self.assertTrue(os.path.exists(os.path.join(target, "data.db")))
        self.assertTrue(os.path.exists(os.path.join(target, ".openlist_auth")))

        files = res.get("files") or []
        leaked = [f for f in files if "/videos/" in f or "db.sqlite" in f or f.startswith("logs/")]
        self.assertEqual(leaked, [], f"媒体/日志/db.sqlite 不得被还原，却包含: {leaked}")

    def test_restore_auto_detects_format_by_content(self):
        """按文件真实内容判格式：TGSNAP01 走加密通道，gzip 魔数走明文通道"""
        from services.backup_service import restore_session_backup_file
        self._setup_mock_session_environment(self.tmp_dir)

        # 1) 加密包
        tar_bytes, _ = bridge_server._create_session_archive_bytes(self.tmp_dir)
        enc_path = os.path.join(self.tmp_dir, "a.tar.gz.enc")
        with open(enc_path, "wb") as f:
            f.write(bridge_server._encrypt_session_payload(tar_bytes))
        res_enc = restore_session_backup_file(enc_path, target_dir=os.path.join(self.tmp_dir, "t1"))
        self.assertTrue(res_enc.get("ok"))

        # 2) 明文包
        plain_path = os.path.join(self.tmp_dir, "tg-sessions-20260910-033024.tar.gz")
        self._make_plain_snapshot(plain_path)
        res_plain = restore_session_backup_file(plain_path, target_dir=os.path.join(self.tmp_dir, "t2"))
        self.assertTrue(res_plain.get("ok"))

        # 3) 无法识别的垃圾文件必须报错，绝不能当作备份解包
        junk = os.path.join(self.tmp_dir, "junk.bin")
        with open(junk, "wb") as f:
            f.write(b"NOT_A_BACKUP_AT_ALL")
        with self.assertRaises(ValueError) as ctx:
            restore_session_backup_file(junk)
        self.assertIn("无法识别的备份包格式", str(ctx.exception))

    def test_restore_requires_confirm_and_rejects_bad_name(self):
        """缺 confirm 直接拒绝；名称非法（路径穿越）在触盘之前就被拦住"""
        from services.backup_service import restore_session_backup_async
        r1 = asyncio.run(restore_session_backup_async("tg-sessions-20260910-033024.tar.gz", confirm=False))
        self.assertFalse(r1.get("ok"))
        self.assertIn("二次确认", r1.get("message", ""))

        r2 = asyncio.run(restore_session_backup_async("../../etc/passwd", confirm=True))
        self.assertFalse(r2.get("ok"))
        self.assertIn("非法", r2.get("message", ""))

        r3 = asyncio.run(restore_session_backup_async("not-a-backup.zip", confirm=True))
        self.assertFalse(r3.get("ok"))

    def test_restore_stops_then_starts_backend_container(self):
        """一键还原必须停写后端容器再写入，写完重新启动；顺序不得颠倒"""
        import services.backup_service as bs
        self._setup_mock_session_environment(self.tmp_dir)
        snap = os.path.join(self.tmp_dir, "tg-sessions-20260910-033024.tar.gz")
        self._make_plain_snapshot(snap)

        calls = []

        async def fake_container(action):
            calls.append(action)
            return True, f"{action} ok"

        async def fake_backup():
            return {"filename": "tg-session-prerestore.tar.gz.enc"}

        orig_dir = bs.session_backup_local_dir
        bs.session_backup_local_dir = lambda: self.tmp_dir
        try:
            with patch.object(bs, "_docker_container_action", new=fake_container), \
                 patch.object(bs, "_SESSION_BACKUP_PRERESTORE", True), \
                 patch.object(bs, "create_session_backup", new=fake_backup):
                res = asyncio.run(bs.restore_session_backup_async(
                    "tg-sessions-20260910-033024.tar.gz", origin="local", confirm=True))
        finally:
            bs.session_backup_local_dir = orig_dir

        self.assertTrue(res.get("ok"), res.get("message"))
        self.assertEqual(calls, ["stop", "start"], f"必须先停后启，实际: {calls}")
        self.assertTrue(res.get("containerStopped"))
        self.assertTrue(res.get("containerRestarted"))
        self.assertEqual(res.get("prerestoreName"), "tg-session-prerestore.tar.gz.enc")
        # 还原写入真实文件
        self.assertTrue(os.path.exists(os.path.join(self.tmp_dir, "account", "8652569586", "td.binlog")))

    def test_restore_aborts_when_container_cannot_stop(self):
        """停容器失败必须中止还原并保留回滚快照，绝不半写"""
        import services.backup_service as bs
        self._setup_mock_session_environment(self.tmp_dir)
        snap = os.path.join(self.tmp_dir, "tg-sessions-20260910-033024.tar.gz")
        self._make_plain_snapshot(snap)
        # 破坏现场：把现有 td.binlog 标记为“未被还原覆盖”
        marker = b"UNTOUCHED_ORIGINAL_SESSION"
        with open(os.path.join(self.tmp_dir, "account", "8652569586", "td.binlog"), "wb") as f:
            f.write(marker)

        async def fail_stop(action):
            return False, "docker stop 退出码 1"

        async def fake_backup():
            return {"filename": "tg-session-prerestore.tar.gz.enc"}

        orig_dir = bs.session_backup_local_dir
        bs.session_backup_local_dir = lambda: self.tmp_dir
        try:
            with patch.object(bs, "_docker_container_action", new=fail_stop), \
                 patch.object(bs, "create_session_backup", new=fake_backup):
                res = asyncio.run(bs.restore_session_backup_async(
                    "tg-sessions-20260910-033024.tar.gz", origin="local", confirm=True))
        finally:
            bs.session_backup_local_dir = orig_dir

        self.assertFalse(res.get("ok"))
        self.assertIn("停写后端容器失败", res.get("message", ""))
        self.assertEqual(res.get("prerestoreName"), "tg-session-prerestore.tar.gz.enc")
        with open(os.path.join(self.tmp_dir, "account", "8652569586", "td.binlog"), "rb") as f:
            self.assertEqual(f.read(), marker, "停写失败时不得改动现场文件")

    def test_restore_from_remote_downloads_then_restores(self):
        """云端快照还原：先取回本地 fetched/ 再还原，取回失败则中止且不落盘"""
        import services.backup_service as bs
        self._setup_mock_session_environment(self.tmp_dir)
        name = "tg-session-20260906-035557.tar.gz.enc"
        tar_bytes, _ = bridge_server._create_session_archive_bytes(self.tmp_dir)
        enc_payload = bridge_server._encrypt_session_payload(tar_bytes)

        orig_dir = bs.session_backup_local_dir
        orig_resolve = bs._resolve_session_backup_remote_dir
        orig_token = bs._openlist_token
        bs.session_backup_local_dir = lambda: self.tmp_dir

        async def fake_resolve(tok=None):
            return "/onedrive/TG-Backups", "测试桩", ["onedrive"]

        async def fake_token():
            return "tok"

        async def fake_download(remote_path, local_path, max_bytes=0):
            self.assertEqual(remote_path, f"/onedrive/TG-Backups/{name}")
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(enc_payload)
            return True, "ok"

        async def fake_container(action):
            return True, "ok"

        async def fake_backup():
            return {"filename": "pre.enc"}

        try:
            with patch.object(bs, "_resolve_session_backup_remote_dir_detail", new=fake_resolve), \
                 patch.object(bs, "_openlist_token", new=fake_token), \
                 patch.object(bs, "openlist_download_to_file", new=fake_download), \
                 patch.object(bs, "_docker_container_action", new=fake_container), \
                 patch.object(bs, "create_session_backup", new=fake_backup):
                res = asyncio.run(bs.restore_session_backup_async(name, origin="remote", confirm=True))

                # 取回失败必须中止，不得进入还原
                async def fail_download(remote_path, local_path, max_bytes=0):
                    return False, "云端下载返回 HTTP 502"
                with patch.object(bs, "openlist_download_to_file", new=fail_download):
                    res_fail = asyncio.run(bs.restore_session_backup_async(name, origin="remote", confirm=True))
        finally:
            bs.session_backup_local_dir = orig_dir
            bs._resolve_session_backup_remote_dir = orig_resolve
            bs._openlist_token = orig_token

        self.assertTrue(res.get("ok"), res.get("message"))
        self.assertEqual(res.get("origin"), "remote")
        self.assertFalse(res_fail.get("ok"))
        self.assertIn("取回备份包失败", res_fail.get("message", ""))

    def test_list_session_backups_reports_real_files(self):
        """快照列表必须来自真实文件系统：本地两种命名都要出现，且如实标注加密与否"""
        import services.backup_service as bs
        os.makedirs(os.path.join(self.tmp_dir, "session-backups"), exist_ok=True)
        d = os.path.join(self.tmp_dir, "session-backups")
        enc = os.path.join(d, "tg-session-20260906-035557.tar.gz.enc")
        plain = os.path.join(d, "tg-sessions-20260910-033024.tar.gz")
        for p, sz in ((enc, 1024), (plain, 2048)):
            with open(p, "wb") as f:
                f.write(b"x" * sz)
        # 非备份文件不得出现在列表里
        with open(os.path.join(d, "notes.txt"), "wb") as f:
            f.write(b"nope")

        orig_dir = bs.session_backup_local_dir
        orig_resolve = bs._resolve_session_backup_remote_dir
        orig_token = bs._openlist_token
        bs.session_backup_local_dir = lambda: d

        async def fake_resolve(tok=None):
            return "/onedrive/TG-Backups", "测试桩", ["onedrive"]

        async def fake_token():
            return "tok"

        async def fake_list(path):
            self.assertEqual(path, "/onedrive/TG-Backups")
            return {"ok": True, "files": [
                {"name": "tg-session-20260906-035557.tar.gz.enc", "path": "/x", "size": 314039782, "modified": "2026-09-06T03:56:20Z"},
                {"name": "random.txt", "path": "/y", "size": 5, "modified": "2026-09-06T03:56:20Z"},
            ], "message": ""}

        try:
            with patch.object(bs, "_resolve_session_backup_remote_dir_detail", new=fake_resolve), \
                 patch.object(bs, "_openlist_token", new=fake_token), \
                 patch.object(bs, "openlist_list_files", new=fake_list):
                res = asyncio.run(bs.list_session_backups())
        finally:
            bs.session_backup_local_dir = orig_dir
            bs._resolve_session_backup_remote_dir = orig_resolve
            bs._openlist_token = orig_token

        names = {i["name"]: i for i in res.get("items") or []}
        self.assertIn("tg-session-20260906-035557.tar.gz.enc", names)
        self.assertIn("tg-sessions-20260910-033024.tar.gz", names)
        self.assertNotIn("notes.txt", names)
        self.assertNotIn("random.txt", names)
        self.assertTrue(names["tg-session-20260906-035557.tar.gz.enc"]["encrypted"])
        self.assertFalse(names["tg-sessions-20260910-033024.tar.gz"]["encrypted"])
        self.assertGreaterEqual(res.get("localCount"), 2)
        self.assertGreaterEqual(res.get("remoteCount"), 1)

    def test_status_counts_daily_plain_snapshots(self):
        """健康判定必须把每日明文快照算进来，不得再误报「建议备份」"""
        import services.backup_service as bs
        os.makedirs(os.path.join(self.tmp_dir, "session-backups"), exist_ok=True)
        d = os.path.join(self.tmp_dir, "session-backups")
        p = os.path.join(d, "tg-sessions-20260910-033024.tar.gz")
        with open(p, "wb") as f:
            f.write(b"x" * 512)
        # 状态文件里没有任何手动记录，只有每日本地快照
        orig_dir = bs.session_backup_local_dir
        orig_resolve = bs._resolve_session_backup_remote_dir
        orig_token = bs._openlist_token
        bs.session_backup_local_dir = lambda: d

        async def fake_resolve(tok=None):
            return "/onedrive/TG-Backups", "测试桩", ["onedrive"]

        async def fake_token():
            return "tok"

        try:
            with patch.object(bs, "_resolve_session_backup_remote_dir_detail", new=fake_resolve), \
                 patch.object(bs, "_openlist_token", new=fake_token), \
                 patch.object(bs, "openlist_list_files", new=AsyncMock(
                     return_value={"ok": False, "files": [], "message": "未登录"})):
                st = asyncio.run(bs._get_session_backup_status())
        finally:
            bs.session_backup_local_dir = orig_dir
            bs._resolve_session_backup_remote_dir = orig_resolve
            bs._openlist_token = orig_token

        self.assertGreaterEqual(st.get("backupCount"), 1, "每日明文快照必须计入本地份数")
        self.assertNotEqual(st.get("label"), "尚未备份")
        self.assertNotEqual(st.get("label"), "建议备份")
        # 云端不可用时必须如实提示，且不得谎称已推送
        self.assertIn("云端不可用", st.get("message", ""))
        self.assertFalse(st.get("uploadedToOpenList"))


    def test_status_not_ok_when_remote_copy_deleted(self):
        """云端副本被删后不得继续谎报「冷备正常」：必须回落为仅本地留存。

        场景：状态文件里仍留着历史 uploaded_to_openlist=true（曾经上传成功），
        但 OpenList 目录已被清空。此时若不看真实探测结果，卡片会假装仍有异地冗余。
        """
        import services.backup_service as bs
        d = os.path.join(self.tmp_dir, "session-backups")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "tg-sessions-20260910-033024.tar.gz"), "wb") as f:
            f.write(b"x" * 512)
        sf = os.path.join(self.tmp_dir, ".session_backup_status.json")
        with open(sf, "w", encoding="utf-8") as f:
            json.dump({"uploaded_to_openlist": True, "last_backup_time": time.time() - 3600}, f)

        orig_dir = bs.session_backup_local_dir
        orig_resolve = bs._resolve_session_backup_remote_dir
        orig_token = bs._openlist_token
        bs.session_backup_local_dir = lambda: d

        async def fake_resolve(tok=None):
            return "/onedrive/TG-Backups", "测试桩", ["onedrive"]

        async def fake_token():
            return "tok"

        try:
            with patch.object(bs, "_resolve_session_backup_remote_dir_detail", new=fake_resolve), \
                 patch.object(bs, "_openlist_token", new=fake_token), \
                 patch.object(bs, "openlist_list_files", new=AsyncMock(
                     return_value={"ok": True, "files": [], "message": ""})):
                st = asyncio.run(bs._get_session_backup_status())
        finally:
            bs.session_backup_local_dir = orig_dir
            bs._resolve_session_backup_remote_dir = orig_resolve
            bs._openlist_token = orig_token

        self.assertEqual(st.get("remoteCount"), 0)
        self.assertFalse(st.get("uploadedToOpenList"), "云端已空，不得再声称已异地冗余")
        self.assertEqual(st.get("label"), "仅本地留存")
        self.assertIn("云端", st.get("message", ""))

    # =========================================================================
    # 7. 云端备份目录解析：多网盘时绝不猜、自定义路径优先
    # =========================================================================

    def _patch_mounts(self, bs, mounts, with_backups=()):
        """打桩：根目录网盘列表 + 各网盘是否已有 TG-Backups。"""
        async def fake_mounts(token):
            return list(mounts)

        async def fake_has(token, mount):
            return mount in with_backups

        return patch.object(bs, "_openlist_mount_names", new=fake_mounts), \
               patch.object(bs, "_mount_has_backups", new=fake_has)

    def test_multi_mount_without_config_never_guesses(self):
        """多网盘且未指定目录时必须拒绝猜测，绝不能落到「第一个目录」。

        历史 bug：旧实现取根目录下第一个目录，于是 /google、/onedrive、/ppan
        的返回顺序一变，备份就悄悄写到了另一个网盘。
        """
        import services.backup_service as bs
        orig = dict(bs._ARCHIVE_CONFIG)
        try:
            bs._ARCHIVE_CONFIG["sessionBackupDir"] = ""
            bs._ARCHIVE_CONFIG["defaultDir"] = ""
            p1, p2 = self._patch_mounts(bs, ["google", "onedrive", "ppan", "quark", "wopan"])
            with p1, p2:
                d, source, mounts = asyncio.run(bs._resolve_session_backup_remote_dir_detail("tok"))
        finally:
            bs._ARCHIVE_CONFIG.clear()
            bs._ARCHIVE_CONFIG.update(orig)

        self.assertEqual(d, "", "多网盘无从判断时必须返回空串，交由用户显式指定")
        self.assertEqual(len(mounts), 5)
        self.assertIn("尚未指定", source)
        for m in ("/google", "/onedrive", "/ppan", "/quark", "/wopan"):
            self.assertIn(m, source)

    def test_existing_backup_dir_is_reused(self):
        """已有备份目录所在网盘必须被复用，避免重启后「换地方」。"""
        import services.backup_service as bs
        orig = dict(bs._ARCHIVE_CONFIG)
        try:
            bs._ARCHIVE_CONFIG["sessionBackupDir"] = ""
            bs._ARCHIVE_CONFIG["defaultDir"] = "/onedrive/yello"
            p1, p2 = self._patch_mounts(bs, ["google", "onedrive"], with_backups=("onedrive",))
            with p1, p2:
                d, source, _ = asyncio.run(bs._resolve_session_backup_remote_dir_detail("tok"))
        finally:
            bs._ARCHIVE_CONFIG.clear()
            bs._ARCHIVE_CONFIG.update(orig)
        self.assertEqual(d, "/onedrive/TG-Backups")
        self.assertIn("复用", source)

    def test_custom_dir_overrides_auto_detection(self):
        """设置页自定义路径优先于一切自动推导，且支持任意网盘与自定义子目录名。"""
        import services.backup_service as bs
        orig = dict(bs._ARCHIVE_CONFIG)
        try:
            bs._ARCHIVE_CONFIG["sessionBackupDir"] = "/quark/my-custom-backups"
            bs._ARCHIVE_CONFIG["defaultDir"] = "/onedrive/yello"
            p1, p2 = self._patch_mounts(bs, ["google", "onedrive", "quark"], with_backups=("onedrive",))
            with p1, p2:
                d, source, _ = asyncio.run(bs._resolve_session_backup_remote_dir_detail("tok"))
        finally:
            bs._ARCHIVE_CONFIG.clear()
            bs._ARCHIVE_CONFIG.update(orig)
        self.assertEqual(d, "/quark/my-custom-backups")
        self.assertIn("自定义", source)

    def test_single_mount_auto_selected_with_custom_mount_name(self):
        """网盘名可以任意（别人的自定义挂载名），不依赖 onedrive 这个具体名字。"""
        import services.backup_service as bs
        orig = dict(bs._ARCHIVE_CONFIG)
        try:
            bs._ARCHIVE_CONFIG["sessionBackupDir"] = ""
            bs._ARCHIVE_CONFIG["defaultDir"] = ""
            p1, p2 = self._patch_mounts(bs, ["my-cloud-drive"])
            with p1, p2:
                d, source, _ = asyncio.run(bs._resolve_session_backup_remote_dir_detail("tok"))
        finally:
            bs._ARCHIVE_CONFIG.clear()
            bs._ARCHIVE_CONFIG.update(orig)
        self.assertEqual(d, "/my-cloud-drive/TG-Backups")
        self.assertIn("唯一网盘", source)

    def test_env_var_has_highest_priority(self):
        """环境变量可强制指定，优先级最高（运维应急通道）。"""
        import services.backup_service as bs
        orig = dict(bs._ARCHIVE_CONFIG)
        old_env = os.environ.get("TG_SESSION_BACKUP_REMOTE_DIR")
        try:
            os.environ["TG_SESSION_BACKUP_REMOTE_DIR"] = "/custom/env/dir"
            bs._ARCHIVE_CONFIG["sessionBackupDir"] = "/quark/ignored"
            d, source, _ = asyncio.run(bs._resolve_session_backup_remote_dir_detail("tok"))
        finally:
            if old_env is None:
                os.environ.pop("TG_SESSION_BACKUP_REMOTE_DIR", None)
            else:
                os.environ["TG_SESSION_BACKUP_REMOTE_DIR"] = old_env
            bs._ARCHIVE_CONFIG.clear()
            bs._ARCHIVE_CONFIG.update(orig)
        self.assertEqual(d, "/custom/env/dir")
        self.assertIn("环境变量", source)

    def test_upload_aborts_with_actionable_message_when_dir_unresolved(self):
        """目录无法确定时上传必须失败并给出可读原因，而不是静默写错网盘。"""
        import services.backup_service as bs
        orig = dict(bs._ARCHIVE_CONFIG)
        try:
            bs._ARCHIVE_CONFIG["sessionBackupDir"] = ""
            bs._ARCHIVE_CONFIG["defaultDir"] = ""
            p1, p2 = self._patch_mounts(bs, ["google", "onedrive"])
            with p1, p2, \
                 patch.object(bs, "_openlist_ready", new=AsyncMock(return_value=True)), \
                 patch.object(bs, "_openlist_token", new=AsyncMock(return_value="tok")):
                ok, msg = asyncio.run(bs._upload_session_backup_to_openlist("x.tar.gz.enc", b"data"))
        finally:
            bs._ARCHIVE_CONFIG.clear()
            bs._ARCHIVE_CONFIG.update(orig)
        self.assertFalse(ok)
        self.assertIn("尚未指定云端备份目录", msg)
        self.assertIn("/google", msg)
        self.assertIn("/onedrive", msg)
if __name__ == "__main__":
    unittest.main()
