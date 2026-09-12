# -*- coding: utf-8 -*-
r"""回归测试：浏览页滚动自动加载下一页（无限滚动）。

需求：浏览下载页不要每次手动点「加载更多」，滚到底部应自动加载下一页。

实现约束：
- 哨兵行 #browse-more-row 保留（含兜底按钮 + spinner），带 data-cursor；
- IntersectionObserver 逻辑在 browse.html 内联脚本中（防抖 / 预加载 / 重挂）；
- 已到末页（无游标）时不得渲染哨兵，避免观察器空转；
- 空列表必须仍显示空态，不得因本改动丢失。
"""
import re
import unittest

from core.templates import templates


class _Req:
    scope = {"type": "http"}


def _row(n):
    return {"telegramId": 1, "chatId": -1, "messageId": n, "fileId": n,
            "uniqueId": "u%d" % n, "name": "f%d.mp4" % n, "ext": "MP4",
            "size_str": "1 MB", "date_str": "2026-09-09", "type": "video",
            "type_label": "视频", "thumb": "", "thumb_uid": "", "dl": "idle",
            "tr": "idle", "dur_str": "", "is_archived": False,
            "is_archiving": False, "cloud_path": "", "cloud_drive": "",
            "openlist_url": "", "archived_date": "", "local_path": ""}


def _render_fragment(cursor, rows=None, count=100, loaded=30, collapsed=0):
    return templates.get_template("partials/_browse_files.html").render({
        "request": _Req(),
        "browse_rows": [_row(i) for i in range(1, 4)] if rows is None else rows,
        "browse_count": count, "browse_cursor": cursor,
        "browse_collapsed": collapsed, "browse_loaded": loaded,
        "sel_tg": "1", "sel_chat": "-1", "sel_type": "video",
    })

def _browse_page_html():
    """经真实 TestClient 拿整页，避免手拼上下文缺字段。"""
    from fastapi.testclient import TestClient
    import bridge_server
    client = TestClient(bridge_server.app)
    r = client.get("/browse", cookies={bridge_server.PORTAL_COOKIE: bridge_server._make_portal_token()})
    assert r.status_code == 200
    return r.text


class TestInfiniteScrollSentinel(unittest.TestCase):
    def test_cursor_renders_sentinel_with_data_cursor(self):
        """有游标：哨兵行存在且带 data-cursor 与兜底按钮。"""
        html = _render_fragment(cursor="12345")
        self.assertIn('id="browse-more-row"', html)
        self.assertIn('data-cursor="12345"', html)
        self.assertIn('id="browseMoreBtn"', html)
        self.assertIn('id="browseMoreSpin"', html)

    def test_last_page_has_no_sentinel(self):
        """已到末页（无游标）：不渲染哨兵，避免观察器空转。"""
        html = _render_fragment(cursor="", loaded=100, count=100)
        self.assertNotIn("browse-more-row", html)
        # 空态也不得误显（列表其实非空）
        self.assertNotIn("没有匹配的文件", html)

    def test_empty_list_still_shows_empty_state(self):
        """空列表仍显示空态，不因本改动丢失。"""
        html = _render_fragment(cursor="", rows=[], count=0, loaded=0)
        self.assertIn("没有匹配的文件", html)
        self.assertNotIn("browse-more-row", html)

    def test_sentinel_keeps_htmx_chain(self):
        """哨兵按钮仍走 htmx（GET /partials/browse-files + outerHTML swap）。"""
        html = _render_fragment(cursor="77")
        self.assertIn('hx-get="/partials/browse-files?', html)
        self.assertIn("cursor=77", html)
        self.assertIn('hx-target="#browse-more-row"', html)
        self.assertIn('hx-swap="outerHTML"', html)

    def test_loaded_count_shown(self):
        """「已列 X / 共 Y」进度与折叠提示仍展示。"""
        html = _render_fragment(cursor="9", loaded=30, count=100, collapsed=4)
        self.assertIn("30", html)
        self.assertIn("100", html)
        self.assertIn("已折叠", html)


class TestBrowsePageAutoLoadScript(unittest.TestCase):
    """browse.html 内联脚本必须具备自动加载的全部要素。"""

    @classmethod
    def setUpClass(cls):
        cls.html = _browse_page_html()
        scripts = re.findall(r"<script>(.*?)</script>", cls.html, re.S)
        cls.js = "\n;\n".join(s for s in scripts if "IntersectionObserver" in s)

    def test_page_uses_intersection_observer(self):
        self.assertIn("IntersectionObserver", self.html)

    def test_debounce_guard_present(self):
        """在途防抖必须存在，否则快速滚动会重复请求同一页。"""
        self.assertIn("inflight", self.js)
        self.assertIn("htmx:afterSwap", self.js)

    def test_error_releases_debounce(self):
        """请求失败必须放开防抖，否则一次网络错误就永久卡住。"""
        self.assertIn("htmx:responseError", self.js)
        self.assertIn("htmx:sendError", self.js)
    def test_preload_margin(self):
        """提前预加载，滚动更顺滑。"""
        self.assertIn("rootMargin", self.js)

    def test_reattaches_to_new_sentinel(self):
        """新片段插入后必须重挂观察器（outerHTML 换掉了旧哨兵）。"""
        self.assertIn("browse-more-row", self.js)

    def test_after_swap_rearms_without_target_filter(self):
        """核心回归：afterSwap 不得限定 swap 目标。

        加载下一页的 swap 目标是 #browse-more-row（outerHTML 自替换），
        旧实现只认 #browse-rows，导致第一页加载完后观察器仍挂在已移除的
        旧哨兵上——之后滚动永远不再触发（用户报告「滚动刷新不生效」）。
        """
        m = re.search(r"htmx:afterSwap', function \(\) \{(.*?)\}\);", self.js, re.S)
        self.assertIsNotNone(m, "afterSwap 处理器缺失")
        body = m.group(1)
        self.assertNotIn("browse-rows", body, "afterSwap 不得按目标 ID 过滤")
        self.assertIn("arm()", body, "afterSwap 必须无条件重挂观察器")

    def test_safety_timeout_releases_stuck_inflight(self):
        """3 秒安全复位：swap 事件缺失时防抖也能自动解除，不永久卡死。"""
        self.assertIn("scheduleSafetyReset", self.js)
        self.assertIn("3000", self.js)

    def test_fallback_button_click_reuse(self):
        """自动加载复用兜底按钮的 click，共享同一条 htmx 链路。"""
        self.assertIn("btn.click()", self.js)

    def test_graceful_without_intersection_observer(self):
        """老浏览器没有 IntersectionObserver 时优雅退化（不报错）。"""
        self.assertIn("'IntersectionObserver' in window", self.js)


if __name__ == "__main__":
    unittest.main()
