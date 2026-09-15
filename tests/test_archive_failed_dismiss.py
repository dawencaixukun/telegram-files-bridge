# -*- coding: utf-8 -*-
"""回归测试：失败归档记录的「删除/忽略」接口。

背景：永久性失败（本地文件已丢失等）被熔断后既修不好也删不掉，
永远挂在告警中心。本接口给死记录一个出口：只删记录，不动文件本体。
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("TG_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
import bridge_server


def _mk_job(jid, state="failed", err="本地文件不存在或已被移动，且云端没有完整副本（云端 缺失 字节）"):
    from services.archive_service import _ARCHIVE_JOBS, _archive_save
    _ARCHIVE_JOBS[jid] = {
        "id": jid, "unique_id": f"U-{jid}", "filename": f"{jid}.mp4",
        "size_bytes": 1000, "local_path": "/nonexistent", "remote_path": "/onedrive/x",
        "remote_dir": "/onedrive", "policy": "overwrite", "delete_local": False,
        "state": state, "progress": 0, "error": err,
        "created_at": 0.0, "updated_at": 0.0, "archived_at": 0.0,
    }
    _archive_save()
    return _ARCHIVE_JOBS[jid]


class TestFailedDismiss(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(bridge_server.app)
        token = bridge_server._make_portal_token()
        self.csrf = "csrf-secret-token-xyz789"
        self.cookies = {
            bridge_server.PORTAL_COOKIE: token,
            bridge_server.CSRF_COOKIE: self.csrf,
        }
        self.headers = {
            bridge_server.CSRF_HEADER: self.csrf,
            "X-CSRF-Token": self.csrf,
        }
        from services.archive_service import _ARCHIVE_JOBS
        self._jobs_ref = _ARCHIVE_JOBS
        self._snapshot = {k: dict(v) for k, v in _ARCHIVE_JOBS.items()}

    def tearDown(self):
        self._jobs_ref.clear()
        self._jobs_ref.update(self._snapshot)

    def _req(self, method, path, **kw):
        kw.setdefault("cookies", self.cookies)
        kw.setdefault("headers", self.headers)
        return self.client.request(method, path, **kw)

    def test_delete_single_failed_job(self):
        """DELETE 单条 failed 记录：200、记录消失、云端/本地文件不受影响。"""
        _mk_job("del-a")
        r = self._req("DELETE", "/api/archive/failed", json={"jobId": "del-a"})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["deletedCount"], 1)
        self.assertIn("del-a", d["deletedJobIds"])
        self.assertNotIn("del-a", self._jobs_ref)

    def test_post_dismiss_alias(self):
        """POST /dismiss 是 DELETE 的别名（前端 fetch 简化）。"""
        _mk_job("del-b")
        r = self._req("POST", "/api/archive/failed/dismiss", json={"jobId": "del-b"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertNotIn("del-b", self._jobs_ref)

    def test_batch_and_all_failed(self):
        """批量 jobIds 与 allFailed 两条路径。"""
        _mk_job("del-c")
        _mk_job("del-d")
        r = self._req("POST", "/api/archive/failed/dismiss", json={"jobIds": ["del-c", "del-d"]})
        self.assertEqual(r.json()["deletedCount"], 2)
        _mk_job("del-e")
        _mk_job("del-f")
        r = self._req("POST", "/api/archive/failed/dismiss", json={"allFailed": True})
        self.assertGreaterEqual(r.json()["deletedCount"], 2)
        for jid in ("del-e", "del-f"):
            self.assertNotIn(jid, self._jobs_ref)

    def test_non_failed_state_is_skipped_not_deleted(self):
        """非 failed 状态绝不删除（done/queued/uploading/cancelled 保护）。"""
        _mk_job("keep-done", state="done")
        _mk_job("keep-queue", state="queued")
        r = self._req("POST", "/api/archive/failed/dismiss",
                      json={"jobIds": ["keep-done", "keep-queue"]})
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["deletedCount"], 0)
        self.assertEqual(d["skippedCount"], 2)
        self.assertIn("keep-done", self._jobs_ref)   # 原样保留
        self.assertIn("keep-queue", self._jobs_ref)

    def test_missing_id_400(self):
        """既无 jobId 也无 jobIds 也无 allFailed → 400。"""
        r = self._req("POST", "/api/archive/failed/dismiss", json={})
        self.assertEqual(r.status_code, 400)

    def test_only_record_removed_files_untouched(self):
        """删除的只是记录：接口不提供任何文件删除语义。"""
        _mk_job("del-g")
        r = self._req("DELETE", "/api/archive/failed", json={"jobId": "del-g"})
        d = r.json()
        # 响应里不存在任何文件删除相关字段（与 /local/delete 之类接口区分）
        self.assertNotIn("localDeleted", d)
        self.assertNotIn("remoteDeleted", d)
        self.assertNotIn("files", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
