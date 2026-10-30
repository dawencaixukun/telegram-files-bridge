# -*- coding: utf-8 -*-
"""
tests/test_disk_breakdown.py
============================
回归测试：仪表盘「本地磁盘占用」必须能下钻到目录。

历史问题：KPI 只给一个分区级数字（27 GB），用户看不出这些空间被什么占了 ——
是归档下载残留、TDLib 媒体缓存、系统日志，还是 docker 镜像。于是「要不要清、
清哪个」全靠猜。

本文件锁定六条契约：
1. 口径用 st_blocks（实际分配块），不是 st_size —— 两者能差 GB 级，
   只有前者能与 df 的「已用」对上。
2. 硬链接按 (st_dev, st_ino) 去重，同一 inode 只算一次。
3. 不跟随符号链接、不跨设备。
4. 直接躺在目标目录下的散文件（/swapfile 这类）必须单独成行，
   不能静默并进「自身占用」。
5. 「自身占用」= 总量 - 子目录 - 已列散文件，不得与已列条目重复计数。
6. 扫描结果带缓存，?refresh 能强制重扫；超时/跳过要如实标记，不许静默截断。
"""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

import bridge_server
from services.disk_service import (
    _disk_mount_point, _disk_rel_depth, _disk_scan_subtree,
    disk_breakdown, disk_cache_clear, disk_kpi_hint,
)

BLOCK = 512


def _write(path, size):
    """写入指定字节数的文件，返回实际占用（st_blocks * 512）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    return os.stat(path).st_blocks * BLOCK


class _TmpTree(unittest.TestCase):
    def setUp(self):
        disk_cache_clear()
        self.root = tempfile.mkdtemp(prefix="tg_disk_")

    def tearDown(self):
        disk_cache_clear()
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)


class TestScanSemantics(_TmpTree):
    """扫描口径：分配块、硬链接去重、不跟符号链接。"""

    def test_uses_allocated_blocks_not_apparent_size(self):
        """稀疏文件 st_size 很大但几乎不占块 —— 必须按分配块计。"""
        path = os.path.join(self.root, "sparse.bin")
        with open(path, "wb") as f:
            f.truncate(8 * 1024 * 1024)  # 8 MB 逻辑大小，实际 0 块
        st = os.stat(path)
        self.assertEqual(st.st_size, 8 * 1024 * 1024)
        res = _disk_scan_subtree(self.root, float("inf"))
        self.assertEqual(res["total"], st.st_blocks * BLOCK,
                         "8 MB 稀疏文件被按 st_size 计成了 8 MB")

    def test_hardlink_counted_once(self):
        a = os.path.join(self.root, "a.bin")
        used = _write(a, 200 * 1024)
        link = os.path.join(self.root, "b.bin")
        os.link(a, link)
        res = _disk_scan_subtree(self.root, float("inf"))
        self.assertEqual(res["total"], used,
                         "硬链接被当成两份文件重复计数")

    def test_symlink_not_followed(self):
        target_dir = tempfile.mkdtemp(prefix="tg_disk_out_")
        self.addCleanup(lambda: __import__("shutil").rmtree(target_dir, ignore_errors=True))
        _write(os.path.join(target_dir, "big.bin"), 500 * 1024)
        os.symlink(target_dir, os.path.join(self.root, "link"))
        res = _disk_scan_subtree(self.root, float("inf"))
        self.assertEqual(res["total"], 0, "符号链接被跟随，把外部目录算进了本目录")

    def test_scan_is_complete_flag(self):
        _write(os.path.join(self.root, "x.bin"), 4096)
        res = _disk_scan_subtree(self.root, float("inf"))
        self.assertFalse(res["partial"])
        self.assertEqual(res["skipped"], 0)


class TestBreakdown(_TmpTree):
    """下钻结果的结构与算术。"""

    def setUp(self):
        super().setUp()
        self.a = _write(os.path.join(self.root, "alpha", "a.bin"), 300 * 1024)
        self.b = _write(os.path.join(self.root, "beta", "b.bin"), 100 * 1024)
        self.loose = _write(os.path.join(self.root, "swapfile"), 50 * 1024)

    def test_entries_listed_by_size_desc(self):
        d = disk_breakdown(self.root)
        names = [e["name"] for e in d["entries"]]
        self.assertEqual(names[:2], ["alpha", "beta"])
        self.assertIn("swapfile", names)
        self.assertGreaterEqual(d["entries"][0]["bytes"], d["entries"][1]["bytes"])

    def test_loose_file_is_its_own_row(self):
        """散文件必须成行，不能被并进「自身占用」吞掉。"""
        d = disk_breakdown(self.root)
        row = next((e for e in d["entries"] if e["name"] == "swapfile"), None)
        self.assertIsNotNone(row, "目录下的散文件没有单独成行")
        self.assertFalse(row["dir"])
        self.assertEqual(row["bytes"], self.loose)

    def test_self_bytes_does_not_double_count(self):
        """已成行的内容不得再计入「自身占用」，否则同屏展示两遍同一批字节。"""
        d = disk_breakdown(self.root)
        listed = sum(e["bytes"] for e in d["entries"])
        self.assertEqual(d["selfBytes"], max(0, d["total"] - listed))
        self.assertEqual(d["total"], self.a + self.b + self.loose)

    def test_pct_share(self):
        d = disk_breakdown(self.root)
        total = d["total"]
        for e in d["entries"]:
            self.assertAlmostEqual(e["pct"], round(e["bytes"] / total * 100.0, 1), places=1)
        self.assertLessEqual(sum(e["pct"] for e in d["entries"]), 100.5)

    def test_parent_and_path_for_navigation(self):
        d = disk_breakdown(self.root)
        self.assertTrue(d["ok"])
        self.assertEqual(d["path"], os.path.abspath(self.root))
        self.assertEqual(d["parent"], os.path.dirname(os.path.abspath(self.root)))

    def test_drill_down_into_child(self):
        d = disk_breakdown(os.path.join(self.root, "alpha"))
        self.assertTrue(d["ok"])
        self.assertEqual(d["total"], self.a)
        self.assertEqual(d["parent"], os.path.abspath(self.root))

    def test_missing_dir_is_soft_error(self):
        d = disk_breakdown(os.path.join(self.root, "nope"))
        self.assertFalse(d["ok"])
        self.assertIn("不存在", d["message"])

    def test_pseudo_fs_rejected(self):
        d = disk_breakdown("/proc")
        self.assertFalse(d["ok"])

    def test_empty_dir_ok(self):
        empty = os.path.join(self.root, "empty")
        os.makedirs(empty, exist_ok=True)
        d = disk_breakdown(empty)
        self.assertTrue(d["ok"])
        self.assertEqual(d["total"], 0)
        self.assertEqual(d["entries"], [])

    def test_residual_never_negative(self):
        d = disk_breakdown(self.root)
        self.assertGreaterEqual(d["residualBytes"], 0)

    def test_subdirectory_total_is_inclusive(self):
        """子目录行必须含它的全部子孙 —— 否则 /var 这种「自己没文件」的
        目录会显示 0.00 GB，点开分区一看全是大片 0。"""
        deep = os.path.join(self.root, "gamma", "x", "y")
        deep_bytes = _write(os.path.join(deep, "z.bin"), 400 * 1024)
        d = disk_breakdown(self.root)
        row = next(e for e in d["entries"] if e["name"] == "gamma")
        self.assertGreaterEqual(row["bytes"], deep_bytes)
        # 下钻进去必须能看到中间层，而不是直接空掉
        sub = disk_breakdown(os.path.join(self.root, "gamma"))
        self.assertTrue(any(e["name"] == "x" for e in sub["entries"]))

    def test_empty_path_defaults_to_mount_root(self):
        """空路径必须从分区根开始，不能默认到 app-data。"""
        d = disk_breakdown("")
        self.assertTrue(d["ok"])
        self.assertTrue(d["isMountRoot"], "空路径没有落在分区根上")
        self.assertEqual(d["path"], _disk_mount_point(d["path"]))

    def test_residual_only_on_mount_root(self):
        d = disk_breakdown(self.root)
        self.assertEqual(d["residualBytes"], 0,
                         "子目录上的 residual 是与整个分区相减的噪音，必须为 0")


class TestCache(_TmpTree):
    """缓存：第二次命中且标 cached；清空后重扫。"""

    def test_second_call_hits_cache(self):
        _write(os.path.join(self.root, "f.bin"), 4096)
        first = disk_breakdown(self.root)
        self.assertFalse(first["cached"])
        second = disk_breakdown(self.root)
        self.assertTrue(second["cached"])
        self.assertEqual(second["total"], first["total"])

    def test_cache_clear_forces_rescan(self):
        sub = os.path.join(self.root, "sub")
        _write(os.path.join(sub, "f.bin"), 4096)
        disk_breakdown(self.root)
        _write(os.path.join(sub, "g.bin"), 4096 * 4)
        disk_cache_clear()
        again = disk_breakdown(self.root)
        self.assertFalse(again["cached"])
        self.assertGreater(again["total"], 0)

    def test_scan_timeout_marks_partial_not_zero(self):
        """超时只能变 partial（下限），绝不能把未知当 0 悄悄返回。"""
        _write(os.path.join(self.root, "f.bin"), 4096)
        res = _disk_scan_subtree(self.root, 0.0)  # 立刻到期
        self.assertTrue(res["partial"])
        d = disk_breakdown(self.root)
        # 正常预算下不应残缺
        self.assertFalse(d["partial"])


class TestHelpers(unittest.TestCase):
    def test_rel_depth(self):
        self.assertEqual(_disk_rel_depth("/a", "/a"), 0)
        self.assertEqual(_disk_rel_depth("/a", "/a/b"), 1)
        self.assertEqual(_disk_rel_depth("/a", "/a/b/c"), 2)
        self.assertEqual(_disk_rel_depth("/a", "/other"), 99)

    def test_mount_point_of_root_is_root(self):
        self.assertEqual(_disk_mount_point("/"), "/")

    def test_mount_point_contains_path(self):
        mp = _disk_mount_point(tempfile.gettempdir())
        self.assertTrue(os.path.isdir(mp))
        self.assertTrue(os.path.abspath(tempfile.gettempdir()).startswith(mp))

    def test_kpi_hint_has_readings(self):
        info = disk_kpi_hint()
        self.assertIn("root", info)
        self.assertGreaterEqual(info["total"], 0)
        self.assertLessEqual(info["pct"], 100.0)


class TestEndpoint(unittest.TestCase):
    """接口层：路径透传、refresh 生效、伪目录 404。"""

    def setUp(self):
        from fastapi.testclient import TestClient
        self.tmp = tempfile.mkdtemp(prefix="tg_disk_api_")
        _write(os.path.join(self.tmp, "sub", "f.bin"), 8192)
        self.client = TestClient(bridge_server.app)
        self.cookies = {bridge_server.PORTAL_COOKIE: bridge_server._make_portal_token(),
                        bridge_server.CSRF_COOKIE: "csrf-test"}

    def tearDown(self):
        disk_cache_clear()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_requires_login(self):
        r = self.client.get("/api/disk/breakdown")
        self.assertIn(r.status_code, (302, 401, 403))

    def test_returns_breakdown(self):
        r = self.client.get("/api/disk/breakdown",
                            params={"path": self.tmp}, cookies=self.cookies,
                            headers={"Accept": "application/json"})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["path"], os.path.abspath(self.tmp))
        self.assertTrue(any(e["name"] == "sub" for e in d["entries"]))

    def test_refresh_param_rescans(self):
        self.client.get("/api/disk/breakdown", params={"path": self.tmp},
                        cookies=self.cookies, headers={"Accept": "application/json"})
        r = self.client.get("/api/disk/breakdown", params={"path": self.tmp, "refresh": 1},
                            cookies=self.cookies, headers={"Accept": "application/json"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["cached"])

    def test_pseudo_path_404(self):
        r = self.client.get("/api/disk/breakdown", params={"path": "/proc"},
                            cookies=self.cookies, headers={"Accept": "application/json"})
        self.assertEqual(r.status_code, 404)

    def test_watermark_reports_mount_root(self):
        r = self.client.get("/api/disk/watermark/status", cookies=self.cookies,
                            headers={"Accept": "application/json"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("mountRoot"), "缺少下钻起点 mountRoot")


class TestUiWiring(unittest.TestCase):
    """模板/前端接线：KPI 可点、弹窗已挂载、设置页有入口。"""

    def _read(self, rel):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, rel), "r", encoding="utf-8") as f:
            return f.read()

    def test_dashboard_kpi_marked_clickable(self):
        from services.task_service import _dashboard_stats
        stats = asyncio.run(_dashboard_stats([], {"used_gb": 27.0}))
        card = next(k for k in stats["kpis"] if k["label"] == "本地磁盘占用")
        self.assertEqual(card.get("click"), "disk")

    def test_dashboard_template_handles_click(self):
        tpl = self._read("templates/dashboard.html")
        self.assertIn("__openDiskBreakdown", tpl)
        self.assertIn("kpi-clickable", tpl)

    def test_modal_partial_included(self):
        tpl = self._read("templates/base.html")
        self.assertIn("partials/_disk_breakdown_modal.html", tpl)

    def test_modal_has_navigation_slots(self):
        tpl = self._read("templates/partials/_disk_breakdown_modal.html")
        for anchor in ("diskBreakdownBackdrop", "diskRows", "diskPathChip", "diskUpBtn"):
            self.assertIn(anchor, tpl)

    def test_settings_has_entry(self):
        tpl = self._read("templates/settings.html")
        self.assertIn("__openDiskBreakdown", tpl)

    def test_app_js_defines_handlers(self):
        js = self._read("static/js/app.js")
        for fn in ("__openDiskBreakdown", "__closeDiskBreakdown", "__diskLoad",
                   "__diskEnter", "__diskUp", "__diskRefresh"):
            self.assertIn(fn, js)


if __name__ == "__main__":
    unittest.main()
