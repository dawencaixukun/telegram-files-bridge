# -*- coding: utf-8 -*-
"""
test_disk_watermark.py — 本地磁盘高低水位动态熔断保护、挂起排队与自动唤醒自动化测试套件
========================================================================================
测试目标与覆盖矩阵：
1. 百分比动态水位算法与阈值判定 (_get_disk_usage_percent, _is_disk_high_watermark_exceeded, _is_disk_low_watermark_reached):
   - 默认阈值：高水位熔断 85.0%，低水位唤醒 75.0%
   - 水位边界判定：84.9% 放行 / 85.0% 熔断；75.1% 维持挂起 / 74.9% 唤醒
   - 支持动态配置覆盖（例如 90% 熔断 / 80% 唤醒）
2. 高水位熔断拦截与 waiting_disk 排队 (全入口覆盖):
   - POST /browse/download：拦截新请求，置为 waiting_disk，返回 friendly 提示，不落盘、不调后端
   - POST /submit：链接批量提交入口熔断拦截并安全入队
   - POST /api/tg/quick-download：直投下载接口熔断拦截并返回标准包络 (code=DISK_WATERMARK_EXCEEDED, state=waiting_disk)
   - waiting_disk 队列持久化：_waiting_disk_save 与 _waiting_disk_load 保证服务重启不丢失
3. 界面与状态接口呈现:
   - GET /tasks 与 GET /partials/tasks-table：呈现黄色胶囊标签「磁盘挂起」与悬浮提示
   - GET /api/disk/watermark/status：输出水位状态、磁盘使用率、阈值与挂起任务数
4. 75% 低水位自动唤醒恢复调度 (_check_and_wake_waiting_disk_tasks / POST /api/disk/wake):
   - 磁盘降至 75% 以下时，FIFO 顺序依次唤醒挂起任务并恢复下载
   - 二次熔断防御：唤醒过程中若磁盘再度达到 85%，立刻中止唤醒，保留其余任务安全挂起
5. 被动应急清理 (Emergency Cleanup):
   - 触发条件：磁盘达到 85% 且开启 diskAutoClean
   - 零数据丢失安全红线：仅安全释放已成功归档到网盘（state == 'done'）的最早本地文件，严禁触碰下载中或未归档文件
"""

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import bridge_server


class TestDiskWatermarkProtection(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_disk_wm_test_")
        self.orig_app_root = bridge_server.APP_ROOT_DIR
        self.orig_archive_config = dict(bridge_server._ARCHIVE_CONFIG)
        self.orig_waiting_tasks = dict(bridge_server._WAITING_DISK_TASKS)
        self.orig_archive_jobs = dict(bridge_server._ARCHIVE_JOBS)

        bridge_server.APP_ROOT_DIR = self.tmp_dir
        bridge_server._WAITING_DISK_FILE = os.path.join(self.tmp_dir, ".waiting_disk.json")
        bridge_server._WAITING_DISK_TASKS.clear()
        bridge_server._ARCHIVE_JOBS.clear()

        # 统一设置默认高低水位
        bridge_server._ARCHIVE_CONFIG["diskHighWatermarkPercent"] = 85.0
        bridge_server._ARCHIVE_CONFIG["diskLowWatermarkPercent"] = 75.0
        bridge_server._ARCHIVE_CONFIG["diskAutoClean"] = True

        self.client = TestClient(bridge_server.app)

    def tearDown(self):
        bridge_server.APP_ROOT_DIR = self.orig_app_root
        bridge_server._WAITING_DISK_FILE = os.path.join(self.orig_app_root, ".waiting_disk.json")
        bridge_server._ARCHIVE_CONFIG.clear()
        bridge_server._ARCHIVE_CONFIG.update(self.orig_archive_config)
        bridge_server._WAITING_DISK_TASKS.clear()
        bridge_server._WAITING_DISK_TASKS.update(self.orig_waiting_tasks)
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self.orig_archive_jobs)
        bridge_server._TASKS_CACHE = {"expire": 0.0, "value": None}
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _auth_cookies(self):
        token = bridge_server._make_portal_token()
        csrf = "csrf-token-disk-wm-8888"
        cookies = {
            bridge_server.PORTAL_COOKIE: token,
            bridge_server.CSRF_COOKIE: csrf,
        }
        headers = {
            bridge_server.CSRF_HEADER: csrf,
        }
        return cookies, headers

    # =========================================================================
    # 1. 百分比动态水位算法与阈值判定
    # =========================================================================

    def test_watermark_percentage_and_threshold_detection(self):
        """验证动态高低水位判定：85% 熔断线与 75% 唤醒线精准识别"""
        # 1. 84.9%：未达到 85% 高水位，未回落至 75% 低水位
        with patch.object(bridge_server, "_get_disk_usage_percent", return_value=84.9):
            high_exceeded, cur, high_t = bridge_server._is_disk_high_watermark_exceeded()
            low_reached, _, low_t = bridge_server._is_disk_low_watermark_reached()
            self.assertFalse(high_exceeded)
            self.assertEqual(cur, 84.9)
            self.assertEqual(high_t, 85.0)
            self.assertFalse(low_reached)
            self.assertEqual(low_t, 75.0)

        # 2. 85.0%：触碰高水位熔断线，触发熔断
        with patch.object(bridge_server, "_get_disk_usage_percent", return_value=85.0):
            high_exceeded, cur, _ = bridge_server._is_disk_high_watermark_exceeded()
            self.assertTrue(high_exceeded)

        # 3. 88.5%：高于 85% 高水位，熔断状态
        with patch.object(bridge_server, "_get_disk_usage_percent", return_value=88.5):
            high_exceeded, cur, _ = bridge_server._is_disk_high_watermark_exceeded()
            low_reached, _, _ = bridge_server._is_disk_low_watermark_reached()
            self.assertTrue(high_exceeded)
            self.assertFalse(low_reached)

        # 4. 75.1%：虽低于高水位，但尚未触碰 75% 唤醒线，保持挂起
        with patch.object(bridge_server, "_get_disk_usage_percent", return_value=75.1):
            high_exceeded, _, _ = bridge_server._is_disk_high_watermark_exceeded()
            low_reached, _, _ = bridge_server._is_disk_low_watermark_reached()
            self.assertFalse(high_exceeded)
            self.assertFalse(low_reached)

        # 5. 74.9%：成功回落至 75% 以下，触发唤醒
        with patch.object(bridge_server, "_get_disk_usage_percent", return_value=74.9):
            low_reached, cur, _ = bridge_server._is_disk_low_watermark_reached()
            self.assertTrue(low_reached)

    def test_custom_configured_watermarks(self):
        """验证支持自定义配置的水位阈值（如 90% 熔断 / 80% 唤醒）"""
        bridge_server._ARCHIVE_CONFIG["diskHighWatermarkPercent"] = 90.0
        bridge_server._ARCHIVE_CONFIG["diskLowWatermarkPercent"] = 80.0

        with patch.object(bridge_server, "_get_disk_usage_percent", return_value=88.0):
            high_exceeded, _, high_t = bridge_server._is_disk_high_watermark_exceeded()
            low_reached, _, low_t = bridge_server._is_disk_low_watermark_reached()
            self.assertFalse(high_exceeded)
            self.assertEqual(high_t, 90.0)
            self.assertFalse(low_reached)
            self.assertEqual(low_t, 80.0)

        with patch.object(bridge_server, "_get_disk_usage_percent", return_value=90.5):
            high_exceeded, _, _ = bridge_server._is_disk_high_watermark_exceeded()
            self.assertTrue(high_exceeded)

        with patch.object(bridge_server, "_get_disk_usage_percent", return_value=79.5):
            low_reached, _, _ = bridge_server._is_disk_low_watermark_reached()
            self.assertTrue(low_reached)

    # =========================================================================
    # 2. 高水位熔断拦截与 waiting_disk 排队
    # =========================================================================

    def test_browse_download_interception_at_high_watermark(self):
        """【核心验收条件 2 部分】：模拟 85% 高水位，验证 /browse/download 被安全拦截并置入 waiting_disk"""
        cookies, headers = self._auth_cookies()

        payload = {
            "files": [
                {
                    "fileId": 1001,
                    "name": "sample_video_high_res.mp4",
                    "size": "500 MB",
                    "telegramId": 8652569586,
                    "chatId": -100123456789,
                    "messageId": 42,
                    "uniqueId": "AgAD_video_test_1001",
                    "chatTitle": "测试影视群",
                }
            ]
        }

        # 模拟磁盘处于 87.5% 高水位，应急清理后仍为 87.5%
        with patch.object(bridge_server, "_get_disk_usage_percent", return_value=87.5), \
             patch.object(bridge_server.BACKEND, "start_download_multiple", new_callable=AsyncMock) as mock_start:

            resp = self.client.post("/browse/download", json=payload, cookies=cookies, headers=headers)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()

            # 验证熔断包络与友好的等待提示
            self.assertFalse(data.get("ok"))
            self.assertEqual(data.get("code"), "DISK_WATERMARK_EXCEEDED")
            self.assertEqual(data.get("state"), "waiting_disk")
            self.assertEqual(data.get("count"), 1)
            self.assertIn("87.5%", data.get("message", ""))
            self.assertIn("85.0%", data.get("message", ""))
            self.assertIn("waiting_disk 挂起队列", data.get("message", ""))

            # 核心安全红线：后端下载必须绝不能被触发！
            mock_start.assert_not_called()

            # 验证任务成功持久化入队 waiting_disk
            self.assertEqual(len(bridge_server._WAITING_DISK_TASKS), 1)
            task = list(bridge_server._WAITING_DISK_TASKS.values())[0]
            self.assertEqual(task.get("filename"), "sample_video_high_res.mp4")
            self.assertEqual(task.get("status"), "waiting_disk")
            self.assertEqual(task.get("uniqueId"), "AgAD_video_test_1001")

    def test_submit_links_interception_at_high_watermark(self):
        """【核心验收条件 2 部分】：模拟 85% 高水位，验证 /submit 链接提交被安全拦截并挂起"""
        cookies, headers = self._auth_cookies()

        with patch.object(bridge_server, "_get_disk_usage_percent", return_value=86.2), \
             patch.object(bridge_server, "_resolve_links_to_files", new_callable=AsyncMock) as mock_resolve:

            form = {"links": "https://t.me/c/1827364521/200\n"}
            resp = self.client.post("/submit", data=form, cookies=cookies, headers=headers)
            self.assertEqual(resp.status_code, 200)

            # 验证正常下载解析未被调用
            mock_resolve.assert_not_called()

            # 验证页面返回中包含熔断挂起提示
            html = resp.text
            self.assertIn("86.2%", html)
            self.assertIn("waiting_disk", html)

            # 验证任务入队
            self.assertGreater(len(bridge_server._WAITING_DISK_TASKS), 0)

    def test_quick_download_interception_at_high_watermark(self):
        """【核心验收条件 2 部分】：模拟 85% 高水位，验证 /api/tg/quick-download 直投接口被安全拦截并挂起"""
        cookies, headers = self._auth_cookies()

        body = {
            "files": [
                {
                    "fileId": 2002,
                    "name": "quick_file.mkv",
                    "size": "1.2 GB",
                    "telegramId": 8652569586,
                    "chatId": -100987654321,
                    "messageId": 99,
                    "uniqueId": "AgAD_quick_2002",
                    "chatTitle": "精品资源",
                }
            ],
            "autoArchive": True,
        }

        with patch.object(bridge_server, "_get_disk_usage_percent", return_value=89.0), \
             patch.object(bridge_server.BACKEND, "start_download_multiple", new_callable=AsyncMock) as mock_start:

            resp = self.client.post("/api/tg/quick-download", json=body, cookies=cookies, headers=headers)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()

            self.assertFalse(data.get("ok"))
            self.assertEqual(data.get("code"), "DISK_WATERMARK_EXCEEDED")
            self.assertEqual(data.get("state"), "waiting_disk")
            self.assertIn("89.0%", data.get("message", ""))
            self.assertIn("85.0%", data.get("message", ""))

            mock_start.assert_not_called()
            self.assertGreater(len(bridge_server._WAITING_DISK_TASKS), 0)

    def test_waiting_disk_queue_persistence_and_reload(self):
        """验证 waiting_disk 挂起队列持久化落盘与加载恢复"""
        task_id = "test_task_persist_01"
        bridge_server._WAITING_DISK_TASKS[task_id] = {
            "id": task_id,
            "filename": "persisted_movie.mp4",
            "size": 1024,
            "size_str": "1 MB",
            "status": "waiting_disk",
            "created_at": time.time(),
            "payload": {"files": [{"fileId": 999}]},
        }

        # 保存
        bridge_server._waiting_disk_save()
        self.assertTrue(os.path.exists(bridge_server._WAITING_DISK_FILE))

        # 清空内存后重新加载
        bridge_server._WAITING_DISK_TASKS.clear()
        self.assertEqual(len(bridge_server._WAITING_DISK_TASKS), 0)

        bridge_server._waiting_disk_load()
        self.assertIn(task_id, bridge_server._WAITING_DISK_TASKS)
        self.assertEqual(bridge_server._WAITING_DISK_TASKS[task_id]["filename"], "persisted_movie.mp4")

    # =========================================================================
    # 3. 界面与状态接口展示
    # =========================================================================

    def test_tasks_table_renders_waiting_disk_pill(self):
        """验证任务列表页面正确渲染 waiting_disk 挂起胶囊与提示"""
        cookies, _ = self._auth_cookies()

        # 注入一条挂起任务
        task_id = "test_ui_task_01"
        bridge_server._WAITING_DISK_TASKS[task_id] = {
            "id": task_id,
            "filename": "ui_display_test.zip",
            "size": 2048,
            "size_str": "2.0 GB",
            "source": "测试群组",
            "msg_id": "88",
            "status": "waiting_disk",
            "error_msg": "本地磁盘使用率超 85%，任务已安全挂起，回落至 75% 自动恢复调度",
            "created_at": time.time(),
            "uniqueId": "AgAD_ui_01",
        }

        resp = self.client.get("/tasks", cookies=cookies)
        self.assertEqual(resp.status_code, 200)
        html = resp.text

        self.assertIn("ui_display_test.zip", html)
        self.assertIn("磁盘挂起", html)
        self.assertIn("本地磁盘使用率超 85%", html)

    def test_api_disk_watermark_status(self):
        """验证 GET /api/disk/watermark/status 准确输出系统水位与挂起任务详情"""
        cookies, _ = self._auth_cookies()

        bridge_server._WAITING_DISK_TASKS["t1"] = {
            "id": "t1",
            "filename": "status_test.mp4",
            "size_str": "100 MB",
            "status": "waiting_disk",
            "created_at": time.time(),
        }

        with patch.object(bridge_server, "_get_disk_usage_percent", return_value=86.5), \
             patch.object(bridge_server, "_get_disk_free_gb", return_value=4.2):

            resp = self.client.get("/api/disk/watermark/status", cookies=cookies)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()

            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("status"), "high")
            self.assertEqual(data.get("usagePercent"), 86.5)
            self.assertEqual(data.get("freeGB"), 4.2)
            self.assertEqual(data.get("highWatermarkPercent"), 85.0)
            self.assertEqual(data.get("lowWatermarkPercent"), 75.0)
            self.assertEqual(data.get("waitingTasksCount"), 1)

    # =========================================================================
    # 4. 75% 低水位自动唤醒恢复调度
    # =========================================================================

    def test_wake_waiting_tasks_when_disk_drops_below_75(self):
        """【核心验收条件 2 部分】：模拟磁盘回落至 75% 以下，挂起任务自动 FIFO 唤醒恢复调度"""
        cookies, headers = self._auth_cookies()

        now = time.time()
        # 创建 3 个挂起任务，提交时间递增
        task1 = {
            "id": "wake_task_1",
            "filename": "first_submitted.mp4",
            "payload": {"files": [{"fileId": 111}]},
            "status": "waiting_disk",
            "created_at": now - 300,
        }
        task2 = {
            "id": "wake_task_2",
            "filename": "second_submitted.mp4",
            "payload": {"files": [{"fileId": 222}]},
            "status": "waiting_disk",
            "created_at": now - 200,
        }
        task3 = {
            "id": "wake_task_3",
            "filename": "third_submitted.mp4",
            "payload": {"files": [{"fileId": 333}]},
            "status": "waiting_disk",
            "created_at": now - 100,
        }
        bridge_server._WAITING_DISK_TASKS["wake_task_1"] = task1
        bridge_server._WAITING_DISK_TASKS["wake_task_2"] = task2
        bridge_server._WAITING_DISK_TASKS["wake_task_3"] = task3

        woken_order = []

        async def fake_start_download(payload):
            f_id = payload["files"][0]["fileId"]
            woken_order.append(f_id)

        # 模拟磁盘回落至 73.5% (< 75.0% 唤醒阈值)
        with patch.object(bridge_server, "_get_disk_usage_percent", return_value=73.5), \
             patch.object(bridge_server.BACKEND, "start_download_multiple", side_effect=fake_start_download):

            # 触发唤醒
            resp = self.client.post("/api/disk/wake", cookies=cookies, headers=headers)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("woken"), 3)
            self.assertEqual(data.get("remainingWaiting"), 0)

            # 验证 3 个任务均被恢复调度，且严格按 FIFO 顺序唤醒 (111 -> 222 -> 333)
            self.assertEqual(woken_order, [111, 222, 333])

            # 验证 waiting_disk 队列已出队清空
            self.assertEqual(len(bridge_server._WAITING_DISK_TASKS), 0)

    def test_wake_interrupted_if_disk_reaches_high_watermark_during_process(self):
        """验证二次熔断保护：唤醒过程中若磁盘占用再度升至 85%，立即中止唤醒"""
        now = time.time()
        for i in range(4):
            tid = f"task_{i}"
            bridge_server._WAITING_DISK_TASKS[tid] = {
                "id": tid,
                "filename": f"file_{i}.mp4",
                "payload": {"files": [{"fileId": 100 + i}]},
                "status": "waiting_disk",
                "created_at": now + i,
            }

        # 模拟唤醒前 72.0%，唤醒第 1 个任务后磁盘立即升到 86.0%
        usage_sequence = [72.0, 72.0, 86.0, 86.0, 86.0]
        usage_mock = MagicMock(side_effect=usage_sequence)

        with patch.object(bridge_server, "_get_disk_usage_percent", usage_mock), \
             patch.object(bridge_server.BACKEND, "start_download_multiple", new_callable=AsyncMock) as mock_start:

            woken = asyncio.run(bridge_server._check_and_wake_waiting_disk_tasks())

            # 仅唤醒了 1 个任务，后续任务因达到 85% 熔断中止
            self.assertEqual(woken, 1)
            self.assertEqual(mock_start.call_count, 1)
            # 剩余 3 个任务继续保留在挂起队列中
            self.assertEqual(len(bridge_server._WAITING_DISK_TASKS), 3)

    # =========================================================================
    # 5. 被动应急清理 (Emergency Cleanup)
    # =========================================================================

    def test_emergency_cleanup_releases_only_completed_archived_files(self):
        """验证高水位被动清理：仅安全释放已成功归档 (done) 的最早本地文件，严禁删除未归档文件"""
        # 创建 3 个本地文件
        f1 = os.path.join(self.tmp_dir, "archived_early.mp4")
        f2 = os.path.join(self.tmp_dir, "archived_late.mp4")
        f3 = os.path.join(self.tmp_dir, "downloading_in_progress.mp4")

        with open(f1, "wb") as f:
            f.write(b"FILE1" * 1024)
        with open(f2, "wb") as f:
            f.write(b"FILE2" * 1024)
        with open(f3, "wb") as f:
            f.write(b"FILE3" * 1024)

        now = time.time()
        # j1: 已完成归档 (最早)
        bridge_server._ARCHIVE_JOBS["j1"] = {
            "id": "j1", "filename": "archived_early.mp4", "local_path": f1,
            "state": "done", "created_at": now - 500, "updated_at": now - 400,
        }
        # j2: 已完成归档 (较晚)
        bridge_server._ARCHIVE_JOBS["j2"] = {
            "id": "j2", "filename": "archived_late.mp4", "local_path": f2,
            "state": "done", "created_at": now - 300, "updated_at": now - 200,
        }
        # j3: 正在下载中/归档中，绝不可被应急删除！
        bridge_server._ARCHIVE_JOBS["j3"] = {
            "id": "j3", "filename": "downloading_in_progress.mp4", "local_path": f3,
            "state": "download", "created_at": now - 100,
        }

        # 模拟高水位：第一次清理后降为 74%，退出清理
        usage_sequence = [88.0, 74.0, 74.0]
        usage_mock = MagicMock(side_effect=usage_sequence)

        with patch.object(bridge_server, "_get_disk_usage_percent", usage_mock), \
             patch.object(bridge_server, "_get_disk_free_gb", return_value=10.0):

            cleaned = asyncio.run(bridge_server._disk_guard_check())

            self.assertEqual(cleaned, 1)
            # j1 被安全删除释放空间
            self.assertFalse(os.path.exists(f1))
            # j2 因水位已降至 74% 未被删除
            self.assertTrue(os.path.exists(f2))
            # j3（下载中）绝对未被删除！
            self.assertTrue(os.path.exists(f3))


if __name__ == "__main__":
    unittest.main()
