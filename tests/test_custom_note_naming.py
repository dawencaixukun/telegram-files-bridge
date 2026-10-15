# -*- coding: utf-8 -*-
"""回归测试：收藏/监听会话备注自动命名 + 无意义文件名 Caption 提取 + 重命名弹窗关闭契约。"""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core.config import (
    _CUSTOM_FILE_NOTES,
    _human_name,
    _is_meaningless_filename,
)
from services.watch_service import (
    parse_chat_history_notes,
    sync_chat_custom_notes,
)


class TestCustomNoteNaming(unittest.TestCase):
    def setUp(self):
        self._saved_notes = dict(_CUSTOM_FILE_NOTES)
        _CUSTOM_FILE_NOTES.clear()

    def tearDown(self):
        _CUSTOM_FILE_NOTES.clear()
        _CUSTOM_FILE_NOTES.update(self._saved_notes)

    def test_meaningless_filename_detection(self):
        self.assertTrue(_is_meaningless_filename(""))
        self.assertTrue(_is_meaningless_filename("文件 AgADASMAAmgiAVY.mp4"))
        self.assertTrue(_is_meaningless_filename("文件_AgADASMAAmgiAVY.mp4"))
        self.assertTrue(_is_meaningless_filename("3 (1).mp4"))
        self.assertTrue(_is_meaningless_filename("3 (2).mp4"))
        self.assertTrue(_is_meaningless_filename("6197272392310465281.mp4"))
        self.assertTrue(_is_meaningless_filename("video_2024-11-02_19-52-31 (19).mp4"))
        self.assertFalse(_is_meaningless_filename("55verymei.mp4"))
        self.assertFalse(_is_meaningless_filename("海提.mp4"))

    def test_pre_forward_note_single_and_album(self):
        """转发时顺手写备注：单视频与相册合集均正确绑定。"""
        msgs = [
            # 最新消息在前（模拟 TDLib GetChatHistory 返回序）
            {
                "id": 6544162816,
                "date": 1791749286,
                "mediaAlbumId": 14333994287981721,
                "forwardInfo": {"origin": {}},
                "content": {
                    "video": {
                        "fileName": "3 (2).mp4",
                        "video": {"remote": {"uniqueId": "AgAD_yEAAnQhSFQ"}},
                    }
                },
            },
            {
                "id": 6543114240,
                "date": 1791749285,
                "mediaAlbumId": 14333994287981721,
                "forwardInfo": {"origin": {}},
                "content": {
                    "video": {
                        "fileName": "3 (1).mp4",
                        "video": {"remote": {"uniqueId": "AgAD_iEAAnQhSFQ"}},
                    }
                },
            },
            {
                "id": 6542065664,
                "date": 1791749285,
                "mediaAlbumId": 0,
                "forwardInfo": None,
                "content": {"text": {"text": "55verymei"}},
            },
            {
                "id": 6541017088,
                "date": 1791749257,
                "mediaAlbumId": 0,
                "forwardInfo": {"origin": {}},
                "content": {
                    "video": {
                        "fileName": "",
                        "video": {"remote": {"uniqueId": "AgADASMAAmgiAVY"}},
                    }
                },
            },
            {
                "id": 6374293504,
                "date": 1790537186,
                "mediaAlbumId": 0,
                "forwardInfo": {"origin": {}},
                "content": {
                    "video": {
                        "fileName": "video_2024-11-02_19-52-31 (19).mp4",
                        "video": {"remote": {"uniqueId": "AgADExQAAiqisFY"}},
                    }
                },
            },
            {
                "id": 6373244928,
                "date": 1790537186,
                "mediaAlbumId": 0,
                "forwardInfo": None,
                "content": {"text": {"text": "海提"}},
            },
        ]
        parsed = parse_chat_history_notes(msgs)
        self.assertEqual(parsed["AgADExQAAiqisFY"]["note"], "海提")
        self.assertEqual(parsed["AgAD_iEAAnQhSFQ"]["note"], "55verymei 1")
        self.assertEqual(parsed["AgAD_yEAAnQhSFQ"]["note"], "55verymei 2")
        # 28 秒前单独转发且未跟备注的视频不应误绑后面的转发备注
        self.assertNotIn("AgADASMAAmgiAVY", parsed)

    def test_post_forward_note_and_explicit_reply(self):
        """先转发视频再发文字备注（Pass 2）与显式回复改名（Pass 3）。"""
        msgs = [
            {
                "id": 300,
                "date": 1050,
                "mediaAlbumId": 0,
                "forwardInfo": None,
                "replyTo": {"messageId": 100},
                "content": {"text": {"text": "回复修正后的名字"}},
            },
            {
                "id": 200,
                "date": 1010,
                "mediaAlbumId": 0,
                "forwardInfo": None,
                "content": {"text": {"text": "初版后置备注"}},
            },
            {
                "id": 100,
                "date": 1000,
                "mediaAlbumId": 0,
                "forwardInfo": {"origin": {}},
                "content": {
                    "video": {
                        "fileName": "",
                        "video": {"remote": {"uniqueId": "UID_V1"}},
                    }
                },
            },
        ]
        parsed = parse_chat_history_notes(msgs)
        self.assertEqual(parsed["UID_V1"]["note"], "回复修正后的名字")
        self.assertTrue(parsed["UID_V1"]["explicit"])

    def test_two_videos_post_note_and_duplicate_note_dedup(self):
        """连发两个独立视频再补一条备注自动编号 1/2；不同时间写同名备注自动递增防重。"""
        msgs = [
            # 后发的一条独立视频，居然也备注了同名 "周末合集"
            {
                "id": 500,
                "date": 2005,
                "mediaAlbumId": 0,
                "forwardInfo": None,
                "content": {"text": {"text": "周末合集 1"}},
            },
            {
                "id": 400,
                "date": 2000,
                "mediaAlbumId": 0,
                "forwardInfo": {"origin": {}},
                "content": {
                    "video": {
                        "fileName": "",
                        "video": {"remote": {"uniqueId": "UID_DUP"}},
                    }
                },
            },
            # 先连发两个非相册独立视频，再发一条备注 "周末合集"
            {
                "id": 300,
                "date": 1005,
                "mediaAlbumId": 0,
                "forwardInfo": None,
                "content": {"text": {"text": "周末合集"}},
            },
            {
                "id": 200,
                "date": 1002,
                "mediaAlbumId": 0,
                "forwardInfo": {"origin": {}},
                "content": {
                    "video": {
                        "fileName": "",
                        "video": {"remote": {"uniqueId": "UID_B2"}},
                    }
                },
            },
            {
                "id": 100,
                "date": 1000,
                "mediaAlbumId": 0,
                "forwardInfo": {"origin": {}},
                "content": {
                    "video": {
                        "fileName": "",
                        "video": {"remote": {"uniqueId": "UID_B1"}},
                    }
                },
            },
        ]
        parsed = parse_chat_history_notes(msgs)
        self.assertEqual(parsed["UID_B1"]["note"], "周末合集 1")
        self.assertEqual(parsed["UID_B2"]["note"], "周末合集 2")
        self.assertEqual(parsed["UID_DUP"]["note"], "周末合集 1 2")

    def test_multi_videos_sorted_by_file_size_smallest_is_1(self):
        """发两个/多个视频时：即使大视频先发、小视频后发，也严格按文件最小=1、次小=2、最大=N编号。"""
        msgs = [
            # 后发的视频（id=300），但体积最小（25 MB）-> 应该排第 1
            {
                "id": 300,
                "date": 1002,
                "mediaAlbumId": 999,
                "forwardInfo": {"origin": {}},
                "content": {
                    "video": {
                        "fileName": "3 (2).mp4",
                        "video": {"size": 25 * 1024 * 1024, "remote": {"uniqueId": "UID_SMALL"}},
                    }
                },
            },
            # 先发的视频（id=200），但体积最大（180 MB）-> 应该排第 2
            {
                "id": 200,
                "date": 1001,
                "mediaAlbumId": 999,
                "forwardInfo": {"origin": {}},
                "content": {
                    "video": {
                        "fileName": "3 (1).mp4",
                        "video": {"size": 180 * 1024 * 1024, "remote": {"uniqueId": "UID_LARGE"}},
                    }
                },
            },
            {
                "id": 100,
                "date": 1001,
                "mediaAlbumId": 0,
                "forwardInfo": None,
                "content": {"text": {"text": "合集测试"}},
            },
        ]
        parsed = parse_chat_history_notes(msgs)
        self.assertEqual(parsed["UID_SMALL"]["note"], "合集测试 1")
        self.assertEqual(parsed["UID_LARGE"]["note"], "合集测试 2")

    def test_album_swipe_reply_renames_whole_album_or_specific_index(self):
        """对合集左滑回复（TG 默认只带合集首条 mid）：支持整组改名、多行逐个改名、或 '2 名字' 指定单个。"""
        base_msgs = [
            {
                "id": 202,
                "date": 1000,
                "mediaAlbumId": 777,
                "forwardInfo": {"origin": {}},
                "content": {
                    "video": {
                        "fileName": "3 (2).mp4",
                        "video": {"size": 30 * 1024 * 1024, "remote": {"uniqueId": "ALB_SMALL"}},
                    }
                },
            },
            {
                "id": 201,
                "date": 1000,
                "mediaAlbumId": 777,
                "forwardInfo": {"origin": {}},
                "content": {
                    "video": {
                        "fileName": "3 (1).mp4",
                        "video": {"size": 90 * 1024 * 1024, "remote": {"uniqueId": "ALB_LARGE"}},
                    }
                },
            },
        ]
        # 1) 左滑合集回复一个名字 -> 整个合集按大小变成 1, 2
        m1 = [{"id": 301, "date": 1020, "replyTo": {"messageId": 201}, "content": {"text": {"text": "新合集名"}}}] + base_msgs
        p1 = parse_chat_history_notes(m1)
        self.assertEqual(p1["ALB_SMALL"]["note"], "新合集名 1")
        self.assertEqual(p1["ALB_LARGE"]["note"], "新合集名 2")

        # 2) 左滑合集回复两行名字 -> 按大小第 1 行给小视频、第 2 行给大视频
        m2 = [{"id": 302, "date": 1030, "replyTo": {"messageId": 201}, "content": {"text": {"text": "上集预告\n下集正片"}}}] + base_msgs
        p2 = parse_chat_history_notes(m2)
        self.assertEqual(p2["ALB_SMALL"]["note"], "上集预告")
        self.assertEqual(p2["ALB_LARGE"]["note"], "下集正片")

        # 3) 左滑合集回复 "2 单独改大视频" -> 只改第 2 个
        m3 = [{"id": 303, "date": 1040, "replyTo": {"messageId": 201}, "content": {"text": {"text": "2 单独改大视频"}}}] + m1
        p3 = parse_chat_history_notes(m3)
        self.assertEqual(p3["ALB_SMALL"]["note"], "新合集名 1")
        self.assertEqual(p3["ALB_LARGE"]["note"], "单独改大视频")

    def test_link_message_reply_note(self):
        """对发送 t.me 链接的消息左滑回复文字：通过 _LINK_MSG_TO_UID 映射给该链接下载的视频。"""
        from services.watch_service import _LINK_MSG_TO_UID
        _LINK_MSG_TO_UID["msg:8652569586:888"] = "UID_FROM_LINK_1"
        msgs = [
            {
                "id": 889,
                "date": 1020,
                "replyTo": {"messageId": 888},
                "content": {"text": {"text": "受限群视频专属名"}},
            },
        ]
        parse_chat_history_notes(msgs)
        self.assertEqual(_CUSTOM_FILE_NOTES.get("UID_FROM_LINK_1"), "受限群视频专属名")

    def test_human_name_uses_custom_note_and_caption_fallback(self):
        _CUSTOM_FILE_NOTES["UID_NOTE_1"] = "我的专属备注"
        rec1 = {"uniqueId": "UID_NOTE_1", "fileName": "3 (1).mp4", "type": "video"}
        self.assertEqual(_human_name(rec1), "我的专属备注.mp4")

        # 无自定义备注，但原名为空或无意义，从 caption 提取并清洗广告
        rec2 = {
            "uniqueId": "UID_CAP_1",
            "fileName": "",
            "type": "video",
            "caption": "【防失联电报群@ads123】 极品周末合集 第一弹\n第二行描述",
        }
        self.assertEqual(_human_name(rec2), "极品周末合集 第一弹.mp4")

    def test_ui_prompt_closes_backdrop_on_ok_and_cancel(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "static", "js", "ui_confirm.js"), encoding="utf-8").read()
        self.assertIn("var closePrompt = function", src)
        self.assertIn("el.classList.remove('show')", src)


if __name__ == "__main__":
    unittest.main()
