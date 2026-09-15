# -*- coding: utf-8 -*-
"""回归测试：取回断点续传的核心语义。

用本地 HTTP 服务复刻三种服务端行为，断言续传逻辑正确：
  A) 支持 Range（206）-> 从断点续传，不重下已有字节，最终内容与源一致
  B) **忽略 Range**（200 + 全量）-> 必须重写而不是追加，否则拼出损坏文件
  C) 传输截断 -> 必须判为不完整，不能把半截文件当成功

B 是最容易写错的一条：若只看 resume_from 就决定 "ab" 追加，
服务端一旦忽略 Range，新数据会接在旧残片后面，得到前段旧、后段新的坏文件。
"""
import os
import sys
import threading
import hashlib
import asyncio
import tempfile
import http.server
import socketserver
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PAYLOAD = bytes(range(256)) * 4096          # 1MB 确定性内容
TOTAL = len(PAYLOAD)
FULL_MD5 = hashlib.md5(PAYLOAD).hexdigest()

_MODE = {"v": "range"}


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        mode = _MODE["v"]
        rng = self.headers.get("Range")
        if mode == "range" and rng and rng.startswith("bytes="):
            start = int(rng.split("=")[1].split("-")[0])
            body = PAYLOAD[start:]
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, TOTAL - 1, TOTAL))
            self.send_header("Content-Length", str(len(body)))
        elif mode == "truncate":
            body = PAYLOAD[: TOTAL // 2]
            self.send_response(200)
            self.send_header("Content-Length", str(TOTAL))   # 声明全量，只发一半
        else:
            body = PAYLOAD
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:  # noqa: BLE001
            pass


class TestRetrieveResume(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    async def _download(self, target, resume_from, mode):
        """复刻 retrieve_service 的续传核心逻辑。"""
        import httpx
        _MODE["v"] = mode
        hdrs = {}
        if resume_from > 0:
            hdrs["Range"] = "bytes=%d-" % resume_from
        known = TOTAL
        # trust_env=False：本用例打的是 127.0.0.1 上的本地测试服务器（回环），
        # 不能被环境变量代理劫持。开发机 .bashrc 会导出 socks5h:// 代理，裸
        # httpx.AsyncClient() 会因「Unknown scheme for proxy URL」直接抛错。
        # 生产侧对应代码用的是同样 trust_env=False 的 _openlist_upload_client。
        async with httpx.AsyncClient(trust_env=False) as cli:
            async with cli.stream("GET", "http://127.0.0.1:%d/f" % self.port,
                                  headers=hdrs) as r:
                partial = (r.status_code == 206)
                # 服务端忽略 Range -> 必须从头重写，否则会拼出损坏文件
                if resume_from > 0 and not partial:
                    resume_from = 0
                cl = int(r.headers.get("content-length") or 0)
                total = (resume_from + cl) if (partial and cl) else (cl or known or 0)
                fh = open(target, "ab" if resume_from else "wb")
                try:
                    async for chunk in r.aiter_bytes(chunk_size=65536):
                        fh.write(chunk)
                finally:
                    fh.close()
        final = os.path.getsize(target)
        if total > 0 and final < total:
            raise RuntimeError("下载不完整：期望 %d 实际 %d" % (total, final))
        return final

    def test_resume_with_range_server(self):
        """A) 支持 Range：续传后内容正确。"""
        p = tempfile.mktemp(suffix=".part")
        try:
            half = TOTAL // 2
            with open(p, "wb") as f:
                f.write(PAYLOAD[:half])
            n = asyncio.run(self._download(p, half, "range"))
            self.assertEqual(n, TOTAL)
            with open(p, "rb") as f:
                self.assertEqual(hashlib.md5(f.read()).hexdigest(), FULL_MD5)
        finally:
            if os.path.exists(p):
                os.remove(p)

    def test_range_ignored_must_rewrite(self):
        """B) 服务端忽略 Range：必须重写，不得追加拼接。"""
        p = tempfile.mktemp(suffix=".part")
        try:
            half = TOTAL // 2
            with open(p, "wb") as f:
                f.write(PAYLOAD[:half])
            n = asyncio.run(self._download(p, half, "norange"))
            self.assertEqual(n, TOTAL)
            with open(p, "rb") as f:
                self.assertEqual(
                    hashlib.md5(f.read()).hexdigest(), FULL_MD5,
                    "追加了全量数据 -> 文件损坏（前段旧+后段新）")
        finally:
            if os.path.exists(p):
                os.remove(p)

    def test_truncated_detected(self):
        """C) 截断必须被检出（协议层或大小校验），不得当成功。"""
        p = tempfile.mktemp(suffix=".part")
        try:
            with self.assertRaises(Exception):
                asyncio.run(self._download(p, 0, "truncate"))
        finally:
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    unittest.main()
