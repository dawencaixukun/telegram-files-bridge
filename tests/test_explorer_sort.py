# -*- coding: utf-8 -*-
"""
tests/test_explorer_sort.py
===========================
回归测试：浏览页（网盘）按 名称 / 大小 / 修改时间 排序。

历史问题：网格与列表视图里「大小」「修改时间」只是纯文本，点了没反应。
文件管理器不能排序，等于只能靠肉眼在几百个文件里找最大/最新的那个。

本文件锁定六条契约：
1. 服务端为每个条目下发数值型 mtime（epoch 秒），前端只比数值不比字符串 ——
   字符串比较在「只给日期」「缺秒」「带时区」时会给出错误顺序。
2. mtime 解析吃得住 OpenList 的多种时间格式，解析不出来退化为 0 而不是崩。
3. 卡片带 data-size-bytes / data-mtime / data-sort-name / data-is-dir，
   排序函数才有东西可比。
4. 勾选状态在重排后保留 —— 重排只移动节点，不能重建卡片。
5. 排序偏好持久化到 localStorage（用户的时间排序习惯不该每次重设）。
6. 文件夹恒在文件前面，与排序字段无关。
"""
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import bridge_server
from core.auth import _make_portal_token, PORTAL_COOKIE, CSRF_COOKIE
from services.openlist_service import _openlist_mtime_epoch

ENTRIES = [
    {"name": "big.mp4", "path": "/onedrive/d/big.mp4", "is_dir": False,
     "size": 900 * 1024 * 1024, "size_human": "900 MB",
     "modified": "2026-08-01 10:00:00", "mtime": 1785000000.0,
     "thumb": "", "type": 2, "is_video": True, "ext": "MP4",
     "drive": "onedrive", "drive_label": "OneDrive", "openlist_url": "http://x/1"},
    {"name": "small.mp4", "path": "/onedrive/d/small.mp4", "is_dir": False,
     "size": 10 * 1024 * 1024, "size_human": "10 MB",
     "modified": "2026-09-15 10:00:00", "mtime": 1789000000.0,
     "thumb": "", "type": 2, "is_video": True, "ext": "MP4",
     "drive": "onedrive", "drive_label": "OneDrive", "openlist_url": "http://x/2"},
    {"name": "dirA", "path": "/onedrive/d/dirA", "is_dir": True,
     "size": 0, "size_human": "—", "modified": "2026-07-01 10:00:00",
     "mtime": 1780000000.0, "thumb": "", "type": 1, "is_video": False,
     "ext": "DIR", "drive": "onedrive", "drive_label": "OneDrive",
     "openlist_url": "http://x/3"},
]

LIST_DATA = {
    "ok": True, "path": "/onedrive/d", "parent_path": "/onedrive",
    "breadcrumbs": [{"name": "全部网盘", "path": "/", "is_last": False},
                    {"name": "onedrive", "path": "/onedrive", "is_last": False},
                    {"name": "d", "path": "/onedrive/d", "is_last": True}],
    "entries": ENTRIES, "dirs_count": 1, "files_count": 2,
    "total_size": 910 * 1024 * 1024, "total_size_human": "910 MB",
    "write": True, "message": "",
}
STORAGES = [{"mount_path": "/onedrive", "name": "onedrive", "driver": "Onedrive",
             "display_name": "OneDrive", "status": "work", "status_label": "正常在线",
             "remark": "", "capacity_str": "就绪", "openlist_url": "http://x"}]


class TestMtimeParsing(unittest.TestCase):
    """mtime 解析必须覆盖 OpenList 实际会返回的全部形态。"""

    def test_rfc3339_with_offset(self):
        self.assertGreater(_openlist_mtime_epoch("2026-08-20T15:30:12+08:00"), 0)

    def test_utc_z_suffix(self):
        self.assertGreater(_openlist_mtime_epoch("2026-08-20T15:30:12Z"), 0)

    def test_naive_datetime(self):
        self.assertGreater(_openlist_mtime_epoch("2026-08-20 15:30:12"), 0)

    def test_minute_precision(self):
        self.assertGreater(_openlist_mtime_epoch("2026-08-20T15:30"), 0)

    def test_date_only(self):
        self.assertGreater(_openlist_mtime_epoch("2026-08-20"), 0)

    def test_ordering_is_correct(self):
        """核心契约：解析结果的大小顺序必须和时间真实先后一致。"""
        older = _openlist_mtime_epoch("2026-08-01 10:00:00")
        newer = _openlist_mtime_epoch("2026-09-15 10:00:00")
        self.assertLess(older, newer)

    def test_timezone_offset_affects_instant(self):
        """同一墙上时间、不同时区，UTC 时刻必须不同 —— 否则跨时区混排就错。"""
        local = _openlist_mtime_epoch("2026-08-20T15:30:12+08:00")
        utc = _openlist_mtime_epoch("2026-08-20T15:30:12+00:00")
        self.assertAlmostEqual(utc - local, 8 * 3600, delta=1)

    def test_empty_and_zero_value(self):
        self.assertEqual(_openlist_mtime_epoch(""), 0.0)
        self.assertEqual(_openlist_mtime_epoch(None), 0.0)
        self.assertEqual(_openlist_mtime_epoch("0001-01-01T00:00:00Z"), 0.0,
                         "Go 零值时间代表「未提供」，不该当成公元 1 年的真实时刻")

    def test_garbage_does_not_raise(self):
        self.assertEqual(_openlist_mtime_epoch("garbage"), 0.0)
        self.assertEqual(_openlist_mtime_epoch("2026-13-45"), 0.0)


class TestEntryHasSortFields(unittest.TestCase):
    """服务端必须下发数值字段，前端才有东西可比。"""

    def test_entries_carry_mtime(self):
        import asyncio
        import services.openlist_service as svc

        class FakeResp:
            status_code = 200

        async def fake_post(url, json=None, headers=None, **kw):
            return FakeResp()

        async def fake_token():
            return "Bearer t"

        payload = {"code": 200, "message": "success", "data": {"write": True, "content": [
            {"name": "a.mp4", "is_dir": False, "size": 100,
             "modified": "2026-08-20T15:30:12+08:00", "type": 2},
            {"name": "b", "is_dir": True, "size": 0, "modified": "2026-08-19T00:00:00Z"},
        ]}}
        with patch.object(svc._openlist_client, "post", new=fake_post), \
             patch.object(svc, "_openlist_token", fake_token), \
             patch.object(svc, "_openlist_env", return_value=(200, payload["data"], "success")):
            data = asyncio.run(svc.openlist_fs_list_all("/onedrive/d"))
        self.assertTrue(data["ok"])
        for e in data["entries"]:
            self.assertIn("mtime", e, f"条目缺少排序用的 mtime: {e.get('name')}")
            self.assertIsInstance(e["mtime"], float)
        a = next(e for e in data["entries"] if e["name"] == "a.mp4")
        self.assertGreater(a["mtime"], 0)
        self.assertEqual(a["modified"], "2026-08-20 15:30:12",
                         "展示用字符串保持原有的人类可读格式（T 换成空格）")


class TestSortUi(unittest.TestCase):
    """/explorer 页面：排序控件、数据属性、重排逻辑。"""

    def setUp(self):
        self.client = TestClient(bridge_server.app)
        self.cookies = {PORTAL_COOKIE: _make_portal_token(), CSRF_COOKIE: "c"}

    def _page(self):
        with patch("services.openlist_service.openlist_fs_list_all",
                   new=AsyncMock(return_value=LIST_DATA)), \
             patch("services.openlist_service._openlist_ready",
                   new=AsyncMock(return_value=True)), \
             patch("services.openlist_service.openlist_storages",
                   new=AsyncMock(return_value=STORAGES)), \
             patch("services.openlist_service.openlist_mounts",
                   new=AsyncMock(return_value={"ok": True, "mounts": ["/onedrive"]})):
            resp = self.client.get("/explorer?path=/onedrive/d", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def test_sort_control_rendered(self):
        html = self._page()
        self.assertIn('class="toolbar-sort"', html)
        self.assertIn("showSortMenu", html)
        self.assertIn("toggleSortDir()", html)

    def test_cards_carry_sort_attributes(self):
        """卡片必须带真实数值属性 —— 排序函数只认这些，不解析展示文本。"""
        html = self._page()
        self.assertIn('data-is-dir="1"', html)   # 文件夹
        self.assertIn('data-is-dir="0"', html)   # 文件
        # 渲染后的真实数值（不是模板字面量）
        self.assertIn('data-size-bytes="943718400"', html)
        self.assertIn('data-mtime="1785000000.0"', html)
        self.assertIn('data-mtime="1789000000.0"', html)
        self.assertIn('data-sort-name="big.mp4"', html)
        self.assertIn('data-sort-name="dirA"', html)

    def test_template_declares_sort_attributes(self):
        """模板源里三个卡片类型都要声明排序属性（漏一个就会静默不排序）。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "templates", "explorer.html"), encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count("data-sort-name=\"{{ f.name }}\""), 2,
                         "文件夹卡片与文件卡片都要有排序属性")
        self.assertEqual(src.count("data-mtime=\"{{ f.mtime or 0 }}\""), 2)
        self.assertEqual(src.count("data-size-bytes=\"{{ f.size or 0 }}\""), 2)

    def test_list_header_columns_sortable(self):
        html = self._page()
        self.assertIn("sortByColumn('name')", html)
        self.assertIn("sortByColumn('size')", html)
        self.assertIn("sortByColumn('mtime')", html)
        self.assertIn('class="lh-size lh-sort"', html)
        self.assertIn("aria-sort", html)

    def test_apply_sort_moves_nodes_not_rebuilds(self):
        """重排必须只移动节点：重建卡片会丢勾选状态与图片缓存。"""
        html = self._page()
        self.assertIn("applySort: function ()", html)
        self.assertIn("document.createDocumentFragment()", html)
        self.assertIn("grid.insertBefore(frag", html)
        # 不能有 innerHTML 重建
        start = html.index("applySort: function ()")
        body = html[start:start + 2200]
        self.assertNotIn("innerHTML", body, "排序不能重建 DOM，否则勾选状态全丢")
        self.assertNotIn("location.reload", body)

    def test_folders_grouped_before_files(self):
        html = self._page()
        self.assertIn("// ① 文件夹分组优先（与排序字段无关）", html)
        self.assertIn("if (ad !== bd) return ad - bd;", html)

    def test_name_is_stable_tiebreaker(self):
        """大小/时间相同的项必须有确定的最终顺序，否则每次排序位置都在变。"""
        html = self._page()
        self.assertIn("localeCompare", html)
        self.assertIn("data-sort-name", html)

    def test_sort_persisted(self):
        html = self._page()
        self.assertIn("explorer_sort_key", html)
        self.assertIn("explorer_sort_dir", html)
        self.assertIn("localStorage.setItem('explorer_sort_key'", html)
        self.assertIn("localStorage.setItem('explorer_sort_dir'", html)

    def test_first_paint_applies_saved_sort(self):
        """首屏就要按上次偏好重排，否则用户会看到「设置没生效」一瞬。"""
        html = self._page()
        self.assertIn("if (self.sortKey !== 'name' || self.sortDir !== 'asc') self.applySort();", html)

    def test_click_same_column_toggles_direction(self):
        html = self._page()
        start = html.index("sortByColumn: function (key)")
        body = html[start:start + 500]
        self.assertIn("if (this.sortKey === key)", body)
        self.assertIn("this.toggleSortDir();", body)

    def test_sort_menu_is_self_drawn(self):
        """排序下拉必须是自绘面板，不能用原生 <select>（用户反馈：暗夜主题看不清）。

        原生 select 的展开列表由浏览器 UA 绘制，option 底色和选中高亮在部分
        浏览器上改不动，暗色主题下就是一块看不清的面板。自绘面板直接吃
        tokens.css 的主题变量，深浅两套主题才可能稳定一致。
        """
        html = self._page()
        self.assertNotIn("toolbar-sort-select", html,
                         "排序控件退回了原生 <select>，暗色主题下展开面板会失控")
        self.assertNotIn("<select", html.split('class="toolbar-sort"')[1].split("</div>")[0])
        # 面板复用 .toolbar-menu：底色/描边/悬停全部来自主题变量
        self.assertIn('class="toolbar-menu toolbar-sort-menu"', html)
        self.assertIn("pickSortKey", html)
        self.assertIn('class="toolbar-sort-check', html)

    def test_sort_menu_items_match_options(self):
        """下拉项与三项排序字段一一对应，且按钮上显示当前字段名。"""
        html = self._page()
        start = html.index("sortOptions:")
        opts = html[start:start + 260]
        for key in ("name", "size", "mtime"):
            self.assertIn("key: '%s'" % key, opts)
        for label in ("名称", "大小", "修改时间"):
            self.assertIn("label: '%s'" % label, opts)
        self.assertIn("get sortLabel()", html)
        self.assertIn("x-text=\"sortLabel\"", html)

    def test_color_scheme_follows_theme(self):
        """color-scheme 必须随 data-theme 覆盖，否则 UA 部件永远用浅色默认值。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "static", "css", "main.css"), encoding="utf-8") as f:
            css = f.read()
        self.assertIn('[data-theme="dark"] { color-scheme: dark; }', css)
        self.assertIn('[data-theme="light"] { color-scheme: light; }', css)

    def test_root_page_has_no_sort_control(self):
        """根页面没有条目可排，不应渲染排序控件。"""
        with patch("services.openlist_service.openlist_storages",
                   new=AsyncMock(return_value=STORAGES)), \
             patch("services.openlist_service._openlist_ready",
                   new=AsyncMock(return_value=True)), \
             patch("services.openlist_service.openlist_mounts",
                   new=AsyncMock(return_value={"ok": True, "mounts": ["/onedrive"]})), \
             patch("routers.explorer._attach_drive_stats", new=AsyncMock(return_value=None)):
            html = self.client.get("/explorer?path=/", cookies=self.cookies).text
        self.assertNotIn('class="toolbar-sort"', html)


if __name__ == "__main__":
    unittest.main()
