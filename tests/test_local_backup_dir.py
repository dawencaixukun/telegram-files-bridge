# -*- coding: utf-8 -*-
"""回归测试：本地备份目录可自定义 + 仅本地备份。

背景（用户可见的缺口）：
- 本地冷备目录原先写死在代码里（与 app 平级的 session-backups），
  用户无法把冷备落到独立磁盘/挂载卷，只能跟数据盘同生共死。
- 也没有「只备份到本地、不上传云端」的选项：未配置 OpenList 时，
  备份入口的体验是「失败」而不是「已落本地」。

约束：
- 自定义路径必须优先于默认推导，且必须是绝对路径；
- 自定义目录不可用时必须显式报错，绝不静默回退（否则用户以为备份在独立盘上）；
- 仅本地备份不得触发任何云端上传，且状态应记为 ok 而非告警；
- 默认（未配置）时的行为必须与历史一致，不能破坏现有部署。
"""
import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import core.state as state
import services.backup_service as backup


class TestLocalBackupDirResolution(unittest.TestCase):
    def setUp(self):
        self._backup_cfg = dict(state._ARCHIVE_CONFIG)
        self._env = os.environ.pop("TG_SESSION_BACKUP_LOCAL_DIR", None)

    def tearDown(self):
        state._ARCHIVE_CONFIG.clear()
        state._ARCHIVE_CONFIG.update(self._backup_cfg)
        if self._env is not None:
            os.environ["TG_SESSION_BACKUP_LOCAL_DIR"] = self._env
        else:
            os.environ.pop("TG_SESSION_BACKUP_LOCAL_DIR", None)

    def test_custom_dir_takes_priority(self):
        """设置页自定义路径必须优先于默认推导。"""
        with tempfile.TemporaryDirectory() as tmp:
            state._ARCHIVE_CONFIG["localBackupDir"] = tmp
            d, src = backup._resolve_local_backup_dir()
            self.assertEqual(d, tmp)
            self.assertEqual(src, "设置页自定义路径")

    def test_env_var_overrides_custom(self):
        """环境变量是部署级覆盖，优先级高于设置页。"""
        with tempfile.TemporaryDirectory() as tmp:
            state._ARCHIVE_CONFIG["localBackupDir"] = tmp
            with tempfile.TemporaryDirectory() as env_dir:
                os.environ["TG_SESSION_BACKUP_LOCAL_DIR"] = env_dir
                d, src = backup._resolve_local_backup_dir()
                self.assertEqual(d, env_dir)
                self.assertIn("环境变量", src)

    def test_default_when_unset(self):
        """未配置时必须落到默认目录（与历史行为一致）。"""
        state._ARCHIVE_CONFIG["localBackupDir"] = ""
        d, src = backup._resolve_local_backup_dir()
        self.assertTrue(d.endswith("session-backups"))
        self.assertIn("默认", src)

    def test_session_backup_local_dir_backward_compatible(self):
        """旧签名 session_backup_local_dir() 必须仍只返回路径字符串。"""
        with tempfile.TemporaryDirectory() as tmp:
            state._ARCHIVE_CONFIG["localBackupDir"] = tmp
            out = backup.session_backup_local_dir()
            self.assertIsInstance(out, str)
            self.assertEqual(out, tmp)


class TestLocalOnlyBackup(unittest.TestCase):
    def setUp(self):
        self._backup_cfg = dict(state._ARCHIVE_CONFIG)
        self._env = os.environ.pop("TG_SESSION_BACKUP_LOCAL_DIR", None)
        self.tmp = tempfile.mkdtemp(prefix="tg-local-backup-test-")
        state._ARCHIVE_CONFIG["localBackupDir"] = self.tmp

    def tearDown(self):
        state._ARCHIVE_CONFIG.clear()
        state._ARCHIVE_CONFIG.update(self._backup_cfg)
        if self._env is not None:
            os.environ["TG_SESSION_BACKUP_LOCAL_DIR"] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_local_only_writes_and_skips_upload(self):
        """仅本地备份：快照落在自定义目录，且完全不触碰云端上传。"""
        upload = AsyncMock(return_value=(True, "/x/TG-Backups/a.enc"))
        with patch.object(backup, "_upload_session_backup_to_openlist", new=upload):
            res = asyncio.run(backup.create_session_backup(local_only=True))

        self.assertTrue(res.get("ok"))
        self.assertFalse(res.get("uploadedToOpenList"), "仅本地备份不得声称已上传")
        self.assertFalse(res.get("uploaded_to_openlist"))
        self.assertEqual(res.get("status"), "ok", "仅本地备份不应记为告警")
        upload.assert_not_awaited()
        lp = res.get("local_path")
        self.assertTrue(lp and os.path.isfile(lp), "快照必须真实落盘")
        self.assertEqual(os.path.dirname(lp), self.tmp, "必须落在自定义目录")
        self.assertGreater(os.path.getsize(lp), 0)
        self.assertEqual(res.get("local_dir"), self.tmp)
        self.assertIn(self.tmp, res.get("message") or "")

    def test_normal_backup_still_uploads(self):
        """不传 localOnly 时，上传行为必须保持不变。"""
        upload = AsyncMock(return_value=(True, "/x/TG-Backups/a.enc"))
        with patch.object(backup, "_upload_session_backup_to_openlist", new=upload):
            res = asyncio.run(backup.create_session_backup())
        upload.assert_awaited_once()
        self.assertTrue(res.get("uploadedToOpenList"))
        self.assertEqual(res.get("remotePath"), "/x/TG-Backups/a.enc")

    def test_unwritable_dir_raises_instead_of_silent_fallback(self):
        """自定义目录不可用时必须显式报错，绝静默回退到默认目录。"""
        if os.name == "nt":
            state._ARCHIVE_CONFIG["localBackupDir"] = "Z:\\definitely\\missing\\drive"
        else:
            state._ARCHIVE_CONFIG["localBackupDir"] = "/proc/nonexistent/not-writable"
        with self.assertRaises(Exception) as ctx:
            asyncio.run(backup.create_session_backup(local_only=True))
        self.assertIn("本地冷备目录不可用", str(ctx.exception))

    def test_listing_exposes_local_dir_metadata(self):
        """快照列表接口必须暴露本地目录、来源与可写性，供设置页展示。"""
        lst = asyncio.run(backup.list_session_backups())
        self.assertTrue(lst.get("ok"))
        self.assertEqual(lst.get("localDir"), self.tmp)
        self.assertEqual(lst.get("localDirSaved"), self.tmp)
        self.assertIn("设置页自定义路径", lst.get("localSource") or "")
        self.assertIsInstance(lst.get("localWritable"), bool)
        # 原有的云端字段必须保留，不能因本改动丢失
        for key in ("remoteDir", "remoteMounts", "remoteError", "needsConfig", "items"):
            self.assertIn(key, lst)


if __name__ == "__main__":
    unittest.main()
