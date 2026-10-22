# -*- coding: utf-8 -*-
"""
tests/test_submit_link_notes.py — 「提交链接下载」支持行内中文备注名

用户需求：提交链接下载时，链接后面可以紧跟一个中文备注名；备注名就是最终
文件名，没有备注就沿用链接里视频自带的文件名/文案。

本测试锁定三处契约：
1. `_split_link_notes` 能拆出「链接 + 备注」（备注在链接后/前、各种分隔符）；
2. 备注会被清洗成合法文件名片段（不含 / \\ 引号书名号、无制表符）；
3. `POST /tasks` 接受 {"links": [...], "notes": {link: note}} 并把备注带到
   解析层（用 monkeypatch 拦下 _resolve_links_to_files 断言入参）。
"""
import unittest
from unittest.mock import patch

import bridge_server
from core.auth import _make_portal_token
from core.config import PORTAL_COOKIE, CSRF_COOKIE
from fastapi.testclient import TestClient
from services.task_service import _split_link_notes


class SplitLinkNotesTest(unittest.TestCase):
    def test_note_after_link_with_space(self):
        links, notes = _split_link_notes(["https://t.me/chan/123 我的自定义名"])
        self.assertEqual(links, ["https://t.me/chan/123"])
        self.assertEqual(notes["https://t.me/chan/123"], "我的自定义名")

    def test_note_with_fullwidth_separator(self):
        links, notes = _split_link_notes(["https://t.me/c/2333333/456｜标题党"])
        self.assertEqual(links, ["https://t.me/c/2333333/456"])
        self.assertEqual(notes["https://t.me/c/2333333/456"], "标题党")

    def test_note_before_link(self):
        links, notes = _split_link_notes(["备注在前 https://t.me/chan/9"])
        self.assertEqual(links, ["https://t.me/chan/9"])
        self.assertEqual(notes["https://t.me/chan/9"], "备注在前")

    def test_link_without_note_stays_bare(self):
        links, notes = _split_link_notes(["https://t.me/chan/1", "https://t.me/chan/2   "])
        self.assertEqual(links, ["https://t.me/chan/1", "https://t.me/chan/2"])
        self.assertEqual(notes, {})

    def test_private_link_no_scheme(self):
        links, notes = _split_link_notes(["t.me/c/123456/78 私密"])
        self.assertEqual(links, ["https://t.me/c/123456/78"])
        self.assertEqual(notes["https://t.me/c/123456/78"], "私密")

    def test_note_sanitized_for_filename(self):
        _, notes = _split_link_notes(['https://t.me/chan/5 "a/b\\c「好」'])
        note = notes["https://t.me/chan/5"]
        self.assertNotIn("/", note)
        self.assertNotIn("\\", note)
        for ch in "\"'「」":
            self.assertNotIn(ch, note)
        self.assertNotIn("\t", note)

    def test_unparseable_line_is_kept_for_caller_to_report(self):
        links, notes = _split_link_notes(["不是链接"])
        self.assertEqual(links, ["不是链接"])
        self.assertEqual(notes, {})


class SubmitEndpointNotesTest(unittest.TestCase):
    def test_post_tasks_forwards_notes(self):
        client = TestClient(bridge_server.app)
        csrf = "probe-csrf"
        captured = {}

        async def _fake_resolve(links, force=False, notes_by_link=None, link_to_msg=None):
            captured["links"] = list(links)
            captured["notes"] = dict(notes_by_link or {})
            return 1, ""

        with patch("routers.tasks._resolve_links_to_files", _fake_resolve), \
             patch("routers.tasks.tasks_all", return_value=[]):
            r = client.post(
                "/tasks",
                json={"links": ["https://t.me/chan/123 我的名字"],
                      "notes": {"https://t.me/chan/123": "我的名字"}},
                cookies={PORTAL_COOKIE: _make_portal_token(), CSRF_COOKIE: csrf},
                headers={"X-CSRF-Token": csrf},
            )

        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["links"], ["https://t.me/chan/123"])
        self.assertEqual(captured["notes"].get("https://t.me/chan/123"), "我的名字")


if __name__ == "__main__":
    unittest.main()
