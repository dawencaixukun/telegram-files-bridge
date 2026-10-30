# -*- coding: utf-8 -*-
"""
tests/test_thumb_heal_size.py
=============================
缩略图补图选档回归（用户反馈：云端归档缩略图不如浏览页清晰）：

历史缺陷：_heal_thumbnail 兜底选档用 min(...)，永远挑【最小】的可下载节点。
TG 照片消息带多档尺寸（i/m/j…），补图结果恒为最小档，云端归档卡片上
一拉伸就糊；浏览页视频只有单档缩略图、图片被全站屏蔽，同一缺陷不暴露。
契约：同档内必须挑【最大】的节点（AQAD 缩略图优先，无 AQAD 时在全部
候选里挑最大），_THUMB_HEAL_MAX_BYTES 上限行为不变。
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.media_service as ms


def _node(uid, size):
    # 与 _collect_file_nodes 契约一致：File 节点 = {"id": int, "remote": {"id", "uniqueId"}}
    return {"id": 7, "size": size, "remote": {"id": 7, "uniqueId": uid, "id_": uid}}


class TestThumbHealPicksLargest(unittest.TestCase):
    def _run_heal(self, nodes, main_uid="AQADmain"):
        """在 GetMessage 命中多档尺寸的场景下跑一次补图，返回被选中的 uid。"""
        async def fake_api(method, payload, timeout=None):
            if method == "GetMessage":
                return {"photo": {"sizes": nodes}}
            if method == "DownloadFile":
                return {"local": {"isDownloadingCompleted": True, "path": "x"}}
            raise AssertionError("unexpected " + method)

        captured = {}

        async def fake_api2(method, payload, timeout=None):
            if method == "DownloadFile":
                captured["fileId"] = payload.get("fileId")
                return {"local": {"isDownloadingCompleted": True, "path": "x"}}
            return await fake_api(method, payload, timeout)

        with patch.object(ms, "telegram_api_call", new=fake_api2), \
             patch.object(ms, "_thumb_heal_failed", {}), \
             patch.object(ms, "_thumb_heal_inflight", set()):
            uid = asyncio.run(ms._heal_thumbnail(main_uid, 42, 1001))
        return uid, captured

    def test_photo_multi_size_picks_largest_thumb(self):
        """照片多档尺寸：不再挑最小档，必须挑最大档（清晰度契约）。"""
        nodes = [_node("AgACi", 4 * 1024), _node("AgACm", 24 * 1024), _node("AgACj", 180 * 1024)]
        uid, cap = self._run_heal(nodes)
        self.assertIsNotNone(uid)
        self.assertEqual(cap["fileId"], 7)  # 选中节点的 File id
        # 选中 uid 是最大档
        self.assertEqual(uid, "AgACj")

    def test_aqad_preferred_over_larger_agac(self):
        """AQAD 缩略图优先：即使 AgAC 更大也选 AQAD。"""
        nodes = [_node("AgACbig", 500 * 1024), _node("AQADthumb", 64 * 1024)]
        uid, _ = self._run_heal(nodes)
        self.assertEqual(uid, "AQADthumb")

    def test_video_single_thumb_unchanged(self):
        """视频消息（单档缩略图）行为不变：照常选中该缩略图。"""
        nodes = [_node("AQADthumb", 32 * 1024)]
        uid, _ = self._run_heal(nodes)
        self.assertEqual(uid, "AQADthumb")


if __name__ == "__main__":
    unittest.main()
