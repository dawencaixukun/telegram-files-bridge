# -*- coding: utf-8 -*-
r"""回归测试：浏览页「媒体」分类只保留视频（屏蔽图片）。

历史缺陷：后端 type=media 同时返回 video 与 photo，「媒体」分类下
图片与视频混排（用户要求：媒体分类只留视频）。

契约：_browse_files 对 type=media 的页面在去重后二次过滤，仅保留
type=video 或 mimeType=video/* 的记录；其他类型不受影响。
"""
import asyncio
import unittest
from unittest.mock import patch, AsyncMock
from services import browse_service as bs


def _file(ftype="video", mime="video/mp4", uid=None):
    return {
        "id": uid or 1, "uniqueId": uid or "UID-1", "telegramId": 10086,
        "chatId": -100123, "messageId": 1, "date": 1789000000,
        "size": 1024, "type": ftype, "mimeType": mime,
        "fileName": "f.mp4", "downloadStatus": "idle",
    }


def _run_browse(files, type_):
    """受控执行 _browse_files：mock 后端响应与去重状态。"""
    fake_resp = {"files": files, "count": len(files), "nextFromMessageId": 0}

    async def run():
        with patch.object(bs.BACKEND, "_safe_id", side_effect=lambda v: v), \
             patch.object(bs.BACKEND, "_request", new=AsyncMock(return_value=fake_resp)), \
             patch.object(bs, "_browse_seen_state",
                          return_value={"collapsed": 0, "loaded": 0}), \
             patch.object(bs, "_browse_dedup_page",
                          side_effect=lambda state, fs, cur: list(fs)):
            return await bs._browse_files(10086, -100123, type_)

    return asyncio.run(run())


class TestMediaTypeVideoOnly(unittest.TestCase):
    def test_media_filters_out_photos(self):
        """media 页面：photo 记录被过滤，仅剩 video。"""
        rows, count, cursor, _ = _run_browse(
            [_file("video"), _file("photo", "image/jpeg", "UID-p")], "media")
        self.assertEqual(len(rows), 1)
        self.assertEqual(count, 1)

    def test_document_page_also_filters_photos(self):
        """「全部」(document) 页同样屏蔽图片：浏览页全站只见视频。"""
        rows, count, _, _ = _run_browse(
            [_file("video"), _file("photo", "image/jpeg", "UID-p")], "document")
        self.assertEqual(len(rows), 1)
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
