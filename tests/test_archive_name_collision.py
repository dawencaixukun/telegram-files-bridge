# -*- coding: utf-8 -*-
"""归档「同名坍缩」防线的回归测试。

背景（数据丢失级事故）：文件名清洗会整段删掉 Telegram 广告尾巴，而区分同批文件的
序号常长在尾巴里（`…@siwasc8_10/11/31.mp4`）。清洗后多个不同文件指向同一个
`remote_path`，OpenList 同路径上传是覆盖语义 → 云端只剩最后一个，且 deleteLocal
已删本地。实测已造成 12 个文件（约 11.44 GB）无法找回。

本文件锁住五道防线，任何一道被改回去都必须让测试红：
  1. 清洗不再因 `@` 把整段方括号名吃掉（区分度不再丢在源头）
  2. `_archive_unique_name` 同名自动补 ` (N)`，不同 uid 不共用路径
  3. `_archive_name_suffix` 把被吃掉的序号补回名字
  4. 自动归档路径也走同一套唯一化（原先完全没有同名守卫）
  5. 前端读 `errors` 并显示部分失败；`_archive_upload_lock` 同路径串行
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import (  # noqa: E402
    _clean_archive_filename, _archive_unique_name, _archive_name_suffix, _archive_join,
)
from core.state import _archive_upload_lock  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _readp(path):
    """读项目内文件（用 with 关闭句柄，避免 ResourceWarning 噪音）。"""
    with open(path, encoding="utf-8") as fh:
        return fh.read()



class TestCleanKeepsDistinguishers(unittest.TestCase):
    """防线 1：清洗不能把「区分同一批文件的信息」整段吃掉。"""

    def test_handle_in_bracket_no_longer_wipes_bracket(self):
        """回归实锤：`【优优】…电报搜：@siwasc8_10.mp4` 里的 `@` 曾让整段方括号被删。

        删除后名字退化成无句柄版，与「带 (1) 的兄弟文件」洗出同一串 ——
        同一批文件于是全部撞同一路径。
        """
        out = _clean_archive_filename("【优优】全网优质丝袜资源电报搜：@siwasc8_10.mp4")
        # 必须仍有可区分的数字尾巴，且不是被洗成与兄弟文件一模一样的串
        self.assertIn("10", out)
        self.assertTrue(out.endswith(".mp4"))

    def test_siblings_stay_distinct(self):
        """同批文件的清洗结果必须两两不同（这是不再互相覆盖的前提）。"""
        raws = [
            "【优优】全网优质丝袜资源电报搜：@siwasc8_10.mp4",
            "【优优】全网优质丝袜资源电报搜：@siwasc8_11.mp4",
            "【优优】全网优质丝袜资源电报搜：@siwasc8_31.mp4",
        ]
        outs = [_clean_archive_filename(r) for r in raws]
        self.assertEqual(len(set(outs)), len(outs),
                         "同批文件清洗后出现同名: %r" % (outs,))

    def test_normal_name_with_bracket_resource_not_eaten(self):
        """带正常词括号的名字不能被整段吃掉（`资源` 是常见正常词）。"""
        out = _clean_archive_filename("资源 2024 1080p.mp4")
        self.assertIn("2024", out)
        self.assertIn("1080p", out)

    def test_ad_junk_still_cleaned(self):
        """广告尾巴仍要被清（修复不能把清洗整条废掉）。"""
        out = _clean_archive_filename("电影名 www.example.com @somechannel 2024.mp4")
        self.assertNotIn("example.com", out)
        self.assertNotIn("@somechannel", out)
        self.assertIn("电影名", out)
        self.assertIn("2024", out)

    def test_all_handle_name_not_reduced_to_bare_number(self):
        """安全网二：名字整段只有句柄时，不能退化成无意义的纯数字名。"""
        out = _clean_archive_filename("@channel_10.mp4")
        self.assertNotEqual(out, "10.mp4")
        self.assertTrue(out.endswith(".mp4"))

    def test_numeric_name_kept_when_already_numeric(self):
        """本来就叫纯数字的名字不乱改（不能拿 nonexistent 回退玩坏它）。"""
        self.assertEqual(_clean_archive_filename("10.mp4"), "10.mp4")


class TestUniqueName(unittest.TestCase):
    """防线 2：同名自动补 ` (N)`；同一文件复用自己路径不变名。"""

    def test_first_use_keeps_name(self):
        n = _archive_unique_name("/d", "a.mp4", owner_of={}, uid="u1")
        self.assertEqual(n, "a.mp4")

    def test_other_uid_gets_suffix(self):
        p = _archive_join("/d", "a.mp4").lower()
        n = _archive_unique_name("/d", "a.mp4", owner_of={p: {"u1"}}, uid="u2")
        self.assertEqual(n, "a (1).mp4")

    def test_same_uid_reuses_path(self):
        """同一文件重复归档（重新归档按钮）应复用原路径，不该越积越多 (N)。"""
        p = _archive_join("/d", "a.mp4").lower()
        n = _archive_unique_name("/d", "a.mp4", owner_of={p: {"u1"}}, uid="u1")
        self.assertEqual(n, "a.mp4")

    def test_counts_up_past_existing(self):
        p0 = _archive_join("/d", "a.mp4").lower()
        p1 = _archive_join("/d", "a (1).mp4").lower()
        p2 = _archive_join("/d", "a (2).mp4").lower()
        n = _archive_unique_name("/d", "a.mp4",
                                 owner_of={p0: {"u1"}, p1: {"u2"}, p2: {"u3"}}, uid="u9")
        self.assertEqual(n, "a (3).mp4")

    def test_three_colliding_files_get_three_paths(self):
        """核心断言：三个不同文件绝不共用同一个 remote_path。"""
        owners = {}
        names = []
        for uid in ("u1", "u2", "u3"):
            nm = _archive_unique_name("/d", "same.mp4", owner_of=owners, uid=uid, suffix=uid)
            owners.setdefault(_archive_join("/d", nm).lower(), set()).add(uid)
            names.append(nm)
        self.assertEqual(len(set(names)), 3, names)

    def test_extension_preserved(self):
        p = _archive_join("/d", "a.mp4").lower()
        n = _archive_unique_name("/d", "a.mp4", owner_of={p: {"u1"}}, uid="u2")
        self.assertTrue(n.endswith(".mp4"))

    def test_no_ext_name(self):
        p = _archive_join("/d", "noext").lower()
        n = _archive_unique_name("/d", "noext", owner_of={p: {"u1"}}, uid="u2")
        self.assertEqual(n, "noext (1)")


class TestNameSuffix(unittest.TestCase):
    """防线 3：序号被清洗吃掉时补回名字。"""

    def test_recovers_lost_index(self):
        raw = "【优优】全网优质丝袜资源电报搜：@siwasc8_10.mp4"
        clean = "【优优】全网优质丝袜资源电报搜：.mp4"
        self.assertEqual(_archive_name_suffix(raw, clean), "10")

    def test_empty_when_clean_keeps_it(self):
        self.assertEqual(_archive_name_suffix("v_10.mp4", "v_10.mp4"), "")

    def test_empty_when_no_index(self):
        self.assertEqual(_archive_name_suffix("普通文件.mp4", "普通文件.mp4"), "")

    def test_suffix_makes_names_distinct(self):
        """补回序号后，两个原本同名的清洗结果能区分开。"""
        cd = "【优优】…搜：.mp4"
        a = _archive_unique_name("/d", cd, owner_of={}, uid="u1",
                                 suffix=_archive_name_suffix("x_10.mp4", cd))
        b = _archive_unique_name("/d", cd, owner_of={}, uid="u2",
                                 suffix=_archive_name_suffix("x_11.mp4", cd))
        self.assertNotEqual(a, b)
        self.assertIn("10", a)
        self.assertIn("11", b)


class TestAutoPathHasGuard(unittest.TestCase):
    """防线 4：自动归档（订阅规则）也必须走唯一化 —— 它原先完全没有守卫。"""

    def test_sweep_uses_unique_name(self):
        src = _readp(os.path.join(ROOT, "services", "archive_service.py"))
        seg = src[src.index("_auto_archive_sweep_impl"):]
        seg = seg[:seg.index("async def _auto_archive_loop")]
        self.assertIn("_archive_unique_name", seg,
                      "自动归档入队未做文件名唯一化，同名坍缩会复发")
        self.assertIn("path_owners", seg)

    def test_manual_path_uses_unique_name(self):
        src = _readp(os.path.join(ROOT, "routers", "archive.py"))
        self.assertIn("_archive_unique_name", src)
        self.assertIn("path_owners", src)

    def test_old_reject_guard_removed(self):
        """旧的「同名排入本批就整条拒绝」已换成唯一化：不能再退回去。"""
        src = _readp(os.path.join(ROOT, "routers", "archive.py"))
        self.assertNotIn("请分开归档", src)


class TestFrontendReportsErrors(unittest.TestCase):
    """防线 5：前端必须读 errors，不能假报成功。"""

    def test_start_archive_reads_errors(self):
        js = _readp(os.path.join(ROOT, "static", "js", "app.js"))
        seg = js[js.index("window.__startArchive"):]
        seg = seg[:seg.index("/* ---------------- 归档进度轮询")]
        self.assertIn("d.errors", seg, "前端未读后端 errors（会假报「已加入归档队列」）")
        self.assertIn("部分文件未加入队列", seg)

    def test_warn_toast_supported(self):
        js = _readp(os.path.join(ROOT, "static", "js", "app.js"))
        self.assertIn("'warn'", js)
        css = _readp(os.path.join(ROOT, "static", "css", "main.css"))
        self.assertIn(".toast.warn", css)


class TestUploadLock(unittest.TestCase):
    """防线 5b：同一 remote_path 的上传必须串行（消灭 eTag 冲突）。"""

    def test_same_path_same_lock(self):
        a = _archive_upload_lock("/d/a.mp4")
        b = _archive_upload_lock("/d/a.mp4")
        self.assertIs(a, b)

    def test_case_insensitive(self):
        self.assertIs(_archive_upload_lock("/d/A.mp4"), _archive_upload_lock("/d/a.mp4"))

    def test_different_paths_different_locks(self):
        self.assertIsNot(_archive_upload_lock("/d/a.mp4"), _archive_upload_lock("/d/b.mp4"))

    def test_lock_actually_serializes(self):
        """两个协程争同一路径的锁时不能同时进入临界区。"""
        order = []

        async def worker(tag):
            async with _archive_upload_lock("/d/serial.mp4"):
                order.append(("in", tag))
                await asyncio.sleep(0.05)
                order.append(("out", tag))

        async def main():
            await asyncio.gather(worker("A"), worker("B"))

        asyncio.run(main())
        # 必须成对进出：in A, out A, in B, out B（或 B 先），不能交错
        self.assertEqual(order[0][0], "in")
        self.assertEqual(order[1][0], "out")
        self.assertEqual(order[1][1], order[0][1])
        self.assertEqual(order[2][0], "in")
        self.assertEqual(order[3][0], "out")

    def test_worker_uses_lock(self):
        src = _readp(os.path.join(ROOT, "services", "archive_service.py"))
        seg = src[src.index("async def _archive_worker"):]
        seg = seg[:seg.index("async def ", seg.index("async def _archive_worker") + 10)]
        self.assertIn("_archive_upload_lock", seg,
                      "上传未按路径串行，同路径并发写会报 resourceModified / 静默覆盖")


if __name__ == "__main__":
    unittest.main(verbosity=2)
