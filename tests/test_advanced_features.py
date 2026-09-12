# -*- coding: utf-8 -*-
"""
test_advanced_features.py — 四大高级产品特性端到端与边界自动化测试套件
========================================================================================
测试目标与覆盖矩阵：
1. 特性一：全局 uniqueId 下载防重与智能关联网盘
   - Level 1: 云端网盘归档精确匹配 (unique_id) 与备选内容指纹匹配 (name_size)，验证资产位置与直达链接
   - Level 2: 本地在存资产判定 (tasks_all, completed 且宿主机磁盘落盘)，验证本地路径与一键归档动作
   - Level 3: 任务队列排队/下载中状态拦截，验证实时下载进度与任务详情动作
   - 边界验证: 磁盘文件缺失或已标记删除 (DELETED_LOCAL_UIDS) 时不误报查重
   - API 契约验证: POST /api/files/check-dedup 独立端点与 link 自动解析回退
   - 接口联动与拦截: POST /api/tg/resolve-link 注入 dedup 属性;
                    POST /api/tg/quick-download 重复秒级拦截 (409 DUPLICATE_ASSET) 与 force 强制穿透;
                    POST /browse/download 重复拦截与 force 强制穿透
2. 特性二：Telegram 消息通知外发引擎（Bot/收藏夹双通道）
   - 配置中心: GET & POST /api/notify/config，凭据脱敏 (******) 与事件勾选持久化
   - SSRF 严格防护: 目标白名单仅允许 https://api.telegram.org，内网 IP 与非 https 强制阻断
   - 双通道调度: Bot API 独立推送 与 Saved Messages (TDLib) 收藏夹直投
   - 四大事件模型与卡片排版:
     a. DOWNLOAD_COMPLETED: 阈值过滤 (50MB) 与 HTML 卡片（文件名、大小、耗时、来源会话、本地路径）
     b. ARCHIVE_SUCCESS: HTML 卡片（文件名、大小、网盘名、云端路径、上传耗时、OpenList直达链接）
     c. ARCHIVE_FAILED: 智能模式识别归纳与 HTML 告警卡片（401鉴权过期、容量超限、409文件冲突、网络超时）
     d. DISK_WATERMARK_ALERT: 磁盘高水位告警卡片（占用率、警戒线、剩余可用、挂起计数）与 600s 防抖保护
   - 测试推送端点: POST /api/notify/test
3. 特性三：跨库全局聚合搜索与 Ctrl+K 快捷呼出面板
   - 路由别名与基础功能: GET /api/search 与 GET /api/search/aggregate 完全对齐
   - 边界与空查: 空关键词或空白符返回 0 条并保持统一三栏数据结构
   - 跨库三栏聚合: 同时并发检索任务库 (tasks)、本地在存 (local)、云端网盘 (cloud)
   - 逻辑边界: tasks 栏严格剔除已完成 completed 任务；local 栏必须要求物理磁盘文件真实在存
   - 多条件检索: 大小写不敏感 (case-insensitive) 与多词空格 AND 检索 (例如 "debian arm64")
   - 分页限制: limit 参数截断
   - 动作与直达: 任务栏对应 /tasks/{id}，本地在存对应 /library/local，云端网盘对应 /library/cloud 及 openlistUrl
4. 特性四：归档失败智能归类引擎与一键批量重试
   - 错误模式识别器: _classify_archive_error 覆盖 6 大错误类型 (token_expired, storage_full, conflict, timeout, unknown)
   - 聚合诊断端点: GET /api/archive/failed 与 GET /api/archive/failed-summary
   - 批量重试调度: POST /api/archive/retry-failed 与 POST /api/archive/batch-retry
   - 重试策略与恢复: 全量重试、按错误分类过滤重试、按指定 Job ID 重试
   - 鉴权自动续期: 当重试项包含 token_expired 时自动触发 _openlist_relogin() 凭据刷新
   - 冲突覆盖策略: 当 forceOverwrite=True 时自动将 conflict 任务策略升格为 overwrite
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


class BaseAdvancedFeatureTestCase(unittest.TestCase):
    """高级特性测试公共基类：环境准备、状态隔离与凭据辅助方法。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tg_adv_test_")
        self.client = TestClient(bridge_server.app)

        # 备份全局状态
        self._orig_archive_jobs = dict(bridge_server._ARCHIVE_JOBS)
        self._orig_archive_file = bridge_server._ARCHIVE_FILE
        self._orig_notify_config = dict(bridge_server._NOTIFY_CONFIG)
        self._orig_notify_file = bridge_server._NOTIFY_CONFIG_FILE
        self._orig_deleted_local_uids = set(bridge_server._DELETED_LOCAL_UIDS)
        self._orig_last_disk_alert = bridge_server._NOTIFY_LAST_DISK_ALERT
        self._orig_tasks_cache = dict(bridge_server._TASKS_CACHE)

        # 重定向持久化文件至测试临时目录
        bridge_server._ARCHIVE_FILE = os.path.join(self.tmp_dir, ".test_archive_jobs.json")
        bridge_server._NOTIFY_CONFIG_FILE = os.path.join(self.tmp_dir, ".test_notify_config.json")
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._DELETED_LOCAL_UIDS.clear()
        bridge_server._NOTIFY_LAST_DISK_ALERT = 0.0
        bridge_server._TASKS_CACHE = {"expire": 0.0, "value": None}

        # 初始化通知配置
        bridge_server._NOTIFY_CONFIG.update({
            "enabled": True,
            "channel": "both",
            "botToken": "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ",
            "chatId": "-100123456789",
            "minFileSizeMB": 50,
            "events": {
                "downloadCompleted": True,
                "archiveSuccess": True,
                "archiveFailed": True,
                "diskWatermarkAlert": True,
            }
        })

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        # 恢复全局状态
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self._orig_archive_jobs)
        bridge_server._ARCHIVE_FILE = self._orig_archive_file
        bridge_server._NOTIFY_CONFIG.clear()
        bridge_server._NOTIFY_CONFIG.update(self._orig_notify_config)
        bridge_server._NOTIFY_CONFIG_FILE = self._orig_notify_file
        bridge_server._DELETED_LOCAL_UIDS.clear()
        bridge_server._DELETED_LOCAL_UIDS.update(self._orig_deleted_local_uids)
        bridge_server._NOTIFY_LAST_DISK_ALERT = self._orig_last_disk_alert
        bridge_server._TASKS_CACHE.clear()
        bridge_server._TASKS_CACHE.update(self._orig_tasks_cache)

    def _auth_cookies(self):
        token = bridge_server._make_portal_token()
        csrf = "csrf-secret-token-xyz789"
        return {
            bridge_server.PORTAL_COOKIE: token,
            bridge_server.CSRF_COOKIE: csrf,
        }, {
            bridge_server.CSRF_HEADER: csrf,
        }


# =====================================================================
# 1. 全局 uniqueId 下载防重与智能关联网盘自动化测试
# =====================================================================
class TestUniqueIdDedupAndAssetAssociation(BaseAdvancedFeatureTestCase):
    """验证全局 uniqueId 防重拦截、多级指纹匹配、资产状态智能识别与一键动作。"""

    def test_dedup_cloud_match_by_unique_id(self):
        """测试 Level 1：云端网盘已归档资产以 uniqueId 精确命中，返回云端位置与 OpenList 直链。"""
        bridge_server._ARCHIVE_JOBS["job-cloud-01"] = {
            "id": "job-cloud-01",
            "unique_id": "AQAD_unique_cloud_1001",
            "filename": "Nature_Documentary_4K.mp4",
            "size_bytes": 1024 * 1024 * 200,
            "remote_path": "/阿里云盘/Documentaries/Nature_Documentary_4K.mp4",
            "remote_dir": "/阿里云盘/Documentaries",
            "state": "done",
            "archived_at": 1725280000.0,
            "created_at": 1725279000.0,
        }

        async def run_check():
            res = await bridge_server._check_file_dedup(
                unique_id="AQAD_unique_cloud_1001",
                filename="Nature_Documentary_4K.mp4",
                size_bytes=1024 * 1024 * 200
            )
            self.assertTrue(res["duplicate"])
            self.assertEqual(res["duplicateType"], "cloud")
            self.assertEqual(res["matchedBy"], "unique_id")
            self.assertIn("阿里云盘", res["prompt"])
            self.assertIn("/阿里云盘/Documentaries/Nature_Documentary_4K.mp4", res["prompt"])

            asset = res["asset"]
            self.assertEqual(asset["status"], "archived")
            self.assertEqual(asset["cloudPath"], "/阿里云盘/Documentaries/Nature_Documentary_4K.mp4")
            self.assertEqual(asset["drive"], "阿里云盘")
            self.assertIn("127.0.0.1:5244", asset["openlistUrl"])
            self.assertFalse(asset["localExists"])

            action_types = [a["type"] for a in res["actions"]]
            self.assertIn("open_cloud", action_types)
            self.assertIn("retrieve_cloud", action_types)
            self.assertIn("force_download", action_types)

        asyncio.run(run_check())

    def test_dedup_cloud_match_by_name_and_size_fallback(self):
        """测试 Level 1 降级：无 uniqueId 时，通过 (文件名 + 大小) 二阶内容指纹成功命中云端资产。"""
        bridge_server._ARCHIVE_JOBS["job-cloud-02"] = {
            "id": "job-cloud-02",
            "unique_id": "AQAD_some_old_id",
            "filename": "Linux_Tutorial_2026.mkv",
            "size_bytes": 52428800,
            "remote_path": "/OneDrive/Videos/Linux_Tutorial_2026.mkv",
            "remote_dir": "/OneDrive/Videos",
            "state": "done",
            "archived_at": 1725281000.0,
        }

        async def run_check():
            # 模拟跨群转发后 uniqueId 缺失，但名称和体积吻合
            res = await bridge_server._check_file_dedup(
                unique_id="",
                filename="linux_tutorial_2026.mkv",
                size_bytes=52428800
            )
            self.assertTrue(res["duplicate"])
            self.assertEqual(res["duplicateType"], "cloud")
            self.assertEqual(res["matchedBy"], "name_size")
            self.assertEqual(res["asset"]["drive"], "OneDrive")

        asyncio.run(run_check())

    def test_dedup_local_match_and_actions(self):
        """测试 Level 2：本地磁盘落盘在存资产智能命中，返回本地绝对路径与立即归档动作。"""
        local_file = os.path.join(self.tmp_dir, "local_cached_video.mp4")
        with open(local_file, "wb") as f:
            f.write(b"local video data content " * 100)

        mock_tasks = [
            {
                "id": 501,
                "_unique_id": "AQAD_local_asset_2002",
                "filename": "local_cached_video.mp4",
                "_download_status": "completed",
                "status": "completed",
                "local_path": local_file,
                "_size_bytes": os.path.getsize(local_file),
            }
        ]

        async def run_check():
            with patch("bridge_server.tasks_all", new=AsyncMock(return_value=mock_tasks)):
                res = await bridge_server._check_file_dedup(
                    unique_id="AQAD_local_asset_2002",
                    filename="local_cached_video.mp4"
                )
                self.assertTrue(res["duplicate"])
                self.assertEqual(res["duplicateType"], "local")
                self.assertEqual(res["matchedBy"], "unique_id")
                self.assertIn("本地在存", res["prompt"])
                self.assertTrue(res["asset"]["localExists"])
                self.assertEqual(res["asset"]["localPath"], local_file)

                action_types = [a["type"] for a in res["actions"]]
                self.assertIn("open_local", action_types)
                self.assertIn("archive_now", action_types)
                self.assertIn("force_download", action_types)

        asyncio.run(run_check())

    def test_dedup_local_deleted_or_missing_file_bypassed(self):
        """测试 Level 2 边界：若本地文件已标记删除或物理磁盘不存在，不误判为本地在存。"""
        non_exist_file = os.path.join(self.tmp_dir, "deleted_from_disk.mp4")
        # 磁盘上不创建该文件

        mock_tasks = [
            {
                "id": 502,
                "_unique_id": "AQAD_deleted_3003",
                "filename": "deleted_from_disk.mp4",
                "_download_status": "completed",
                "status": "completed",
                "local_path": non_exist_file,
                "_size_bytes": 1024,
            }
        ]

        async def run_check():
            with patch("bridge_server.tasks_all", new=AsyncMock(return_value=mock_tasks)):
                res = await bridge_server._check_file_dedup(
                    unique_id="AQAD_deleted_3003",
                    filename="deleted_from_disk.mp4"
                )
                # 物理文件不存在，不应命中 local 重复
                self.assertFalse(res["duplicate"])
                self.assertEqual(res["duplicateType"], "none")

        asyncio.run(run_check())

    def test_dedup_task_queue_in_flight_match(self):
        """测试 Level 3：正在下载或排队中的任务命中，返回当前下载进度与查看任务动作。"""
        mock_tasks = [
            {
                "id": 601,
                "_unique_id": "AQAD_downloading_4004",
                "filename": "Large_Debian_ISO.iso",
                "_download_status": "downloading",
                "status": "downloading",
                "progress": 72,
                "_size_bytes": 2048000000,
            }
        ]

        async def run_check():
            with patch("bridge_server.tasks_all", new=AsyncMock(return_value=mock_tasks)):
                res = await bridge_server._check_file_dedup(
                    unique_id="AQAD_downloading_4004",
                    filename="Large_Debian_ISO.iso"
                )
                self.assertTrue(res["duplicate"])
                self.assertEqual(res["duplicateType"], "task")
                self.assertIn("72%", res["prompt"])
                self.assertEqual(res["asset"]["taskId"], 601)
                self.assertEqual(res["asset"]["progress"], 72)

                action_types = [a["type"] for a in res["actions"]]
                self.assertIn("view_task", action_types)
                self.assertIn("force_download", action_types)

        asyncio.run(run_check())

    def test_api_check_dedup_endpoint_and_link_parsing(self):
        """测试 POST /api/files/check-dedup 端点：支持 JSON 直接查重与 link 自动解析回退。"""
        cookies, headers = self._auth_cookies()

        bridge_server._ARCHIVE_JOBS["job-cloud-api"] = {
            "id": "job-cloud-api",
            "unique_id": "AQAD_api_test_5005",
            "filename": "api_check.mp4",
            "size_bytes": 1000,
            "remote_path": "/阿里云盘/api_check.mp4",
            "state": "done",
            "archived_at": 1725280000.0,
        }

        # 1. 直接 uniqueId 查重
        resp1 = self.client.post("/api/files/check-dedup", json={"uniqueId": "AQAD_api_test_5005"}, cookies=cookies, headers=headers)
        self.assertEqual(resp1.status_code, 200)
        data1 = resp1.json()
        self.assertTrue(data1["ok"])
        self.assertTrue(data1["data"]["duplicate"])
        self.assertEqual(data1["data"]["duplicateType"], "cloud")

        # 2. 通过 t.me 链接自动解析出 uniqueId 并查重
        fake_records = [{
            "id": 999,
            "_unique_id": "AQAD_api_test_5005",
            "uniqueId": "AQAD_api_test_5005",
            "name": "api_check.mp4",
            "size": 1000
        }]
        with patch("bridge_server.chat_sources", new=AsyncMock(return_value=[{"telegramId": 1}])), \
             patch.object(bridge_server.BACKEND, "resolve_link", new=AsyncMock(return_value=fake_records)):
            resp2 = self.client.post("/api/files/check-dedup", json={"link": "https://t.me/c/1827364521/987"}, cookies=cookies, headers=headers)
            self.assertEqual(resp2.status_code, 200)
            data2 = resp2.json()
            self.assertTrue(data2["data"]["duplicate"])
            self.assertEqual(data2["data"]["duplicateType"], "cloud")

    def test_quick_download_interception_and_force_bypass(self):
        """测试 POST /api/tg/quick-download：重复提交秒级拦截 409 DUPLICATE_ASSET，force 穿透放行。"""
        cookies, headers = self._auth_cookies()

        bridge_server._ARCHIVE_JOBS["job-quick-dup"] = {
            "id": "job-quick-dup",
            "unique_id": "AQAD_quick_dup_6006",
            "filename": "archived_quick.mp4",
            "size_bytes": 3000,
            "remote_path": "/阿里云盘/archived_quick.mp4",
            "state": "done",
        }

        fake_file_payload = {
            "telegramId": 1,
            "chatId": 100,
            "messageId": 20,
            "fileId": 201,
            "uniqueId": "AQAD_quick_dup_6006",
            "name": "archived_quick.mp4",
            "size": 3000
        }

        # 1. 非 force 请求：秒级拦截 409
        resp_blocked = self.client.post(
            "/api/tg/quick-download",
            json={"force": False, "files": [fake_file_payload]},
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(resp_blocked.status_code, 409)
        data_blocked = resp_blocked.json()
        self.assertFalse(data_blocked["ok"])
        self.assertEqual(data_blocked["code"], "DUPLICATE_ASSET")
        self.assertEqual(data_blocked["duplicateType"], "cloud")
        self.assertEqual(data_blocked["skippedDuplicates"], 1)

        # 2. 携带 force: True：穿透防重检查并成功拉起下载
        with patch.object(bridge_server.BACKEND, "start_download_multiple", new=AsyncMock(return_value={"ok": True})):
            resp_force = self.client.post(
                "/api/tg/quick-download",
                json={"force": True, "files": [fake_file_payload]},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_force.status_code, 200)
            data_force = resp_force.json()
            self.assertTrue(data_force["ok"])
            self.assertEqual(data_force["count"], 1)

    def test_browse_download_interception_and_force_bypass(self):
        """测试 POST /browse/download：浏览页重复下载拦截与 force 强制穿透。"""
        cookies, headers = self._auth_cookies()

        bridge_server._ARCHIVE_JOBS["job-browse-dup"] = {
            "id": "job-browse-dup",
            "unique_id": "AQAD_browse_dup_7007",
            "filename": "browse_movie.mp4",
            "size_bytes": 5000,
            "remote_path": "/阿里云盘/Movies/browse_movie.mp4",
            "state": "done",
            "archived_at": 1725280000.0,
        }

        fake_browse_file = {
            "telegramId": 1,
            "chatId": 100,
            "messageId": 30,
            "fileId": 301,
            "uniqueId": "AQAD_browse_dup_7007",
            "filename": "browse_movie.mp4",
            "size": 5000
        }

        # 1. 非 force 下载拦截
        resp_intercepted = self.client.post(
            "/browse/download",
            json={"force": False, "files": [fake_browse_file]},
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(resp_intercepted.status_code, 200)
        data_int = resp_intercepted.json()
        self.assertFalse(data_int["ok"])
        self.assertEqual(data_int["code"], "DUPLICATE_ASSET")
        self.assertEqual(data_int["duplicateType"], "cloud")
        self.assertEqual(data_int["skippedDup"], 1)

        # 2. force: True 强制重下
        with patch.object(bridge_server.BACKEND, "start_download_multiple", new=AsyncMock(return_value={"ok": True})):
            resp_force = self.client.post(
                "/browse/download",
                json={"force": True, "files": [fake_browse_file]},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_force.status_code, 200)
            data_force = resp_force.json()
            self.assertTrue(data_force["ok"])
            self.assertEqual(data_force["count"], 1)


    def test_resolve_link_injects_dedup_info(self):
        """测试 POST /api/tg/resolve-link：解析链接返回结果中每个媒体对象注入完整的 dedup 指纹信息。"""
        cookies, headers = self._auth_cookies()

        bridge_server._ARCHIVE_JOBS["job-link-cloud"] = {
            "id": "job-link-cloud",
            "unique_id": "uid_link_dup_8888",
            "filename": "Special_Presentation.mp4",
            "size_bytes": 50000000,
            "remote_path": "/阿里云盘/Presentations/Special_Presentation.mp4",
            "state": "done",
            "archived_at": 1725280000.0,
        }

        mock_sources = [{"telegramId": 1001, "chatId": 100, "title": "Main Account"}]
        mock_backend_files = [{
            "fileId": 7001,
            "id": 7001,
            "uniqueId": "uid_link_dup_8888",
            "name": "Special_Presentation.mp4",
            "size": 50000000,
            "type": "video",
            "chatId": -1001827364521,
            "chatTitle": "技术群",
            "messageId": 999,
        }]

        with patch("bridge_server.chat_sources", new=AsyncMock(return_value=mock_sources)), \
             patch.object(bridge_server.BACKEND, "resolve_link", new=AsyncMock(return_value=mock_backend_files)):
            resp = self.client.post(
                "/api/tg/resolve-link",
                json={"link": "https://t.me/c/1827364521/999"},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            files = data["data"]["files"]
            self.assertEqual(len(files), 1)
            dedup = files[0]["dedup"]
            self.assertTrue(dedup["duplicate"])
            self.assertEqual(dedup["duplicateType"], "cloud")
            self.assertTrue(files[0]["isAlreadyArchived"])

    def test_submit_post_dedup_interception_and_force(self):
        """测试 POST /submit：提交重复下载链接时进行防重拦截，带 force 穿透。"""
        cookies, headers = self._auth_cookies()

        bridge_server._ARCHIVE_JOBS["job-submit-dup"] = {
            "id": "job-submit-dup",
            "unique_id": "uid_submit_dup_9999",
            "filename": "submit_test.mp4",
            "size_bytes": 60000,
            "remote_path": "/阿里云盘/submit_test.mp4",
            "state": "done",
            "archived_at": 1725280000.0,
        }

        mock_sources = [{"telegramId": 1001, "chatId": 100, "title": "Main Account"}]
        mock_backend_files = [{
            "fileId": 8001,
            "id": 8001,
            "uniqueId": "uid_submit_dup_9999",
            "name": "submit_test.mp4",
            "size": 60000,
            "type": "video",
            "chatId": -1001827364521,
            "messageId": 888,
        }]

        # 1. 默认 force=False 提交：触发查重拦截
        with patch("bridge_server.chat_sources", new=AsyncMock(return_value=mock_sources)), \
             patch.object(bridge_server.BACKEND, "resolve_link", new=AsyncMock(return_value=mock_backend_files)):
            resp_dup = self.client.post(
                "/submit",
                data={"links": "https://t.me/c/1827364521/888", "force": "false"},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_dup.status_code, 200)
            # 返回的 HTML 页面中包含拦截报错或提示
            self.assertIn("归档至", resp_dup.text)
            self.assertIn("submit_test.mp4", resp_dup.text)

        # 2. force=True 强制提交：通过并成功调用 start_download_multiple
        with patch("bridge_server.chat_sources", new=AsyncMock(return_value=mock_sources)), \
             patch.object(bridge_server.BACKEND, "resolve_link", new=AsyncMock(return_value=mock_backend_files)), \
             patch.object(bridge_server.BACKEND, "start_download_multiple", new=AsyncMock(return_value={"ok": True})) as mock_start:
            resp_force = self.client.post(
                "/submit",
                data={"links": "https://t.me/c/1827364521/888", "force": "true"},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp_force.status_code, 200)
            mock_start.assert_awaited_once()


# =====================================================================
# 2. Telegram 消息通知外发引擎自动化测试
# =====================================================================
class TestTelegramNotificationEngine(BaseAdvancedFeatureTestCase):
    """验证 Telegram 消息通知外发引擎、配置管理、防SSRF、双通道调度与卡片模板。"""

    def test_notification_config_get_and_post(self):
        """测试通知配置读取与更新，确保敏感 Token 严格脱敏存储与展示。"""
        cookies, headers = self._auth_cookies()

        # 1. 获取当前脱敏配置
        resp_get = self.client.get("/api/notify/config", cookies=cookies)
        self.assertEqual(resp_get.status_code, 200)
        cfg = resp_get.json()["config"]
        self.assertTrue(cfg["enabled"])
        self.assertIn("******", cfg["botToken"])
        self.assertTrue(cfg["hasBotToken"])

        # 2. 保存新配置（包含最小文件阈值与事件开关）
        update_payload = {
            "enabled": True,
            "channel": "both",
            "botToken": "987654321:ZYXwvuTsRQPonMlkJiHgFeDcBa",
            "chatId": "-10099887766",
            "minFileSizeMB": 80,
            "events": {
                "downloadCompleted": True,
                "archiveSuccess": True,
                "archiveFailed": False,
                "diskWatermarkAlert": True
            }
        }
        resp_post = self.client.post(
            "/api/notify/config",
            json=update_payload,
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(resp_post.status_code, 200)
        saved_cfg = resp_post.json()["config"]
        self.assertEqual(saved_cfg["minFileSizeMB"], 80)
        self.assertFalse(saved_cfg["events"]["archiveFailed"])
        self.assertIn("******", saved_cfg["botToken"])

    def test_ssrf_strict_whitelist_enforcement(self):
        """测试防 SSRF 安全屏障：强制白名单限制 https://api.telegram.org，阻断任何内部 IP 与非法协议。"""
        async def run_ssrf_tests():
            # 1. 允许合法的 api.telegram.org
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"ok": True}))):
                ok, err = await bridge_server._send_via_bot("123:ABC", "-1001", "<b>test</b>")
                self.assertTrue(ok)
                self.assertEqual(err, "")

            # 2. 拦截非 https 协议 (http://)
            with patch("bridge_server.urlsplit", return_value=MagicMock(scheme="http", hostname="api.telegram.org")):
                ok, err = await bridge_server._send_via_bot("123:ABC", "-1001", "<b>test</b>")
                self.assertFalse(ok)
                self.assertIn("非法", err)

            # 3. 拦截本地回环 127.0.0.1
            with patch("bridge_server.urlsplit", return_value=MagicMock(scheme="https", hostname="127.0.0.1")):
                ok, err = await bridge_server._send_via_bot("123:ABC", "-1001", "<b>test</b>")
                self.assertFalse(ok)
                self.assertIn("非法", err)

            # 4. 拦截云元数据地址 169.254.169.254
            with patch("bridge_server.urlsplit", return_value=MagicMock(scheme="https", hostname="169.254.169.254")):
                ok, err = await bridge_server._send_via_bot("123:ABC", "-1001", "<b>test</b>")
                self.assertFalse(ok)
                self.assertIn("非法", err)

            # 5. 拦截外部任意域名 evil-attacker.com
            with patch("bridge_server.urlsplit", return_value=MagicMock(scheme="https", hostname="evil-attacker.com")):
                ok, err = await bridge_server._send_via_bot("123:ABC", "-1001", "<b>test</b>")
                self.assertFalse(ok)
                self.assertIn("非法", err)

        asyncio.run(run_ssrf_tests())

    def test_saved_messages_channel_dispatch(self):
        """测试 Telegram Saved Messages（收藏夹）零配置通道直投。"""
        async def run_test():
            mock_sources = [{"telegramId": 88888888, "title": "My Account"}]
            with patch("bridge_server.chat_sources", new=AsyncMock(return_value=mock_sources)), \
                 patch("bridge_server.telegram_api_call", new=AsyncMock(return_value={"@type": "message"})) as mock_api:
                ok, err = await bridge_server._send_via_saved_messages("<b>Card Title</b>\nCard Body")
                self.assertTrue(ok)
                self.assertEqual(err, "")
                mock_api.assert_awaited_once()
                call_args = mock_api.await_args[0]
                self.assertEqual(call_args[0], "SendMessage")
                self.assertEqual(call_args[1]["chatId"], 88888888)

        asyncio.run(run_test())

    def test_event_download_completed_card_and_threshold(self):
        """测试大文件下载完成事件：小文件 (<50MB) 自动过滤静默，大文件 (>=50MB) 触发卡片排版。"""
        async def run_test():
            dispatched = []

            async def mock_dispatch(event_type, html_content, **kwargs):
                dispatched.append((event_type, html_content))
                return {"ok": True}

            with patch("bridge_server._dispatch_notification", side_effect=mock_dispatch):
                # 1. 小文件 (<50MB): 不外发
                small_file = {
                    "filename": "small_doc.pdf",
                    "_size_bytes": 10 * 1024 * 1024,
                    "size": "10.0 MB",
                    "source": "学习资料群",
                    "local_path": "/data/small_doc.pdf"
                }
                bridge_server.notify_download_completed(small_file)
                await asyncio.sleep(0.01)
                self.assertEqual(len(dispatched), 0)

                # 2. 大文件 (>=50MB): 成功生成 HTML 卡片并分发
                big_file = {
                    "filename": "open_source_os_2026.iso",
                    "_size_bytes": 1024 * 1024 * 1024,
                    "size": "1.00 GB",
                    "source": "开源镜像频道",
                    "local_path": "/data/open_source_os_2026.iso"
                }
                bridge_server.notify_download_completed(big_file)
                await asyncio.sleep(0.01)
                self.assertEqual(len(dispatched), 1)
                event_name, html_card = dispatched[0]
                self.assertEqual(event_name, "downloadCompleted")
                self.assertIn("【大文件下载完成】", html_card)
                self.assertIn("open_source_os_2026.iso", html_card)
                self.assertIn("1.00 GB", html_card)
                self.assertIn("开源镜像频道", html_card)
                self.assertIn("/data/open_source_os_2026.iso", html_card)

        asyncio.run(run_test())

    def test_event_archive_success_card(self):
        """测试网盘归档成功事件：验证目标网盘名、云端路径、耗时计算与 OpenList 直达链接。"""
        async def run_test():
            dispatched = []

            async def mock_dispatch(event_type, html_content, **kwargs):
                dispatched.append((event_type, html_content))
                return {"ok": True}

            with patch("bridge_server._dispatch_notification", side_effect=mock_dispatch):
                arch_job = {
                    "filename": "conference_keynote.mp4",
                    "size_bytes": 350 * 1024 * 1024,
                    "remote_path": "/GoogleDrive/TechTalks/conference_keynote.mp4",
                    "created_at": time.time() - 45,
                    "archived_at": time.time(),
                }
                bridge_server.notify_archive_success(arch_job)
                await asyncio.sleep(0.01)
                self.assertEqual(len(dispatched), 1)
                event_name, html_card = dispatched[0]
                self.assertEqual(event_name, "archiveSuccess")
                self.assertIn("【网盘归档成功】", html_card)
                self.assertIn("conference_keynote.mp4", html_card)
                self.assertIn("GoogleDrive", html_card)
                self.assertIn("/GoogleDrive/TechTalks/conference_keynote.mp4", html_card)
                self.assertIn("45秒", html_card)
                self.assertIn('href="http://127.0.0.1:5244/GoogleDrive/TechTalks/conference_keynote.mp4"', html_card)

        asyncio.run(run_test())

    def test_event_archive_failed_card_and_categories(self):
        """测试网盘归档失败告警事件：精准识别 4 大故障类别与排查建议。"""
        async def run_test():
            dispatched = []

            async def mock_dispatch(event_type, html_content, **kwargs):
                dispatched.append((event_type, html_content))
                return {"ok": True}

            with patch("bridge_server._dispatch_notification", side_effect=mock_dispatch):
                # 1. 鉴权过期
                bridge_server.notify_archive_failed({
                    "filename": "f1.mp4",
                    "remote_path": "/Aliyun/f1.mp4",
                    "error": "HTTP 401 token expired invalid credentials"
                })
                # 2. 容量超限
                bridge_server.notify_archive_failed({
                    "filename": "f2.mp4",
                    "remote_path": "/Aliyun/f2.mp4",
                    "error": "disk quota exceeded storage full"
                })
                # 3. 文件冲突
                bridge_server.notify_archive_failed({
                    "filename": "f3.mp4",
                    "remote_path": "/Aliyun/f3.mp4",
                    "error": "409 Conflict already exists on cloud"
                })
                # 4. 网络超时
                bridge_server.notify_archive_failed({
                    "filename": "f4.mp4",
                    "remote_path": "/Aliyun/f4.mp4",
                    "error": "504 Gateway Timeout readtimeout error"
                })

                await asyncio.sleep(0.01)
                self.assertEqual(len(dispatched), 4)
                self.assertIn("鉴权过期", dispatched[0][1])
                self.assertIn("容量超限", dispatched[1][1])
                self.assertIn("文件冲突", dispatched[2][1])
                self.assertIn("网络超时", dispatched[3][1])

        asyncio.run(run_test())

    def test_event_disk_watermark_alert_and_debounce(self):
        """测试 VPS 磁盘高水位告警卡片排版与 600 秒防抖去重机制。"""
        async def run_test():
            dispatched = []

            async def mock_dispatch(event_type, html_content, **kwargs):
                dispatched.append((event_type, html_content))
                return {"ok": True}

            with patch("bridge_server._dispatch_notification", side_effect=mock_dispatch):
                bridge_server._NOTIFY_LAST_DISK_ALERT = 0.0

                # 第一次触发告警：正常外发
                bridge_server.notify_disk_watermark_alert(
                    cur_pct=89.2, high_thresh=85.0, free_gb=12.45, waiting_count=5
                )
                await asyncio.sleep(0.01)
                self.assertEqual(len(dispatched), 1)
                html_card = dispatched[0][1]
                self.assertIn("【VPS 磁盘高水位熔断告警】", html_card)
                self.assertIn("89.2%", html_card)
                self.assertIn("85.0%", html_card)
                self.assertIn("12.45 GB", html_card)
                self.assertIn("5 个任务等待", html_card)

                # 短时间内（如 5 秒后）再次调用：被防抖直接吸收，不产生二次刷屏
                bridge_server.notify_disk_watermark_alert(
                    cur_pct=89.5, high_thresh=85.0, free_gb=12.00, waiting_count=6
                )
                await asyncio.sleep(0.01)
                self.assertEqual(len(dispatched), 1)

        asyncio.run(run_test())

    def test_api_notify_test_endpoint(self):
        """测试 POST /api/notify/test 一键测试推送 API。"""
        cookies, headers = self._auth_cookies()
        with patch("bridge_server._dispatch_notification", new=AsyncMock(return_value={"ok": True, "results": {"bot": True}})):
            resp = self.client.post("/api/notify/test", json={"channel": "bot"}, cookies=cookies, headers=headers)
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["ok"])
            self.assertIn("成功", resp.json()["message"])


# =====================================================================
# 3. 跨库全局聚合搜索自动化测试
# =====================================================================
class TestCrossLibraryAggregateSearch(BaseAdvancedFeatureTestCase):
    """验证跨库全局聚合搜索 (Tasks/Local/Cloud 三栏) 模糊检索、多词匹配与动作链接。"""

    def test_search_endpoints_and_empty_query(self):
        """测试 GET /api/search 与 GET /api/search/aggregate 路由对齐，以及空输入边界。"""
        cookies, headers = self._auth_cookies()

        # 1. 空查询
        resp1 = self.client.get("/api/search", cookies=cookies)
        self.assertEqual(resp1.status_code, 200)
        d1 = resp1.json()
        self.assertTrue(d1["ok"])
        self.assertEqual(d1["total"], 0)
        self.assertEqual(d1["counts"], {"tasks": 0, "local": 0, "cloud": 0})
        self.assertEqual(d1["results"], {"tasks": [], "local": [], "cloud": []})

        # 2. 别名路由一致性
        resp2 = self.client.get("/api/search/aggregate?q=   ", cookies=cookies)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.json()["total"], 0)

    def test_search_simultaneous_three_column_matching(self):
        """测试多库同时命中：同一关键词 'ubuntu' 并发检索 tasks、local 与 cloud 三栏。"""
        cookies, headers = self._auth_cookies()

        # 1. 本地磁盘落地文件
        local_iso = os.path.join(self.tmp_dir, "ubuntu-server-24.04-amd64.iso")
        with open(local_iso, "wb") as f:
            f.write(b"iso content")

        # 2. 任务池：包含 active task 与 completed local task
        mock_tasks = [
            {
                "id": 101,
                "_unique_id": "uid-task-ubuntu-dl",
                "uniqueId": "uid-task-ubuntu-dl",
                "filename": "ubuntu-desktop-24.04.iso",
                "source": "Ubuntu Mirrors",
                "status": "downloading",
                "progress": 45,
                "size": "4.5 GB",
                "_size_bytes": 4500000000,
                "_download_status": "downloading",
            },
            {
                "id": 102,
                "_unique_id": "uid-local-ubuntu-srv",
                "uniqueId": "uid-local-ubuntu-srv",
                "filename": "ubuntu-server-24.04-amd64.iso",
                "source": "Ubuntu Server",
                "status": "completed",
                "progress": 100,
                "local_path": local_iso,
                "size": "2.1 GB",
                "_size_bytes": 2100000000,
                "_download_status": "completed",
            }
        ]

        # 3. 云端网盘归档池
        bridge_server._ARCHIVE_JOBS["job-cloud-ubuntu"] = {
            "id": "job-cloud-ubuntu",
            "unique_id": "uid-cloud-ubuntu-arm",
            "filename": "ubuntu-core-arm64.iso",
            "size_bytes": 800000000,
            "remote_path": "/阿里云盘/ISOs/ubuntu-core-arm64.iso",
            "remote_dir": "/阿里云盘/ISOs",
            "state": "done",
            "archived_at": 1725280000.0,
            "created_at": 1725280000.0,
        }

        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=mock_tasks)):
            resp = self.client.get("/api/search?q=ubuntu", cookies=cookies)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["counts"]["tasks"], 1)
            self.assertEqual(data["counts"]["local"], 1)
            self.assertEqual(data["counts"]["cloud"], 1)
            self.assertEqual(data["total"], 3)

            # 校验任务项字段与跳转动作
            task_item = data["results"]["tasks"][0]
            self.assertEqual(task_item["id"], 101)
            self.assertEqual(task_item["status"], "downloading")
            self.assertEqual(task_item["actionUrl"], "/tasks/101")

            # 校验本地在存项字段与跳转动作
            local_item = data["results"]["local"][0]
            self.assertEqual(local_item["uniqueId"], "uid-local-ubuntu-srv")
            self.assertEqual(local_item["localPath"], local_iso)
            self.assertEqual(local_item["actionUrl"], "/library/local")

            # 校验云端网盘项字段与 OpenList 直达动作
            cloud_item = data["results"]["cloud"][0]
            self.assertEqual(cloud_item["drive"], "阿里云盘")
            self.assertIn("127.0.0.1:5244", cloud_item["openlistUrl"])
            self.assertEqual(cloud_item["actionUrl"], "/library/cloud")

    def test_search_completed_task_excluded_from_tasks_column(self):
        """测试检索状态隔离：已完成 (completed) 任务严禁混入 tasks 栏（应归入 local 在存栏）。"""
        cookies, headers = self._auth_cookies()

        local_file = os.path.join(self.tmp_dir, "done_video.mp4")
        with open(local_file, "wb") as f:
            f.write(b"video")

        mock_tasks = [
            {
                "id": 888,
                "_unique_id": "uid-done-video",
                "filename": "done_video.mp4",
                "status": "completed",
                "_download_status": "completed",
                "local_path": local_file,
                "progress": 100,
            }
        ]

        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=mock_tasks)):
            resp = self.client.get("/api/search?q=done_video", cookies=cookies)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["counts"]["tasks"], 0)
            self.assertEqual(data["counts"]["local"], 1)

    def test_search_multi_keyword_and_matching(self):
        """测试多关键词空格 AND 组合检索：必须同时包含所有关键词才能命中。"""
        cookies, headers = self._auth_cookies()

        local_f1 = os.path.join(self.tmp_dir, "linux_kernel_x86_64.tar")
        local_f2 = os.path.join(self.tmp_dir, "linux_kernel_arm64.tar")
        with open(local_f1, "wb") as f:
            f.write(b"1")
        with open(local_f2, "wb") as f:
            f.write(b"2")

        mock_tasks = [
            {
                "id": 301,
                "_unique_id": "uid-k1",
                "filename": "linux_kernel_x86_64.tar",
                "status": "completed",
                "_download_status": "completed",
                "local_path": local_f1,
            },
            {
                "id": 302,
                "_unique_id": "uid-k2",
                "filename": "linux_kernel_arm64.tar",
                "status": "completed",
                "_download_status": "completed",
                "local_path": local_f2,
            }
        ]

        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=mock_tasks)):
            # 检索 "linux kernel arm64"：只应命中 local_f2
            resp = self.client.get("/api/search?q=linux%20kernel%20arm64", cookies=cookies)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["counts"]["local"], 1)
            self.assertEqual(data["results"]["local"][0]["filename"], "linux_kernel_arm64.tar")

    def test_search_case_insensitivity_and_limit(self):
        """测试大小写不敏感检索与 limit 参数截断。"""
        cookies, headers = self._auth_cookies()

        mock_tasks = []
        for i in range(10):
            mock_tasks.append({
                "id": 900 + i,
                "_unique_id": f"uid-limit-{i}",
                "filename": f"KUBERNETES_Cluster_Doc_{i}.pdf",
                "status": "downloading",
                "_download_status": "downloading",
                "progress": 10,
            })

        with patch("bridge_server.tasks_all", new=AsyncMock(return_value=mock_tasks)):
            # 小写查询 "kubernetes" 匹配大写 "KUBERNETES"，且 limit=3 截断返回
            resp = self.client.get("/api/search?q=kubernetes&limit=3", cookies=cookies)
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["counts"]["tasks"], 10)  # 总匹配数 10
            self.assertEqual(len(data["results"]["tasks"]), 3)  # 列表截断为 3 条


# =====================================================================
# 4. 归档失败智能归类引擎与一键批量重试自动化测试
# =====================================================================
class TestArchiveFailureClassificationAndBatchRetry(BaseAdvancedFeatureTestCase):
    """验证归档失败 6 大错误正则识别引擎、聚合诊断看板与一键批量重试调度。"""

    def test_classify_archive_error_regex_patterns(self):
        """测试模式识别引擎：精准分类鉴权过期、容量超限、文件冲突、网络超时与未知异常。"""
        # 1. 鉴权过期 (token_expired)
        c1, l1, a1 = bridge_server._classify_archive_error("HTTP 401 Unauthorized: token expired")
        self.assertEqual(c1, "token_expired")
        self.assertIn("鉴权过期", l1)

        c2, _, _ = bridge_server._classify_archive_error("OpenListAuthErr: 登录失效凭证过期")
        self.assertEqual(c2, "token_expired")

        # 2. 容量超限 (storage_full)
        c3, l3, _ = bridge_server._classify_archive_error("HTTP 507: Insufficient Storage quota exceeded 网盘空间已满")
        self.assertEqual(c3, "storage_full")
        self.assertIn("容量超限", l3)

        # 3. 文件冲突 (conflict)
        c4, l4, _ = bridge_server._classify_archive_error("409 Conflict: target file already exists 目标已存在同名冲突")
        self.assertEqual(c4, "conflict")
        self.assertIn("文件冲突", l4)

        # 4. 网络超时 (timeout)
        c5, l5, _ = bridge_server._classify_archive_error("504 Gateway Timeout: Read timed out during connection")
        self.assertEqual(c5, "timeout")
        self.assertIn("网络超时", l5)

        c6, _, _ = bridge_server._classify_archive_error("ConnectError: Connection refused 网络连接超时")
        self.assertEqual(c6, "timeout")

        # 5. 未知异常 (unknown)
        c7, l7, _ = bridge_server._classify_archive_error("Unexpected OS FileSystem errno 12345")
        self.assertEqual(c7, "unknown")
        self.assertIn("未知异常", l7)

    def test_failed_summary_endpoints(self):
        """测试 GET /api/archive/failed 与 GET /api/archive/failed-summary 聚合统计与明细输出。"""
        cookies, headers = self._auth_cookies()

        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS["job-err-auth"] = {
            "id": "job-err-auth",
            "unique_id": "uid-err-1",
            "filename": "err_auth.mp4",
            "size_bytes": 1000,
            "remote_path": "/阿里云盘/err_auth.mp4",
            "state": "failed",
            "error": "401 token expired",
            "updated_at": 1725281000.0,
        }
        bridge_server._ARCHIVE_JOBS["job-err-quota"] = {
            "id": "job-err-quota",
            "unique_id": "uid-err-2",
            "filename": "err_quota.mp4",
            "size_bytes": 2000,
            "remote_path": "/阿里云盘/err_quota.mp4",
            "state": "failed",
            "error": "insufficient quota disk full",
            "updated_at": 1725281100.0,
        }
        bridge_server._ARCHIVE_JOBS["job-err-conflict"] = {
            "id": "job-err-conflict",
            "unique_id": "uid-err-3",
            "filename": "err_conflict.mp4",
            "size_bytes": 3000,
            "remote_path": "/阿里云盘/err_conflict.mp4",
            "state": "failed",
            "error": "409 already exists",
            "updated_at": 1725281200.0,
        }
        bridge_server._ARCHIVE_JOBS["job-err-timeout"] = {
            "id": "job-err-timeout",
            "unique_id": "uid-err-4",
            "filename": "err_timeout.mp4",
            "size_bytes": 4000,
            "remote_path": "/阿里云盘/err_timeout.mp4",
            "state": "failed",
            "error": "504 Gateway Timeout",
            "updated_at": 1725281300.0,
        }

        resp = self.client.get("/api/archive/failed-summary", cookies=cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])

        # 验证聚合计数
        cats = data["summary"]["categories"]
        self.assertEqual(data["summary"]["total"], 4)
        self.assertEqual(cats["token_expired"], 1)
        self.assertEqual(cats["storage_full"], 1)
        self.assertEqual(cats["conflict"], 1)
        self.assertEqual(cats["timeout"], 1)

        # 验证按时间倒序排列的诊断列表
        diagnostics = data["diagnostics"]
        self.assertEqual(len(diagnostics), 4)
        self.assertEqual(diagnostics[0]["id"], "job-err-timeout")
        self.assertEqual(diagnostics[0]["category"], "timeout")
        self.assertIn("建议", diagnostics[0]["suggestedFix"])

    def test_batch_retry_all_failed_jobs(self):
        """测试一键重试全部失败任务：状态重置为 queued，清空错误，重试次数递增并拉起 worker。"""
        cookies, headers = self._auth_cookies()

        bridge_server._ARCHIVE_JOBS.clear()
        for i in range(3):
            jid = f"job-batch-{i}"
            bridge_server._ARCHIVE_JOBS[jid] = {
                "id": jid,
                "unique_id": f"uid-batch-{i}",
                "filename": f"video_{i}.mp4",
                "size_bytes": 1000 * (i + 1),
                "remote_path": f"/阿里云盘/video_{i}.mp4",
                "state": "failed",
                "error": "502 Bad Gateway timeout",
                "retry_count": 0,
            }

        with patch("bridge_server._archive_worker", new=AsyncMock()) as mock_worker:
            resp = self.client.post(
                "/api/archive/batch-retry",
                json={"category": "all"},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["retriedCount"], 3)
            self.assertEqual(len(data["retriedJobIds"]), 3)

            # 验证所有任务已转为 queued，错误信息清空，重试计数递增
            for i in range(3):
                j = bridge_server._ARCHIVE_JOBS[f"job-batch-{i}"]
                self.assertEqual(j["state"], "queued")
                self.assertEqual(j["error"], "")
                self.assertEqual(j["progress"], 0)
                self.assertEqual(j["retry_count"], 1)

    def test_batch_retry_filtered_by_category(self):
        """测试按故障类别精准批量重试：仅重试指定分类任务，其他分类保持 failed 不受影响。"""
        cookies, headers = self._auth_cookies()

        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS["job-timeout"] = {
            "id": "job-timeout",
            "state": "failed",
            "error": "504 Gateway Timeout",
            "retry_count": 0,
        }
        bridge_server._ARCHIVE_JOBS["job-quota"] = {
            "id": "job-quota",
            "state": "failed",
            "error": "disk quota exceeded",
            "retry_count": 0,
        }

        with patch("bridge_server._archive_worker", new=AsyncMock()):
            resp = self.client.post(
                "/api/archive/retry-failed",
                json={"category": "timeout"},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["retriedCount"], 1)
            self.assertEqual(data["retriedJobIds"], ["job-timeout"])

            # 验证仅 timeout 变为 queued，quota 维持 failed
            self.assertEqual(bridge_server._ARCHIVE_JOBS["job-timeout"]["state"], "queued")
            self.assertEqual(bridge_server._ARCHIVE_JOBS["job-quota"]["state"], "failed")

    def test_batch_retry_filtered_by_job_ids(self):
        """测试指定 jobIds 单项/多项重试。"""
        cookies, headers = self._auth_cookies()

        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS["job-pick-1"] = {"id": "job-pick-1", "state": "failed", "error": "timeout"}
        bridge_server._ARCHIVE_JOBS["job-pick-2"] = {"id": "job-pick-2", "state": "failed", "error": "timeout"}

        with patch("bridge_server._archive_worker", new=AsyncMock()):
            resp = self.client.post(
                "/api/archive/batch-retry",
                json={"jobIds": ["job-pick-2"]},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["retriedCount"], 1)
            self.assertEqual(bridge_server._ARCHIVE_JOBS["job-pick-1"]["state"], "failed")
            self.assertEqual(bridge_server._ARCHIVE_JOBS["job-pick-2"]["state"], "queued")

    def test_batch_retry_auto_relogin_on_token_expired(self):
        """测试自动续期机制：当重试任务中包含鉴权过期时，自动调用 _openlist_relogin() 刷新凭据。"""
        cookies, headers = self._auth_cookies()

        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS["job-auth-fail"] = {
            "id": "job-auth-fail",
            "state": "failed",
            "error": "401 unauthorized token expired",
            "retry_count": 0,
        }

        with patch("bridge_server._openlist_relogin", new=AsyncMock(return_value="fresh-token-token")) as mock_relogin, \
             patch("bridge_server._archive_worker", new=AsyncMock()):
            resp = self.client.post(
                "/api/archive/batch-retry",
                json={"category": "all"},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["retriedCount"], 1)
            mock_relogin.assert_awaited_once()

    def test_batch_retry_force_overwrite_policy_update(self):
        """测试冲突覆盖策略：forceOverwrite=True 时，将文件冲突任务 policy 自动提升为 overwrite。"""
        cookies, headers = self._auth_cookies()

        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS["job-conflict-item"] = {
            "id": "job-conflict-item",
            "state": "failed",
            "error": "409 Conflict already exists",
            "policy": "skip",
            "retry_count": 0,
        }

        with patch("bridge_server._archive_worker", new=AsyncMock()):
            resp = self.client.post(
                "/api/archive/batch-retry",
                json={"category": "conflict", "forceOverwrite": True},
                cookies=cookies,
                headers=headers
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["retriedCount"], 1)
            self.assertEqual(bridge_server._ARCHIVE_JOBS["job-conflict-item"]["policy"], "overwrite")
            self.assertEqual(bridge_server._ARCHIVE_JOBS["job-conflict-item"]["state"], "queued")

    def test_batch_retry_no_matching_jobs(self):
        """测试空任务池或无匹配任务时的健壮性表现。"""
        cookies, headers = self._auth_cookies()
        bridge_server._ARCHIVE_JOBS.clear()

        resp = self.client.post(
            "/api/archive/batch-retry",
            json={"category": "all"},
            cookies=cookies,
            headers=headers
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["retriedCount"], 0)


if __name__ == "__main__":
    unittest.main()
