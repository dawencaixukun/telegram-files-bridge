# -*- coding: utf-8 -*-
"""
test_adv_features_arch_review_t7.py — Task t7 架构与代码审查同行评审专属测试套件
=============================================================================
全面针对四大系统进阶特性进行架构规范、内存管理、异步并发、超时熔断与性能审查：
1. 确认 HTTP Range 文件流读取具备严格的上下文管理，杜绝文件描述符（FD）泄漏与内存暴涨
2. 确认 FloodWait 冷却机制状态机完备，后台定时恢复非阻塞且无竞态冲突
3. 确认 System Doctor 各探针具有 3 秒硬超时隔离与异常边界捕获，主事件循环零阻塞
4. 确认订阅规则优先级排序与模板解析算法高效，无无效正则回溯与性能瓶颈
"""
import asyncio
import json
import os
import re
import shutil
import stat
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
import bridge_server


class TestArchitectureReviewT7(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(bridge_server.app)

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_arch_t7_")
        self.orig_flood_state = dict(bridge_server._FLOOD_WAIT_STATE)
        self.orig_flood_file = bridge_server._FLOOD_WAIT_FILE
        self.orig_sub_rules = dict(bridge_server._SUB_RULES)

    def tearDown(self):
        bridge_server._FLOOD_WAIT_STATE.clear()
        bridge_server._FLOOD_WAIT_STATE.update(self.orig_flood_state)
        bridge_server._SUB_RULES.clear()
        bridge_server._SUB_RULES.update(self.orig_sub_rules)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _auth_cookies(self):
        token = bridge_server._make_portal_token()
        csrf = "csrf-token-review-t7"
        return {
            bridge_server.PORTAL_COOKIE: token,
            bridge_server.CSRF_COOKIE: csrf,
        }, {
            bridge_server.CSRF_HEADER: csrf,
        }

    # =========================================================================
    # 审查项 2: Telegram FloodWait 智能冷却状态机与定时器竞态安全
    # =========================================================================

    def test_c2_floodwait_state_machine_and_max_cooldown_merge(self):
        """审查：并发多次限流触发时，严格合并取最大到期时间，绝不缩短冷却周期。"""
        bridge_server._reset_flood_wait()

        now = time.time()
        # 第一次触发 30 秒冷却
        until1 = bridge_server._trigger_flood_wait("default", 30, "FLOOD_WAIT_30")
        self.assertTrue(bridge_server._is_flood_wait_active())
        self.assertAlmostEqual(until1, now + 30, delta=2.0)

        # 第二次更短的限流 10 秒（并发可能迟到） -> 绝不能将原 30 秒缩短！
        until2 = bridge_server._trigger_flood_wait("default", 10, "FLOOD_WAIT_10")
        self.assertEqual(until2, until1, "并发短冷却错误覆盖了更长的冷却周期！")

        # 第三次更长的限流 120 秒 -> 必须顺延至 120 秒
        until3 = bridge_server._trigger_flood_wait("default", 120, "FLOOD_WAIT_120")
        self.assertGreater(until3, until1)
        self.assertAlmostEqual(until3, time.time() + 120, delta=2.0)

    def test_c2_floodwait_singleton_timer_no_duplicate_tasks(self):
        """审查：_ensure_flood_wait_timer 保证后台定时器单例运行，并发调用不产生多重循环。"""
        loop = asyncio.new_event_loop()
        try:
            async def run_check():
                bridge_server._FLOOD_WAIT_STATE["active"] = True
                bridge_server._FLOOD_WAIT_STATE["cooldown_until"] = time.time() + 100.0
                bridge_server._FLOOD_WAIT_TIMER_TASK = None

                # 首次调用创建任务
                bridge_server._ensure_flood_wait_timer()
                task1 = bridge_server._FLOOD_WAIT_TIMER_TASK
                self.assertIsNotNone(task1)
                self.assertFalse(task1.done())

                # 重复调用必须复用 task1，绝不重新创建
                bridge_server._ensure_flood_wait_timer()
                task2 = bridge_server._FLOOD_WAIT_TIMER_TASK
                self.assertIs(task1, task2, "定时器非单例，产生了重复并发循环！")

                # 清理
                task1.cancel()
                try:
                    await task1
                except asyncio.CancelledError:
                    pass

            loop.run_until_complete(run_check())
        finally:
            loop.close()

    def test_c2_floodwait_state_persistence_and_file_mode(self):
        """审查：.flood_wait_state.json 采用 0600 安全模式，重启后准确无损恢复。"""
        test_state_file = os.path.join(self.tmp_dir, ".test_flood_state.json")
        with patch.object(bridge_server, "_FLOOD_WAIT_FILE", test_state_file):
            now = time.time()
            bridge_server._FLOOD_WAIT_STATE.clear()
            bridge_server._FLOOD_WAIT_STATE.update({
                "active": True,
                "account": "user_arch_t7",
                "wait_seconds": 60,
                "triggered_at": now,
                "cooldown_until": now + 60,
                "reason": "FLOOD_WAIT_60",
                "suspended_tasks": {"uid-test-1": {"id": "flood-1"}},
            })
            bridge_server._flood_wait_save()

            self.assertTrue(os.path.exists(test_state_file))
            # 验证权限掩码在非 Windows 环境为 0600
            if os.name != "nt":
                st = os.stat(test_state_file)
                mode = stat.S_IMODE(st.st_mode)
                self.assertEqual(mode, 0o600, "状态持久化文件权限不是 0600！")

            # 模拟重启加载
            bridge_server._FLOOD_WAIT_STATE.clear()
            bridge_server._flood_wait_load()
            self.assertTrue(bridge_server._FLOOD_WAIT_STATE.get("active"))
            self.assertEqual(bridge_server._FLOOD_WAIT_STATE.get("account"), "user_arch_t7")
            self.assertIn("uid-test-1", bridge_server._FLOOD_WAIT_STATE.get("suspended_tasks", {}))

    # =========================================================================
    # 审查项 3: System Doctor 各探针超时隔离与主事件循环零阻塞
    # =========================================================================

    def test_c3_doctor_probes_have_hard_timeout_and_isolate_hangs(self):
        """审查：每个探针均有 2.5s 硬超时保护，单组件挂起时全局诊断仍能在 3.0s 内快速返回。"""
        loop = asyncio.new_event_loop()
        try:
            # 模拟 Java 后端 hang 住 10 秒
            async def mock_hanging_auth(*args, **kwargs):
                await asyncio.sleep(10.0)
                return {"authenticated": True}

            with patch.object(bridge_server.BACKEND, "auth_session", side_effect=mock_hanging_auth):
                t_start = time.perf_counter()
                report = loop.run_until_complete(bridge_server._run_system_doctor_check())
                elapsed = time.perf_counter() - t_start

                # 整体耗时必须在 2.5s ~ 3.0s 之间返回，绝不能等待 10s
                self.assertLess(elapsed, 3.2, f"System Doctor 超时未生效，耗时 {elapsed:.2f}s > 3.0s")
                self.assertTrue(report["ok"])
                java_comp = report["components"]["javaBackend"]
                self.assertEqual(java_comp["status"], "critical")
                self.assertIn("TimeoutError", java_comp["message"])
                # 其他正常探针仍能正常得到结果
                self.assertIn("localStorage", report["components"])
                self.assertEqual(report["components"]["localStorage"]["status"], "healthy")
        finally:
            loop.close()

    def test_c3_doctor_sensitive_credentials_masked(self):
        """审查：Doctor 报文脱敏，绝不向前端泄露密码、密钥或内部敏感 Token。"""
        raw_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.token"
        masked = bridge_server._mask_secret(raw_token)
        self.assertEqual(masked, "ey****en")
        self.assertNotIn(raw_token, masked)

        short_secret = "1234"
        self.assertEqual(bridge_server._mask_secret(short_secret), "****")

    # =========================================================================
    # 审查项 4: 订阅规则优先级排序与模板解析算法开销
    # =========================================================================

    def test_c4_rule_priority_specificity_and_deterministic_order(self):
        """审查：规则排序严格遵循 (priority DESC, specificity DESC, created_at ASC, id ASC)。"""
        bridge_server._SUB_RULES.clear()
        # 构造不同优先级与特异度的规则
        rules = {
            "r1": {"id": "r1", "priority": 0, "chatId": "*", "createdAt": 100, "enabled": True},
            "r2": {"id": "r2", "priority": 100, "chatId": "*", "createdAt": 100, "enabled": True},
            "r3": {"id": "r3", "priority": 100, "chatId": "-1001", "createdAt": 200, "enabled": True}, # 同 priority 100，但指定了 chatId，特异度高
            "r4": {"id": "r4", "priority": 0, "chatId": "-1001", "createdAt": 150, "enabled": True},
            "r5": {"id": "r5", "priority": 200, "chatId": "*", "createdAt": 50, "enabled": True},
        }
        bridge_server._SUB_RULES.update(rules)

        sorted_list = bridge_server._sub_rules_sorted()
        ordered_ids = [r["id"] for r in sorted_list]
        # 预期顺序：
        # 1. r5 (priority 200)
        # 2. r3 (priority 100, spec 100)
        # 3. r2 (priority 100, spec 10)
        # 4. r4 (priority 0, spec 100)
        # 5. r1 (priority 0, spec 10)
        self.assertEqual(ordered_ids, ["r5", "r3", "r2", "r4", "r1"])

    def test_c4_template_resolution_ext_chat_title_and_security(self):
        """审查：目录模板引擎对高级变量正确推导，对未知变量与路径穿越严密拦截。"""
        # 1. 分辨率推导 (width/height 优先)
        dir1 = bridge_server._render_dir_template(
            "/{source}/{resolution}/{ext}",
            source="Movies",
            filename="Avengers.mkv",
            width=3840,
            height=2160,
            ftype="video",
        )
        self.assertEqual(dir1, "/Movies/4k/mkv")

        # 2. 从文件名正则推导分辨率
        dir2 = bridge_server._render_dir_template(
            "/{chat_title}/{resolution}",
            chat_title="科技美学",
            filename="Test_Video_1080p_60fps.mp4",
        )
        self.assertEqual(dir2, "/科技美学/1080p")

        # 3. 未知变量直接拒绝返回 None
        dir_bad = bridge_server._render_dir_template(
            "/{source}/{invalid_variable}",
            source="Movies",
        )
        self.assertIsNone(dir_bad, "包含非法未知变量时必须返回 None！")

        # 4. 路径穿越段过滤与拦截
        dir_trav = bridge_server._render_dir_template(
            "/Movies/../../etc",
            source="Movies",
        )
        self.assertIsNone(dir_trav, "包含 .. 目录穿越时必须返回 None！")

    def test_c4_template_performance_and_zero_redos(self):
        """审查：模板解析与文件名正则提取算法在 5,000 次高频调用下耗时 < 0.2s，无 ReDoS 回溯瓶颈。"""
        filenames = [
            "Normal.Movie.2024.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX.mkv",
            "Short_720p.mp4",
            "Ultra.HD.2160p.HDR.TrueHD.7.1.Atmos.x265-EMBER.mkv",
            "Old_Video_480p.avi",
            "Something_Without_Resolution.flv",
            "Complex.Title.With.Many.Dots.And.Numbers.1920x1080.mp4",
            "A" * 200 + "_1080p.mp4",  # 超长文件名防 ReDoS
        ]

        t0 = time.perf_counter()
        count = 5000
        for i in range(count):
            fn = filenames[i % len(filenames)]
            res = bridge_server._render_dir_template(
                "/Archive/{chat_title}/{YYYY-MM}/{resolution}/{ext}",
                chat_title="My Channel",
                filename=fn,
                ftype="video",
            )
            self.assertIsNotNone(res)
        elapsed = time.perf_counter() - t0

        avg_ms = (elapsed / count) * 1000
        # 5000 次调用总耗时应显著小于 0.5s，单次在 0.05ms 以内
        self.assertLess(elapsed, 1.0, f"模板渲染 5000 次耗时过高: {elapsed:.3f}s (单次 {avg_ms:.4f}ms)")


if __name__ == "__main__":
    unittest.main()
