# -*- coding: utf-8 -*-
"""
tests/test_explorer_view_toggle.py — /explorer「网格视图 / 列表视图」切换与卡片精简的回归测试
================================================================================================
覆盖：
1. 非根路径页面含 viewMode 状态、localStorage 持久化 key（explorer_view_mode）与 setViewMode 方法
2. 顶部工具栏渲染「网格 / 列表」两个切换按钮（.view-switch，默认网格为 primary）
3. 卡片网格容器挂 :class 动态 view-list，且模板内联了 view-list 相关 CSS（同一 DOM 换布局）
4. 列表视图表头（.list-head）与每行的类型图标 / 大小 / 时间 / 网盘标签 / 播放按钮
5. 搜索过滤（x-show 依赖 q）在卡片上仍然存在，未因视图切换而复制 DOM
6. 根路径不做切换（无 .view-switch 按钮）
7. 顶栏 OpenList 域名按钮只显示「已配置好」，裸域名只进 title
8. 文件夹卡片不再使用大图缩略图区（无 42px 内联 svg、改用 t-folder-inline 小图标）
"""
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import bridge_server
from core.auth import _make_portal_token, PORTAL_COOKIE, CSRF_COOKIE, CSRF_HEADER

ENTRIES = [
    {
        "name": "clip.mp4",
        "path": "/onedrive/yello/clip.mp4",
        "is_dir": False,
        "size": 1024 * 1024 * 50,
        "size_human": "50.0 MB",
        "modified": "2026-08-20 15:30",
        "thumb": "https://thumb.example.com/clip.jpg",
        "type": 2,
        "is_video": True,
        "ext": "MP4",
        "drive": "onedrive",
        "drive_label": "OneDrive",
        "openlist_url": "http://127.0.0.1:5244/onedrive/yello/clip.mp4",
    },
    {
        "name": "subfolder",
        "path": "/onedrive/yello/subfolder",
        "is_dir": True,
        "size": 0,
        "size_human": "—",
        "modified": "2026-08-20 12:00",
        "thumb": "",
        "type": 1,
        "is_video": False,
        "ext": "DIR",
        "drive": "onedrive",
        "drive_label": "OneDrive",
        "openlist_url": "http://127.0.0.1:5244/onedrive/yello/subfolder",
    },
]

SUB_FOLDER_DATA = {
    "ok": True,
    "path": "/onedrive/yello",
    "parent_path": "/onedrive",
    "breadcrumbs": [
        {"name": "全部网盘", "path": "/", "is_last": False},
        {"name": "onedrive", "path": "/onedrive", "is_last": False},
        {"name": "yello", "path": "/onedrive/yello", "is_last": True},
    ],
    "entries": ENTRIES,
    "dirs_count": 1,
    "files_count": 1,
    "total_size": 1024 * 1024 * 50,
    "total_size_human": "50.0 MB",
    "write": True,
    "message": "",
    "openlist_ready": True,
}

MOCK_STORAGES = [
    {
        "mount_path": "/onedrive", "name": "onedrive", "driver": "Onedrive",
        "display_name": "OneDrive", "status": "work", "status_label": "正常在线",
        "remark": "", "capacity_str": "就绪",
        "openlist_url": "http://127.0.0.1:5244/onedrive",
    },
]


class TestExplorerViewToggle(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(bridge_server.app)
        csrf = "csrf-token-view-toggle-test"
        self.cookies = {PORTAL_COOKIE: _make_portal_token(), CSRF_COOKIE: csrf}
        self.headers = {CSRF_HEADER: csrf}

    def _get_subfolder_page(self):
        with patch("services.openlist_service.openlist_fs_list_all", new=AsyncMock(return_value=SUB_FOLDER_DATA)), \
             patch("services.openlist_service._openlist_ready", new=AsyncMock(return_value=True)), \
             patch("services.openlist_service.openlist_storages", new=AsyncMock(return_value=MOCK_STORAGES)):
            resp = self.client.get("/explorer?path=/onedrive/yello", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def _get_root_page(self):
        with patch("services.openlist_service.openlist_storages", new=AsyncMock(return_value=MOCK_STORAGES)), \
             patch("services.openlist_service._openlist_ready", new=AsyncMock(return_value=True)):
            resp = self.client.get("/explorer?path=/", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        return resp.text

    # -------------------------------------------------------------
    # 1. Alpine 状态与持久化
    # -------------------------------------------------------------
    def test_view_mode_state_and_persistence(self):
        """视图模式是 Alpine 组件状态，默认 grid，并持久化到 localStorage['explorer_view_mode']。"""
        html = self._get_subfolder_page()
        self.assertIn("window.driveExplorer = function driveExplorer()", html)
        self.assertIn("viewMode:", html)
        self.assertIn("setViewMode: function (mode)", html)
        self.assertIn("localStorage.getItem('explorer_view_mode')", html)
        self.assertIn("localStorage.setItem('explorer_view_mode', m)", html)
        # 默认值为 grid（前缀查找，不依赖格式化空白）
        self.assertIn("'grid'", html)
        # 切换不刷新页面、不发请求：setViewMode 里不应出现 reload / fetch
        start = html.index("setViewMode: function (mode)")
        body = html[start:start + 400]
        self.assertNotIn("location.reload", body)
        self.assertNotIn("fetch(", body)

    # -------------------------------------------------------------
    # 2. 切换按钮
    # -------------------------------------------------------------
    def test_view_switch_buttons_rendered(self):
        """非根路径工具栏渲染「网格 / 列表」切换按钮，选中态用 primary。"""
        html = self._get_subfolder_page()
        self.assertIn('class="view-switch"', html)
        self.assertIn("setViewMode('grid')", html)
        self.assertIn("setViewMode('list')", html)
        self.assertIn("网格", html)
        self.assertIn("列表", html)
        # 图标来自 base.html 的 symbol
        self.assertIn('href="#ic-grid"', html)
        self.assertIn('href="#ic-list"', html)
        # 选中态绑定
        self.assertIn("viewMode === 'grid' ? 'primary' : 'ghost'", html)
        self.assertIn("viewMode === 'list' ? 'primary' : 'ghost'", html)

    # -------------------------------------------------------------
    # 2b. 统一工具条：搜索 + 视图切换 + 网盘下拉（⋯ 已上移导航栏）
    # -------------------------------------------------------------
    def test_toolbar_is_single_row_with_more_menu(self):
        """搜索/统计留在工具条；网盘下拉与视图切换已上移到左上角 .explorer-top-bar。
        ⋯ 菜单三项仍在导航栏「功能」，工具条里没有 .toolbar-more-btn。"""
        html = self._get_subfolder_page()
        self.assertIn('class="explorer-toolbar"', html)
        self.assertIn('class="toolbar-search"', html)
        self.assertIn('class="toolbar-stats"', html)
        self.assertIn('class="toolbar-menu drive-menu"', html)
        self.assertIn('x-show="showDriveMenu"', html)
        self.assertIn('@click.outside="showDriveMenu = false"', html)
        # 网盘下拉与视图切换都落在左上角顶栏内，不再出现在工具条里
        top_start = html.index('class="explorer-top-bar"')
        top_end = html.index('class="explorer-toolbar"')
        top_html = html[top_start:top_end]
        self.assertIn('drive-picker', top_html)
        self.assertIn('class="view-switch"', top_html)
        toolbar_start = html.index('class="explorer-toolbar"')
        toolbar_end = html.index('<!-- 进行中后台跨盘传输横幅通知 -->')
        toolbar_html = html[toolbar_start:toolbar_end]
        self.assertNotIn('drive-picker', toolbar_html)
        self.assertNotIn('class="view-switch"', toolbar_html)
        # ⋯ 菜单已整体移除
        self.assertNotIn('x-show="showMoreMenu"', html)
        self.assertNotIn("showMoreMenu", html)
        # 顶栏功能菜单持有这三项（不在工具条）
        self.assertIn("刷新缓存", html)
        self.assertIn("在 OpenList 中打开当前目录", html)
        self.assertIn("OpenList 域名：", html)
        toolbar_start = html.index('class="explorer-toolbar"')
        toolbar_end = html.index('<!-- 进行中后台跨盘传输横幅通知 -->')
        toolbar_html = html[toolbar_start:toolbar_end]
        self.assertEqual(toolbar_html.count("__refreshExplorerCache"), 0)
        self.assertEqual(toolbar_html.count("__openExplorerInOpenList"), 0)
        self.assertNotIn('class="btn sm ghost toolbar-more-btn"', toolbar_html)

    def test_toolbar_uniform_height_css(self):
        """工具条元素共用 --toolbar-h，窄屏搜索占满一行且不溢出。"""
        html = self._get_subfolder_page()
        self.assertIn("--toolbar-h: 36px", html)
        self.assertIn("height: var(--toolbar-h)", html)
        self.assertIn(".explorer-toolbar .btn,", html)
        self.assertIn(".toolbar-search { flex: 1 1 100%; max-width: none;", html)
        self.assertIn(".toolbar-stats { display: none; }", html)
        self.assertIn("padding-left: 32px;", html)
        self.assertIn("left: 9px;", html)
        self.assertIn("margin-left: 12px;", html)

    # -------------------------------------------------------------
    # 3. 同一 DOM + CSS 换布局
    # -------------------------------------------------------------
    def test_single_dom_with_view_list_class_and_css(self):
        """卡片网格容器用 :class 动态加 view-list，模板内联对应 CSS（不复制 DOM）。"""
        html = self._get_subfolder_page()
        self.assertIn('<div class="thumb-grid" :class="viewMode === \'list\' ? \'view-list\' : \'\'">', html)
        # 内联 CSS 规则
        self.assertIn(".thumb-grid.view-list {", html)
        self.assertIn(".thumb-grid.view-list .thumb {", html)
        self.assertIn(".thumb-grid.view-list .t-img", html)
        self.assertIn(".thumb-grid.view-list .t-name", html)
        self.assertIn(".thumb-grid.view-list .t-foot", html)
        # 移动端精简列
        self.assertIn("@media (max-width: 720px)", html)
        self.assertIn(".thumb-grid.view-list .thumb {", html)
        # 只有一个卡片网格容器（没有为列表视图复制第二套卡片）
        self.assertEqual(html.count('class="thumb-grid"'), 1)
        self.assertEqual(html.count('class="thumb folder-card"'), 1)
        self.assertEqual(html.count('class="thumb explorer-card"'), 1)

    # -------------------------------------------------------------
    # 4. 列表视图的列内容
    # -------------------------------------------------------------
    def test_list_view_columns(self):
        """表头 名称/大小/修改时间/所属网盘/操作 + 行内类型图标与播放按钮。"""
        html = self._get_subfolder_page()
        self.assertIn('class="list-head"', html)
        # 名称/大小/修改时间已改为可点排序的表头按钮（见 test_explorer_sort.py），
        # 其余两列仍是普通文本列。断言用「列名 + 其后的排序标记」而不是把
        # 换行/缩进写进匹配串 —— 模板排版一改就会误报。
        # 只在表头区块里找，否则会先命中工具条排序下拉里的 <option>大小</option>。
        # 匹配前把换行/缩进压平，模板排版调整不会误报。
        head_start = html.index('class="list-head"')
        head_flat = " ".join(html[head_start:head_start + 1600].split())
        for label in ("名称", "大小", "修改时间"):
            self.assertIn(label, head_flat)
            after = head_flat.split(label, 1)[1][:160]
            self.assertIn('class="lh-sort-mark"', after,
                          f"表头列 {label} 应为可点排序按钮")
        for label in ("所属网盘", "操作"):
            self.assertIn(f">{label}</span>", html)
        # 行内大小 / 时间 / 网盘标签
        self.assertIn('class="tnum t-size"', html)
        self.assertIn("50.0 MB", html)
        self.assertIn('class="tnum t-time"', html)
        self.assertIn("2026-08-20 15:30", html)
        self.assertIn('class="t-drive-tag"', html)
        # 视频行保留在线播放入口（列表视图专用「播放」按钮 + 名称点击）
        self.assertIn('class="btn sm primary t-play-btn"', html)
        self.assertIn("window.__playCloudVideo('/onedrive/yello/clip.mp4'", html)
        # 操作按钮齐备（文件：OpenList/改名/复制/移动/下载）
        self.assertIn(">OpenList</a>", html)
        self.assertIn(">改名</button>", html)
        self.assertIn(">复制</button>", html)
        self.assertIn(">移动</button>", html)
        self.assertIn("/api/explorer/download?path=", html)
        # 文件夹行：进入 / 改名 / OpenList
        self.assertIn(">进入</a>", html)

    # -------------------------------------------------------------
    # 5. 搜索过滤在两视图下都生效
    # -------------------------------------------------------------
    def test_search_filter_applies_to_single_card_set(self):
        """x-show 过滤条件仍挂在唯一的卡片集合上，切换视图不影响过滤。"""
        html = self._get_subfolder_page()
        self.assertIn('x-model="q"', html)
        # 属性用单引号包裹，避免 tojson 的双引号截断属性（曾导致过滤完全失效）
        self.assertIn("""x-show='!q || "clip.mp4".toLowerCase().includes(q.toLowerCase())'""", html)
        self.assertIn("""x-show='!q || "subfolder".toLowerCase().includes(q.toLowerCase())'""", html)
        # 两个卡片各一次，没有重复的卡片集合
        self.assertEqual(html.count("includes(q.toLowerCase())"), 2)

    # -------------------------------------------------------------
    # 6. 根路径：可交互网盘列表 + 统计，且不做视图切换
    # -------------------------------------------------------------
    def test_root_has_no_all_drives_row(self):
        """根页面网盘大列表已整体移除：主视图是「在全部网盘查询」搜索组件，
        下拉里仍保留「全部网盘」项（返回根）。"""
        html = self._get_root_page()
        self.assertNotIn('drive-root-all', html)
        # 大列表整套元素已删
        self.assertNotIn('class="drive-root-list"', html)
        self.assertNotIn('class="drive-root-card"', html)
        # 主视图是搜索组件
        self.assertIn('class="root-search"', html)
        self.assertIn("在全部网盘查询", html)

    def test_root_page_has_no_view_switch(self):
        """根路径（全部网盘）不出现视图切换按钮，也不出现视图态切换。"""
        html = self._get_root_page()
        self.assertNotIn('class="view-switch"', html)
        self.assertNotIn("setViewMode('list')", html)
        # 根页面不再有网盘列表行
        self.assertNotIn('class="drive-grid"', html)
        self.assertNotIn('class="drive-root-list"', html)
        self.assertIn('class="root-search"', html)
        self.assertIn('drive-picker-btn', html)

    def test_root_drive_picker_lists_all_drives(self):
        """点「全部网盘」弹出迷你目录浏览器：骨架（面包屑/列表/底栏）齐全，
        根层级盘符数据（display_name + mount_path）由模板渲染进 JS，不再有整卡大卡片。"""
        drives = [
            {"mount_path": p, "name": p.strip("/"), "driver": d, "display_name": n,
             "status": "work", "status_label": "正常在线", "remark": "",
             "capacity_str": "就绪", "openlist_url": "http://127.0.0.1:5244" + p,
             "stats": {"dirs": 1, "files": 2, "size": "3 MB"}}
            for p, d, n in (("/google", "GoogleDrive", "Google Drive"),
                            ("/onedrive", "Onedrive", "OneDrive"),
                            ("/wopan", "WoPan", "沃家云盘"),
                            ("/quark", "Quark", "夸克网盘"))
        ]
        with patch("services.openlist_service.openlist_storages", new=AsyncMock(return_value=drives)), \
             patch("services.openlist_service._openlist_ready", new=AsyncMock(return_value=True)), \
             patch("routers.explorer._attach_drive_stats", new=AsyncMock(return_value=None)):
            html = self.client.get("/explorer?path=/", cookies=self.cookies).text
        self.assertIn('x-show="showDriveMenu"', html)
        self.assertIn('toolbar-menu drive-menu', html)
        # 迷你目录浏览器骨架与数据源
        self.assertIn('id="driveCrumb"', html)
        self.assertIn('id="driveDirList"', html)
        self.assertIn('id="driveOpenBtn"', html)
        self.assertIn('__driveBrowseOpen', html)
        self.assertIn('__driveBrowseLoad', html)
        self.assertIn('__driveStorages', html)
        # 根层级盘符（名 + 挂载路径）已渲染进 JS 数据（tojson 会转义非 ASCII，挂载路径原样）
        for path in ("/google", "/onedrive", "/wopan", "/quark"):
            self.assertIn(path, html)
        # 旧的大卡片整套元素已移除
        for legacy in ('class="drive-card"', 'class="drive-card-stats"',
                       'drive-enter-btn', 'drive-openlist-btn'):
            self.assertNotIn(legacy, html, f"遗留大卡片元素未清理: {legacy}")

    def test_root_drive_stats_from_backend(self):
        """后端补齐 stats 时根目录正常渲染（stats 原渲染在网盘列表行，该列表已移除；
        现在只要求页面不因 stats 报错，仍是「在全部网盘查询」搜索组件）。"""
        stats = {"dirs": 3, "files": 7, "size": "1.5 GB"}
        storages = [dict(MOCK_STORAGES[0])]
        storages[0]["stats"] = stats
        with patch("services.openlist_service.openlist_storages", new=AsyncMock(return_value=storages)), \
             patch("services.openlist_service._openlist_ready", new=AsyncMock(return_value=True)), \
             patch("routers.explorer._attach_drive_stats", new=AsyncMock(return_value=None)):
            resp = self.client.get("/explorer?path=/", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn('class="root-search"', html)
        self.assertNotIn('class="drive-root-list"', html)

    def test_attach_drive_stats_concurrent_and_cached(self):
        """_attach_drive_stats 并发拉取并写入 stats；单个网盘失败只影响自己。"""
        import asyncio
        import routers.explorer as rx

        async def fake_list(path, refresh=False):
            if path == "/broken":
                raise RuntimeError("openlist boom")
            return {
                "ok": True,
                "entries": [
                    {"name": "a", "is_dir": True, "size": 0},
                    {"name": "b", "is_dir": False, "size": 2048},
                ],
                "dirs_count": 1,
                "files_count": 1,
                "total_size_human": "2.0 KB",
            }

        storages = [
            {"mount_path": "/one"},
            {"mount_path": "/two"},
            {"mount_path": "/broken"},
        ]
        rx._DRIVE_STATS_CACHE.clear()
        with patch.object(rx._openlist_mod, "openlist_fs_list_all", new=fake_list):
            asyncio.run(rx._attach_drive_stats(storages))
        self.assertEqual(storages[0]["stats"], {"dirs": 1, "files": 1, "size": "2.0 KB"})
        self.assertEqual(storages[1]["stats"], {"dirs": 1, "files": 1, "size": "2.0 KB"})
        self.assertIsNone(storages[2]["stats"])
        # 缓存命中：第二次不再调用列表接口
        storages2 = [{"mount_path": "/one"}, {"mount_path": "/two"}]
        with patch.object(rx._openlist_mod, "openlist_fs_list_all",
                          new=AsyncMock(side_effect=AssertionError("should be cached"))):
            asyncio.run(rx._attach_drive_stats(storages2))
        self.assertEqual(storages2[0]["stats"]["files"], 1)
        rx._DRIVE_STATS_CACHE.clear()

    # -------------------------------------------------------------
    # 7. OpenList 域名入口上移到顶部导航栏「功能」菜单
    # -------------------------------------------------------------
    def test_openlist_domain_entry_in_more_menu(self):
        """域名入口只在顶栏「功能」菜单里出现，文案为「OpenList 域名：<host>」，
        工具条不再有任何菜单按钮（⋯ 已上移）。"""
        html = self._get_subfolder_page()
        self.assertIn("featureOpenlistBase ? ('OpenList 域名：' + featureDisplayDomain(featureOpenlistBase) + '（去设置修改）') : '设置 OpenList 域名'", html)
        # 域名配置已收敛到设置页：各页面不得再自建域名弹窗（那是「这页配了那页没配」的根源）
        self.assertNotIn("openDomainModal", html, "浏览页不应再有自己的域名弹窗")
        self.assertIn('href="/settings#openlistPublicBase"', html, "顶栏域名入口应直达设置页")
        # 旧的「已配置好」大按钮文案已移除
        self.assertNotIn("openlistBase ? '已配置好' : '设置域名'", html)
        self.assertNotIn("'OpenList: ' + displayDomain(openlistBase)", html)
        # 域名按钮只剩一处（顶栏功能菜单）
        self.assertEqual(html.count("featureDisplayDomain(featureOpenlistBase)"), 1)
        # 工具条里不再有 ⋯ 菜单：三项操作全部上移到导航栏
        toolbar_start = html.index('class="explorer-toolbar"')
        toolbar_end = html.index('<!-- 进行中后台跨盘传输横幅通知 -->')
        toolbar_html = html[toolbar_start:toolbar_end]
        self.assertEqual(toolbar_html.count("__refreshExplorerCache"), 0)
        self.assertEqual(toolbar_html.count("__openExplorerInOpenList"), 0)
        self.assertNotIn('class="toolbar-more-btn"', toolbar_html)
        # 导航栏渲染「功能」按钮与 title（仅 /explorer）
        self.assertIn('title="功能仅在当前浏览页面生效"', html)
        self.assertIn('x-data="navFeatureMenu()"', html)
        self.assertIn("navFeatureMenu", html)
        # 刷新缓存/外跳/域名三项在功能菜单里
        self.assertIn("刷新缓存", html)
        self.assertIn("在 OpenList 中打开当前目录", html)

    # -------------------------------------------------------------
    # 8. 文件夹卡片不再用大图缩略图区
    # -------------------------------------------------------------
    def test_folder_card_drops_big_thumbnail(self):
        """文件夹卡片隐藏大图缩略图区，改用文件名前 15px 小图标；媒体卡片缩略图不变。"""
        html = self._get_subfolder_page()
        self.assertIn(".thumb-grid:not(.view-list) .thumb.folder-card .t-img { display: none; }", html)
        self.assertIn('class="t-folder-inline"', html)
        # 旧的 42px 内联图标已移除
        self.assertNotIn('style="width:42px;height:42px;"', html)
        # 媒体卡片仍然保留真实缩略图
        self.assertIn('https://thumb.example.com/clip.jpg', html)


class TestBrowseMemory(unittest.TestCase):
    """网盘浏览位置记忆（用户诉求：刷新/重进标签还回到上次点的文件夹）。

    双层记忆：
    - 服务端 .browse_memory.json：裸 /explorer（侧栏链接、新标签页、换浏览器）
      302 续播到上次目录；网盘被移除后不续播（防死链）；显式 ?path= 永不重定向。
    - 客户端 localStorage（explorer_scroll:<path>）：刷新后滚动位置还原。
    """

    def setUp(self):
        self.client = TestClient(bridge_server.app)
        self.cookies = {PORTAL_COOKIE: _make_portal_token()}
        import core.state as cs
        self.cs = cs
        self._saved = dict(cs._BROWSE_MEMORY)

    def tearDown(self):
        self.cs._BROWSE_MEMORY.clear()
        self.cs._BROWSE_MEMORY.update(self._saved)

    def _mock_root(self, mounts=("/onedrive", "/wopan")):
        from unittest.mock import AsyncMock, patch
        storages = [{"mount_path": m, "name": m.strip("/"), "driver": "X",
                     "display_name": m.strip("/").title(), "status": "work",
                     "status_label": "正常在线", "remark": "", "capacity_str": "就绪",
                     "openlist_url": "http://x" + m} for m in mounts]
        return patch("services.openlist_service.openlist_storages", new=AsyncMock(return_value=storages)), \
               patch("services.openlist_service._openlist_ready", new=AsyncMock(return_value=True)), \
               patch("services.openlist_service.openlist_mounts", new=AsyncMock(return_value={"ok": True, "mounts": list(mounts)})), \
               patch("routers.explorer._attach_drive_stats", new=AsyncMock(return_value=None))

    def test_bare_explorer_resumes_last_path(self):
        import core.state as cs
        cs._BROWSE_MEMORY["last_path"] = "/onedrive/yello"
        p1, p2, p3, p4 = self._mock_root()
        with p1, p2, p3, p4:
            r = self.client.get("/explorer", cookies=self.cookies, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("path=/onedrive/yello", r.headers.get("location", ""))

    def test_explicit_root_never_redirects(self):
        """根页入口（?path=/）必须永远能回到「全部网盘」，不被续播劫持。"""
        import core.state as cs
        cs._BROWSE_MEMORY["last_path"] = "/onedrive/yello"
        p1, p2, p3, p4 = self._mock_root()
        with p1, p2, p3, p4:
            r = self.client.get("/explorer?path=/", cookies=self.cookies, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="drive-workspace"', r.text)

    def test_dead_drive_not_resumed(self):
        """上次目录所属网盘已被移除 → 不续播（防 302 进错误页），正常落根。"""
        import core.state as cs
        cs._BROWSE_MEMORY["last_path"] = "/removed-drive/sub"
        p1, p2, p3, p4 = self._mock_root(("/onedrive", "/wopan"))
        with p1, p2, p3, p4:
            r = self.client.get("/explorer", cookies=self.cookies, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="drive-workspace"', r.text)

    def test_visit_recorded_and_chips_rendered(self):
        """成功列目录 → 记忆写入；根页渲染续播卡与常去网盘 chips（含 stats）。"""
        import core.state as cs
        cs._BROWSE_MEMORY.update({"last_path": "", "recent": {}})
        from unittest.mock import AsyncMock, patch
        storages = [{"mount_path": "/onedrive", "name": "onedrive", "driver": "X",
                     "display_name": "OneDrive", "status": "work", "status_label": "正常在线",
                     "remark": "", "capacity_str": "就绪", "openlist_url": "http://x",
                     "stats": {"dirs": 2, "files": 14, "size": "18.4 GB"}}]
        list_ok = {"ok": True, "path": "/onedrive/yello", "parent_path": "/onedrive",
                   "breadcrumbs": [], "entries": [], "dirs_count": 0, "files_count": 0,
                   "total_size_human": "—", "write": True, "message": ""}
        with patch("services.openlist_service.openlist_storages", new=AsyncMock(return_value=storages)), \
             patch("services.openlist_service._openlist_ready", new=AsyncMock(return_value=True)), \
             patch("services.openlist_service.openlist_mounts", new=AsyncMock(return_value={"ok": True, "mounts": ["/onedrive"]})), \
             patch("services.openlist_service.openlist_fs_list_all", new=AsyncMock(return_value=list_ok)):
            r1 = self.client.get("/explorer?path=/onedrive/yello", cookies=self.cookies)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(cs._BROWSE_MEMORY["last_path"], "/onedrive/yello")
        self.assertGreaterEqual(cs._BROWSE_MEMORY["recent"].get("/onedrive", 0), 1)
        # 根页：续播卡 + chips（stats 真正渲染，此前算了就扔）
        with patch("services.openlist_service.openlist_storages", new=AsyncMock(return_value=storages)), \
             patch("services.openlist_service._openlist_ready", new=AsyncMock(return_value=True)), \
             patch("services.openlist_service.openlist_mounts", new=AsyncMock(return_value={"ok": True, "mounts": ["/onedrive"]})), \
             patch("routers.explorer._attach_drive_stats", new=AsyncMock(return_value=None)):
            r2 = self.client.get("/explorer?path=/", cookies=self.cookies)
        html = r2.text
        self.assertIn('class="resume-card"', html)
        self.assertIn("/onedrive/yello", html)
        self.assertIn('class="drive-chip"', html)
        self.assertIn("18.4 GB", html)

    def test_scroll_memory_wired(self):
        """滚动位置记忆：按目录路径分键存 localStorage，加载时还原。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "templates", "explorer.html"), encoding="utf-8").read()
        self.assertIn("explorer_scroll:", src)
        self.assertIn("__bindScrollMemory", src)

    def test_memory_persist_roundtrip(self):
        """.browse_memory.json 落盘 + 重启恢复。"""
        import core.state as cs
        cs._BROWSE_MEMORY.update({"last_path": "/google/a", "recent": {"/google": 3}})
        cs._browse_memory_save()
        cs._BROWSE_MEMORY.clear()
        cs._BROWSE_MEMORY.update({"last_path": "", "updated_at": 0.0, "recent": {}})
        cs._browse_memory_load()
        self.assertEqual(cs._BROWSE_MEMORY["last_path"], "/google/a")
        self.assertEqual(cs._BROWSE_MEMORY["recent"]["/google"], 3)


if __name__ == "__main__":
    unittest.main()
