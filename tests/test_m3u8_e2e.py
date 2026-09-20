# -*- coding: utf-8 -*-
"""
test_m3u8_e2e.py — M3U8 下载端到端测试（真实 HTTP 源站 + 归档入队）
==================================================================
使用一个进程内 aiohttp 之外的极简 HTTP 服务器（http.server，线程）提供
playlist / 分片 / 密钥，验证全链路：

  提交任务 → 解析 playlist → 取密钥 → 并发分片(含 AES-128 解密)
  → 合并成品 → 自动创建归档 job（复用 OpenList 上传管线）→ 回收暂存分片

覆盖：
1. 明文 TS 流端到端：产物字节 == 分片顺序拼接
2. AES-128 加密流端到端：产物为解密后明文
3. fMP4（EXT-X-MAP）端到端：init 段 + 分片
4. 断点续传：预先放置部分分片，重试只补缺失分片
5. 归档入队：unique_id = m3u8-<sha1>，filename 带扩展名，local_path 指向成品
6. 取消：任务转为 cancelled
7. 分片回收：成功任务自动清理 seg_*.bin（成品保留）；失败任务保留分片
8. 删除任务：终态任务删除后记录/分片目录/本地成品一并消失
9. 归档目录优先级：任务 remote_dir > 设置页 defaultDir > /m3u8
10. remote_dir 校验：非法值（非 / 开头 / 根 / 含 ..）提交即失败
"""
import os
import io
import sys
import time
import shutil
import asyncio
import hashlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bridge_server
import services.m3u8_service as m3u8
from services.m3u8_service import M3u8Error, _parse_m3u8


# ---------------------------------------------------------------------
# 假源站：按路径提供 playlist / 分片 / 密钥
# ---------------------------------------------------------------------

class _FakeHlsHandler(BaseHTTPRequestHandler):
    """极简 HLS 源站：数据在 server.fixtures 里由用例填充。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音
        pass

    def do_GET(self):  # noqa: N802
        fixtures = self.server.fixtures  # type: ignore[attr-defined]
        # 记录请求路径（续传测试据此断言「已下载分片未被重新请求」）
        log = self.server.request_log  # type: ignore[attr-defined]
        log.append(self.path)
        if self.path in self.server.fail_paths:  # type: ignore[attr-defined]
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = fixtures.get(self.path)
        delay = getattr(self.server, "delay_secs", 0.0)
        if delay:
            time.sleep(delay)  # 让用例能把取消/超时打在传输进行中
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


class _FakeHlsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _FakeHlsHandler)
        self.fixtures = {}
        self.fixtures = {}
        self.request_log: list = []   # 收到的请求路径（续传断言用）
        self.fail_paths: set = set()  # 命中即返回 500（制造失败态用）
        self._thread = None

    @property
    def base(self) -> str:
        return "http://127.0.0.1:%d" % self.server_address[1]

    def start(self):
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self.shutdown()
        self.server_close()


def _aes_encrypt(plain: bytes, key: bytes, iv: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padded) + enc.finalize()


def _asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


class TestM3u8EndToEnd(unittest.TestCase):
    """端到端：真实 HTTP + 真实分片拼接 + 归档入队。"""

    def setUp(self):
        self.server = _FakeHlsServer()
        self.server.start()
        self.tmp = tempfile.mkdtemp(prefix="m3u8_e2e_")

        # 隔离：下载目录 / 任务表 / Token / 归档
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
        # 回环假源站需要放行私网校验
        m3u8._ALLOW_PRIVATE_TARGETS = True
        # 归档到本地临时目录（不触发真实 OpenList 上传；仅在 _register_archive_job 层验证）
        bridge_server._ARCHIVE_CONFIG["defaultDir"] = "/m3u8"

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

    def _run_task_sync(self, url, title="", headers=None, timeout=30,
                       remote_dir=""):
        """提交任务并等待其进入终态（同步拉取）。

        关键：状态变终态后 runner 协程仍要跑「归档入队 + 分片回收」，必须再等
        它真正收尾。否则 `asyncio.run` 退出时会 cancel 掉这个尚未结束的 runner，
        其 CancelledError 分支会把刚写好的 done 翻成 cancelled —— 表现为随机
        flaky（约 1/40）。
        """
        async def _go():
            res = await m3u8.m3u8_submit(url, title, headers, remote_dir)
            tid = res["task"]["id"]
            deadline = time.time() + timeout
            while time.time() < deadline:
                t = bridge_server._M3U8_TASKS[tid]
                if t.get("state") in ("done", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.05)
            await self._join_runner(tid, timeout)
            return bridge_server._M3U8_TASKS[tid]
        return _asyncio_run(_go())

    @staticmethod
    async def _join_runner(tid, timeout=20):
        """等 runner 协程真正结束（归档入队与分片回收都在其后半段）。"""
        entry = bridge_server._M3U8_RUNNERS.get(tid)
        if entry is None or entry[1] is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(entry[1]), timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass

    def _seg_leftovers(self, tid):
        """任务目录下残留的暂存分片（成功任务应被回收干净）。"""
        d = os.path.join(m3u8.M3U8_DOWNLOAD_DIR, str(tid))
        if not os.path.isdir(d):
            return []
        return sorted(f for f in os.listdir(d)
                      if f == "init.bin" or (f.startswith("seg_") and f.endswith(".bin")))

    def _wait_leftovers_gone(self, tid, timeout=10.0):
        """分片回收发生在 done 之后（同一协程内），需要等一下再断言。"""
        deadline = time.time() + timeout
        left = self._seg_leftovers(tid)
        while left and time.time() < deadline:
            time.sleep(0.05)
            left = self._seg_leftovers(tid)
        return left

    def test_plain_ts_end_to_end(self):
        """明文 TS 流：产物应为分片顺序拼接。"""
        segs = [b"SEGMENT-ONE-" * 40, b"SEGMENT-TWO-" * 40, b"SEGMENT-THREE-" * 40]
        self.server.fixtures = {
            "/v/index.m3u8": (
                "#EXTM3U\n#EXT-X-VERSION:3\n"
                + "".join("#EXTINF:6.0,\nseg%d.ts\n" % i for i in range(3))
                + "#EXT-X-ENDLIST\n"
            ).encode(),
        }
        for i, s in enumerate(segs):
            self.server.fixtures["/v/seg%d.ts" % i] = s

        t = self._run_task_sync(self.server.base + "/v/index.m3u8", "试验影片")
        self.assertEqual(t["state"], "done", t.get("error"))
        self.assertEqual(t["total_segments"], 3)
        self.assertEqual(t["done_segments"], 3)
        out = t["output_path"]
        self.assertTrue(os.path.exists(out))
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"".join(segs))
        # 归档 job 已创建，且 unique_id / 文件名 / local_path 正确
        uid = "m3u8-" + hashlib.sha1(t["url"].encode()).hexdigest()[:16]
        self.assertTrue(t["archived_job_id"])
        job = bridge_server._ARCHIVE_JOBS[t["archived_job_id"]]
        self.assertEqual(job["unique_id"], uid)
        self.assertEqual(job["local_path"], out)
        self.assertTrue(job["filename"].endswith((".ts", ".mp4")))
        self.assertEqual(job["remote_dir"], "/m3u8")

    def test_aes128_end_to_end(self):
        """AES-128 加密流：产物应为解密后的明文。"""
        key = bytes(range(16))
        iv = bytes(16)  # _media_playlist 里用 0x0..01；此处直接显式 IV
        plains = [b"plain-one-" * 30, b"plain-two-" * 30]
        self.server.fixtures = {
            "/e/key.bin": key,
            "/e/index.m3u8": (
                "#EXTM3U\n#EXT-X-VERSION:3\n"
                '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x' + "00" * 15 + "00\n"
                + "".join("#EXTINF:6.0,\ns%d.ts\n" % i for i in range(2))
                + "#EXT-X-ENDLIST\n"
            ).encode(),
        }
        for i, p in enumerate(plains):
            self.server.fixtures["/e/s%d.ts" % i] = _aes_encrypt(p, key, iv)

        t = self._run_task_sync(self.server.base + "/e/index.m3u8", "加密影片")
        self.assertEqual(t["state"], "done", t.get("error"))
        with open(t["output_path"], "rb") as f:
            self.assertEqual(f.read(), b"".join(plains))
        self.assertTrue(t["encrypted"])

    def test_fmp4_map_end_to_end(self):
        """fMP4（EXT-X-MAP）：init 段前置于分片，产物扩展名 .mp4。"""
        init = b"INIT-SEGMENT-DATA"
        segs = [b"fmp4-seg-a" * 20, b"fmp4-seg-b" * 20]
        self.server.fixtures = {
            "/f/index.m3u8": (
                "#EXTM3U\n#EXT-X-VERSION:7\n"
                '#EXT-X-MAP:URI="init.mp4"\n'
                + "".join("#EXTINF:6.0,\ns%d.m4s\n" % i for i in range(2))
                + "#EXT-X-ENDLIST\n"
            ).encode(),
            "/f/init.mp4": init,
        }
        for i, s in enumerate(segs):
            self.server.fixtures["/f/s%d.m4s" % i] = s

        t = self._run_task_sync(self.server.base + "/f/index.m3u8", "fMP4 影片")
        self.assertEqual(t["state"], "done", t.get("error"))
        with open(t["output_path"], "rb") as f:
            self.assertEqual(f.read(), init + b"".join(segs))
        self.assertTrue(t["filename"].endswith(".mp4"))

    def test_resume_skips_downloaded_segments(self):
        """断点续传：源站分片消失后，重试仍能完成（证明复用了磁盘分片）。

        注意：这里不能拿 done 态直接调 retry（retry 只接受 failed/cancelled），
        必须真正制造一个失败态：先让源站在第二个分片处返回 500 使任务失败，
        但第一个分片已成功落盘；随后撤掉分片（404），再重试 —— 若实现真的
        跳过已下载分片，则只需补齐缺失的那个，任务应能成功。
        """
        seg_a = b"AA" * 100
        seg_b = b"BB" * 100
        self.server.fixtures = {
            "/r/index.m3u8": (
                "#EXTM3U\n#EXTINF:6.0,\na.ts\n#EXTINF:6.0,\nb.ts\n#EXT-X-ENDLIST\n"
            ).encode(),
            "/r/a.ts": seg_a,
            "/r/b.ts": seg_b,
        }
        # 直接驱动一次「只下载第一个分片」的失败流程：让 b.ts 暂时不可用
        self.server.request_log.clear()
        self.server.fail_paths = {"/r/b.ts"}
        t1 = self._run_task_sync(self.server.base + "/r/index.m3u8", "续传影片")
        self.assertEqual(t1["state"], "failed", "b.ts 不可用时应失败")
        seg_dir = os.path.join(m3u8.M3U8_DOWNLOAD_DIR, t1["id"])
        # 第一个分片必须已落盘（并发下不保证，故显式放置以稳定验证续传语义）
        os.makedirs(seg_dir, exist_ok=True)
        with open(os.path.join(seg_dir, "seg_00000.bin"), "wb") as f:
            f.write(seg_a)

        # 恢复 b.ts，并让 a.ts 永久 404：续传若生效就不该再请求 a.ts
        self.server.fail_paths = set()
        del self.server.fixtures["/r/a.ts"]
        self.server.request_log.clear()

        # 重试必须与等待处在同一个事件循环里：m3u8_retry 只是 create_task，
        # 若用 asyncio.run 单独跑它，循环一退出协程就被取消（任务变 cancelled）。
        import asyncio

        async def _retry_and_wait():
            await m3u8.m3u8_retry(t1["id"])
            deadline = time.time() + 20
            while time.time() < deadline:
                cur = bridge_server._M3U8_TASKS[t1["id"]]
                if cur.get("state") in ("done", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.05)
            # 终态后 runner 还要跑归档入队与分片回收，等它收尾再退出事件循环
            # （否则 asyncio.run 会 cancel 掉它，把 done 翻成 cancelled）
            await self._join_runner(t1["id"], 20)
            return bridge_server._M3U8_TASKS[t1["id"]]

        t = _asyncio_run(_retry_and_wait())
        self.assertEqual(t["state"], "done", t.get("error"))
        # 关键断言：续传生效 → 全程没有再请求已落盘的 a.ts
        self.assertNotIn("/r/a.ts", self.server.request_log,
                         "已下载分片不应被重新请求")
        self.assertIn("/r/b.ts", self.server.request_log)
        with open(t["output_path"], "rb") as f:
            self.assertEqual(f.read(), seg_a + seg_b)

    def test_submit_rejected_on_disk_high_watermark(self):
        """磁盘高水位时提交应被拒绝（与 TG 下载同一口径）。"""
        # 注意：阈值 0.0 会被服务内 `x or 85.0` 的真值兜底吞掉，故用极小正值
        bridge_server._ARCHIVE_CONFIG["diskHighWatermarkPercent"] = 0.01
        self.server.fixtures = {
            "/w/index.m3u8": ("#EXTM3U\n#EXTINF:6.0,\na.ts\n#EXT-X-ENDLIST\n").encode(),
            "/w/a.ts": b"W" * 50,
        }
        try:
            with self.assertRaises(M3u8Error) as ctx:
                _asyncio_run(m3u8.m3u8_submit(self.server.base + "/w/index.m3u8", "水位"))
            self.assertIn("高水位", str(ctx.exception))
        finally:
            bridge_server._ARCHIVE_CONFIG["diskHighWatermarkPercent"] = 85.0

    def test_flow_control_when_max_active_reached(self):
        """活动任务打满时提交应被拒绝，避免无限堆积。"""
        saved = m3u8.M3U8_MAX_ACTIVE
        m3u8.M3U8_MAX_ACTIVE = 1
        try:
            self.server.fixtures = {
                "/n/a.m3u8": ("#EXTM3U\n#EXTINF:6.0,\na.ts\n#EXT-X-ENDLIST\n").encode(),
                "/n/b.m3u8": ("#EXTM3U\n#EXTINF:6.0,\nb.ts\n#EXT-X-ENDLIST\n").encode(),
                "/n/a.ts": b"A" * 50,
                "/n/b.ts": b"B" * 50,
            }
            self.server.delay_secs = 0.6

            async def _go():
                await m3u8.m3u8_submit(self.server.base + "/n/a.m3u8", "占位")
                # 第二个应因活动数上限被拒
                with self.assertRaises(M3u8Error) as c:
                    await m3u8.m3u8_submit(self.server.base + "/n/b.m3u8", "溢出")
                return str(c.exception)

            try:
                msg = _asyncio_run(_go())
            finally:
                self.server.delay_secs = 0.0
            self.assertIn("上限", msg)
        finally:
            m3u8.M3U8_MAX_ACTIVE = saved

    def test_cancel_running_task(self):
        """取消：下载中的任务应转为 cancelled，且不留归档任务。

        用小分片 + 立即取消容易撞上「已经完成」的竞态，测不出取消语义；
        这里让分片响应阻塞（源站 sleep），确保取消发生在下载进行中。
        """
        segs = [b"X" * 300] * 3
        self.server.fixtures = {
            "/c/index.m3u8": (
                "#EXTM3U\n" + "".join("#EXTINF:6.0,\ns%d.ts\n" % i for i in range(3))
                + "#EXT-X-ENDLIST\n"
            ).encode(),
        }
        for i, s in enumerate(segs):
            self.server.fixtures["/c/s%d.ts" % i] = s
        # 每个分片延迟 1s 返回：给取消留出确定的时间窗
        self.server.delay_secs = 1.0
        self._orig_delay = 0.0

        async def _go():
            import asyncio
            res = await m3u8.m3u8_submit(self.server.base + "/c/index.m3u8", "取消影片")
            tid = res["task"]["id"]
            # 等它真正进入 running（拿到 playlist 并开始拉分片）
            for _ in range(100):
                if bridge_server._M3U8_TASKS[tid].get("state") == "running":
                    break
                await asyncio.sleep(0.02)
            await m3u8.m3u8_cancel(tid)
            deadline = time.time() + 10
            while time.time() < deadline:
                t = bridge_server._M3U8_TASKS[tid]
                if t.get("state") in ("done", "failed", "cancelled"):
                    return t
                await asyncio.sleep(0.05)
            return bridge_server._M3U8_TASKS[tid]

        try:
            t = _asyncio_run(_go())
        finally:
            self.server.delay_secs = 0.0
        self.assertEqual(t["state"], "cancelled", t.get("error"))
        # 取消的任务不应产生归档任务
        self.assertFalse(t.get("archived_job_id"))
        uid = "m3u8-" + hashlib.sha1(t["url"].encode()).hexdigest()[:16]
        self.assertFalse(any(j.get("unique_id") == uid
                             for j in bridge_server._ARCHIVE_JOBS.values()))
        # 取消可能发生在完成前（cancelled）或竞态下已完成（done）；两者都不是 failed
        self.assertIn(t["state"], ("cancelled", "done"), t.get("error"))

    def test_submit_rejects_master_playlist(self):
        """提交 master playlist 应报错并给出可选清晰度提示。"""
        self.server.fixtures = {
            "/m/master.m3u8": (
                "#EXTM3U\n"
                '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n360/index.m3u8\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720\n720/index.m3u8\n'
            ).encode(),
        }
        t = self._run_task_sync(self.server.base + "/m/master.m3u8", "主列表")
        self.assertEqual(t["state"], "failed")
        self.assertIn("主播放列表", t["error"])

    def test_resolve_reports_media_info(self):
        """resolve 应返回分片数/时长/是否加密。"""
        self.server.fixtures = {
            "/i/index.m3u8": (
                "#EXTM3U\n#EXTINF:4.0,\na.ts\n#EXTINF:4.0,\nb.ts\n#EXT-X-ENDLIST\n"
            ).encode(),
        }
        info = _asyncio_run(m3u8.m3u8_resolve(self.server.base + "/i/index.m3u8"))
        self.assertEqual(info["kind"], "media")
        self.assertEqual(info["segments"], 2)
        self.assertEqual(info["duration_secs"], 8.0)
        self.assertFalse(info["encrypted"])

    def test_title_traversal_is_sanitized(self):
        """title 含路径穿越/盘符时，成品必须仍落在任务目录内。"""
        self.server.fixtures = {
            "/t/index.m3u8": ("#EXTM3U\n#EXTINF:6.0,\na.ts\n#EXT-X-ENDLIST\n").encode(),
            "/t/a.ts": b"T" * 120,
        }
        for evil in ("../../../evil", r"..\..\evil", "C:\\Windows\\Temp\\evil", "/tmp/evil"):
            with self.subTest(title=evil):
                t = self._run_task_sync(
                    self.server.base + "/t/index.m3u8", evil)
                # 同名 URL 会被提交去重，故直接改标题后重跑不可行；
                # 这里用不同标题失败即说明净化未生效
                self.assertIn(t["state"], ("done", "failed", "queued"))
                if t["state"] == "done":
                    seg_dir = os.path.abspath(
                        os.path.join(m3u8.M3U8_DOWNLOAD_DIR, t["id"]))
                    out = os.path.abspath(t["output_path"])
                    self.assertEqual(os.path.dirname(out), seg_dir,
                                     "成品必须落在本任务目录内")
                # 任务表里的 title 也不应残留目录分量
                self.assertNotIn("/", str(t.get("title") or ""))
                self.assertNotIn("\\", str(t.get("title") or ""))
                break  # 同 URL 去重：只需验证一次真实落盘路径

    def test_safe_title_unit(self):
        """_safe_title 单元：路径分量与 .. 必须被剥离。"""
        from services.m3u8_service import _safe_title
        self.assertEqual(_safe_title(r"..\..\evil"), "evil")
        self.assertEqual(_safe_title(r"C:\Windows\Temp\x"), "x")
        self.assertEqual(_safe_title("/tmp/evil"), "evil")
        self.assertEqual(_safe_title("正常片名 1080p"), "正常片名 1080p")
        self.assertEqual(_safe_title(""), "")

    def test_iv_invalid_is_rejected(self):
        """非法 IV 必须报错，绝不静默回落到 media sequence（会造成静默损坏）。"""
        from services.m3u8_service import _iv_bytes, M3u8Error as E
        with self.assertRaises(E):
            _iv_bytes({"iv": "0xabc"}, 3)          # 奇数长度
        with self.assertRaises(E):
            _iv_bytes({"iv": "0x" + "aa" * 8}, 3)  # 8 字节而非 16
        with self.assertRaises(E):
            _iv_bytes({"iv": "zzzz"}, 3)           # 非十六进制
        # 未提供 IV 时才回落到 seq
        self.assertEqual(_iv_bytes({"iv": ""}, 3), (3).to_bytes(16, "big"))

    def test_resolve_returns_variants_for_master(self):
        """resolve 对 master 应返回清晰度列表。"""
        self.server.fixtures = {
            "/mm/master.m3u8": (
                "#EXTM3U\n"
                '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n360.m3u8\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720\n720.m3u8\n'
            ).encode(),
        }
        info = _asyncio_run(m3u8.m3u8_resolve(self.server.base + "/mm/master.m3u8"))
        self.assertEqual(info["kind"], "master")
        self.assertEqual(len(info["variants"]), 2)
        self.assertEqual(info["variants"][1]["resolution"], "1280x720")

    def test_submit_is_idempotent_for_same_url(self):
        """同一 URL 重复提交应返回 duplicate=True，不新建任务。"""
        self.server.fixtures = {
            "/d/index.m3u8": ("#EXTM3U\n#EXTINF:6.0,\na.ts\n#EXT-X-ENDLIST\n").encode(),
            "/d/a.ts": b"Z" * 100,
        }

        async def _go():
            r1 = await m3u8.m3u8_submit(self.server.base + "/d/index.m3u8", "去重")
            r2 = await m3u8.m3u8_submit(self.server.base + "/d/index.m3u8", "去重")
            return r1, r2

        r1, r2 = _asyncio_run(_go())
        self.assertTrue(r2["duplicate"])
        self.assertEqual(r1["task"]["id"], r2["task"]["id"])


    # ------------------------------------------------------------------
    # 能力 1：分片回收 / 删除任务
    # ------------------------------------------------------------------
    def test_success_reclaims_segments_but_keeps_output(self):
        """成功下载后 seg_*.bin 自动回收，成品仍存在（deleteLocal 未生效时）。"""
        segs = [b"S" * 200] * 3
        self.server.fixtures = {
            "/rc/index.m3u8": (
                "#EXTM3U\n" + "".join("#EXTINF:6.0,\ns%d.ts\n" % i for i in range(3))
                + "#EXT-X-ENDLIST\n").encode(),
        }
        for i, s in enumerate(segs):
            self.server.fixtures["/rc/s%d.ts" % i] = s
        saved_dl = bridge_server._ARCHIVE_CONFIG.get("deleteLocal")
        bridge_server._ARCHIVE_CONFIG["deleteLocal"] = False
        try:
            t = self._run_task_sync(self.server.base + "/rc/index.m3u8", "回收影片")
            self.assertEqual(t["state"], "done", t.get("error"))
            left = self._wait_leftovers_gone(t["id"])
            self.assertEqual(left, [], "成功任务的分片应被自动回收")
            self.assertTrue(os.path.exists(t["output_path"]),
                            "成品不得被分片回收带走")
            with open(t["output_path"], "rb") as f:
                self.assertEqual(f.read(), b"".join(segs))
        finally:
            bridge_server._ARCHIVE_CONFIG["deleteLocal"] = saved_dl

    def test_failed_task_keeps_segments_for_resume(self):
        """失败任务不回收已下载分片（断点续传依据）。"""
        self.server.fixtures = {
            "/rk/index.m3u8": (
                "#EXTM3U\n#EXTINF:6.0,\na.ts\n#EXTINF:6.0,\nb.ts\n#EXT-X-ENDLIST\n").encode(),
            "/rk/a.ts": b"A" * 100,
            "/rk/b.ts": b"B" * 100,
        }
        self.server.fail_paths = {"/rk/b.ts"}
        saved_conc = m3u8.M3U8_CONCURRENCY
        m3u8.M3U8_CONCURRENCY = 1     # 串行：a.ts 先落盘，b.ts 失败即中止
        try:
            t = self._run_task_sync(self.server.base + "/rk/index.m3u8", "失败影片")
        finally:
            m3u8.M3U8_CONCURRENCY = saved_conc
            self.server.fail_paths = set()
        self.assertEqual(t["state"], "failed", t.get("error"))
        d = os.path.join(m3u8.M3U8_DOWNLOAD_DIR, t["id"])
        self.assertTrue(os.path.exists(os.path.join(d, "seg_00000.bin")),
                        "失败任务必须保留分片以便 retry 续传")

    def test_delete_task_end_to_end_removes_local_files(self):
        """终态任务 delete：任务记录、分片目录与本地成品一并消失。"""
        self.server.fixtures = {
            "/dl/index.m3u8": (
                "#EXTM3U\n#EXTINF:6.0,\na.ts\n#EXT-X-ENDLIST\n").encode(),
            "/dl/a.ts": b"D" * 120,
        }
        # 关掉自动归档：避免归档 worker 与删除动作争同一份成品
        saved_auto = bridge_server._ARCHIVE_CONFIG.get("autoArchive")
        bridge_server._ARCHIVE_CONFIG["autoArchive"] = False
        try:
            t = self._run_task_sync(self.server.base + "/dl/index.m3u8", "删除影片")
            self.assertEqual(t["state"], "done", t.get("error"))
            out = t["output_path"]
            self.assertTrue(os.path.exists(out))
            self._wait_leftovers_gone(t["id"])

            res = _asyncio_run(m3u8.m3u8_delete(t["id"]))
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["freed"], 120, "分片已回收，只剩成品可释放")
            self.assertNotIn(t["id"], bridge_server._M3U8_TASKS)
            self.assertFalse(os.path.exists(out), "本地成品应被删除")
            self.assertFalse(
                os.path.isdir(os.path.join(m3u8.M3U8_DOWNLOAD_DIR, t["id"])),
                "空任务目录应被删除")
        finally:
            bridge_server._ARCHIVE_CONFIG["autoArchive"] = saved_auto

    # ------------------------------------------------------------------
    # 能力 2：插件自定义归档目录
    # ------------------------------------------------------------------
    def test_submit_rejects_invalid_remote_dir(self):
        """非法 remote_dir（非 / 开头 / 根目录 / 含 .. / 编码穿透）必须拒绝。"""
        url = self.server.base + "/bad/index.m3u8"
        self.server.fixtures = {
            "/bad/index.m3u8": (
                "#EXTM3U\n#EXTINF:6.0,\na.ts\n#EXT-X-ENDLIST\n").encode(),
            "/bad/a.ts": b"x" * 10,
        }
        for bad in ("onedrive", "/", "/a/../b", "https://x/y", "/a/%2e%2e/b"):
            with self.subTest(remote_dir=bad):
                with self.assertRaises(M3u8Error) as ctx:
                    _asyncio_run(m3u8.m3u8_submit(url, "非法归档", None, bad))
                self.assertIn("归档目录", str(ctx.exception))
        # 校验失败绝不能留下任务记录
        self.assertEqual(bridge_server._M3U8_TASKS, {})

    def test_remote_dir_normalized_and_saved_on_task(self):
        """合法 remote_dir 归一化后存进任务记录、公开视图与归档 job。"""
        self.server.fixtures = {
            "/rd/index.m3u8": (
                "#EXTM3U\n#EXTINF:6.0,\na.ts\n#EXT-X-ENDLIST\n").encode(),
            "/rd/a.ts": b"R" * 60,
        }
        saved_default = bridge_server._ARCHIVE_CONFIG.get("defaultDir")
        bridge_server._ARCHIVE_CONFIG["defaultDir"] = "/设置页默认"
        try:
            t = self._run_task_sync(self.server.base + "/rd/index.m3u8", "归档影片",
                                    remote_dir="//onedrive//剧集/")
            self.assertEqual(t["state"], "done", t.get("error"))
            self.assertEqual(t["remote_dir"], "/onedrive/剧集")
            self.assertEqual(m3u8._m3u8_public(t)["remote_dir"], "/onedrive/剧集")
            job = bridge_server._ARCHIVE_JOBS[t["archived_job_id"]]
            self.assertEqual(job["remote_dir"], "/onedrive/剧集")
            self.assertTrue(job["remote_path"].startswith("/onedrive/剧集/"))
        finally:
            bridge_server._ARCHIVE_CONFIG["defaultDir"] = saved_default

    def test_archive_dir_priority_task_over_default_over_fallback(self):
        """归档远端目录优先级：任务 remote_dir > 设置页 defaultDir > /m3u8。"""
        for i in range(3):
            self.server.fixtures["/pri%d/index.m3u8" % i] = (
                "#EXTM3U\n#EXTINF:6.0,\na.ts\n#EXT-X-ENDLIST\n").encode()
            self.server.fixtures["/pri%d/a.ts" % i] = b"P" * 40

        saved_default = bridge_server._ARCHIVE_CONFIG.get("defaultDir")
        try:
            # 1) 两者都有 → 用任务自带的
            bridge_server._ARCHIVE_CONFIG["defaultDir"] = "/设置页默认"
            t1 = self._run_task_sync(self.server.base + "/pri0/index.m3u8", "优先1",
                                     remote_dir="/插件指定")
            self.assertEqual(t1["remote_dir"], "/插件指定")
            self.assertEqual(
                bridge_server._ARCHIVE_JOBS[t1["archived_job_id"]]["remote_dir"],
                "/插件指定")

            # 2) 任务未指定 → 用设置页默认目录
            t2 = self._run_task_sync(self.server.base + "/pri1/index.m3u8", "优先2")
            self.assertEqual(t2["remote_dir"], "")
            self.assertEqual(
                bridge_server._ARCHIVE_JOBS[t2["archived_job_id"]]["remote_dir"],
                "/设置页默认")

            # 3) 都没配置 → 硬编码 /m3u8
            bridge_server._ARCHIVE_CONFIG["defaultDir"] = ""
            t3 = self._run_task_sync(self.server.base + "/pri2/index.m3u8", "优先3")
            self.assertEqual(
                bridge_server._ARCHIVE_JOBS[t3["archived_job_id"]]["remote_dir"],
                "/m3u8")
        finally:
            bridge_server._ARCHIVE_CONFIG["defaultDir"] = saved_default


if __name__ == "__main__":
    unittest.main(verbosity=2)
