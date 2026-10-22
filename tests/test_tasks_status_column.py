# -*- coding: utf-8 -*-
"""
tests/test_tasks_status_column.py — 任务列表「状态」收敛为下载状态

用户要求：
1. 筛选条里的「状态」下拉整行删掉（任务列表上方不再有这一格）；
2. 表格阶段列只保留下载相关状态（待下载 / 下载中 / 已下载 / 失败），
   不再出现「归档中 / 已归档 / 校验中」这些归档阶段口径。

保留的后端能力：/partials/tasks 仍接受 status 参数（旧链接/书签不至于 500），
只是 UI 不再暴露入口。
"""
import unittest

import bridge_server
from core.auth import _make_portal_token
from core.config import PORTAL_COOKIE, CSRF_COOKIE
from fastapi.testclient import TestClient


def _task(status, **kw):
    t = {
        "id": "t1", "time": "10-18 06:00", "source": "测试频道", "msg_id": "1",
        "filename": "a.mp4", "size": "1.0 MB", "status": status, "loaded": "512 KB",
        "progress": 50, "local_path": "—", "cloud_path": "—", "source_url": "",
        "error_msg": "", "_unique_id": "uid1", "_telegram_id": "1",
    }
    t.update(kw)
    return t


class TasksStatusColumnTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(bridge_server.app)
        csrf = "probe-csrf"
        cls.cookies = {PORTAL_COOKIE: _make_portal_token(), CSRF_COOKIE: csrf}
        cls.csrf = csrf

    def test_filter_bar_has_no_status_select(self):
        from unittest.mock import patch
        with patch("routers.tasks.tasks_all", return_value=[]):
            html = self.client.get("/tasks", cookies=self.cookies).text
        self.assertNotIn('name="status"', html, "筛选条的状态下拉应已删除")
        self.assertIn('name="date_from"', html, "日期范围筛选应保留")

    def _render(self, tasks):
        from unittest.mock import patch
        with patch("routers.tasks.tasks_all", return_value=tasks):
            r = self.client.get("/partials/tasks", cookies=self.cookies)
        self.assertEqual(r.status_code, 200)
        return r.text

    def test_download_states_only(self):
        html = self._render([
            _task("pending"), _task("download"), _task("downloaded"),
            _task("failed", error_msg="下载超时"), _task("upload"), _task("archived"),
        ])
        self.assertIn("待下载", html)
        self.assertIn("下载中", html)
        self.assertIn("已下载", html)
        self.assertIn("下载失败", html)
        self.assertIn("下载超时", html)
        # 归档阶段不再作为主状态出现
        self.assertNotIn(">归档中<", html)
        self.assertNotIn(">已归档<", html)
        self.assertNotIn(">校验中<", html)

    def test_archive_progress_kept_as_secondary_hint(self):
        html = self._render([_task("upload", progress=37)])
        self.assertIn("归档 37%", html, "归档进度应降级为副文案，而非主状态")
        self.assertIn("已下载", html)


if __name__ == "__main__":
    unittest.main()
