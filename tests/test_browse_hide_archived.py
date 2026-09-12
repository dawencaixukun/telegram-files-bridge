# -*- coding: utf-8 -*-
r"""回归测试：浏览页「隐藏已归档」筛选的服务端化。

历史缺陷：该筛选是纯前端 display:none —— 只对当前 DOM 里的卡片生效，
切换媒体类型/聊天（__browseReload 重拉网格）或滚动加载下一页后，
新渲染的归档卡片不再应用筛选，「已归档」的图片/视频又冒出来。

现契约：
1. 勾选状态进入 __BROWSE_STATE.hideArchived；
2. __browseReload 与「加载更多」按钮的 URL 都带 hide_archived 参数；
3. 服务端 _browse_files 在 hide_archived=True 时过滤归档件（既有逻辑）；
4. 复选框初始 checked 与服务端状态一致（boost 切页回来不丢）。
"""
import re
import unittest

from core.templates import templates


class _Req:
    scope = {"type": "http"}


def _row(name, is_archived, openlist_url="http://x/a.mp4"):
    return {
        "telegramId": 10086, "chatId": -100123, "messageId": 1, "fileId": 2,
        "uniqueId": "UID-%s" % name, "name": name, "ext": "MP4",
        "size_str": "1.0 MB", "date_str": "2026-09-09 08:01",
        "type": "video", "type_label": "视频",
        "thumb": "", "thumb_uid": "", "dl": "idle", "tr": "idle", "dur_str": "",
        "is_archived": is_archived, "is_archiving": False,
        "cloud_path": "/阿里云盘/tg/%s" % name if is_archived else "",
        "cloud_drive": "阿里云盘", "openlist_url": openlist_url if is_archived else "",
        "archived_date": "09-09 08:05", "local_path": "",
    }


class TestHideArchivedFilter(unittest.TestCase):
    def _render_partial(self, rows, hide_archived=False, cursor=0):
        return templates.get_template("partials/_browse_files.html").render({
            "request": _Req(), "browse_rows": rows, "browse_count": len(rows),
            "browse_cursor": cursor, "browse_collapsed": 0, "browse_loaded": len(rows),
            "sel_tg": "10086", "sel_chat": "-100123", "sel_type": "media",
            "hide_archived": hide_archived,
        })

    def test_load_more_url_carries_hide_archived(self):
        """「加载更多」按钮 URL 必须携带 hide_archived，翻页后筛选不丢。"""
        html = self._render_partial([_row("a.mp4", True)], hide_archived=True, cursor=12345)
        self.assertIn("hide_archived=1", html)
        html2 = self._render_partial([_row("b.mp4", False)], hide_archived=False, cursor=99)
        self.assertIn("hide_archived=0", html2)

    def test_browse_page_state_and_checkbox(self):
        """/browse 页必须把筛选注入 __BROWSE_STATE 且复选框初始 checked 同步。"""
        from fastapi.testclient import TestClient
        import preview_server

        client = TestClient(preview_server.app)
        html = client.get("/browse").text
        # 全局状态里必须有 hideArchived 字段
        self.assertRegex(html, r"hideArchived:\s*(true|false)")
        # 勾选回调写状态 + 服务端重拉（而非纯 display:none）
        self.assertIn("__browseToggleHideArchived", html)
        m = re.search(r"__browseToggleHideArchived = function[\s\S]*?\n  \};", html)
        self.assertIsNotNone(m, "__browseToggleHideArchived 缺失")
        body = m.group(0)
        self.assertIn("hideArchived", body)
        self.assertIn("__browseReload", body, "筛选切换必须走服务端重拉")

    def test_reload_url_carries_hide_archived(self):
        """__browseReload 拼的 URL 必须带 hide_archived 参数。"""
        from fastapi.testclient import TestClient
        import preview_server

        client = TestClient(preview_server.app)
        html = client.get("/browse").text
        m = re.search(r"__browseReload = function[\s\S]*?\n  \};", html)
        self.assertIsNotNone(m, "__browseReload 缺失")
        self.assertIn("hide_archived=", m.group(0))


if __name__ == "__main__":
    unittest.main()
