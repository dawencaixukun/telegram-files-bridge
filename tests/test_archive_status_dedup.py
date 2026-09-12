"""测试归档状态汇聚：同一 uniqueId 存在多次重试/历史失败记录时，始终以最新创建的任务状态为准。"""
import unittest
import time
from fastapi.testclient import TestClient
import bridge_server


class TestArchiveStatusDedup(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(bridge_server.app)
        bridge_server._ARCHIVE_JOBS.clear()

    def test_archive_status_returns_latest_job_per_unique_id(self):
        """若同一文件在历史中多次失败，最后一次成功，/archive/status 必须返回 done 而非 failed。"""
        uid = "AgAD_test_video_uid"
        now = time.time()

        # 3 次历史失败记录
        bridge_server._ARCHIVE_JOBS["job1"] = {
            "id": "job1", "unique_id": uid, "filename": "test.mp4",
            "state": "failed", "error": "本地文件不存在", "created_at": now - 300,
        }
        bridge_server._ARCHIVE_JOBS["job2"] = {
            "id": "job2", "unique_id": uid, "filename": "test.mp4",
            "state": "failed", "error": "本地文件不存在", "created_at": now - 200,
        }
        bridge_server._ARCHIVE_JOBS["job3"] = {
            "id": "job3", "unique_id": uid, "filename": "test.mp4",
            "state": "failed", "error": "网络超时", "created_at": now - 100,
        }
        # 最新 1 次成功记录
        bridge_server._ARCHIVE_JOBS["job4"] = {
            "id": "job4", "unique_id": uid, "filename": "test.mp4",
            "state": "done", "progress": 100, "archived_at": now, "created_at": now,
        }

        cookies = {bridge_server.PORTAL_COOKIE: bridge_server._make_portal_token()}
        resp = self.client.get("/archive/status", cookies=cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        jobs = data.get("jobs", [])

        # 该 uid 汇聚后只输出 1 条且状态为 done
        matched = [j for j in jobs if j.get("uniqueId") == uid]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["state"], "done")
        self.assertEqual(matched[0]["pillLabel"], "已归档")
        self.assertEqual(matched[0]["pillCls"], "archived")


if __name__ == "__main__":
    unittest.main()
