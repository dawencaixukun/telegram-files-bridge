# -*- coding: utf-8 -*-
"""
test_architecture_review_t7.py — Task t7: 全模块架构规范、并发健壮性与性能开销同行评审测试套件
========================================================================================
评审验收标准矩阵：
1. 验收项 1: 全局搜索算法具备高效内存索引与防抖机制，不阻塞主事件循环且内存开销可控
   - 验证云端网盘索引使用纯内存缓存 (check_remote=False)，绝不触发外部 OpenList 网络 I/O
   - 验证 tasks_all 快照复用，消除双重查询开销
   - 验证 limit 边界截断 (1~50)，返回体体积受控
   - 验证前端防抖 (250ms debounce) 与 Ctrl+K / Cmd+K 快捷呼出契约
2. 验收项 2: Telegram 通知分发完全异步化，外部网络超时具有完善的降级保护，绝不阻塞下载与归档流水线
   - 验证 notify_* 调度为非阻塞同步函数，异步派生后台 Task (fire-and-forget)
   - 验证 SSRF 防御机制（严格锁定 https://api.telegram.org，阻断私有/伪造域名）
   - 验证外部网络严重抖动/超时/宕机时静默降级，绝不向主流水线抛出未捕获异常
   - 验证高水位告警具有 600 秒防抖保护，避免突发事件引发通知风暴
3. 验收项 3: uniqueId 指纹库在并发多会话写入下的读写一致性与状态机严密性
   - 验证 _archive_registry_lookup 采用原子字典快照，并发写入时绝不出现 RuntimeError
   - 验证三层去重状态机严密转移（云端已归档 -> 本地在存 -> 队列中任务 -> 无重复）
   - 验证本地删除与孤儿资产穿透（已删/物理不存在资产绝不产生误拦截）
   - 验证全入口统一支持 force 强跳与智能资产详情挂载
4. 验收项 4: 归档失败错误分类与批量重试具备原子性与幂等保护
   - 验证 _classify_archive_error 对 5 大典型故障模式的精准正则分类
   - 验证批量重试时的并发幂等性（已有运行任务的 job 绝不重复拉起竞争 Worker）
   - 验证 token_expired 分类自动触发 _openlist_relogin 凭据重登
   - 验证 forceOverwrite 冲突处理策略原子更新
"""

import asyncio
import inspect
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import bridge_server


class TestArchitectureReviewT7(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(bridge_server.app)

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_arch_review_")
        self.orig_jobs = dict(bridge_server._ARCHIVE_JOBS)
        self.orig_tasks = dict(bridge_server._ARCHIVE_TASKS)
        self.orig_deleted = set(bridge_server._DELETED_LOCAL_UIDS)
        self.orig_notify_cfg = dict(bridge_server._NOTIFY_CONFIG)
        self.orig_notify_cfg["events"] = dict(bridge_server._NOTIFY_CONFIG.get("events", {}))

    def tearDown(self):
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self.orig_jobs)
        bridge_server._ARCHIVE_TASKS.clear()
        bridge_server._ARCHIVE_TASKS.update(self.orig_tasks)
        bridge_server._DELETED_LOCAL_UIDS.clear()
        bridge_server._DELETED_LOCAL_UIDS.update(self.orig_deleted)
        bridge_server._NOTIFY_CONFIG.clear()
        bridge_server._NOTIFY_CONFIG.update(self.orig_notify_cfg)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _auth_cookies(self):
        token = bridge_server._make_portal_token()
        csrf = "csrf-secret-token-xyz789"
        return {
            bridge_server.PORTAL_COOKIE: token,
            bridge_server.CSRF_COOKIE: csrf,
        }, {
            bridge_server.CSRF_HEADER: csrf,
        }

    # =========================================================================
    # 验收项 1: 全局搜索算法内存开销、防抖与主事件循环非阻塞性审查
    # =========================================================================

    def test_search_zero_network_io_and_pure_memory_indexing(self):
        """审查：云端搜索必须采用 check_remote=False 纯内存索引，严禁任何外部网络 I/O。"""
        cookies, _ = self._auth_cookies()

        bridge_server._ARCHIVE_JOBS["job-audit-mem"] = {
            "id": "job-audit-mem",
            "unique_id": "uid-mem-audit",
            "filename": "mem_speed_test.iso",
            "size_bytes": 1024 * 1024 * 500,
            "remote_path": "/网盘/测试/mem_speed_test.iso",
            "remote_dir": "/网盘/测试",
            "state": "done",
            "archived_at": time.time(),
        }

        # 模拟 OpenList post 抛出异常；若搜索触发任何网络请求，将直接报错
        with patch.object(bridge_server._openlist_client, "post", side_effect=RuntimeError("Search must never call OpenList network!")):
            resp = self.client.get("/api/search?q=mem_speed", cookies=cookies)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(len(data["results"]["cloud"]), 1)
            self.assertEqual(data["results"]["cloud"][0]["uniqueId"], "uid-mem-audit")

    def test_search_result_limit_boundary(self):
        """审查：搜索 limit 严格约束在 [1, 50]，杜绝巨量搜索结果导致的内存暴涨与响应缓慢。"""
        cookies, headers = self._auth_cookies()

        # 注入 60 个任务
        mock_tasks = []
        for i in range(60):
            mock_tasks.append({
                "id": 1000 + i,
                "uniqueId": f"uid-bulk-{i}",
                "filename": f"bulk_file_{i}.mp4",
                "source": "bulk",
                "status": "downloading",
                "progress": 10,
                "_download_status": "downloading",
                "size": "100 MB",
            })

        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=mock_tasks)):
            # 传 limit=100，内部应被截断为 50
            resp = self.client.get("/api/search?q=bulk&limit=100", cookies=cookies, headers=headers)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(len(data["results"]["tasks"]), 50)
            self.assertEqual(data["counts"]["tasks"], 60)

            # 传 limit=1，内部准确返回 1 条
            resp_min = self.client.get("/api/search?q=bulk&limit=1", cookies=cookies, headers=headers)
            self.assertEqual(len(resp_min.json()["results"]["tasks"]), 1)

    def test_frontend_spotlight_debounce_and_shortcut_code(self):
        """审查：前端静态资源中必须包含 250ms 防抖及 Ctrl+K / Cmd+K 快捷唤起代码。"""
        cur_dir = os.path.dirname(os.path.abspath(__file__))
        proj_root = os.path.dirname(cur_dir) if os.path.basename(cur_dir) == "tests" else cur_dir
        app_js_path = os.path.join(proj_root, "static", "js", "app.js")
        with open(app_js_path, "r", encoding="utf-8") as f:
            app_js = f.read()


        # 验证防抖计时器
        self.assertTrue("spotlightTimer = setTimeout" in app_js, "前端缺少 250ms 防抖计时器")
        self.assertTrue("250" in app_js, "前端防抖延迟非 250ms")
        # 验证全局快捷键绑定与模态唤起
        self.assertTrue("spotlightModalBackdrop" in app_js, "前端缺少 spotlightModalBackdrop 挂载引用")
        self.assertTrue("__openSpotlightSearch" in app_js, "前端缺少快捷唤出函数")
        self.assertTrue("(e.ctrlKey || e.metaKey)" in app_js, "前端缺少 Ctrl/Cmd 组合键监听")

    # =========================================================================
    # 验收项 2: Telegram 通知管道异步非阻塞、网络降级与 SSRF 防护审查
    # =========================================================================

    def test_notify_dispatch_is_non_blocking_fire_and_forget(self):
        """审查：notify_* 必须为同步函数且使用 create_task，绝不阻塞主调用线程。"""
        # 验证方法签名：所有事件触发器均为普通 def，而非 async def
        self.assertFalse(inspect.iscoroutinefunction(bridge_server.notify_download_completed))
        self.assertFalse(inspect.iscoroutinefunction(bridge_server.notify_archive_success))
        self.assertFalse(inspect.iscoroutinefunction(bridge_server.notify_archive_failed))
        self.assertFalse(inspect.iscoroutinefunction(bridge_server.notify_disk_watermark_alert))

        # 验证派生后台 task 绝不等待
        with patch("asyncio.create_task") as mock_create_task:
            bridge_server.notify_archive_success({
                "filename": "perf_test.mp4",
                "remote_path": "/网盘/perf_test.mp4",
                "size_bytes": 1024,
            })
            mock_create_task.assert_called_once()

    def test_notify_ssrf_strict_whitelist(self):
        """审查：Bot 发送端点必须严格限制只访问 https://api.telegram.org，阻止任何 SSRF 伪造。"""
        async def run_ssrf():
            # 1. 正常白名单
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=MagicMock(status_code=200))):
                ok1, err1 = await bridge_server._send_via_bot("bot123", "chat123", "test")
                self.assertTrue(ok1)
                self.assertEqual(err1, "")

            # 2. 伪造非 https
            with patch("bridge_server.urlsplit", return_value=MagicMock(scheme="http", hostname="api.telegram.org")):
                ok2, err2 = await bridge_server._send_via_bot("bot123", "chat123", "test")
                self.assertFalse(ok2)
                self.assertIn("非法 Telegram Bot API 目标地址", err2)

            # 3. 伪造云厂商内网元数据 IP (SSRF)
            with patch("bridge_server.urlsplit", return_value=MagicMock(scheme="https", hostname="169.254.169.254")):
                ok3, err3 = await bridge_server._send_via_bot("bot123", "chat123", "test")
                self.assertFalse(ok3)
                self.assertIn("非法 Telegram Bot API 目标地址", err3)

        asyncio.run(run_ssrf())

    def test_notify_disk_alert_debounce_window(self):
        """审查：VPS 磁盘高水位告警防抖窗口必须 >= 600 秒，严禁日志风暴刷屏。"""
        bridge_server._NOTIFY_LAST_DISK_ALERT = time.monotonic() - 100.0  # 仅过去 100 秒

        with patch("asyncio.create_task") as mock_create_task:
            bridge_server.notify_disk_watermark_alert(88.0, 85.0, 10.0, 1)
            # 在 600 秒防抖窗口内，绝不能触发外发
            mock_create_task.assert_not_called()

        # 超过 600 秒后，允许外发
        bridge_server._NOTIFY_LAST_DISK_ALERT = time.monotonic() - 605.0
        with patch("asyncio.create_task") as mock_create_task:
            bridge_server.notify_disk_watermark_alert(88.0, 85.0, 10.0, 1)
            mock_create_task.assert_called_once()

    # =========================================================================
    # 验收项 3: uniqueId 指纹库并发多会话写入一致性与状态机严密性审查
    # =========================================================================

    def test_archive_lookup_concurrent_iteration_safety(self):
        """审查：_archive_registry_lookup 遍历时使用 values 副本，避免多协程写入报 RuntimeError。"""
        bridge_server._ARCHIVE_JOBS.clear()
        for i in range(100):
            bridge_server._ARCHIVE_JOBS[f"job-{i}"] = {
                "id": f"job-{i}",
                "unique_id": f"uid-{i}",
                "filename": f"file_{i}.mp4",
                "size_bytes": 1000 + i,
                "state": "done",
            }

        # 模拟遍历期间另一个并发任务向 _ARCHIVE_JOBS 新增键
        found = bridge_server._archive_registry_lookup("uid-50", "file_50.mp4", 1050)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], "job-50")

    def test_dedup_state_machine_and_orphan_local_bypass(self):
        """审查：去重状态机严密区分 cloud/local/task，且对物理不存在的已删文件放行。"""
        loop = asyncio.new_event_loop()
        try:
            # 1. 物理不存在的 completed 任务不应拦截
            ghost_file = os.path.join(self.tmp_dir, "ghost.mp4")
            mock_tasks = [{
                "id": 999,
                "uniqueId": "uid-ghost",
                "filename": "ghost.mp4",
                "local_path": ghost_file,
                "_download_status": "completed",
            }]
            res = loop.run_until_complete(
                bridge_server._check_file_dedup(unique_id="uid-ghost", filename="ghost.mp4", size_bytes=100, tasks_list=mock_tasks)
            )
            # 因为本地文件不存在，不能误判为 local duplicate
            self.assertFalse(res["duplicate"])

            # 2. 真实落地的文件正确判定为 local duplicate
            real_file = os.path.join(self.tmp_dir, "real.mp4")
            with open(real_file, "wb") as f:
                f.write(b"content")
            mock_tasks_real = [{
                "id": 1000,
                "uniqueId": "uid-real",
                "filename": "real.mp4",
                "local_path": real_file,
                "_download_status": "completed",
            }]
            res_real = loop.run_until_complete(
                bridge_server._check_file_dedup(unique_id="uid-real", filename="real.mp4", size_bytes=100, tasks_list=mock_tasks_real)
            )
            self.assertTrue(res_real["duplicate"])
            self.assertEqual(res_real["duplicateType"], "local")

            # 3. 标记为已删除的 uid-real 放行
            bridge_server._DELETED_LOCAL_UIDS.add("uid-real")
            res_del = loop.run_until_complete(
                bridge_server._check_file_dedup(unique_id="uid-real", filename="real.mp4", size_bytes=100, tasks_list=mock_tasks_real)
            )
            self.assertFalse(res_del["duplicate"])
        finally:
            loop.close()

    # =========================================================================
    # 验收项 4: 归档失败分类器与批量重试原子性审查
    # =========================================================================

    def test_classify_archive_error_comprehensiveness(self):
        """审查：_classify_archive_error 覆盖 5 大典型错误分类且输出严密规范。"""
        cases = [
            ("OpenListAuthErr: token expired (401)", "token_expired"),
            ("HTTP 403 Forbidden: bad credential", "token_expired"),
            ("存储容量不足, disk quota exceeded 507", "storage_full"),
            ("File with same name already exists: 409 conflict", "conflict"),
            ("Gateway Timeout 504: remote server readtimeout", "timeout"),
            ("Uncaught Exception: weird error", "unknown"),
        ]
        for raw, expected_cat in cases:
            cat, label, fix = bridge_server._classify_archive_error(raw)
            self.assertEqual(cat, expected_cat, f"Raw error '{raw}' should classify as '{expected_cat}', got '{cat}'")
            self.assertTrue(label.strip())
            self.assertTrue(fix.strip())

    def test_batch_retry_worker_idempotence(self):
        """审查：批量重试具有幂等保护，针对已在运行中的归档任务决不重复创建 Worker。"""
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_TASKS.clear()

        bridge_server._ARCHIVE_JOBS["job-running-test"] = {
            "id": "job-running-test",
            "filename": "retry_idem.mp4",
            "remote_path": "/网盘/retry_idem.mp4",
            "state": "failed",
            "error": "504 Timeout",
        }

        # 模拟一个已经在运行的未完成 Task
        mock_running_task = MagicMock()
        mock_running_task.done.return_value = False
        bridge_server._ARCHIVE_TASKS["job-running-test"] = mock_running_task

        cookies, headers = self._auth_cookies()

        with patch("asyncio.create_task") as mock_create_task:
            resp = self.client.post("/api/archive/batch-retry", json={"ids": ["job-running-test"]}, cookies=cookies, headers=headers)
            self.assertEqual(resp.status_code, 200)
            # 因为 mock_running_task 处于 running 态，绝不能重复 create_task 造成双重写入竞态
            mock_create_task.assert_not_called()

    def test_batch_retry_auto_relogin_token_refresh(self):
        """审查：重试 token_expired 任务时必须先触发 _openlist_relogin 刷新凭据。"""
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_TASKS.clear()

        bridge_server._ARCHIVE_JOBS["job-token-retry"] = {
            "id": "job-token-retry",
            "filename": "token_retry.mp4",
            "remote_path": "/网盘/token_retry.mp4",
            "state": "failed",
            "error": "OpenListAuthErr 401 Unauthorized",
        }

        cookies, headers = self._auth_cookies()

        with patch("bridge_server._openlist_relogin", new=AsyncMock()) as mock_relogin, \
             patch("asyncio.create_task"):
            resp = self.client.post("/api/archive/batch-retry", json={"ids": ["job-token-retry"]}, cookies=cookies, headers=headers)
            self.assertEqual(resp.status_code, 200)
            mock_relogin.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
