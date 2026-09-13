# -*- coding: utf-8 -*-
r"""回归测试：命名过滤不得吃掉文件名主体与后缀，查重不得误杀不同文件。

用户发现的真实问题：命名过滤会导致「文件丢失文件后缀名」。
排查后确认是三个独立缺陷，都会造成用户可见的「文件丢失」：

1. `_clean_archive_filename` 的群号规则写成 `(?:电报群?|TG群?|资源群?|发布群?|...)`，
   `群?` 让「群」可选，于是独立的「资源 / 发布 / 首发 / 电报 / TG」等
   完全正常的词被整段删除：
       `资源 2024 1080p.mp4` -> `2024 1080p.mp4`
   域名规则 `www\.[a-z0-9_]+` 不含 TLD，还把 `www.example.com.mp4` 啃成 `com.mp4`。

2. 查重指纹用 `fn.split(".")[0]` 比对，把同剧不同集判成同一个文件：
       `剧名.EP01.1080p.mp4`  与  `剧名.EP02.1080p.mp4`  ->  视为重复
   第二集在提交/归档时被**静默跳过**，用户真的丢文件。

3. 后端未返回 fileName 时显示名退化成「文件 <uniqueId>」（无后缀），
   归档后云端就是一堆没有后缀的文件，播放器/刮削器全认不出来。
"""
import posixpath
import unittest
from core.config import _clean_archive_filename, _ensure_archive_ext, _same_file_name


class TestCleanArchiveFilenamePreserves(unittest.TestCase):
    """缺陷 1：清洗不得吃掉名字主体，也不得动扩展名。"""

    def test_normal_words_are_not_eaten(self):
        """这些词曾是广告关键词，但独立出现时是正常文件名的一部分。"""
        cases = [
            ("资源.xyz.mp4", "资源"),
            ("资源 2024 1080p.mp4", "资源"),
            ("剧名 资源 第1集.mkv", "资源"),
            ("发布 2024 1080p.mp4", "发布"),
            ("剧名 发布 第1集.mkv", "发布"),
            ("首发 2024 1080p.mp4", "首发"),
            ("剧名 首发 第1集.mkv", "首发"),
            ("电报 2024 1080p.mp4", "电报"),
            ("剧名 电报 第1集.mkv", "电报"),
            ("TG 2024 1080p.mp4", "TG"),
            ("剧名 TG 第1集.mkv", "TG"),
        ]
        for raw, must_keep in cases:
            with self.subTest(raw=raw):
                out = _clean_archive_filename(raw, enabled=True)
                self.assertIn(must_keep.lower(), out.lower(),
                              "清洗吃掉了名字主体: %r -> %r" % (raw, out))

    def test_domain_rules_do_not_eat_name(self):
        """`www.example.com.mp4` 不得被啃成 `com.mp4`；年份不得被当域名删掉。"""
        cases = [
            ("www.example.com.mp4", "www"),
            ("测试.www.example.com.mp4", "测试"),
            ("电影.2024.xyz.mp4", "2024"),
            ("剧名.EP01.1080p.mkv", "EP01"),
            ("video.t.me.mp4", "video"),
        ]
        for raw, must_keep in cases:
            with self.subTest(raw=raw):
                out = _clean_archive_filename(raw, enabled=True)
                self.assertIn(must_keep.lower(), out.lower(),
                              "域名规则吃掉了名字: %r -> %r" % (raw, out))

    def test_extension_always_preserved(self):
        """扩展名必须在清洗前后完全一致（大小写不敏感）。"""
        for raw in ("资源.xyz.mp4", "www.example.com.mp4", "剧名.EP01.1080p.mkv",
                    "中文名.MP4", "a.tar.gz", "无扩展名",
                    "剧名【关注公众号】.mp4", "视频@channel.mkv"):
            with self.subTest(raw=raw):
                out = _clean_archive_filename(raw, enabled=True)
                ei = posixpath.splitext(raw)[1].lower()
                eo = posixpath.splitext(out)[1].lower()
                if ei:
                    self.assertEqual(ei, eo, "扩展名被改动: %r -> %r" % (raw, out))

    def test_real_ads_still_removed(self):
        """真实广告仍要清掉——修复不能把功能改没。"""
        cases = [
            ("剧名.EP01.1080p【关注公众号：xxx】.mp4", "公众号"),
            ("电影【唯一地址 t.me/xyz】.mp4", "t.me"),
            ("视频@SomeChannel.mp4", "@somechannel"),
            ("剧名【电报群 @abc】.mkv", "电报群"),
            ("剧名.EP01【首发群】.mp4", "首发群"),
        ]
        for raw, ad in cases:
            with self.subTest(raw=raw):
                out = _clean_archive_filename(raw, enabled=True)
                self.assertNotIn(ad.lower(), out.lower(), "广告未被清除: %r -> %r" % (raw, out))

    def test_overcleaning_falls_back_to_original(self):
        """清洗到几乎什么都不剩时，必须回退原名而非产出空名。"""
        for raw in ("剧名.mp4", "A.mp4", "资源.mp4", "电影.mp4"):
            with self.subTest(raw=raw):
                out = _clean_archive_filename(raw, enabled=True)
                self.assertTrue(out.strip(), "产出空文件名: %r" % raw)
                self.assertEqual(posixpath.splitext(out)[1].lower(),
                                 posixpath.splitext(raw)[1].lower())

    def test_disabled_returns_unchanged(self):
        """开关关闭时必须原样返回。"""
        raw = "资源 2024【公众号】.mp4"
        self.assertEqual(_clean_archive_filename(raw, enabled=False), raw)

    def test_idempotent(self):
        """重复清洗必须稳定（幂等），否则每次归档名字都在变。"""
        for raw in ("资源.xyz.mp4", "剧名.EP01【关注公众号】.mp4", "www.example.com.mp4"):
            with self.subTest(raw=raw):
                once = _clean_archive_filename(raw, enabled=True)
                twice = _clean_archive_filename(once, enabled=True)
                self.assertEqual(once, twice, "清洗不幂等: %r -> %r -> %r" % (raw, once, twice))


class TestSameFileName(unittest.TestCase):
    """缺陷 2：查重指纹不得误杀不同文件。"""

    def test_different_episodes_are_not_same(self):
        """同剧不同集绝不能判为同一个文件（否则静默跳过 = 丢文件）。"""
        pairs = [
            ("剧名.EP01.1080p.mp4", "剧名.EP02.1080p.mp4"),
            ("Show.S01E01.mkv", "Show.S01E02.mkv"),
            ("影片.上集.mp4", "影片.下集.mp4"),
            ("A.2024.mp4", "A.2023.mp4"),
            ("剧名.EP01.1080p.mp4", "剧名.EP01.720p.mp4"),
        ]
        for a, b in pairs:
            with self.subTest(a=a, b=b):
                self.assertFalse(_same_file_name(a, b),
                                 "不同文件被误判为相同: %r vs %r" % (a, b))

    def test_equivalent_names_are_same(self):
        """真正的等价情形仍要识别为同一个（保持查重能力）。"""
        pairs = [
            ("Movie.1080p.mp4", "Movie.1080p.mp4"),
            ("Movie.1080p.MP4", "movie.1080p.mp4"),
            ("影片.mp4", "影片"),
            (" 影片.mp4 ", "影片.mp4"),
        ]
        for a, b in pairs:
            with self.subTest(a=a, b=b):
                self.assertTrue(_same_file_name(a, b), "等价名未识别: %r vs %r" % (a, b))

    def test_empty_names_are_not_same(self):
        """空名之间不得判为相同，否则会误触发去重。"""
        self.assertFalse(_same_file_name("", "a.mp4"))
        self.assertFalse(_same_file_name("a.mp4", ""))
        self.assertFalse(_same_file_name("", ""))

    def test_prefix_names_are_not_same(self):
        """前缀关系不算同一个文件。"""
        self.assertFalse(_same_file_name("影片.mp4", "影片2.mp4"))
        self.assertFalse(_same_file_name("EP01.mkv", "EP010.mkv"))


class TestEnsureArchiveExt(unittest.TestCase):
    """缺陷 3：显示名缺后缀时必须补回真实后缀。"""

    def test_from_local_path(self):
        self.assertEqual(_ensure_archive_ext("文件 AgADEigABC", local_path="/data/a/abc.xyz") ,
                         "文件 AgADEigABC.xyz")
        self.assertEqual(_ensure_archive_ext("文件 AgADEigABC", local_path="/data/a/abc.mp4"),
                         "文件 AgADEigABC.mp4")

    def test_from_mime(self):
        self.assertEqual(_ensure_archive_ext("文件 A", mime="video/x-matroska"), "文件 A.mkv")
        self.assertEqual(_ensure_archive_ext("文件 A", mime="VIDEO/MP4"), "文件 A.mp4")

    def test_from_ftype(self):
        self.assertEqual(_ensure_archive_ext("文件 A", ftype="video"), "文件 A.mp4")
        self.assertEqual(_ensure_archive_ext("文件 A", ftype="audio"), "文件 A.mp3")
        self.assertEqual(_ensure_archive_ext("文件 A", ftype="photo"), "文件 A.jpg")

    def test_existing_ext_untouched(self):
        """已有后缀绝不改动，哪怕 local_path 的后缀不同。"""
        self.assertEqual(_ensure_archive_ext("正常名.mkv", local_path="/x/y.mp4"), "正常名.mkv")

    def test_priority_local_over_mime(self):
        """本地路径后缀优先于 MIME（更贴近真实文件）。"""
        out = _ensure_archive_ext("文件 A", local_path="/x/y.mkv", mime="video/mp4")
        self.assertTrue(out.endswith(".mkv"))

    def test_no_inference_keeps_name(self):
        """推断不出时保持原样，不得瞎编后缀。"""
        self.assertEqual(_ensure_archive_ext("文件 A"), "文件 A")
        self.assertEqual(_ensure_archive_ext("文件 A", mime="application/octet-stream"), "文件 A")

    def test_empty_name(self):
        self.assertEqual(_ensure_archive_ext(""), "")


if __name__ == "__main__":
    unittest.main()
