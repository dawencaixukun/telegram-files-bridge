# -*- coding: utf-8 -*-
"""
test_deep_features.py — 系统级进阶特性端到端与边界自动化测试套件
========================================================================================
本测试套件全面覆盖各项系统级新特性的功能逻辑、性能与异常边界：

注：原「特性一：网页内 HTTP 206 视频流式预览与边下边播」相关测试已随该特性
从项目整体移除（/api/media/* 端点与本地播放器前端均已删除）。

1. 特性二：Telegram FloodWait 智能冷却、任务挂起与倒计时自愈
   - 智能异常解析：TDLib 420 FLOOD_WAIT_X、Bot API 429 retry_after、描述文本正则、异常对象
   - 状态机与持久化：0600 文件安全落盘与服务重启恢复（未到期保持冷却、已到期自动置失效）
   - 并发防击穿：多次限流告警时取最大冷却到期时间戳，严防冷却时长被缩短
   - 任务自动挂起：冷却期间提交直投任务拦截为 FLOOD_WAIT_SUSPENDED，零网络请求发送至 Telegram
   - 任务库联动：tasks_all() 自动注入带有沙漏状态与倒计时的挂起任务
   - 状态查询与手动重置：GET /api/tg/floodwait/status 与 POST /api/tg/floodwait/reset
   - 后台定时器自愈：倒计时归零自动唤醒所有挂起任务并恢复调度

2. 特性三：System Doctor 系统健康与依赖一键自检面板
   - 四大组件独立探针：Java Vert.x 后端、TDLib 会话、OpenList 网盘、VPS 本地存储与水位
   - 组件多状态覆盖：healthy, warning, critical 状态分支与针对性修复建议
   - 并发探测与超时隔离：asyncio.gather 并发拉起，单个探针 2.5s 超时熔断，整体诊断 3.0s 内必达
   - 全局健康汇总：基于各组件状态自动计算 overallStatus (healthy/warning/critical)
   - 凭据脱敏安全契约：_mask_secret 掩码过滤，Token 截断，隐藏敏感内网与主机路径
   - API 端点验证：GET /api/system/doctor, GET /api/doctor/check, GET /api/system/doctor/ping

3. 特性四：订阅目录模板高级变量与规则优先级排序匹配
   - 三大高级变量解析：
     * {resolution}：视频分辨率推导（元数据优先、文件名正则识别 4k/1080p/720p/480p、安全回退 unknown）
     * {ext}：小写文件后缀提取（扩展名清洗、媒体类型推导 video->mp4, photo->jpg、兜底 bin）
     * {chat_title}：真实频道名称提取与 _sub_clean_seg 严格清洗（防目录穿越与禁忌字符）
   - 多因子确定性排序：(-priority, -specificity, created_at, id) 算法，精确 chatId 优于通配符 *
   - 规则匹配优先裁决：高优规则优先命中即熔断，停用规则跳过，无匹配回退
   - API 接口验证：POST /api/subscriptions/rule, /api/subscriptions/reorder, /api/subscriptions/preview-template
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


class BaseDeepFeaturesTestCase(unittest.TestCase):
    """测试基类：环境隔离、文件重定向与认证凭据构建。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_deep_test_")
        self.client = TestClient(bridge_server.app)

        # 备份全局关键状态
        self._orig_flood_wait_state = dict(bridge_server._FLOOD_WAIT_STATE)
        self._orig_flood_wait_file = bridge_server._FLOOD_WAIT_FILE
        self._orig_sub_rules = dict(bridge_server._SUB_RULES)
        self._orig_sub_file = bridge_server._SUBS_FILE
        self._orig_tasks_cache = dict(bridge_server._TASKS_CACHE)
        self._orig_waiting_disk = dict(bridge_server._WAITING_DISK_TASKS)
        self._orig_app_root_dir = bridge_server.APP_ROOT_DIR

        # 重定向持久化文件至测试临时目录
        bridge_server._FLOOD_WAIT_FILE = os.path.join(self.tmp_dir, ".test_flood_wait_state.json")
        bridge_server._SUBS_FILE = os.path.join(self.tmp_dir, ".test_subscriptions.json")
        bridge_server.APP_ROOT_DIR = self.tmp_dir

        # 清理内存状态
        bridge_server._reset_flood_wait()
        bridge_server._SUB_RULES.clear()
        bridge_server._TASKS_CACHE = {"expire": 0.0, "value": None}
        bridge_server._WAITING_DISK_TASKS.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        # 恢复状态
        bridge_server._reset_flood_wait()
        bridge_server._FLOOD_WAIT_STATE.clear()
        bridge_server._FLOOD_WAIT_STATE.update(self._orig_flood_wait_state)
        bridge_server._FLOOD_WAIT_FILE = self._orig_flood_wait_file
        bridge_server._SUB_RULES.clear()
        bridge_server._SUB_RULES.update(self._orig_sub_rules)
        bridge_server._SUBS_FILE = self._orig_sub_file
        bridge_server._TASKS_CACHE.clear()
        bridge_server._TASKS_CACHE.update(self._orig_tasks_cache)
        bridge_server._WAITING_DISK_TASKS.clear()
        bridge_server._WAITING_DISK_TASKS.update(self._orig_waiting_disk)
        bridge_server.APP_ROOT_DIR = self._orig_app_root_dir

    def _auth_cookies(self):
        token = bridge_server._make_portal_token()
        csrf = "csrf-deep-test-token-7788"
        cookies = {
            bridge_server.PORTAL_COOKIE: token,
            bridge_server.CSRF_COOKIE: csrf,
        }
        headers = {
            bridge_server.CSRF_HEADER: csrf,
        }
        return cookies, headers


# =====================================================================
# 2. 特性二：Telegram FloodWait 智能冷却、挂起与恢复调度
# =====================================================================
class TestTelegramFloodWaitEngine(BaseDeepFeaturesTestCase):
    """测试 Telegram FloodWait 异常捕获、状态机持久化与自动唤醒。"""

    def test_extract_flood_wait_seconds_all_variants(self):
        """测试异常与响应解析器对各类限流格式的识别。"""
        # TDLib 420 字典
        self.assertEqual(
            bridge_server._extract_flood_wait_seconds({"@type": "error", "code": 420, "message": "FLOOD_WAIT_15"}),
            15
        )
        # Bot API 429 参数字典
        self.assertEqual(
            bridge_server._extract_flood_wait_seconds({"ok": False, "error_code": 429, "parameters": {"retry_after": 45}}),
            45
        )
        # Bot API 描述文本
        self.assertEqual(
            bridge_server._extract_flood_wait_seconds({"error_code": 429, "description": "Too Many Requests: retry after 30"}),
            30
        )
        # 纯文本与字节串
        self.assertEqual(
            bridge_server._extract_flood_wait_seconds("Telegram API response: FLOOD_WAIT_60 required"),
            60
        )
        self.assertEqual(
            bridge_server._extract_flood_wait_seconds(b"Warning: flood wait of 75 seconds"),
            75
        )
        # 异常对象
        self.assertEqual(
            bridge_server._extract_flood_wait_seconds(Exception("TDLib Exception: FLOOD_WAIT_90")),
            90
        )
        # 无限流普通响应与异常
        self.assertIsNone(bridge_server._extract_flood_wait_seconds({"code": 200, "message": "OK"}))
        self.assertIsNone(bridge_server._extract_flood_wait_seconds(ValueError("Invalid file ID")))

    def test_flood_wait_trigger_and_state_persistence(self):
        """测试触发限流冷却、状态机转换与 0600 磁盘文件持久化。"""
        now = time.time()
        until = bridge_server._trigger_flood_wait("account_vip", 45, reason="FLOOD_WAIT_45")
        self.assertTrue(bridge_server._is_flood_wait_active("account_vip"))
        self.assertGreaterEqual(until, now + 44)

        # 检查持久化文件
        self.assertTrue(os.path.exists(bridge_server._FLOOD_WAIT_FILE))
        with open(bridge_server._FLOOD_WAIT_FILE, "r", encoding="utf-8") as f:
            persisted = json.loads(f.read())
        self.assertTrue(persisted["active"])
        self.assertEqual(persisted["account"], "account_vip")
        self.assertEqual(persisted["reason"], "FLOOD_WAIT_45")

    def test_flood_wait_concurrent_merging_anti_breakthrough(self):
        """测试并发多次触发限流时的最大冷却到期合并（防时间被缩短）。"""
        now = time.time()
        until_orig = bridge_server._trigger_flood_wait("default", 60, reason="FLOOD_WAIT_60")

        # 随后到达一个较短的限流 (20s)，不应缩短已有的 60s 冷却
        until_short = bridge_server._trigger_flood_wait("default", 20, reason="FLOOD_WAIT_20")
        self.assertEqual(until_orig, until_short)

        # 随后到达一个更长的限流 (120s)，应扩展冷却时间
        until_long = bridge_server._trigger_flood_wait("default", 120, reason="FLOOD_WAIT_120")
        self.assertGreater(until_long, until_orig)
        self.assertGreaterEqual(until_long, now + 119)

    def test_flood_wait_load_persistence_recovery(self):
        """测试服务重启时从磁盘恢复活跃与已过期的冷却状态。"""
        # 1. 恢复未来尚未到期的冷却
        future_until = time.time() + 300
        with open(bridge_server._FLOOD_WAIT_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "active": True,
                "account": "default",
                "wait_seconds": 300,
                "cooldown_until": future_until,
                "reason": "FLOOD_WAIT_300",
                "suspended_tasks": {}
            }, f)

        bridge_server._flood_wait_load()
        self.assertTrue(bridge_server._is_flood_wait_active())
        self.assertGreater(bridge_server._get_flood_wait_status()["remainingSeconds"], 250)

        # 2. 恢复已过期的冷却 -> 自动置 active=False
        past_until = time.time() - 50
        with open(bridge_server._FLOOD_WAIT_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "active": True,
                "account": "default",
                "wait_seconds": 50,
                "cooldown_until": past_until,
                "reason": "FLOOD_WAIT_OLD",
                "suspended_tasks": {}
            }, f)

        bridge_server._flood_wait_load()
        self.assertFalse(bridge_server._is_flood_wait_active())
        self.assertEqual(bridge_server._get_flood_wait_status()["remainingSeconds"], 0)

    def test_flood_wait_status_api_endpoint(self):
        """测试 GET /api/tg/floodwait/status 接口。"""
        cookies, _ = self._auth_cookies()

        # 未处于限流状态
        bridge_server._reset_flood_wait()
        resp1 = self.client.get("/api/tg/floodwait/status", cookies=cookies)
        self.assertEqual(resp1.status_code, 200)
        d1 = resp1.json()["data"]
        self.assertFalse(d1["isCooling"])
        self.assertEqual(d1["remainingSeconds"], 0)

        # 触发限流
        bridge_server._trigger_flood_wait("default", 55, reason="FLOOD_WAIT_55")
        resp2 = self.client.get("/api/tg/floodwait/status", cookies=cookies)
        self.assertEqual(resp2.status_code, 200)
        d2 = resp2.json()["data"]
        self.assertTrue(d2["isCooling"])
        self.assertGreater(d2["remainingSeconds"], 0)
        self.assertEqual(d2["totalWaitSeconds"], 55)
        self.assertIn("FLOOD_WAIT", d2["reason"])

    def test_quick_download_interception_during_flood_wait(self):
        """测试在 FloodWait 活跃期间，直投下载请求被拦截置入挂起队列且零网络请求。"""
        cookies, headers = self._auth_cookies()
        bridge_server._trigger_flood_wait("default", 60, reason="FLOOD_WAIT_60")

        mock_backend_download = AsyncMock()
        with patch.object(bridge_server.BACKEND, "start_download_multiple", new=mock_backend_download):
            resp = self.client.post(
                "/api/tg/quick-download",
                json={
                    "force": True,
                    "files": [{
                        "fileId": 1001,
                        "chatId": -100123,
                        "messageId": 500,
                        "telegramId": 999,
                        "uniqueId": "uid-fw-intercept-01",
                        "filename": "fw_movie.mp4",
                        "size": 1024000
                    }]
                },
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp.status_code, 200)
            res_data = resp.json()
            self.assertTrue(res_data["ok"])
            self.assertEqual(res_data["code"], "FLOOD_WAIT_SUSPENDED")
            self.assertEqual(res_data["state"], "flood_wait")
            self.assertGreater(res_data["remainingSeconds"], 0)

            # 核心断言：完全不调用底层下载接口，杜绝击穿 Telegram
            mock_backend_download.assert_not_called()

            # 验证任务成功置入 suspended_tasks
            status = bridge_server._get_flood_wait_status()
            self.assertEqual(status["suspendedTasksCount"], 1)

    def test_tasks_all_includes_suspended_flood_wait_tasks(self):
        """测试全局任务列表 tasks_all() 自动展现挂起中的 FloodWait 任务。"""
        bridge_server._trigger_flood_wait("default", 60, reason="FLOOD_WAIT_60")
        suspended = bridge_server._FLOOD_WAIT_STATE.setdefault("suspended_tasks", {})
        suspended["uid-fw-test-99"] = {
            "id": "flood-uid-fw-test-99",
            "uniqueId": "uid-fw-test-99",
            "filename": "suspended_video.mp4",
            "size": 50000,
            "size_str": "48.8 KB",
            "created_at": time.time(),
        }

        async def run():
            with patch("bridge_server._build_tasks", new=AsyncMock(return_value=[])):
                tasks = await bridge_server.tasks_all(force=True)
                fw_tasks = [t for t in tasks if t.get("_unique_id") == "uid-fw-test-99"]
                self.assertEqual(len(fw_tasks), 1)
                t = fw_tasks[0]
                self.assertEqual(t.get("status"), "waiting_disk")
                self.assertIn("风控", t.get("error_msg", ""))
                self.assertEqual(t.get("stages", [])[0]["name"], "风控冷却")
        asyncio.run(run())

    def test_admin_manual_reset_endpoint(self):
        """测试 POST /api/tg/floodwait/reset 管理员手动解除限流并唤醒任务。"""
        cookies, headers = self._auth_cookies()
        bridge_server._trigger_flood_wait("default", 120, reason="FLOOD_WAIT_120")
        suspended = bridge_server._FLOOD_WAIT_STATE.setdefault("suspended_tasks", {})
        suspended["fw-item-1"] = {"id": "item-1", "uniqueId": "fw-item-1"}

        resp = self.client.post("/api/tg/floodwait/reset", json={}, cookies=cookies, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertFalse(bridge_server._is_flood_wait_active())
        self.assertEqual(len(bridge_server._FLOOD_WAIT_STATE["suspended_tasks"]), 0)

    def test_flood_wait_timer_loop_auto_recovery(self):
        """测试倒计时结束后后台定时器自愈并唤醒挂起任务。"""
        bridge_server._trigger_flood_wait("default", 1, reason="FLOOD_WAIT_1")
        suspended = bridge_server._FLOOD_WAIT_STATE.setdefault("suspended_tasks", {})
        suspended["fw-auto-1"] = {"id": "auto-1", "uniqueId": "fw-auto-1"}

        async def run():
            # 等待 1.2 秒至到期
            await asyncio.sleep(1.2)
            await bridge_server._flood_wait_timer_loop()
            self.assertFalse(bridge_server._is_flood_wait_active())
            self.assertEqual(len(bridge_server._FLOOD_WAIT_STATE["suspended_tasks"]), 0)
        asyncio.run(run())


# =====================================================================
# 3. 特性三：System Doctor 系统健康与依赖自检面板
# =====================================================================
class TestSystemDoctorEngine(BaseDeepFeaturesTestCase):
    """测试 System Doctor 各依赖探针、并发执行、3秒超时熔断与凭据脱敏。"""

    def test_doctor_java_backend_probe_states(self):
        """测试 Java Vert.x 后端探针健康、未鉴权与不可达状态。"""
        async def run():
            # 1. 正常已鉴权 -> healthy
            with patch.object(bridge_server.BACKEND, "auth_session", new=AsyncMock(return_value={"authenticated": True})):
                r1 = await bridge_server._doctor_probe_java_backend()
                self.assertEqual(r1["status"], "healthy")
                self.assertTrue(r1["details"]["authenticated"])

            # 2. 服务在线但未登录 -> warning
            with patch.object(bridge_server.BACKEND, "auth_session", new=AsyncMock(return_value={"authenticated": False})):
                r2 = await bridge_server._doctor_probe_java_backend()
                self.assertEqual(r2["status"], "warning")
                self.assertIn("请检查", r2["recommendation"])

            # 3. 连接拒绝/异常 -> critical
            with patch.object(bridge_server.BACKEND, "auth_session", new=AsyncMock(side_effect=ConnectionRefusedError())):
                r3 = await bridge_server._doctor_probe_java_backend()
                self.assertEqual(r3["status"], "critical")
                self.assertFalse(r3["details"]["authenticated"])
        asyncio.run(run())

    def test_doctor_tdlib_probe_states(self):
        """测试 TDLib 客户端会话探针就绪、限流冷却中与未连接状态。"""
        async def run():
            # 1. 正常 Ready -> healthy
            bridge_server._reset_flood_wait()
            with patch.object(bridge_server.BACKEND, "telegram_api", new=AsyncMock(return_value={"@type": "authorizationStateReady"})):
                r1 = await bridge_server._doctor_probe_tdlib()
                self.assertEqual(r1["status"], "healthy")
                self.assertFalse(r1["details"]["isFloodWait"])

            # 2. 处于 FloodWait 状态 -> warning
            bridge_server._trigger_flood_wait("default", 30, reason="FLOOD_WAIT_30")
            with patch.object(bridge_server.BACKEND, "telegram_api", new=AsyncMock(return_value={"@type": "authorizationStateReady"})):
                r2 = await bridge_server._doctor_probe_tdlib()
                self.assertEqual(r2["status"], "warning")
                self.assertTrue(r2["details"]["isFloodWait"])
                self.assertIn("限流冷却", r2["message"])

            # 3. 探针调用异常 -> critical
            bridge_server._reset_flood_wait()
            with patch.object(bridge_server.BACKEND, "telegram_api", new=AsyncMock(side_effect=RuntimeError("TDLib not ready"))):
                r3 = await bridge_server._doctor_probe_tdlib()
                self.assertEqual(r3["status"], "critical")
        asyncio.run(run())

    def test_doctor_openlist_probe_states(self):
        """测试 OpenList 云端网盘探针在线挂载、未挂载与令牌失效状态。"""
        async def run():
            bridge_server._OPENLIST["token"] = "valid_test_token"
            # 1. 令牌有效且挂载 >= 1 -> healthy
            with patch("bridge_server._openlist_ready", new=AsyncMock(return_value=True)), \
                 patch("bridge_server.openlist_dirs", new=AsyncMock(return_value={"ok": True, "dirs": [{"name": "Drive1"}]})):
                r1 = await bridge_server._doctor_probe_openlist()
                self.assertEqual(r1["status"], "healthy")
                self.assertEqual(r1["details"]["mountCount"], 1)

            # 2. 令牌有效但挂载为 0 -> warning
            with patch("bridge_server._openlist_ready", new=AsyncMock(return_value=True)), \
                 patch("bridge_server.openlist_dirs", new=AsyncMock(return_value={"ok": True, "dirs": []})):
                r2 = await bridge_server._doctor_probe_openlist()
                self.assertEqual(r2["status"], "warning")
                self.assertEqual(r2["details"]["mountCount"], 0)

            # 3. 令牌失效 -> critical
            with patch("bridge_server._openlist_ready", new=AsyncMock(return_value=False)):
                r3 = await bridge_server._doctor_probe_openlist()
                self.assertEqual(r3["status"], "critical")
        asyncio.run(run())

    def test_doctor_local_storage_probe_states(self):
        """测试 VPS 本地存储读写权限与磁盘高低水位熔断状态。"""
        async def run():
            # 1. 水位正常 (<85%) -> healthy
            mock_stat_normal = MagicMock(total=100 * 1024**3, free=60 * 1024**3, used=40 * 1024**3)
            with patch("shutil.disk_usage", return_value=mock_stat_normal):
                r1 = await bridge_server._doctor_probe_local_storage()
                self.assertEqual(r1["status"], "healthy")
                self.assertFalse(r1["details"]["isWatermarkMeltdown"])
                self.assertEqual(r1["details"]["usedPercent"], 40.0)

            # 2. 触碰高水位警戒 (85%~95%) -> warning
            mock_stat_warn = MagicMock(total=100 * 1024**3, free=12 * 1024**3, used=88 * 1024**3)
            with patch("shutil.disk_usage", return_value=mock_stat_warn):
                r2 = await bridge_server._doctor_probe_local_storage()
                self.assertEqual(r2["status"], "warning")
                self.assertTrue(r2["details"]["isWatermarkMeltdown"])
                self.assertIn("高水位预警", r2["message"])

            # 3. 目录只读无写权限 -> critical
            with patch("builtins.open", side_effect=PermissionError("Read-only filesystem")):
                r3 = await bridge_server._doctor_probe_local_storage()
                self.assertEqual(r3["status"], "critical")
                self.assertFalse(r3["details"]["writable"])
        asyncio.run(run())

    def test_doctor_concurrency_and_3s_timeout_protection(self):
        """测试 4 探针并发运行，单项探针 2.5s 超时隔离，诊断总耗时严格 < 3.0s。"""
        async def mock_slow_auth(*args, **kwargs):
            await asyncio.sleep(4.0)
            return {"authenticated": True}

        cookies, _ = self._auth_cookies()
        with patch.object(bridge_server.BACKEND, "auth_session", side_effect=mock_slow_auth), \
             patch.object(bridge_server.BACKEND, "telegram_api", new=AsyncMock(return_value={"@type": "authorizationStateReady"})), \
             patch("bridge_server._openlist_ready", new=AsyncMock(return_value=True)), \
             patch("bridge_server.openlist_dirs", new=AsyncMock(return_value={"ok": True, "dirs": [{"name": "Drive1"}]})):

            start_t = time.perf_counter()
            resp = self.client.get("/api/system/doctor", cookies=cookies)
            elapsed = time.perf_counter() - start_t

            self.assertEqual(resp.status_code, 200)
            # 严格断言在 3 秒超时限制内返回（慢探针被 2.5s wait_for 隔离熔断）
            self.assertLess(elapsed, 3.0)
            data = resp.json()["data"]
            self.assertEqual(data["components"]["javaBackend"]["status"], "critical")
            self.assertEqual(data["components"]["tdlib"]["status"], "healthy")
            self.assertEqual(data["components"]["openlist"]["status"], "healthy")
            self.assertEqual(data["components"]["localStorage"]["status"], "healthy")

    def test_doctor_credentials_masking(self):
        """测试敏感密码、密钥与 Token 的脱敏保护契约。"""
        self.assertEqual(bridge_server._mask_secret(""), "")
        self.assertEqual(bridge_server._mask_secret("123"), "****")
        self.assertEqual(bridge_server._mask_secret("1234"), "****")
        self.assertEqual(bridge_server._mask_secret("my_super_secret_password"), "my****rd")

    def test_doctor_ping_endpoint(self):
        """测试 GET /api/system/doctor/ping 轻量存活探测。"""
        cookies, _ = self._auth_cookies()
        resp = self.client.get("/api/system/doctor/ping", cookies=cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["pong"])


# =====================================================================
# 4. 特性四：订阅目录模板高级变量与规则优先级排序匹配
# =====================================================================
class TestSubscriptionAdvancedVariablesAndPriority(BaseDeepFeaturesTestCase):
    """测试订阅高级变量 {resolution}/{ext}/{chat_title} 解析与规则多因子确定性匹配。"""

    def test_template_variable_resolution_meta_and_regex(self):
        """测试 {resolution} 分辨率推导：元数据高度/宽度、文件名正则匹配与兜底 unknown。"""
        ts = 1757000000.0

        # 1.1 从高度/宽度元数据推导
        self.assertEqual(
            bridge_server._render_dir_template("/v/{resolution}", height=2160, width=3840, ts=ts),
            "/v/4k"
        )
        self.assertEqual(
            bridge_server._render_dir_template("/v/{resolution}", height=1080, width=1920, ts=ts),
            "/v/1080p"
        )
        self.assertEqual(
            bridge_server._render_dir_template("/v/{resolution}", height=720, width=1280, ts=ts),
            "/v/720p"
        )
        self.assertEqual(
            bridge_server._render_dir_template("/v/{resolution}", height=480, width=854, ts=ts),
            "/v/480p"
        )

        # 1.2 从文件名正则匹配推导
        self.assertEqual(
            bridge_server._render_dir_template("/v/{resolution}", filename="Dune.Part.Two.2024.2160p.UHD.mkv", ts=ts),
            "/v/2160p"
        )
        self.assertEqual(
            bridge_server._render_dir_template("/v/{resolution}", filename="Spider-Man.4k.HDR.mp4", ts=ts),
            "/v/4k"
        )
        self.assertEqual(
            bridge_server._render_dir_template("/v/{resolution}", filename="Inception_1080p_BluRay.mp4", ts=ts),
            "/v/1080p"
        )
        self.assertEqual(
            bridge_server._render_dir_template("/v/{resolution}", filename="Documentary.720p.avi", ts=ts),
            "/v/720p"
        )
        self.assertEqual(
            bridge_server._render_dir_template("/v/{resolution}", filename="Clip_1920x1080.ts", ts=ts),
            "/v/1080p"
        )
        self.assertEqual(
            bridge_server._render_dir_template("/v/{resolution}", filename="Classic_3840x2160.mov", ts=ts),
            "/v/4k"
        )

        # 1.3 无法识别时的安全回退 unknown
        self.assertEqual(
            bridge_server._render_dir_template("/v/{resolution}", filename="readme_notes.txt", ts=ts),
            "/v/unknown"
        )

    def test_template_variable_ext_extraction(self):
        """测试 {ext} 小写文件后缀提取与基于媒体类型的推导。"""
        ts = 1757000000.0

        # 从文件名提取并小写化
        self.assertEqual(
            bridge_server._render_dir_template("/ext/{ext}", filename="movie.mp4", ts=ts),
            "/ext/mp4"
        )
        self.assertEqual(
            bridge_server._render_dir_template("/ext/{ext}", filename="SAMPLE_VIDEO.MKV", ts=ts),
            "/ext/mkv"
        )
        self.assertEqual(
            bridge_server._render_dir_template("/ext/{ext}", filename="archive.tar.gz", ts=ts),
            "/ext/gz"
        )

        # 无扩展名时基于 ftype 推导
        self.assertEqual(
            bridge_server._render_dir_template("/ext/{ext}", filename="video_without_ext", ftype="video", ts=ts),
            "/ext/mp4"
        )
        self.assertEqual(
            bridge_server._render_dir_template("/ext/{ext}", filename="photo_without_ext", ftype="photo", ts=ts),
            "/ext/jpg"
        )
        self.assertEqual(
            bridge_server._render_dir_template("/ext/{ext}", filename="other_file", ftype="document", ts=ts),
            "/ext/bin"
        )

    def test_template_variable_chat_title_and_cleaning(self):
        """测试 {chat_title} 变量与防路径穿越清洗。"""
        ts = 1757000000.0

        # 正常频道名
        self.assertEqual(
            bridge_server._render_dir_template("/c/{chat_title}", chat_title="科技前沿频道", ts=ts),
            "/c/科技前沿频道"
        )

        # 包含禁忌字符（\/:*?"<>|）时自动清洗
        dirty_title = '科技/前沿\\精选:*?"<>|合集'
        out = bridge_server._render_dir_template("/c/{chat_title}", chat_title=dirty_title, ts=ts)
        self.assertTrue(out.startswith("/c/"))
        seg = out.split("/c/", 1)[1]
        for bad_char in '/\\:*?"<>|':
            self.assertNotIn(bad_char, seg)

        # 空频道名回退为「未分类」
        self.assertEqual(
            bridge_server._render_dir_template("/c/{chat_title}", chat_title="", source="", ts=ts),
            "/c/未分类"
        )

    def test_template_combination_and_security(self):
        """测试组合多变量模板渲染与未知变量/非法路径拒绝。"""
        ts = 1757000000.0
        lt = time.localtime(ts)
        ym = "%04d-%02d" % (lt.tm_year, lt.tm_mon)

        tpl = "/归档/{chat_title}/{YYYY-MM}/{resolution}/{ext}"
        out = bridge_server._render_dir_template(
            tpl,
            chat_title="纪录片放映室",
            filename="Nature.4K.HEVC.mkv",
            ftype="video",
            ts=ts
        )
        self.assertEqual(out, f"/归档/纪录片放映室/{ym}/4k/mkv")

        # 未知变量返回 None
        self.assertIsNone(bridge_server._render_dir_template("/a/{unknown_var}"))
        # 相对路径（非 / 开头）返回 None
        self.assertIsNone(bridge_server._render_dir_template("relative/{chat_title}", chat_title="A"))
        # 路径穿越 .. 返回 None
        self.assertIsNone(bridge_server._render_dir_template("/a/../b/{ext}", ext="mp4"))

    def test_rule_multi_factor_deterministic_sorting(self):
        """测试多因子确定性排序：(-priority, -specificity, created_at, id)。"""
        r_low_wild = {
            "id": "r_low_wild", "priority": 10, "chatId": "*", "createdAt": 100.0, "enabled": True
        }
        r_low_exact = {
            "id": "r_low_exact", "priority": 10, "chatId": "-1001", "createdAt": 100.0, "enabled": True
        }
        r_high_wild = {
            "id": "r_high_wild", "priority": 50, "chatId": "*", "createdAt": 100.0, "enabled": True
        }
        r_high_exact = {
            "id": "r_high_exact", "priority": 50, "chatId": "-1002", "createdAt": 100.0, "enabled": True
        }

        bridge_server._SUB_RULES["r_low_wild"] = r_low_wild
        bridge_server._SUB_RULES["r_low_exact"] = r_low_exact
        bridge_server._SUB_RULES["r_high_wild"] = r_high_wild
        bridge_server._SUB_RULES["r_high_exact"] = r_high_exact

        sorted_rules = bridge_server._sub_rules_sorted()
        expected_ids = ["r_high_exact", "r_high_wild", "r_low_exact", "r_low_wild"]
        self.assertEqual([r["id"] for r in sorted_rules], expected_ids)

    def test_rule_matching_priority_overrides(self):
        """测试规则匹配：当多个规则匹配同一任务时，高优先级规则优先命中。"""
        r_general = {
            "id": "r_general", "telegramId": 100, "chatId": "*",
            "priority": 0, "dirTemplate": "/常规归档", "enabled": True
        }
        r_vip = {
            "id": "r_vip", "telegramId": 100, "chatId": "-100888",
            "priority": 100, "dirTemplate": "/VIP归档/{resolution}", "enabled": True
        }

        bridge_server._SUB_RULES["r_general"] = r_general
        bridge_server._SUB_RULES["r_vip"] = r_vip

        task = {
            "_telegram_id": 100,
            "_chat_id": "-100888",
            "filename": "video_1080p.mp4",
        }

        matched = bridge_server._sub_match_rule(task)
        self.assertIsNotNone(matched)
        self.assertEqual(matched["id"], "r_vip")

        # 若 VIP 规则停用，则自动回退到普通规则
        r_vip["enabled"] = False
        matched2 = bridge_server._sub_match_rule(task)
        self.assertEqual(matched2["id"], "r_general")

    def test_api_subscriptions_rule_add_update_reorder(self):
        """测试订阅规则创建、更新与重排 API。"""
        cookies, headers = self._auth_cookies()

        mock_sources = [{"telegramId": 100, "chatId": "-1009999", "title": "电影专区"}]
        with patch("bridge_server.chat_sources", new=AsyncMock(return_value=mock_sources)):
            # 1. 创建新规则 POST /api/subscriptions/rule
            rule_payload = {
                "telegramId": 100,
                "chatId": "-1009999",
                "chatTitle": "电影专区",
                "priority": 80,
                "dirTemplate": "/电影/{chat_title}/{resolution}",
                "deleteLocal": True,
                "policy": "skip",
                "enabled": True,
            }
            resp_add = self.client.post(
                "/api/subscriptions/rule",
                json=rule_payload,
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_add.status_code, 200)
            res_add_json = resp_add.json()
            self.assertTrue(res_add_json.get("ok"), res_add_json.get("message"))
            rule_id = res_add_json["rule"]["id"]
            self.assertEqual(bridge_server._SUB_RULES[rule_id]["priority"], 80)

            # 2. 更新已有规则优先级 POST /api/subscriptions/rule
            update_payload = {
                "id": rule_id,
                "priority": 120,
                "dirTemplate": "/电影/{chat_title}/{resolution}/{ext}",
            }
            resp_up = self.client.post(
                "/api/subscriptions/rule",
                json=update_payload,
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_up.status_code, 200)
            self.assertEqual(bridge_server._SUB_RULES[rule_id]["priority"], 120)

            # 3. 批量重排优先级 POST /api/subscriptions/reorder
            reorder_payload = {
                "orders": [{"id": rule_id, "priority": 200}]
            }
            resp_re = self.client.post(
                "/api/subscriptions/reorder",
                json=reorder_payload,
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_re.status_code, 200)
            self.assertEqual(bridge_server._SUB_RULES[rule_id]["priority"], 200)

    def test_api_subscriptions_preview_template(self):
        """测试模板实时演算端点 POST /api/subscriptions/preview-template。"""
        cookies, headers = self._auth_cookies()

        preview_payload = {
            "dirTemplate": "/归档/{chat_title}/{resolution}/{ext}",
            "sample": {
                "chatTitle": "动漫频道",
                "filename": "OnePiece_1080p.mp4",
                "type": "video",
                "width": 1920,
                "height": 1080,
            }
        }
        resp = self.client.post(
            "/api/subscriptions/preview-template",
            json=preview_payload,
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["previewDir"], "/归档/动漫频道/1080p/mp4")

        # 非法未知变量
        resp_err = self.client.post(
            "/api/subscriptions/preview-template",
            json={"dirTemplate": "/归档/{bad_var}"},
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(resp_err.status_code, 200)
        self.assertFalse(resp_err.json()["ok"])


if __name__ == "__main__":
    unittest.main()
