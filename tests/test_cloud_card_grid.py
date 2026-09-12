# -*- coding: utf-8 -*-
"""
tests/test_cloud_card_grid.py
====================================
云端归档页卡片化回归测试（表格 → 浏览页同款 .thumb 卡片网格）：

1. 组内不再是 <table>，而是 thumb-grid 卡片网格；
2. 缩略图经 _cloud_archive_rows 联查任务记录注入（/preview 代理 URL）；
3. 无匹配任务时回退扩展名占位图（不 500、不空图）；
4. 网盘分组折叠结构与筛选 data-* 属性保留（旧筛选 JS 契约不破坏）；
5. 取回/删除/OpenList 按钮与 data-ret-pill 轮询契约保留；
6. 前端 visibleCount 选择器改为 [data-cloud-file]。

历史缺陷：旧版嵌套 <table> 窄屏横向溢出、操作按钮挤压换行，
且缩略图字段未联查，卡片化后无图可显。
"""
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.state as core_state
import bridge_server
from fastapi.testclient import TestClient


def _job(jid, uid, fname, drive="onedrive"):
    d = "/%s/tg-archive" % drive
    return {
        "id": jid, "unique_id": uid, "filename": fname,
        "state": "done", "remote_path": "%s/%s" % (d, fname),
        "remote_dir": d, "size_bytes": 1024,
        "created_at": 1.0, "archived_at": 1.0, "delete_local": False,
    }


def _task(uid, with_thumb=True):
    return {
        "_unique_id": uid, "_thumb_uid": ("tu-" + uid) if with_thumb else "",
        "_thumb": "", "_telegram_id": "777", "_chat_id": 42, "msg_id": 1001,
        "downloadStatus": "completed",
    }


class TestCloudCardGrid(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(bridge_server.app)
        self.token = bridge_server._make_portal_token()
        self.cookies = {bridge_server.PORTAL_COOKIE: self.token}

    def _render(self, jobs, tasks):
        """注入受控归档任务 + 受控任务记录渲染 /library/cloud。"""
        with patch.dict(core_state._ARCHIVE_JOBS, {j["id"]: j for j in jobs}, clear=True), \
             patch("bridge_server.tasks_all", new=AsyncMock(return_value=tasks)), \
             patch("bridge_server.openlist_mounts",
                   new=AsyncMock(return_value={"ok": True, "mounts": ["onedrive"], "message": ""})):
            r = self.client.get("/library/cloud", cookies=self.cookies)
        self.assertEqual(r.status_code, 200)
        return r.text

    def test_rows_are_cards_not_table(self):
        """组内必须渲染卡片网格，不得再出现内嵌表格。"""
        html = self._render([_job("j1", "u1", "a.mp4")], [_task("u1")])
        self.assertIn("thumb-grid cloud-card-grid", html)
        self.assertIn("cloud-card", html)
        self.assertNotIn("af-inner-table", html, "内嵌表格必须移除")
        self.assertNotIn("drive-folder-row", html, "表格行包装必须移除")

    def test_thumbnail_via_task_join(self):
        """有匹配任务时缩略图经 /preview 代理渲染。"""
        html = self._render([_job("j1", "u1", "a.mp4")], [_task("u1", with_thumb=True)])
        self.assertIn("/preview/777/tu-u1?chat=42&amp;msg=1001", html)

    def test_placeholder_without_task_match(self):
        """无匹配任务（历史残留记录）：回退扩展名占位，不 500、不引用空 /preview。"""
        html = self._render([_job("j1", "u-orphan", "a.mp4")], [])
        self.assertIn("t-ext", html)
        self.assertIn("MP4", html, "应渲染扩展名占位")
        self.assertNotIn('src="/preview/', html)

    def test_filter_contract_preserved(self):
        """筛选契约保留：data-filename/drive/status/remotedir 与计数函数。"""
        html = self._render([_job("j1", "u1", "a.mp4")], [_task("u1")])
        self.assertIn('data-filename="a.mp4"', html)
        self.assertIn('data-drive="onedrive"', html)
        self.assertIn('data-status="archived"', html)
        self.assertIn('data-remotedir="/onedrive/tg-archive"', html)
        self.assertIn("visibleCount", html)
        self.assertIn("[data-cloud-file]", html, "visibleCount 必须查卡片选择器")

    def test_action_buttons_and_poll_pill_preserved(self):
        """取回/删除/OpenList 按钮 + data-ret-pill 轮询契约保留。"""
        html = self._render([_job("j1", "u1", "a.mp4")], [_task("u1")])
        self.assertIn("data-retrieve-btn", html)
        self.assertIn("__retrieveCloudFile", html)
        self.assertIn("data-cloud-delete-btn", html)
        self.assertIn("__deleteCloudFile", html)
        self.assertIn("data-ret-pill", html)
        self.assertIn("OpenList", html)

    def test_drive_group_structure_preserved(self):
        """按网盘折叠的分组结构保留（含组头计数/合计/失效提示）。"""
        jobs = [_job("j1", "u1", "a.mp4"), _job("j2", "u2", "b.mp4")]
        tasks = [_task("u1"), _task("u2")]
        html = self._render(jobs, tasks)
        self.assertEqual(html.count('data-drive-group="onedrive"'), 1)
        self.assertIn("2 个", html)
        self.assertIn("合计", html)


if __name__ == "__main__":
    unittest.main()
