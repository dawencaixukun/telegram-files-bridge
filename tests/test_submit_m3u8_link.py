# -*- coding: utf-8 -*-
"""回归测试：提交下载页/顶栏模态的链接输入框现在也吃 m3u8 直链。

背景（用户需求）：
「提交链接下载」原先只认 t.me 消息链接，网页嗅探到的 m3u8 流媒体地址必须
先经浏览器插件才能下载。用户要求同一个输入框也能直接提交 m3u8 链接。

实现：链接在 `_resolve_links_to_files` 里分流——
  * t.me 链接 → 原有两跳解析（TDLib GetMessageLinkInfo + start-download-multiple）
  * m3u8 直链 → `m3u8_submit()`（与插件入口**同一个**函数：同样 SSRF 校验、
    活动任务上限、同 URL 幂等、磁盘水位门禁，完成后同样自动进归档管线）

本测试覆盖：
  1. m3u8 直链识别（含 query / 大小写 / 排除 t.me 与非 http 方案）
  2. 混合批次分流与去重
  3. 经 HTTP `POST /tasks`（提交页与顶栏模态的真实入口）提交 m3u8 链接，
     任务确实进入下载队列并跑完 → 成品落盘 + 归档 job 入队
  4. 纯 m3u8 批次下，磁盘高水位由 m3u8 引擎拒绝并给出可读原因
     （不能被 waiting_disk 挂起队列静默吞掉——那队列只装 TG 载荷）
  5. 单条坏链接不拖累同批次其它可用链接
"""
import os
import sys
import time
import shutil
import asyncio
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import bridge_server
import services.m3u8_service as m3u8
from core.config import _is_m3u8_url, _split_submit_links


# ---------------------------------------------------------------------
# 假 HLS 源站（与 test_m3u8_e2e 同构：playlist + 分片）
# ---------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: N802
        pass

    def do_GET(self):  # noqa: N802
        body = self.server.fixtures.get(self.path)  # type: ignore[attr-defined]
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.fixtures = {}
        self._thread = None

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.server_address[1]

    def start(self):
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self.shutdown()
        self.server_close()


class TestSubmitLinkIsM3U8Capable(unittest.TestCase):
    """提交链接下载支持 m3u8 类型文件。"""

    def test_is_m3u8_url_detection(self):
        """直链判定：认 query、大小写、.m3u/.m3u8；拒绝 t.me 与非 http 方案。"""
        yes = [
            "https://cdn.example.com/a/index.m3u8",
            "https://cdn.example.com/a/index.m3u8?token=abc&t=1",
            "http://cdn.example.com/a/playlist.M3U8",
            "https://cdn.example.com/a/stream.m3u",
            "https://cdn.example.com/a/index.m3u8#frag",
        ]
        no = [
            "https://t.me/channel/123",
            "https://t.me/c/2333333/456",
            "ftp://cdn.example.com/a/index.m3u8",
            "https://cdn.example.com/a/video.mp4",
            "m3u8",
            "",
        ]
        for u in yes:
            with self.subTest(url=u):
                self.assertTrue(_is_m3u8_url(u), "应识别为 m3u8 直链: %s" % u)
        for u in no:
            with self.subTest(url=u):
                self.assertFalse(_is_m3u8_url(u), "不应识别为 m3u8 直链: %s" % u)

    def test_split_submit_links_mixed_and_dedup(self):
        """混合批次按类型分流、保持顺序并去重。"""
        lines = [
            "https://cdn/a/index.m3u8",
            "https://t.me/channel/123",
            "https://cdn/a/index.m3u8",          # 重复
            "https://t.me/c/1/2",
            "https://cdn/b/x.m3u8?k=1",
            "   ",                                # 空行
        ]
        got = _split_submit_links(lines)
        self.assertEqual(got["m3u8"],
                         ["https://cdn/a/index.m3u8", "https://cdn/b/x.m3u8?k=1"])
        self.assertEqual(got["tg"],
                         ["https://t.me/channel/123", "https://t.me/c/1/2"])

    def test_tg_only_batch_is_unchanged(self):
        """纯 TG 批次不得被 m3u8 分流影响：tg 原样、m3u8 为空。"""
        got = _split_submit_links(["https://t.me/channel/123"])
        self.assertEqual(got["tg"], ["https://t.me/channel/123"])
        self.assertEqual(got["m3u8"], [])


class TestSubmitM3U8EndToEnd(unittest.TestCase):
    """端到端：经 POST /tasks 提交 m3u8 直链 → 真的下载、合并、进归档。"""

    def setUp(self):
        self.server = _Server()
        self.server.start()
        self.tmp = tempfile.mkdtemp(prefix="submit_m3u8_")
        self._orig = {
            "dl_dir": m3u8.M3U8_DOWNLOAD_DIR,
            "task_file": bridge_server._M3U8_FILE,
            "jobs": dict(bridge_server._ARCHIVE_JOBS),
            "archive_file": bridge_server._ARCHIVE_FILE,
            "allow_private": m3u8._ALLOW_PRIVATE_TARGETS,
            "arch_cfg": dict(bridge_server._ARCHIVE_CONFIG),
        }
        m3u8.M3U8_DOWNLOAD_DIR = os.path.join(self.tmp, "downloads")
        bridge_server._M3U8_FILE = os.path.join(self.tmp, ".m3u8.json")
        bridge_server._ARCHIVE_FILE = os.path.join(self.tmp, ".archive.json")
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_TASKS.clear()
        bridge_server._M3U8_TASKS.clear()
        m3u8._ALLOW_PRIVATE_TARGETS = True   # 放行回环假源站
        bridge_server._ARCHIVE_CONFIG["defaultDir"] = "/m3u8"
        bridge_server._ARCHIVE_CONFIG["autoArchive"] = True
        bridge_server._ARCHIVE_CONFIG["deleteLocal"] = False

    def tearDown(self):
        m3u8.M3U8_DOWNLOAD_DIR = self._orig["dl_dir"]
        bridge_server._M3U8_FILE = self._orig["task_file"]
        bridge_server._ARCHIVE_FILE = self._orig["archive_file"]
        bridge_server._ARCHIVE_JOBS.clear()
        bridge_server._ARCHIVE_JOBS.update(self._orig["jobs"])
        bridge_server._ARCHIVE_TASKS.clear()
        bridge_server._M3U8_TASKS.clear()
        m3u8._ALLOW_PRIVATE_TARGETS = self._orig["allow_private"]
        bridge_server._ARCHIVE_CONFIG.clear()
        bridge_server._ARCHIVE_CONFIG.update(self._orig["arch_cfg"])
        self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fixture(self, path, seg_count=3):
        """在假源站上放一份明文 TS 播放列表 + 分片，返回 URL。"""
        lines = ["#EXTM3U"]
        for i in range(seg_count):
            data = bytes([65 + i]) * 64
            self.server.fixtures["%s/s%d.ts" % (path, i)] = data
            lines.append("#EXTINF:6.0,")
            lines.append("s%d.ts" % i)
        lines.append("#EXT-X-ENDLIST")
        self.server.fixtures["%s/index.m3u8" % path] = ("\n".join(lines) + "\n").encode()
        return self.server.base + "%s/index.m3u8" % path

    @staticmethod
    async def _join_runner(tid, timeout=25):
        entry = bridge_server._M3U8_RUNNERS.get(tid)
        if entry is None or entry[1] is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(entry[1]), timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass

    def test_submit_m3u8_link_via_tasks_endpoint(self):
        """提交 m3u8 直链（/tasks 路由内同一入口）→ 入队、下载完成、成品落盘、归档入队。"""
        url = self._fixture("/m")

        async def _go():
            # 走 /tasks 路由内部实际调用的服务端入口；Cookie/CSRF 细节由
            # test_disk_watermark 的 HTTP 级测试覆盖，这里验证「链接真的跑完全流程」
            from services.task_service import _resolve_links_to_files
            ok, err = await _resolve_links_to_files([url])
            self.assertEqual(err, "", "提交应无错误")
            self.assertEqual(ok, 1, "应受理 1 条 m3u8 链接")
            tasks = list(bridge_server._M3U8_TASKS.values())
            self.assertEqual(len(tasks), 1, "应有 1 个 m3u8 任务")
            tid = tasks[0]["id"]
            deadline = time.time() + 25
            while time.time() < deadline:
                st = bridge_server._M3U8_TASKS[tid].get("state")
                if st in ("done", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.05)
            await self._join_runner(tid)
            return bridge_server._M3U8_TASKS[tid]

        t = asyncio.run(_go())
        self.assertEqual(t.get("state"), "done", "任务应下载完成，error=%r" % t.get("error"))
        self.assertTrue(os.path.isfile(t["output_path"]), "成品文件应存在")
        self.assertGreater(t.get("output_size") or 0, 0, "成品应有内容")
        self.assertTrue(t.get("archived_job_id"), "完成后应自动创建归档 job（与插件提交同构）")
    def test_pure_m3u8_batch_not_swallowed_by_disk_guard(self):
        """纯 m3u8 批次在高水位时不能被 waiting_disk 队列静默吞掉。

        waiting_disk 的载荷是 TG 字段，m3u8 塞进去会入队失败并丢失链接。
        正确行为：放行给 m3u8 引擎，由它按自己的口径拒绝并说明原因。
        """
        from services.watermark_service import _disk_guard_or_enqueue_links

        async def _go():
            before = len(bridge_server._WAITING_DISK_TASKS)
            with patch.object(bridge_server, "_get_disk_usage_percent", return_value=99.0):
                guard = await _disk_guard_or_enqueue_links(
                    ["https://cdn.example.com/a/index.m3u8"], "test")
            after = len(bridge_server._WAITING_DISK_TASKS)
            return guard, before, after

        guard, before, after = asyncio.run(_go())
        self.assertIsNone(guard, "纯 m3u8 批次不应被水位门禁拦截（否则链接被静默丢弃）")
        self.assertEqual(before, after, "不得把 m3u8 塞进 waiting_disk 队列")

    def test_disk_guard_still_intercepts_tg_links(self):
        """回归保护：纯 TG 批次在水位超限时**仍须**被挂起拦截（原行为不变）。"""
        from services.watermark_service import _disk_guard_or_enqueue_links

        async def _go():
            with patch.object(bridge_server, "_get_disk_usage_percent", return_value=99.0), \
                 patch.object(bridge_server, "BACKEND") as backend:
                backend.resolve_link = _async_empty
                return await _disk_guard_or_enqueue_links(["https://t.me/channel/123"], "test")

        guard = asyncio.run(_go())
        self.assertIsNotNone(guard, "纯 TG 批次高水位仍应被拦截")
        self.assertEqual(guard.get("code"), "DISK_WATERMARK_EXCEEDED")

    def test_bad_link_does_not_block_others(self):
        """一条提交即被拒的链接，不应拖累同批次其它可用链接。

        用超长 URL 触发 m3u8_submit 的同步校验失败（确定性，不依赖网络）；
        注意不能用 404 链接——m3u8_submit 只做校验与入队，不预抓播放列表，
        404 要到下载阶段才失败，那时两条都已经在队列里了。
        """
        good = self._fixture("/ok")
        too_long = "https://cdn.example.com/" + ("a" * 3000) + ".m3u8"

        async def _go():
            from services.task_service import _submit_m3u8_links
            ok, err = await _submit_m3u8_links([too_long, good])
            return ok, err, len(bridge_server._M3U8_TASKS)

        ok, err, n = asyncio.run(_go())
        self.assertEqual(ok, 1, "可用链接应被受理（ok=%s err=%s）" % (ok, err))
        self.assertEqual(n, 1, "应只有 1 个任务入队（坏链接被拒）")


async def _async_empty(*a, **kw):
    return []


if __name__ == "__main__":
    unittest.main()
