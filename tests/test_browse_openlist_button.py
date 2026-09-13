# -*- coding: utf-8 -*-
r"""回归测试：浏览页已归档卡片的底部操作按钮。

需求演进（均为用户反馈）：
1. 已归档旁加 OpenList 打开按钮；
2. 两个独立 badge 视觉生裂 → 连体分段药丸 pill-group；
3. 出现两个「已归档」（独立「云端已归档」胶囊与药丸左段并存）→ 移除独立胶囊；
4. 文字穿模（seg 继承 .badge 基类圆角/边框）→ seg 脱离 badge 类，CSS 自洽；
5. 底部药丸左段「已归档」与缩略图右上角 t-badge「已归档」重复（用户圈出）→
   移除药丸左段，底部只保留 OpenList 按钮；归档状态由缩略图徽章唯一表达。

约束：
- 已归档 + 有 openlist_url：底部只有 pill-group（内仅 OpenList 一枚按钮），
  卡片上「已归档」字样只出现在缩略图 t-badge 一处；
- 已归档 + 无 openlist_url：退化为「资产详情」入口（不再重复「已归档」文案）；
- 未归档卡片不受影响。
"""
import re
import unittest
from core.templates import templates


class _Req:
    scope = {"type": "http"}


def _row(name, is_archived, cloud_path="", openlist_url="", dl="idle"):
    return {
        "telegramId": 10086, "chatId": -100123, "messageId": 1, "fileId": 2,
        "uniqueId": "UID-%s" % name, "name": name, "ext": "MP4",
        "size_str": "213.6 MB", "date_str": "2026-09-09 08:01",
        "type": "video", "type_label": "视频",
        "thumb": "", "thumb_uid": "", "dl": dl, "tr": "idle", "dur_str": "0:11:13",
        "is_archived": is_archived, "is_archiving": False,
        "cloud_path": cloud_path, "cloud_drive": "阿里云盘",
        "openlist_url": openlist_url,
        "archived_date": "09-09 08:05", "local_path": "",
    }


def _render(rows):
    return templates.get_template("partials/_browse_files.html").render({
        "request": _Req(), "browse_rows": rows, "browse_count": len(rows),
        "browse_cursor": "", "browse_collapsed": 0, "browse_loaded": len(rows),
        "sel_tg": "", "sel_chat": "", "sel_type": "video",
    })


def _cards(html):
    return re.findall(r'<div class="thumb browse-card.*?(?=<div class="thumb browse-card|\Z)',
                      html, re.S)


class TestPillGroup(unittest.TestCase):
    def test_archived_card_has_openlist_button_only(self):
        """已归档 + 有 URL：底部 pill-group 只有一枚 OpenList 按钮。"""
        html = _render([_row("a.mp4", True, "/阿里云盘/tg/a.mp4",
                             "http://127.0.0.1:5244/a.mp4")])
        self.assertIn('class="pill-group"', html)
        self.assertIn("seg seg-right", html)
        # 左段已移除：不再有「已归档」按钮段
        self.assertNotIn("seg seg-left", html)

    def test_archive_label_appears_only_in_thumbnail_badge(self):
        """核心回归：「已归档」可见文案全卡片只出现一次（缩略图 t-badge）。
        title 提示属性不计（悬停才显示，非可见重复）。"""
        html = _render([_row("a.mp4", True, "/阿里云盘/tg/a.mp4",
                             "http://127.0.0.1:5244/a.mp4")])
        card = _cards(html)[0]
        self.assertEqual(card.count(">已归档"), 1,
                         "「已归档」可见文案出现多次（用户圈出的缺陷）")
        self.assertIn("t-badge", card)
        self.assertIn("<span>OpenList</span>", card)

    def test_archived_without_url_falls_back_to_detail_entry(self):
        """已归档但无 openlist_url：退化为「资产详情」入口，不重复归档文案。"""
        html = _render([_row("c.mp4", True, cloud_path="", openlist_url="")])
        self.assertNotIn("pill-group", html)
        self.assertIn("资产详情", html)
        self.assertNotIn("云端已归档", html)

    def test_pill_group_only_on_archived_card(self):
        """混合列表中，pill-group 只应出现在已归档那一条。"""
        html = _render([
            _row("archived.mp4", True, "/阿里云盘/tg/a.mp4", "http://x/a.mp4"),
            _row("plain.mp4", False),
        ])
        cards = _cards(html)
        self.assertEqual(len(cards), 2)
        self.assertIn("pill-group", cards[0], "已归档卡片应带 OpenList 按钮")
        self.assertNotIn("pill-group", cards[1], "未归档卡片不该带药丸")

    def test_unarchived_card_unaffected(self):
        """未归档卡片仍显示「未下载」状态胶囊。"""
        html = _render([_row("f.mp4", False)])
        self.assertNotIn("pill-group", html)
        self.assertIn("未下载", html)

    def test_no_msg_label(self):
        """msg N 不得再出现在可见文案中（挤压按钮的根因）。"""
        html = _render([_row("g.mp4", True, "/阿里云盘/tg/g.mp4", "http://x/g.mp4")])
        self.assertNotIn(">msg ", html)
        self.assertIn('data-msg="1"', html)  # dataset 仍保留供抽屉/提交用

    def test_card_carries_dataset_for_js(self):
        """卡片必须带 data-cloud-path / data-openlist-url，JS 才能重拼地址。"""
        html = _render([_row("e.mp4", True, "/阿里云盘/tg/e.mp4", "http://x/e.mp4")])
        self.assertIn('data-cloud-path="/阿里云盘/tg/e.mp4"', html)
        self.assertIn('data-openlist-url="http://x/e.mp4"', html)


if __name__ == "__main__":
    unittest.main()
