# -*- coding: utf-8 -*-
"""
tests/test_account_health_page.py
=================================
账号健康中心（/account）回归测试。

历史缺陷：仪表盘改造删除「近 14 日任务量 & FloodWait」图表时，把
trend_labels/trend_errors/trend_tasks 数据管线一并清理，但 account.html
仍在引用这些字段 —— Jinja 对「已存在但缺属性」的 dict 取属性直接抛
UndefinedError，把 /account 整页打成 500（用户点侧边栏「账号健康中心」
即报 Internal Server Error）。

修复约定（本测试锁定的契约）：
1. _dashboard_stats 必须持续产出近 7 日 trend_labels/trend_tasks/trend_errors
   （真实数据：任务完成时间逐日聚合 + LOG_STORE ERROR 日志逐日聚合）；
2. account.html 渲染 200 且包含两张图表容器与真实序列；
3. 模板侧用 stats.get(...) 安全取值 —— 即使降级上下文无 stats 键也只渲染
   空图，绝不 500。
"""
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import bridge_server


class TestAccountHealthPage(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(bridge_server.app)
        self.token = bridge_server._make_portal_token()
        self.cookies = {bridge_server.PORTAL_COOKIE: self.token}

    def test_account_page_renders_200(self):
        """核心回归：/account 必须渲染 200（历史版本直接 500）。"""
        resp = self.client.get("/account", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("账号健康", resp.text)

    def test_account_page_has_trend_charts_with_real_data(self):
        """/account 包含两张趋势图容器，且注入近 7 日真实序列（非空标签）。"""
        resp = self.client.get("/account", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="floodChart"', resp.text)
        self.assertIn('id="reqChart"', resp.text)
        # 注入的序列必须是 7 个 "%m-%d" 标签（服务端真实聚合结果）
        self.assertRegex(resp.text, r'var days = \["\d{2}-\d{2}"')

    def test_stats_dict_contains_trend_keys(self):
        """_dashboard_stats 返回 dict 必须含 trend_* 三键（模板渲染契约）。"""
        import asyncio

        from services.task_service import _dashboard_stats

        import time as _time
        now = _time.time()
        tasks = [
            {"status": "archived", "_archived_at": now, "_size_bytes": 100},
            {"status": "failed", "_archived_at": 0, "_date_ts": now, "_size_bytes": 1},
        ]
        disk = {"used_gb": 10.0, "total_gb": 100.0, "pct": 10}
        stats = asyncio.run(_dashboard_stats(tasks, disk))
        for key in ("trend_labels", "trend_tasks", "trend_errors"):
            self.assertIn(key, stats)
            self.assertIsInstance(stats[key], list)
            self.assertEqual(len(stats[key]), 7)
        # 完成任务按 _archived_at 落在今天桶
        self.assertGreaterEqual(stats["trend_tasks"][-1], 1)

    def test_account_page_degraded_context_no_500(self):
        """降级上下文（构造上下文失败、无 stats 键）也不得把 /account 打成 500。"""
        with patch("bridge_server._dashboard_stats", new=AsyncMock(side_effect=RuntimeError("boom"))):
            resp = self.client.get("/account", cookies=self.cookies)
        # 降级上下文仍应渲染出页面（模板用 stats.get 安全取值 + 无 stats 空图）
        self.assertEqual(resp.status_code, 200)

    def test_cloud_card_thumb_heal_fallback_for_legacy_records(self):
        """云端卡片：无 thumbnailUniqueId 的旧记录退回主文件 uid + heal=1 补图。"""
        import core.state as core_state

        job = {
            "id": "job-1", "unique_id": "AQADmain", "filename": "v.mp4",
            "state": "done", "remote_path": "/onedrive/tg-archive/v.mp4",
            "remote_dir": "/onedrive/tg-archive", "size_bytes": 1024,
            "created_at": 1.0, "archived_at": 1789710000.0, "delete_local": False,
        }
        task = {
            "_unique_id": "AQADmain", "_thumb_uid": "", "_thumb": "",
            "_telegram_id": 777, "_chat_id": -100123, "msg_id": 42,
            "downloadStatus": "completed",
        }
        with patch.dict(core_state._ARCHIVE_JOBS, {job["id"]: job}, clear=True), \
             patch("bridge_server.tasks_all", new=AsyncMock(return_value=[task])), \
             patch("bridge_server.openlist_mounts",
                   new=AsyncMock(return_value={"ok": True, "mounts": ["onedrive"], "message": ""})):
            resp = self.client.get("/library/cloud", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        # 旧记录 thumb_uid 为空 → 渲染退回主文件 uid 并带 heal=1 补图参数
        self.assertIn("/preview/777/AQADmain?chat=-100123&amp;msg=42&amp;heal=1", resp.text)

    def test_cloud_card_idle_backend_records_fallback(self):
        """VPS 实测缺陷回归：downloadStatus=idle 的后端记录被 tasks_all 过滤，
        云端卡片联查必须用后端原始 /api/files 记录兜底出缩略图。"""
        import core.state as core_state

        job = {
            "id": "job-idle", "unique_id": "AgADidle", "filename": "idle.mp4",
            "state": "done", "remote_path": "/onedrive/tg-archive/idle.mp4",
            "remote_dir": "/onedrive/tg-archive", "size_bytes": 4096,
            "created_at": 1.0, "archived_at": 1789710000.0, "delete_local": True,
        }
        # 后端原始记录：downloadStatus=idle（本地文件已删），但缩略图字段齐全
        raw_file = {
            "uniqueId": "AgADidle", "telegramId": 999, "chatId": -100789,
            "messageId": 555, "thumbnailUniqueId": "AQADthumbIdle",
            "thumbnail": "", "downloadStatus": "idle",
        }
        from core import backend as core_backend
        with patch.dict(core_state._ARCHIVE_JOBS, {job["id"]: job}, clear=True), \
             patch("bridge_server.tasks_all", new=AsyncMock(return_value=[])), \
             patch.object(core_backend.BACKEND, "list_all_files_page_info",
                          new=AsyncMock(return_value={"files": [raw_file]})), \
             patch("bridge_server.openlist_mounts",
                   new=AsyncMock(return_value={"ok": True, "mounts": ["onedrive"], "message": ""})):
            resp = self.client.get("/library/cloud", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("/preview/999/AQADthumbIdle?chat=-100789&amp;msg=555", resp.text)

    def test_cloud_card_normal_thumb_uid_no_heal(self):
        """有 thumbnailUniqueId 的正常记录不带 heal=1，走直取。"""
        import core.state as core_state

        job = {
            "id": "job-2", "unique_id": "AQADmain2", "filename": "a.zip",
            "state": "done", "remote_path": "/onedrive/tg-archive/a.zip",
            "remote_dir": "/onedrive/tg-archive", "size_bytes": 2048,
            "created_at": 1.0, "archived_at": 1789710000.0, "delete_local": False,
        }
        task = {
            "_unique_id": "AQADmain2", "_thumb_uid": "AQADthumb9", "_thumb": "",
            "_telegram_id": 888, "_chat_id": -100456, "msg_id": 7,
            "downloadStatus": "completed",
        }
        with patch.dict(core_state._ARCHIVE_JOBS, {job["id"]: job}, clear=True), \
             patch("bridge_server.tasks_all", new=AsyncMock(return_value=[task])), \
             patch("bridge_server.openlist_mounts",
                   new=AsyncMock(return_value={"ok": True, "mounts": ["onedrive"], "message": ""})):
            resp = self.client.get("/library/cloud", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("/preview/888/AQADthumb9?chat=-100456&amp;msg=7", resp.text)
        self.assertNotIn("AQADthumb9?chat=-100456&amp;msg=7&amp;heal=1", resp.text)


if __name__ == "__main__":
    unittest.main()
