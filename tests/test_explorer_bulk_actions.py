# -*- coding: utf-8 -*-
"""
tests/test_explorer_bulk_actions.py
===================================
回归测试：浏览页（网盘）批量选择 + 批量移动 / 复制 / 删除。

历史问题：浏览页每张卡片只有「改名 / 复制 / 移动 / 下载」单选按钮，一次只能
处理一个文件。用户要整理几十个文件时必须一个个点，而 OpenList 的 fs/copy、
fs/move、fs/remove 本来就支持一次传多个名字 —— 前端从来没把列表传出去。

本文件锁定七条契约：
1. 后端 /api/explorer/delete 存在，按父目录分组批量删除，逐项报成败。
2. 拒绝删除根目录与网盘挂载点（一级路径）—— 这两类操作不该藏在批量按钮后面。
3. /api/explorer/mkdir 存在：跨盘移动需要先有目标目录。
4. 模板渲染勾选框（每个卡片一个）、表头全选、底部批量条三个动作。
5. 批量移动/复制复用同一弹窗，names 传整个列表（不是循环单发）。
6. 被搜索过滤隐藏的卡片不参与批量（看不见的不能被动）。
7. 勾选框点击不触发卡片的「进入目录」跳转。
"""
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import bridge_server
from core.auth import _make_portal_token, PORTAL_COOKIE, CSRF_COOKIE, CSRF_HEADER

ENTRIES = [
    {
        "name": "clip.mp4", "path": "/onedrive/yello/clip.mp4", "is_dir": False,
        "size": 50 * 1024 * 1024, "size_human": "50.0 MB", "modified": "2026-08-20 15:30",
        "thumb": "", "type": 2, "is_video": True, "ext": "MP4",
        "drive": "onedrive", "drive_label": "OneDrive",
        "openlist_url": "http://127.0.0.1:5244/onedrive/yello/clip.mp4",
    },
    {
        "name": "subfolder", "path": "/onedrive/yello/subfolder", "is_dir": True,
        "size": 0, "size_human": "—", "modified": "2026-08-20 12:00", "thumb": "",
        "type": 1, "is_video": False, "ext": "DIR",
        "drive": "onedrive", "drive_label": "OneDrive",
        "openlist_url": "http://127.0.0.1:5244/onedrive/yello/subfolder",
    },
]

LIST_DATA = {
    "ok": True, "path": "/onedrive/yello", "parent_path": "/onedrive",
    "breadcrumbs": [
        {"name": "全部网盘", "path": "/", "is_last": False},
        {"name": "onedrive", "path": "/onedrive", "is_last": False},
        {"name": "yello", "path": "/onedrive/yello", "is_last": True},
    ],
    "entries": ENTRIES, "dirs_count": 1, "files_count": 1,
    "total_size": 50 * 1024 * 1024, "total_size_human": "50.0 MB",
    "write": True, "message": "",
}

STORAGES = [
    {"mount_path": "/onedrive", "name": "onedrive", "driver": "Onedrive",
     "display_name": "OneDrive", "status": "work", "status_label": "正常在线",
     "remark": "", "capacity_str": "就绪", "openlist_url": "http://x/onedrive"},
]


class _Base(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(bridge_server.app)
        csrf = "csrf-bulk-test"
        self.cookies = {PORTAL_COOKIE: _make_portal_token(), CSRF_COOKIE: csrf}
        self.headers = {CSRF_HEADER: csrf}

    def _page(self):
        with patch("services.openlist_service.openlist_fs_list_all",
                   new=AsyncMock(return_value=LIST_DATA)), \
             patch("services.openlist_service._openlist_ready",
                   new=AsyncMock(return_value=True)), \
             patch("services.openlist_service.openlist_storages",
                   new=AsyncMock(return_value=STORAGES)), \
             patch("services.openlist_service.openlist_mounts",
                   new=AsyncMock(return_value={"ok": True, "mounts": ["/onedrive"]})):
            resp = self.client.get("/explorer?path=/onedrive/yello", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def _src(self, rel):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            return f.read()


class TestBulkDeleteEndpoint(_Base):
    """删除接口：分组批量 + 逐项报成败 + 危险路径拦截。"""

    def _post(self, payload):
        return self.client.post("/api/explorer/delete", json=payload,
                                cookies=self.cookies, headers=self.headers)

    def test_requires_login(self):
        r = self.client.post("/api/explorer/delete", json={"paths": ["/onedrive/a.mp4"]})
        self.assertIn(r.status_code, (302, 401, 403))

    def test_deletes_multiple_in_one_call(self):
        """同目录多个文件 → 一次 fs/remove，而不是循环发 N 次。"""
        calls = []

        async def fake_remove(d, names):
            calls.append((d, list(names)))
            return {"ok": True, "code": 200, "message": "ok",
                    "deleted": list(names), "failed": []}

        with patch("services.openlist_service._openlist_fs_remove", new=fake_remove):
            r = self._post({"paths": ["/onedrive/yello/a.mp4", "/onedrive/yello/b.mp4"]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["deleted"], 2)
        self.assertEqual(len(calls), 1, "同目录的多个文件必须合并成一次请求")
        self.assertEqual(calls[0][0], "/onedrive/yello")
        self.assertEqual(sorted(calls[0][1]), ["a.mp4", "b.mp4"])

    def test_groups_by_parent_directory(self):
        """不同父目录 → 按目录分组，每组一次请求。"""
        calls = []

        async def fake_remove(d, names):
            calls.append((d, sorted(names)))
            return {"ok": True, "code": 200, "message": "ok",
                    "deleted": list(names), "failed": []}

        with patch("services.openlist_service._openlist_fs_remove", new=fake_remove):
            r = self._post({"paths": ["/onedrive/a.mp4", "/wopan/x/y.mkv"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ("/onedrive", ["a.mp4"]))
        self.assertEqual(calls[1], ("/wopan/x", ["y.mkv"]))

    def test_single_path_field_also_accepted(self):
        async def fake_remove(d, names):
            return {"ok": True, "code": 200, "message": "ok",
                    "deleted": list(names), "failed": []}

        with patch("services.openlist_service._openlist_fs_remove", new=fake_remove):
            r = self._post({"path": "/onedrive/yello/a.mp4"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["deleted"], 1)

    def test_refuses_mount_point(self):
        """删挂载点 = 清空整个网盘，必须拒绝。"""
        r = self._post({"paths": ["/onedrive"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("挂载点", r.json()["message"])

    def test_refuses_root(self):
        r = self._post({"paths": ["/"]})
        self.assertEqual(r.status_code, 400)

    def test_empty_payload_rejected(self):
        r = self._post({"paths": []})
        self.assertEqual(r.status_code, 400)

    def test_partial_failure_reported(self):
        """部分组失败时必须说清删了几个，不能让用户以为全成功。"""
        async def fake_remove(d, names):
            if d == "/wopan":
                return {"ok": False, "code": 500, "message": "boom",
                        "deleted": [], "failed": list(names)}
            return {"ok": True, "code": 200, "message": "ok",
                    "deleted": list(names), "failed": []}

        with patch("services.openlist_service._openlist_fs_remove", new=fake_remove):
            r = self._post({"paths": ["/onedrive/a.mp4", "/wopan/b.mkv"]})
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["deleted"], 1)
        self.assertEqual(len(body["errors"]), 1)
        self.assertIn("1", body["message"])

    def test_duplicate_paths_deduped(self):
        calls = []

        async def fake_remove(d, names):
            calls.append(list(names))
            return {"ok": True, "code": 200, "message": "ok",
                    "deleted": list(names), "failed": []}

        with patch("services.openlist_service._openlist_fs_remove", new=fake_remove):
            self._post({"paths": ["/onedrive/a.mp4", "/onedrive/a.mp4", "/onedrive/./a.mp4"]})
        self.assertEqual(calls, [["a.mp4"]], "重复路径必须去重，否则同一文件被删多次")

    def test_no_token_still_calls_remove(self):
        """OpenList 未就绪时不静默假装成功：走 service，返回失败原因。"""
        async def fake_remove(d, names):
            return {"ok": False, "code": 502, "message": "OpenList 未就绪: x",
                    "deleted": [], "failed": list(names)}

        with patch("services.openlist_service._openlist_fs_remove", new=fake_remove):
            r = self._post({"paths": ["/onedrive/yello/a.mp4"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("未就绪", r.json()["message"])


class TestMkdirEndpoint(_Base):
    def test_requires_path(self):
        r = self.client.post("/api/explorer/mkdir", json={},
                             cookies=self.cookies, headers=self.headers)
        self.assertEqual(r.status_code, 400)

    def test_normalizes_path(self):
        seen = {}

        class FakeResp:
            status_code = 200

        async def fake_post(url, json=None, headers=None):
            seen["url"] = url
            seen["json"] = json
            return FakeResp()

        import services.openlist_service as svc
        with patch.object(svc._openlist_client, "post", new=fake_post), \
             patch.object(svc, "_openlist_token", new=AsyncMock(return_value="Bearer t")), \
             patch.object(svc, "_openlist_env", return_value=(200, {}, "ok")):
            r = self.client.post("/api/explorer/mkdir", json={"path": "wopan//new"},
                                 cookies=self.cookies, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(seen["url"], "/api/fs/mkdir")
        self.assertEqual(seen["json"]["path"], "/wopan/new")
        self.assertEqual(r.json()["path"], "/wopan/new")


class TestBulkUi(_Base):
    """模板接线：勾选框、全选、批量条、批量传参。"""

    def test_each_card_has_checkbox(self):
        html = self._page()
        self.assertEqual(html.count('class="cloud-check"'), 2,
                         "文件卡片与文件夹卡片都要有勾选框")
        self.assertIn('data-cloud-path="/onedrive/yello/clip.mp4"', html)
        self.assertIn('data-cloud-name="clip.mp4"', html)
        self.assertIn('data-cloud-dir="1"', html, "文件夹要能被识别（删除时提示会连带内容）")

    def test_select_all_in_list_head(self):
        html = self._page()
        self.assertIn('id="cloudSelAll"', html)
        self.assertIn("window.__cloudSelAll(this)", html)
        self.assertIn('class="lh-check"', html)

    def test_batch_bar_has_three_actions(self):
        html = self._page()
        self.assertIn('id="cloudBatchBar"', html)
        self.assertIn('id="cloudSelectedCount"', html)
        self.assertIn("window.__cloudBulkTransfer('copy')", html)
        self.assertIn("window.__cloudBulkTransfer('move')", html)
        self.assertIn("window.__cloudBulkDelete()", html)
        self.assertIn('class="floating-batch-bar"', html)

    def test_checkbox_click_does_not_navigate(self):
        """文件夹卡片整卡点击是「进入目录」，勾选框必须 stopPropagation。"""
        html = self._page()
        idx = html.index('class="cloud-check"')
        snippet = html[max(0, idx - 200):idx + 120]
        self.assertIn("event.stopPropagation()", snippet,
                      "勾选框未阻止冒泡 → 勾一下就跳进文件夹")

    def test_batch_transfer_sends_names_list(self):
        """批量复制/移动必须把整个 names 列表发给后端，不是循环单发。"""
        html = self._page()
        self.assertIn("names: items.map(function (it) { return it.name", html)
        self.assertIn("body: JSON.stringify({ src_dir: src, dst_dir: dst, names: names })", html)
        self.assertNotIn("names: [name] })", html)

    def test_transfer_modal_accepts_array(self):
        html = self._page()
        self.assertIn("Array.isArray(pathOrItems)", html)
        self.assertIn("transferData.count > 1", html)

    def test_hidden_cards_excluded_from_selection(self):
        """过滤隐藏的卡片不得参与批量操作 —— 看不见的不能被动。"""
        html = self._page()
        self.assertIn("card.style.display === 'none'", html)
        self.assertIn("this.$watch('q'", html)

    def test_delete_confirm_is_danger(self):
        html = self._page()
        self.assertIn("danger: true", html)
        self.assertIn("无法恢复", html)

    def test_single_delete_button_on_cards(self):
        html = self._page()
        self.assertIn("window.__deleteCloudEntry(this)", html)
        self.assertGreaterEqual(html.count("window.__deleteCloudEntry(this)"), 2)

    def test_batch_bar_absent_on_root(self):
        """根目录（全部网盘）没有条目可勾选，批量条不应渲染。"""
        with patch("services.openlist_service.openlist_storages",
                   new=AsyncMock(return_value=STORAGES)), \
             patch("services.openlist_service._openlist_ready",
                   new=AsyncMock(return_value=True)), \
             patch("services.openlist_service.openlist_mounts",
                   new=AsyncMock(return_value={"ok": True, "mounts": ["/onedrive"]})), \
             patch("routers.explorer._attach_drive_stats", new=AsyncMock(return_value=None)):
            html = self.client.get("/explorer?path=/", cookies=self.cookies).text
        self.assertNotIn('id="cloudBatchBar"', html)


class TestRemoveService(unittest.TestCase):
    """openlist_service._openlist_fs_remove 的行为契约。"""

    def test_rejects_empty_names(self):
        import asyncio
        from services.openlist_service import _openlist_fs_remove
        res = asyncio.run(_openlist_fs_remove("/a", []))
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], 400)

    def test_strips_path_traversal(self):
        """names 只取 basename，塞 ../ 不会逃出目标目录。"""
        import asyncio
        from services.openlist_service import _openlist_fs_remove
        seen = {}

        class FakeResp:
            status_code = 200

        async def fake_post(url, json=None, headers=None, **kw):
            seen["json"] = json
            return FakeResp()

        async def fake_token():
            return "Bearer t"

        import services.openlist_service as svc
        with patch.object(svc._openlist_client, "post", new=fake_post), \
             patch.object(svc, "_openlist_token", new=fake_token), \
             patch.object(svc, "_openlist_env", return_value=(200, {}, "ok")):
            asyncio.run(svc._openlist_fs_remove("/onedrive", ["../etc/passwd", "ok.mp4"]))
        self.assertEqual(sorted(seen["json"]["names"]), ["ok.mp4", "passwd"])

    def test_timeout_reports_readable_message(self):
        """超时异常 str() 是空串，必须显式给出可读原因，不能只剩「删除失败: 」。"""
        import asyncio
        import httpx
        import services.openlist_service as svc

        seen = {}

        async def fake_post(url, json=None, headers=None, timeout=None):
            seen["timeout"] = timeout
            raise httpx.ReadTimeout("")

        async def fake_token():
            return "Bearer t"

        with patch.object(svc._openlist_client, "post", new=fake_post), \
             patch.object(svc, "_openlist_token", new=fake_token):
            res = asyncio.run(svc._openlist_fs_remove("/onedrive", ["a.mp4"]))
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], 504)
        self.assertIn("超时", res["message"])
        # 必须显式传放宽后的超时，否则会撞上共享客户端的 10 秒
        self.assertGreater(seen["timeout"], 60)

    def test_not_found_is_not_failure(self):
        """文件已被别处删掉时报「不存在」，不该让整批操作报错。"""
        import asyncio
        import services.openlist_service as svc

        class FakeResp:
            status_code = 200

        async def fake_post(url, json=None, headers=None, **kw):
            return FakeResp()

        async def fake_token():
            return "Bearer t"

        with patch.object(svc._openlist_client, "post", new=fake_post), \
             patch.object(svc, "_openlist_token", new=fake_token), \
             patch.object(svc, "_openlist_env", return_value=(500, {}, "object not found")):
            res = asyncio.run(svc._openlist_fs_remove("/onedrive", ["gone.mp4"]))
        self.assertTrue(res["ok"], "object not found 应当视为已删除")


if __name__ == "__main__":
    unittest.main()
