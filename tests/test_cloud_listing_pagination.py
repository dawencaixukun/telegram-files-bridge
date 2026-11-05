# -*- coding: utf-8 -*-
"""
tests/test_cloud_listing_pagination.py
=====================================
回归测试：「云端归档页检测到 80 个云端失效记录」的根因 —— 目录列表分页截断。

线上实测（34vps，/root/tg-files/app-data/.archive_jobs.json）：
  * 归档目录 /onedrive/yello 里实际有 635 个条目；
  * 代码里所有 /api/fs/list 调用都写死 page=1, per_page=500；
  * OpenList 对该目录返回 500 条（total=635），第 501 条之后在程序眼里不存在；
  * 452 条归档记录里 80 条因此被判成「云端失效（已被删除或移动）」，
    实际用 per_page 拉全后 **0 条真的丢失**。

本文件锁定三条契约：
1. _openlist_list_all 必须按 total 翻页拉全，绝不能只返回第一页。
2. 云端归档行判定必须基于全量列表 —— 目录超 500 个文件时不许出现假 missing。
3. 任何一页失败都必须返回错误码，让调用方退化为「不校验」，
   绝不允许把「只拉到一半」当成「文件真的不存在」。
"""
import asyncio
import unittest
from unittest.mock import patch

import bridge_server
from services.openlist_service import _openlist_list_all


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _dir_payload(names, total, page, per_page):
    if per_page <= 0:
        chunk = names
    else:
        start = (page - 1) * per_page
        chunk = names[start:start + per_page]
    return {
        "code": 200,
        "message": "success",
        "data": {
            "content": [{"name": n, "size": 1024, "is_dir": False} for n in chunk],
            "total": total,
        },
    }


class TestListingPagination(unittest.TestCase):
    """分页拉全：单页上限 500 的目录必须能被完整读到。"""

    def _patch_dir(self, names, fail_page=None):
        calls = []

        async def fake_post(path, *a, **kw):
            body = kw.get("json") or {}
            page = int(body.get("page") or 1)
            per_page = int(body.get("per_page") if body.get("per_page") is not None else 500)
            calls.append((page, per_page))
            if fail_page is not None and page == fail_page:
                return _Resp({"code": 500, "message": "boom", "data": None})
            return _Resp(_dir_payload(names, len(names), page, per_page))

        return fake_post, calls

    def test_reads_beyond_first_page(self):
        """635 个条目超过单页 500 —— 必须翻页，第二页的条目不能丢。"""
        names = [f"f{i:04d}.mp4" for i in range(635)]
        fake, calls = self._patch_dir(names)
        with patch.object(bridge_server._openlist_client, "post", new=fake):
            code, content, _msg = asyncio.run(
                _openlist_list_all("tk", "/onedrive/yello"))

        self.assertEqual(code, 200)
        self.assertEqual(len(content), 635, "翻页必须把 635 条全部拉回来")
        self.assertEqual(len(calls), 2, "635 条 / 每页 500 应当恰好翻 2 页")
        got = {c["name"] for c in content}
        self.assertIn("f0432.mp4", got, "第 433 个之后的条目曾经被整页丢掉")
        self.assertIn("f0634.mp4", got, "最后一页的条目必须存在")

    def test_no_paging_when_directory_is_small(self):
        """目录不大时不能白白多发请求（每次列目录都是一次云端往返）。"""
        names = [f"f{i}.mp4" for i in range(12)]
        fake, calls = self._patch_dir(names)
        with patch.object(bridge_server._openlist_client, "post", new=fake):
            code, content, _msg = asyncio.run(_openlist_list_all("tk", "/d"))
        self.assertEqual(code, 200)
        self.assertEqual(len(content), 12)
        self.assertEqual(len(calls), 1, "目录小于一页时只应请求一次")

    def test_page_failure_is_not_silent_truncation(self):
        """中间某页失败必须报错，不能静默返回半份列表当全量。"""
        names = [f"f{i:04d}.mp4" for i in range(635)]
        fake, _calls = self._patch_dir(names, fail_page=2)
        with patch.object(bridge_server._openlist_client, "post", new=fake):
            code, content, msg = asyncio.run(_openlist_list_all("tk", "/d"))
        self.assertNotEqual(code, 200, "第二页失败不能返回 200")
        self.assertEqual(content, [])
        self.assertTrue(msg)

    def test_auth_failure_surfaces_401(self):
        """令牌失效要能冒泡成 401，让调用方走静默重登。"""
        async def fake_post(path, *a, **kw):
            return _Resp({"code": 401, "message": "token is invalidated", "data": None})

        with patch.object(bridge_server._openlist_client, "post", new=fake_post):
            code, content, _msg = asyncio.run(_openlist_list_all("tk", "/d"))
        self.assertEqual(code, 401)
        self.assertEqual(content, [])

    def test_duplicate_entries_across_pages_are_deduped(self):
        """翻页期间目录被并发写入时同一文件可能重复出现，结果必须去重。"""
        page1 = [{"name": f"a{i}.mp4", "size": 1, "is_dir": False} for i in range(500)]
        page2 = [{"name": "a499.mp4", "size": 1, "is_dir": False},
                 {"name": "new.mp4", "size": 1, "is_dir": False}]

        async def fake_post(path, *a, **kw):
            body = kw.get("json") or {}
            page = int(body.get("page") or 1)
            content = page1 if page == 1 else page2
            return _Resp({"code": 200, "message": "success",
                          "data": {"content": content, "total": 501}})

        with patch.object(bridge_server._openlist_client, "post", new=fake_post):
            code, content, _msg = asyncio.run(_openlist_list_all("tk", "/d"))
        self.assertEqual(code, 200)
        names = [c["name"] for c in content]
        self.assertEqual(len(names), len(set(names)), "跨页重复条目必须去重")
        self.assertIn("new.mp4", names)


class TestCloudRowsUseFullListing(unittest.TestCase):
    """云端归档行必须以全量目录列表为准，目录超 500 个文件不得假 missing。"""

    def setUp(self):
        self._orig = dict(bridge_server._ARCHIVE_JOBS)
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._REMOTE_DIR_LIST_CACHE.clear()
        bridge_server._CLOUD_VERIFY_CACHE.clear()
        bridge_server._CLOUD_VERIFY_INFLIGHT.clear()

    def tearDown(self):
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self._orig)
        bridge_server._REMOTE_DIR_LIST_CACHE.clear()
        bridge_server._CLOUD_VERIFY_CACHE.clear()
        bridge_server._CLOUD_VERIFY_INFLIGHT.clear()

    def _make_jobs(self, count):
        for i in range(count):
            jid = f"j{i:04d}"
            name = f"v{i:04d}.mp4"
            bridge_server._ARCHIVE_JOBS[jid] = {
                "id": jid, "unique_id": f"u{i:04d}", "filename": name,
                "remote_dir": "/onedrive/yello",
                "remote_path": f"/onedrive/yello/{name}",
                "size_bytes": 2048, "state": "done",
                "archived_at": 1000.0 + i,
            }

    def test_beyond_500_files_are_not_reported_missing(self):
        """520 个文件全部真实存在 —— 一条 missing 都不许出现。"""
        self._make_jobs(520)
        names = [f"v{i:04d}.mp4" for i in range(520)]

        async def fake_post(path, *a, **kw):
            body = kw.get("json") or {}
            page = int(body.get("page") or 1)
            per_page = int(body.get("per_page") if body.get("per_page") is not None else 500)
            return _Resp(_dir_payload(names, len(names), page, per_page))

        with patch.object(bridge_server._openlist_client, "post", new=fake_post), \
                patch.object(bridge_server, "_openlist_token", new=AsyncMockOf("tk")):
            # 校验「有没有假 missing」必须拿真实列表，走阻塞通道
            rows = asyncio.run(bridge_server._cloud_archive_rows(check_remote="sync"))

        self.assertEqual(len(rows), 520)
        missing = [r for r in rows if r.get("status") == "missing"]
        self.assertEqual(missing, [],
                         "目录超过单页上限时，第二页文件被误判成云端失效")

    def test_genuinely_deleted_file_still_reported_missing(self):
        """翻页不能把真的缺失藏起来：云端少了谁就必须报谁。"""
        self._make_jobs(520)
        names = [f"v{i:04d}.mp4" for i in range(519)]  # 故意少最后一个

        async def fake_post(path, *a, **kw):
            body = kw.get("json") or {}
            page = int(body.get("page") or 1)
            per_page = int(body.get("per_page") if body.get("per_page") is not None else 500)
            return _Resp(_dir_payload(names, len(names), page, per_page))

        with patch.object(bridge_server._openlist_client, "post", new=fake_post), \
                patch.object(bridge_server, "_openlist_token", new=AsyncMockOf("tk")), \
                patch.object(bridge_server, "_openlist_relocate_after_rename",
                             new=AsyncMockOf(None)):
            rows = asyncio.run(bridge_server._cloud_archive_rows(check_remote="sync"))

        missing = sorted(r["filename"] for r in rows if r.get("status") == "missing")
        self.assertEqual(missing, ["v0519.mp4"],
                         "真实缺失的文件必须仍然被标记出来")


class TestCloudRowsNonBlocking(unittest.TestCase):
    """默认通道不得阻塞在 OpenList 目录列表上 —— 云端页 10s 卡顿的根因回归。"""

    def setUp(self):
        self._orig = dict(bridge_server._ARCHIVE_JOBS)
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._REMOTE_DIR_LIST_CACHE.clear()
        bridge_server._CLOUD_VERIFY_CACHE.clear()
        bridge_server._CLOUD_VERIFY_INFLIGHT.clear()

    def tearDown(self):
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self._orig)
        bridge_server._CLOUD_VERIFY_CACHE.clear()
        bridge_server._CLOUD_VERIFY_INFLIGHT.clear()

    def _make_jobs(self, count):
        for i in range(count):
            jid = f"nb{i:04d}"
            name = f"n{i:04d}.mp4"
            bridge_server._ARCHIVE_JOBS[jid] = {
                "id": jid, "unique_id": f"nb-u{i:04d}", "filename": name,
                "remote_dir": "/onedrive/yello",
                "remote_path": f"/onedrive/yello/{name}",
                "size_bytes": 2048, "state": "done",
                "archived_at": 1000.0 + i,
            }

    def test_default_mode_returns_without_waiting_for_listing(self):
        import time as _time
        self._make_jobs(3)

        async def slow_post(path, *a, **kw):
            await asyncio.sleep(0.4)      # 模拟线上 OpenList 的慢目录列表
            return _Resp(_dir_payload(["n0000.mp4", "n0001.mp4", "n0002.mp4"], 3, 1, 500))

        async def run():
            t0 = _time.perf_counter()
            rows = await bridge_server._cloud_archive_rows()
            return rows, _time.perf_counter() - t0

        with patch.object(bridge_server._openlist_client, "post", new=slow_post),                 patch.object(bridge_server, "_openlist_token", new=AsyncMockOf("tk")):
            rows, cost = asyncio.run(run())

        self.assertEqual(len(rows), 3)
        self.assertLess(cost, 0.2, "默认通道不得等待 OpenList 目录列表（实测已阻塞 %.2fs）" % cost)
        # 没有任何快照时不能把行判成缺失，只能标「核验中」
        self.assertEqual([r for r in rows if r.get("status") == "missing"], [])
        self.assertTrue(all(r.get("verifyPending") for r in rows), "首次无快照应标记核验中")


class AsyncMockOf:
    """最小 async 替身（Mock 的 AsyncMock 在 patch.object 里跨版本行为不一）。"""

    def __init__(self, value):
        self.value = value
        self.calls = 0

    async def __call__(self, *a, **kw):
        self.calls += 1
        return self.value


if __name__ == "__main__":
    unittest.main()
