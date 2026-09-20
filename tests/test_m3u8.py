# -*- coding: utf-8 -*-
"""
test_m3u8.py — M3U8(浏览器插件) 下载功能测试套件
==================================================
覆盖矩阵：
1. 解析引擎 (_parse_m3u8 / _parse_attrs / _parse_byterange)
   - media playlist：分片数/时长/相对 URL 拼接/绝对 URL
   - master playlist：多清晰度解析
   - AES-128 加密：KEY URI 解析、IV 判定
   - fMP4：EXT-X-MAP init 段
   - BYTERANGE：显式 @offset 与续算
   - 拒绝：直播流（无 ENDLIST）、SAMPLE-AES、畸形 m3u8、空分片
2. AES-128 解密与 IV 推导
3. SSRF 防护（私网/环回/保留地址拒绝、非 http(s) 拒绝）
4. 插件 Token：生成/校验/重置/常量时间比较
5. API 契约：401 无 Token、401 错 Token、429 限流、400 参数校验、
   /api/ext-token 需登录门禁、resolve/submit 端到端（回环假源站）
6. 端到端下载：明文流 + AES-128 流 → 合并产物 → 校验内容
7. 任务状态机：提交/取消/重试/去重
8. 回归：新增模块不破坏既有导入（bridge_server 可导入）
9. 分片回收 _cleanup_segments：只删 seg_*.bin/init.bin 普通文件、返回释放字节数、
   成品与非匹配文件/软链接/目录不动、task id 越界（"../evil" 等）返回 0 且不删文件
10. 删除任务 m3u8_delete：进行中拒绝；终态删除后记录/分片/成品/空目录一并回收并持久化；
    成品越界时只回收分片
11. 新增端点：/api/ext/m3u8/delete（401/400/404/200）、/api/m3u8/delete（CSRF）、
    /api/ext/m3u8/dirs（复用 openlist_dirs）、submit 的 remote_dir（别名 archive_dir）
"""
import os
import sys
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock

import core.state as state

import bridge_server
from fastapi.testclient import TestClient

import services.m3u8_service as m3u8
from services.m3u8_service import (
    M3u8Error, _parse_m3u8, _parse_attrs, _parse_byterange,
    _assert_public_http_url, _iv_bytes, _aes_decrypt, _client_headers,
    _cleanup_segments, m3u8_delete,
)


def _media_playlist(segments, endlist=True, key=None, map_uri=None, sequence=0):
    """构造 media playlist 文本。"""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    if sequence:
        lines.append("#EXT-X-MEDIA-SEQUENCE:%d" % sequence)
    if map_uri:
        lines.append('#EXT-X-MAP:URI="%s"' % map_uri)
    if key:
        lines.append('#EXT-X-KEY:METHOD=AES-128,URI="%s"%s' % (
            key, (',IV=0x' + "0" * 31 + "1") if key else ""))
    for i, seg in enumerate(segments):
        lines.append("#EXTINF:6.0,")
        lines.append(seg)
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _asyncio_run(coro):
    """同步驱动一个协程（新用例用；端到端测试里有同名辅助）。"""
    import asyncio
    return asyncio.run(coro)


class TestM3u8Parser(unittest.TestCase):
    """1. 解析引擎单元测试。"""

    def test_media_playlist_basic(self):
        text = _media_playlist(["a.ts", "b.ts", "c.ts"])
        p = _parse_m3u8(text, "https://cdn.example.com/hls/index.m3u8")
        self.assertEqual(p["kind"], "media")
        self.assertEqual(len(p["segments"]), 3)
        # 相对 URL 必须拼接为绝对 URL
        self.assertEqual(p["segments"][0]["uri"], "https://cdn.example.com/hls/a.ts")
        self.assertEqual(p["duration"], 18.0)
        self.assertFalse(p["encrypted"])
        self.assertIsNone(p["init_url"])

    def test_media_playlist_absolute_and_relative_mixed(self):
        text = _media_playlist(["a.ts", "https://other.example.com/b.ts", "../c.ts"])
        p = _parse_m3u8(text, "https://cdn.example.com/hls/sub/index.m3u8")
        self.assertEqual(p["segments"][0]["uri"], "https://cdn.example.com/hls/sub/a.ts")
        self.assertEqual(p["segments"][1]["uri"], "https://other.example.com/b.ts")
        self.assertEqual(p["segments"][2]["uri"], "https://cdn.example.com/hls/c.ts")

    def test_master_playlist(self):
        text = (
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n'
            "360p/index.m3u8\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720,NAME="高清"\n'
            "720p/index.m3u8\n"
        )
        p = _parse_m3u8(text, "https://cdn.example.com/vod/master.m3u8")
        self.assertEqual(p["kind"], "master")
        self.assertEqual(len(p["variants"]), 2)
        self.assertEqual(p["variants"][0]["bandwidth"], 800000)
        self.assertEqual(p["variants"][0]["resolution"], "640x360")
        self.assertEqual(p["variants"][1]["name"], "高清")
        self.assertEqual(p["variants"][1]["url"], "https://cdn.example.com/vod/720p/index.m3u8")

    def test_aes128_encryption_detected(self):
        text = _media_playlist(["a.ts", "b.ts"], key="https://cdn.example.com/key.bin")
        p = _parse_m3u8(text, "https://cdn.example.com/hls/index.m3u8")
        self.assertTrue(p["encrypted"])
        self.assertEqual(p["segments"][0]["enc"]["key_url"], "https://cdn.example.com/key.bin")
        self.assertEqual(p["segments"][0]["enc"]["method"], "AES-128")

    def test_key_uri_relative(self):
        text = _media_playlist(["a.ts"], key="keys/k1.bin")
        p = _parse_m3u8(text, "https://cdn.example.com/hls/index.m3u8")
        self.assertEqual(p["segments"][0]["enc"]["key_url"],
                         "https://cdn.example.com/hls/keys/k1.bin")

    def test_fmp4_map(self):
        text = _media_playlist(["s1.m4s", "s2.m4s"], map_uri="init.mp4")
        p = _parse_m3u8(text, "https://cdn.example.com/dash/index.m3u8")
        self.assertEqual(p["init_url"], "https://cdn.example.com/dash/init.mp4")

    def test_live_stream_rejected(self):
        text = _media_playlist(["a.ts", "b.ts"], endlist=False)
        with self.assertRaises(M3u8Error) as ctx:
            _parse_m3u8(text, "https://cdn.example.com/live.m3u8")
        self.assertIn("直播", str(ctx.exception))

    def test_sample_aes_rejected(self):
        text = ("#EXTM3U\n"
                '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="k.bin"\n'
                "#EXTINF:6.0,\na.ts\n#EXT-X-ENDLIST\n")
        with self.assertRaises(M3u8Error) as ctx:
            _parse_m3u8(text, "https://cdn.example.com/i.m3u8")
        self.assertIn("SAMPLE-AES", str(ctx.exception))

    def test_invalid_m3u8(self):
        with self.assertRaises(M3u8Error):
            _parse_m3u8("not a playlist", "https://cdn.example.com/i.m3u8")

    def test_empty_segments_rejected(self):
        with self.assertRaises(M3u8Error):
            _parse_m3u8("#EXTM3U\n#EXT-X-ENDLIST\n", "https://cdn.example.com/i.m3u8")

    def test_bom_tolerated(self):
        text = "\ufeff" + _media_playlist(["a.ts"])
        p = _parse_m3u8(text, "https://cdn.example.com/i.m3u8")
        self.assertEqual(len(p["segments"]), 1)

    def test_parse_attrs_quoted(self):
        attrs = _parse_attrs('BANDWIDTH=800000,RESOLUTION=640x360,NAME="高清 视频"')
        self.assertEqual(attrs["BANDWIDTH"], "800000")
        self.assertEqual(attrs["NAME"], "高清 视频")

    def test_byterange_explicit_and_implicit(self):
        offsets = {}
        self.assertEqual(_parse_byterange("1000@0", "seg.ts", offsets), (0, 1000))
        self.assertEqual(_parse_byterange("500", "seg.ts", offsets), (1000, 500))

    def test_byterange_full_playlist(self):
        text = ("#EXTM3U\n"
                "#EXT-X-BYTERANGE:1000@0\n#EXTINF:6.0,\nall.ts\n"
                "#EXT-X-BYTERANGE:500\n#EXTINF:6.0,\nall.ts\n"
                "#EXT-X-ENDLIST\n")
        p = _parse_m3u8(text, "https://cdn.example.com/i.m3u8")
        self.assertEqual(p["segments"][0]["range"], (0, 1000))
        self.assertEqual(p["segments"][1]["range"], (1000, 500))

    def test_media_sequence_recorded(self):
        text = _media_playlist(["a.ts", "b.ts"], sequence=100)
        p = _parse_m3u8(text, "https://cdn.example.com/i.m3u8")
        self.assertEqual(p["media_sequence"], 100)
        self.assertEqual(p["segments"][0]["seq"], 100)
        self.assertEqual(p["segments"][1]["seq"], 101)

    def test_unsupported_key_method_rejected(self):
        text = ("#EXTM3U\n"
                '#EXT-X-KEY:METHOD=FOO,URI="k.bin"\n'
                "#EXTINF:6.0,\na.ts\n#EXT-X-ENDLIST\n")
        with self.assertRaises(M3u8Error) as ctx:
            _parse_m3u8(text, "https://cdn.example.com/i.m3u8")
        self.assertIn("不支持的加密方式", str(ctx.exception))

    def test_map_byterange_rejected(self):
        text = ('#EXTM3U\n#EXT-X-MAP:URI="init.mp4",BYTERANGE="1000@0"\n'
                "#EXTINF:6.0,\na.m4s\n#EXT-X-ENDLIST\n")
        with self.assertRaises(M3u8Error) as ctx:
            _parse_m3u8(text, "https://cdn.example.com/i.m3u8")
        self.assertIn("BYTERANGE", str(ctx.exception))


class TestAesDecrypt(unittest.TestCase):
    """2. AES-128 解密与 IV 推导。"""

    def _encrypt(self, plain: bytes, key: bytes, iv: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plain) + padder.finalize()
        enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return enc.update(padded) + enc.finalize()

    def test_decrypt_roundtrip(self):
        key = bytes(range(16))
        iv = bytes(16)
        plain = b"hello m3u8 segment data"
        cipher = self._encrypt(plain, key, iv)
        self.assertEqual(_aes_decrypt(cipher, key, iv), plain)

    def test_iv_from_hex(self):
        enc = {"iv": "0x" + "ab" * 16}
        self.assertEqual(_iv_bytes(enc, 5), bytes.fromhex("ab" * 16))

    def test_iv_defaults_to_sequence(self):
        # 未给 IV 时按 media sequence number 大端
        self.assertEqual(_iv_bytes({"iv": ""}, 3), (3).to_bytes(16, "big"))

    def test_bad_key_length(self):
        with self.assertRaises(M3u8Error):
            _aes_decrypt(b"x" * 16, b"short", bytes(16))


class TestSsrfGuard(unittest.TestCase):
    """3. SSRF 防护。"""

    def test_private_ip_literal_rejected(self):
        for url in ("http://127.0.0.1/a.m3u8",
                    "http://192.168.1.10/a.m3u8",
                    "http://10.0.0.5/a.m3u8",
                    "http://169.254.169.254/latest/meta-data/",
                    "http://[::1]/a.m3u8"):
            with self.subTest(url=url):
                with self.assertRaises(M3u8Error):
                    _assert_public_http_url(url)

    def test_non_http_scheme_rejected(self):
        for url in ("file:///etc/passwd", "ftp://example.com/a.m3u8",
                    "gopher://example.com/a"):
            with self.subTest(url=url):
                with self.assertRaises(M3u8Error):
                    _assert_public_http_url(url)

    def test_public_ip_allowed(self):
        # 公网 IP 字面量直接放行（无需 DNS）
        _assert_public_http_url("http://93.184.216.34/video/index.m3u8")

    def test_resolution_failure_rejected(self):
        with self.assertRaises(M3u8Error):
            _assert_public_http_url("http://this-host-should-not-resolve.invalid/a.m3u8")

    def test_allow_private_hook_for_tests(self):
        try:
            m3u8._ALLOW_PRIVATE_TARGETS = True
            _assert_public_http_url("http://127.0.0.1:8000/a.m3u8")
        finally:
            m3u8._ALLOW_PRIVATE_TARGETS = False


class TestExtToken(unittest.TestCase):
    """4. 插件 Token 生命周期。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="m3u8_tok_")
        self._orig_file = m3u8._EXT_TOKEN_FILE
        self._orig_cache = m3u8._EXT_TOKEN_CACHE
        m3u8._EXT_TOKEN_FILE = os.path.join(self.tmp, ".ext_token")
        m3u8._EXT_TOKEN_CACHE = None

    def tearDown(self):
        m3u8._EXT_TOKEN_FILE = self._orig_file
        m3u8._EXT_TOKEN_CACHE = self._orig_cache
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_generate_and_persist(self):
        tok = m3u8.ext_token_get()
        self.assertTrue(tok and len(tok) >= 20)
        self.assertTrue(os.path.exists(m3u8._EXT_TOKEN_FILE))
        # 落盘权限应为 0600（POSIX）
        if os.name == "posix":
            self.assertEqual(os.stat(m3u8._EXT_TOKEN_FILE).st_mode & 0o777, 0o600)

    def test_verify(self):
        tok = m3u8.ext_token_get()
        self.assertTrue(m3u8.ext_token_verify(tok))
        self.assertFalse(m3u8.ext_token_verify(tok + "x"))
        self.assertFalse(m3u8.ext_token_verify(""))
        self.assertFalse(m3u8.ext_token_verify("wrong"))

    def test_reload_from_disk(self):
        tok = m3u8.ext_token_get()
        m3u8._EXT_TOKEN_CACHE = None  # 模拟进程重启
        self.assertEqual(m3u8.ext_token_get(), tok)

    def test_reset_invalidates_old(self):
        old = m3u8.ext_token_get()
        new = m3u8.ext_token_reset()
        self.assertNotEqual(old, new)
        self.assertFalse(m3u8.ext_token_verify(old))
        self.assertTrue(m3u8.ext_token_verify(new))


class TestApiContract(unittest.TestCase):
    """5. 插件 API 契约（鉴权/限流/参数校验）。"""

    def setUp(self):
        self.client = TestClient(bridge_server.app)
        self.tmp = tempfile.mkdtemp(prefix="m3u8_api_")
        self._orig_file = m3u8._EXT_TOKEN_FILE
        self._orig_cache = m3u8._EXT_TOKEN_CACHE
        m3u8._EXT_TOKEN_FILE = os.path.join(self.tmp, ".ext_token")
        m3u8._EXT_TOKEN_CACHE = None
        self.token = m3u8.ext_token_get()
        self.h = {"X-Ext-Token": self.token}

    def tearDown(self):
        m3u8._EXT_TOKEN_FILE = self._orig_file
        m3u8._EXT_TOKEN_CACHE = self._orig_cache
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tasks_requires_token(self):
        r = self.client.get("/api/ext/m3u8/tasks")
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.json()["ok"])

    def test_tasks_rejects_wrong_token(self):
        r = self.client.get("/api/ext/m3u8/tasks", headers={"X-Ext-Token": "bad"})
        self.assertEqual(r.status_code, 401)

    def test_tasks_ok_with_token(self):
        r = self.client.get("/api/ext/m3u8/tasks", headers=self.h)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertIn("tasks", body)

    def test_submit_missing_url(self):
        r = self.client.post("/api/ext/m3u8/submit", headers=self.h, json={})
        self.assertEqual(r.status_code, 400)

    def test_submit_rejects_internal_url(self):
        r = self.client.post("/api/ext/m3u8/submit", headers=self.h,
                             json={"url": "http://127.0.0.1/x.m3u8"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("内网", r.json()["message"])

    def test_m3u8_portal_cancel_missing_task_id(self):
        # 门户端 cancel 缺 task_id → 400（经门禁时的参数校验）
        token = bridge_server._make_portal_token()
        csrf = "csrf-m3u8-0002"
        cookies = {bridge_server.PORTAL_COOKIE: token, bridge_server.CSRF_COOKIE: csrf}
        r = self.client.post("/api/m3u8/cancel", cookies=cookies, json={},
                             headers={bridge_server.CSRF_HEADER: csrf})
        self.assertEqual(r.status_code, 400)

    def test_byterange_and_brace_not_in_public_prefix(self):
        # 保守校验：/api/ext/ 其它路径不应被公开豁免
        r = self.client.get("/api/ext/other")
        self.assertNotEqual(r.status_code, 200)

    def test_public_prefix_not_bypassable_by_traversal(self):
        # 前缀豁免必须是精确前缀：路径穿越不得绕过门户门禁拿到 200
        r = self.client.get("/api/ext/m3u8/../m3u8/tasks")
        self.assertNotEqual(r.status_code, 200)

    def test_m3u8_tasks_without_portal_login(self):
        # /api/m3u8/* 不在公开前缀内 → 未登录 401 JSON
        r = self.client.get("/api/m3u8/tasks")
        self.assertEqual(r.status_code, 401)

    def test_m3u8_tasks_with_portal_login(self):
        token = bridge_server._make_portal_token()
        csrf = "csrf-m3u8-0001"
        cookies = {bridge_server.PORTAL_COOKIE: token, bridge_server.CSRF_COOKIE: csrf}
        r = self.client.get("/api/m3u8/tasks", cookies=cookies)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_m3u8_portal_cancel_requires_csrf(self):
        # 门户端 POST 必须带 CSRF（双提交契约）
        token = bridge_server._make_portal_token()
        cookies = {bridge_server.PORTAL_COOKIE: token, bridge_server.CSRF_COOKIE: "abc"}
        r = self.client.post("/api/m3u8/cancel", cookies=cookies, json={"task_id": "x"})
        self.assertEqual(r.status_code, 403)

    def test_ext_endpoint_ignores_portal_csrf(self):
        # 插件端不参与 CSRF 双提交：带正确 Token 即可（无 CSRF 头）
        r = self.client.post("/api/ext/m3u8/tasks", headers=self.h)
        # POST 到 GET-only 端点 → 405，关键是没有被 CSRF/门户拦截成 403/401
        self.assertNotIn(r.status_code, (401, 403))
        r = self.client.post("/api/ext/m3u8/cancel", headers=self.h, json={})
        self.assertEqual(r.status_code, 400)

    def test_cancel_unknown_task(self):
        r = self.client.post("/api/ext/m3u8/cancel", headers=self.h,
                             json={"task_id": "nope"})
        self.assertEqual(r.status_code, 404)

    def test_rate_limit(self):
        # 限流窗口内超过 120 次/IP → 429
        last = None
        for _ in range(125):
            last = self.client.get("/api/ext/m3u8/tasks", headers=self.h)
            if last.status_code == 429:
                break
        self.assertIsNotNone(last)
        self.assertEqual(last.status_code, 429)
        # 清理限流状态，避免影响其他用例
        import routers.extension as ext
        ext._EXT_REQUESTS.clear()

    def test_token_endpoint_requires_portal_login(self):
        # /api/ext-token 不在公开前缀内 → 未登录应被门禁拦截（401 JSON）
        r = self.client.get("/api/ext-token")
        self.assertEqual(r.status_code, 401)

    def test_token_endpoint_with_portal_cookie(self):
        token = bridge_server._make_portal_token()
        csrf = "csrf-tok-123456"
        cookies = {bridge_server.PORTAL_COOKIE: token, bridge_server.CSRF_COOKIE: csrf}
        r = self.client.get("/api/ext-token", cookies=cookies)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertTrue(r.json()["token"])

class TestSegmentCleanup(unittest.TestCase):
    """9. 分片回收 _cleanup_segments（只删暂存分片 + 目录红线）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="m3u8_clean_")
        self._orig_dir = m3u8.M3U8_DOWNLOAD_DIR
        m3u8.M3U8_DOWNLOAD_DIR = os.path.join(self.tmp, "downloads")
        os.makedirs(m3u8.M3U8_DOWNLOAD_DIR, exist_ok=True)
        self.task = {"id": "abc123", "output_path": ""}
        self.seg_dir = os.path.join(m3u8.M3U8_DOWNLOAD_DIR, self.task["id"])
        os.makedirs(self.seg_dir, exist_ok=True)

    def tearDown(self):
        m3u8.M3U8_DOWNLOAD_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, size, path=None):
        p = os.path.join(path or self.seg_dir, name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        return p

    def test_cleanup_removes_only_segment_files_and_counts_bytes(self):
        """只删 seg_\\d{5}.bin / init.bin 普通文件，返回释放字节数。"""
        self._write("seg_00000.bin", 100)
        self._write("seg_00012.bin", 50)
        self._write("init.bin", 30)
        keep = [
            self._write("影片.mp4", 999),      # 成品：绝不动
            self._write("index.m3u8", 10),     # 播放列表
            self._write("seg_1.bin", 7),       # 位数不符
            self._write("init.bin.bak", 7),    # 后缀不符
            # 大小写不符（正则为区分大小写的精确匹配；用不冲突的数字避免
            # Windows 大小写不敏感文件系统上撞名）
            self._write("SEG_00099.BIN", 7),
        ]
        freed = _cleanup_segments(self.task)
        self.assertEqual(freed, 180)
        for gone in ("seg_00000.bin", "seg_00012.bin", "init.bin"):
            self.assertFalse(os.path.exists(os.path.join(self.seg_dir, gone)), gone)
        for p in keep:
            self.assertTrue(os.path.exists(p), "不应删除非匹配文件: %s" % p)

    def test_cleanup_skips_symlink_and_directory(self):
        """软链接与同名目录绝不删除，也不计入释放量。"""
        real = self._write("seg_00000.bin", 100)
        link = os.path.join(self.seg_dir, "seg_00001.bin")
        os.symlink(real, link)
        sub = os.path.join(self.seg_dir, "seg_00002.bin")
        os.makedirs(sub)
        freed = _cleanup_segments(self.task)
        self.assertEqual(freed, 100)
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.isdir(sub))

    def test_cleanup_never_deletes_output_file(self):
        """成品即使名字命中分片正则也必须保留（红线）。"""
        out = self._write("seg_00000.bin", 64)
        self.task["output_path"] = out
        self._write("seg_00001.bin", 32)
        freed = _cleanup_segments(self.task)
        self.assertEqual(freed, 32)
        self.assertTrue(os.path.exists(out), "成品文件不得被分片回收删除")

    def test_cleanup_rejects_task_id_escaping_download_dir(self):
        """任务目录越界（id 带 ../ 或多级路径）时返回 0 且不删任何文件。"""
        evil_dir = os.path.join(self.tmp, "evil")
        os.makedirs(evil_dir, exist_ok=True)
        victim = self._write("seg_00000.bin", 10, evil_dir)
        for tid in ("../evil", "a/b", "..", ""):
            with self.subTest(task_id=tid):
                freed = _cleanup_segments({"id": tid, "output_path": ""})
                self.assertEqual(freed, 0)
                self.assertTrue(os.path.exists(victim), "越界目录绝不能被删")
        self.assertFalse(os.path.exists(
            os.path.join(self.seg_dir, "seg_00000.bin")))

    def test_cleanup_remove_dir_only_when_empty(self):
        """remove_dir=True 只在目录已空时删目录，绝不带走残留文件。"""
        self._write("seg_00000.bin", 10)
        self.assertEqual(_cleanup_segments(self.task, True), 10)
        self.assertFalse(os.path.exists(self.seg_dir))
        os.makedirs(self.seg_dir, exist_ok=True)
        keep = self._write("影片.mp4", 20)
        self._write("seg_00001.bin", 5)
        self.assertEqual(_cleanup_segments(self.task, True), 5)
        self.assertTrue(os.path.isdir(self.seg_dir))
        self.assertTrue(os.path.exists(keep))


class TestM3u8Delete(unittest.TestCase):
    """10. 删除任务 m3u8_delete（仅终态 / 记录与本地文件一并回收）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="m3u8_del_")
        self._orig = {
            "dl_dir": m3u8.M3U8_DOWNLOAD_DIR,
            "task_file": state._M3U8_FILE,
            "tasks": dict(state._M3U8_TASKS),
        }
        m3u8.M3U8_DOWNLOAD_DIR = os.path.join(self.tmp, "downloads")
        os.makedirs(m3u8.M3U8_DOWNLOAD_DIR, exist_ok=True)
        state._M3U8_FILE = os.path.join(self.tmp, ".m3u8_tasks.json")
        state._M3U8_TASKS.clear()

    def tearDown(self):
        m3u8.M3U8_DOWNLOAD_DIR = self._orig["dl_dir"]
        state._M3U8_FILE = self._orig["task_file"]
        state._M3U8_TASKS.clear()
        state._M3U8_TASKS.update(self._orig["tasks"])
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk_task(self, tid, task_state, seg_sizes=(120,), out_size=400,
                 out_name="影片.ts"):
        d = os.path.join(m3u8.M3U8_DOWNLOAD_DIR, tid)
        os.makedirs(d, exist_ok=True)
        for i, n in enumerate(seg_sizes):
            with open(os.path.join(d, "seg_%05d.bin" % i), "wb") as f:
                f.write(b"s" * n)
        out = ""
        if out_size is not None:
            out = os.path.join(d, out_name)
            with open(out, "wb") as f:
                f.write(b"o" * out_size)
        t = {"id": tid, "url": "http://example.com/x.m3u8", "title": "t",
             "state": task_state, "output_path": out, "remote_dir": "",
             "created_at": 1.0}
        state._M3U8_TASKS[tid] = t
        return t, d, out

    def test_delete_rejected_while_task_active(self):
        """进行中的任务拒绝删除：记录与分片都必须原样保留。"""
        for st in ("queued", "running"):
            with self.subTest(state=st):
                t, d, _ = self._mk_task("run_" + st, st)
                res = _asyncio_run(m3u8_delete(t["id"]))
                self.assertFalse(res["ok"])
                self.assertIn("进行中", res["message"])
                self.assertEqual(res["freed"], 0)
                self.assertIn(t["id"], state._M3U8_TASKS)
                self.assertTrue(os.path.exists(os.path.join(d, "seg_00000.bin")))

    def test_delete_done_task_frees_segments_and_output(self):
        """终态任务：记录移除 + 分片与成品都删除 + 释放字节数正确。"""
        t, d, out = self._mk_task("done1", "done", seg_sizes=(100, 50),
                                  out_size=400)
        res = _asyncio_run(m3u8_delete("done1"))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["freed"], 550)
        self.assertNotIn("done1", state._M3U8_TASKS)
        self.assertFalse(os.path.exists(os.path.join(d, "seg_00000.bin")))
        self.assertFalse(os.path.exists(os.path.join(d, "seg_00001.bin")))
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.isdir(d), "空任务目录应一并删除")
        # 必须持久化：任务表文件里不能再有这条记录
        with open(state._M3U8_FILE, "rb") as f:
            saved = json.loads(f.read().decode("utf-8"))
        self.assertNotIn("done1", saved["tasks"])

    def test_delete_never_touches_output_outside_task_dir(self):
        """成品不在本任务目录内时只回收分片，绝不删除该文件。"""
        t, d, _ = self._mk_task("failed1", "failed", seg_sizes=(70,),
                                out_size=200)
        outside = os.path.join(self.tmp, "outside.ts")
        with open(outside, "wb") as f:
            f.write(b"k" * 300)
        t["output_path"] = outside
        res = _asyncio_run(m3u8_delete("failed1"))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["freed"], 70)
        self.assertTrue(os.path.exists(outside), "越界成品不得被删除")
        self.assertNotIn("failed1", state._M3U8_TASKS)

    def test_delete_unknown_task(self):
        res = _asyncio_run(m3u8_delete("nope"))
        self.assertFalse(res["ok"])
        self.assertIn("不存在", res["message"])
        self.assertEqual(res["freed"], 0)

    def test_delete_rejected_while_archive_uploading(self):
        """归档上传中必须拒绝删除：否则成品会变成无入口可回收的孤儿文件。

        这是「不能谎报已删除」的红线：宁可让用户稍后重试，也不能既摘掉记录、
        又把成品留在盘上——m3u8 任务不进 tasks_all()，此后无任何 UI 能清它。
        """
        t, d, out = self._mk_task("arch1", "done", seg_sizes=(100,), out_size=400)
        t["archived_job_id"] = "JOB1"
        state._ARCHIVE_JOBS["JOB1"] = {"id": "JOB1", "state": "uploading",
                                       "created_at": 1.0}
        try:
            for st in ("queued", "uploading"):
                with self.subTest(job_state=st):
                    state._ARCHIVE_JOBS["JOB1"]["state"] = st
                    res = _asyncio_run(m3u8_delete("arch1"))
                    self.assertFalse(res["ok"], res)
                    self.assertIn("归档上传中", res["message"])
                    self.assertEqual(res["freed"], 0)
                    self.assertIn("arch1", state._M3U8_TASKS, "记录不得被摘掉")
                    self.assertTrue(os.path.exists(out), "上传中的成品绝不能被删")
                    self.assertTrue(os.path.exists(os.path.join(d, "seg_00000.bin")))
        finally:
            state._ARCHIVE_JOBS.pop("JOB1", None)

    def test_delete_allowed_after_archive_finished(self):
        """归档终态（done/failed）不再挡住删除。"""
        try:
            for i, st in enumerate(("done", "failed")):
                with self.subTest(job_state=st):
                    tid = "arch2b%d" % i
                    t, d, out = self._mk_task(tid, "done", seg_sizes=(100,),
                                              out_size=400)
                    t["archived_job_id"] = "JOB2"
                    state._ARCHIVE_JOBS["JOB2"] = {"id": "JOB2", "state": st,
                                                   "created_at": 1.0}
                    res = _asyncio_run(m3u8_delete(tid))
                    self.assertTrue(res["ok"], res)
                    self.assertEqual(res["freed"], 500)
                    self.assertFalse(os.path.exists(out))
        finally:
            state._ARCHIVE_JOBS.pop("JOB2", None)

    def test_delete_rejected_while_runner_still_finishing(self):
        """runner 协程未收尾时拒绝删除（防与飞行中的写入竞赛）。

        取消后协程仍会跑一段：若此时放行删除，飞行中的分片写入会在清目录后
        重新创建文件，留下永久孤儿目录。
        """
        import asyncio

        async def _never_done():
            # 永不返回的协程：模拟「runner 仍在收尾」的存活状态
            await asyncio.Event().wait()

        async def _scenario():
            _t, d, _ = self._mk_task("busy1", "cancelled", seg_sizes=(100,))
            loop = asyncio.get_running_loop()
            task = loop.create_task(_never_done())
            m3u8._M3U8_RUNNERS["busy1"] = [1, task]
            try:
                res = await m3u8_delete("busy1")
                self.assertFalse(res["ok"], res)
                self.assertIn("收尾", res["message"])
                self.assertIn("busy1", state._M3U8_TASKS)
                self.assertTrue(os.path.exists(os.path.join(d, "seg_00000.bin")))
            finally:
                m3u8._M3U8_RUNNERS.pop("busy1", None)
                task.cancel()

        _asyncio_run(_scenario())

    def test_delete_cleans_part_leftovers_and_removes_dir(self):
        """原子写残片 (*.part) 也必须被回收，否则任务目录永远删不掉。"""
        t, d, out = self._mk_task("part1", "failed", seg_sizes=(33,),
                                  out_size=None)
        with open(os.path.join(d, "seg_00001.bin.part"), "wb") as f:
            f.write(b"p" * 77)
        with open(os.path.join(d, "init.bin.part"), "wb") as f:
            f.write(b"i" * 11)
        res = _asyncio_run(m3u8_delete("part1"))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["freed"], 33 + 77 + 11)
        self.assertFalse(os.path.isdir(d), "残片清掉后空目录应被删除")

    def test_delete_rejects_task_id_escaping_download_dir(self):
        """任务 id 含路径分量时：回收返回 0，且目录外的成品绝不被删。

        任务表由磁盘 .m3u8_tasks.json 恢复（setdefault 不校验 id），被篡改的
        表即可构造这种记录，删除路径必须有独立红线兜住。
        """
        outside_dir = os.path.join(self.tmp, "victim")
        os.makedirs(outside_dir, exist_ok=True)
        victim = os.path.join(outside_dir, "keep.ts")
        with open(victim, "wb") as f:
            f.write(b"v" * 99)
        t = {"id": "../victim", "url": "http://e.com/a.m3u8", "title": "t",
             "state": "done", "output_path": victim, "remote_dir": "",
             "created_at": 1.0}
        state._M3U8_TASKS[t["id"]] = t
        res = _asyncio_run(m3u8_delete(t["id"]))
        self.assertEqual(res["freed"], 0)
        self.assertTrue(os.path.exists(victim), "越界成品不得被删除")


class TestM3u8DeleteAndDirsApi(unittest.TestCase):
    """11. 新增端点：删除任务 / 归档目录浏览（双通道鉴权与状态码）。"""

    def setUp(self):
        self.client = TestClient(bridge_server.app)
        self.tmp = tempfile.mkdtemp(prefix="m3u8_delapi_")
        self._orig = {
            "token_file": m3u8._EXT_TOKEN_FILE,
            "token_cache": m3u8._EXT_TOKEN_CACHE,
            "dl_dir": m3u8.M3U8_DOWNLOAD_DIR,
            "task_file": state._M3U8_FILE,
            "tasks": dict(state._M3U8_TASKS),
        }
        m3u8._EXT_TOKEN_FILE = os.path.join(self.tmp, ".ext_token")
        m3u8._EXT_TOKEN_CACHE = None
        m3u8.M3U8_DOWNLOAD_DIR = os.path.join(self.tmp, "downloads")
        os.makedirs(m3u8.M3U8_DOWNLOAD_DIR, exist_ok=True)
        state._M3U8_FILE = os.path.join(self.tmp, ".m3u8_tasks.json")
        state._M3U8_TASKS.clear()
        self.token = m3u8.ext_token_get()
        self.h = {"X-Ext-Token": self.token}

    def tearDown(self):
        m3u8._EXT_TOKEN_FILE = self._orig["token_file"]
        m3u8._EXT_TOKEN_CACHE = self._orig["token_cache"]
        m3u8.M3U8_DOWNLOAD_DIR = self._orig["dl_dir"]
        state._M3U8_FILE = self._orig["task_file"]
        state._M3U8_TASKS.clear()
        state._M3U8_TASKS.update(self._orig["tasks"])
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk_task(self, tid, task_state, seg_sizes=(120,), out_size=400):
        d = os.path.join(m3u8.M3U8_DOWNLOAD_DIR, tid)
        os.makedirs(d, exist_ok=True)
        for i, n in enumerate(seg_sizes):
            with open(os.path.join(d, "seg_%05d.bin" % i), "wb") as f:
                f.write(b"s" * n)
        out = ""
        if out_size is not None:
            out = os.path.join(d, "影片.ts")
            with open(out, "wb") as f:
                f.write(b"o" * out_size)
        t = {"id": tid, "url": "http://example.com/x.m3u8", "state": task_state,
             "output_path": out, "remote_dir": "", "created_at": 1.0}
        state._M3U8_TASKS[tid] = t
        return t, d, out

    # --- 删除任务 ---------------------------------------------------
    def test_ext_delete_requires_token(self):
        r = self.client.post("/api/ext/m3u8/delete", json={"task_id": "x"})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.json()["ok"])

    def test_ext_delete_missing_task_id(self):
        r = self.client.post("/api/ext/m3u8/delete", headers=self.h, json={})
        self.assertEqual(r.status_code, 400)

    def test_ext_delete_unknown_task_404(self):
        r = self.client.post("/api/ext/m3u8/delete", headers=self.h,
                             json={"task_id": "nope"})
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.json()["ok"])

    def test_ext_delete_active_task_404_and_files_kept(self):
        t, d, _ = self._mk_task("api_run", "running")
        r = self.client.post("/api/ext/m3u8/delete", headers=self.h,
                             json={"task_id": "api_run"})
        self.assertEqual(r.status_code, 404)
        self.assertIn("进行中", r.json()["message"])
        self.assertIn("api_run", state._M3U8_TASKS)
        self.assertTrue(os.path.exists(os.path.join(d, "seg_00000.bin")))

    def test_ext_delete_done_task_200(self):
        t, d, out = self._mk_task("api_done", "done")
        r = self.client.post("/api/ext/m3u8/delete", headers=self.h,
                             json={"task_id": "api_done"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["freed"], 120 + 400)
        self.assertNotIn("api_done", state._M3U8_TASKS)
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.isdir(d))

    def test_portal_delete_requires_csrf(self):
        _, _, _ = self._mk_task("portal1", "done")
        cookies = {bridge_server.PORTAL_COOKIE: bridge_server._make_portal_token(),
                   bridge_server.CSRF_COOKIE: "csrf-del-1"}
        r = self.client.post("/api/m3u8/delete", cookies=cookies,
                             json={"task_id": "portal1"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("portal1", state._M3U8_TASKS)

    def test_portal_delete_done_task_200(self):
        _, d, out = self._mk_task("portal2", "done")
        cookies = {bridge_server.PORTAL_COOKIE: bridge_server._make_portal_token(),
                   bridge_server.CSRF_COOKIE: "csrf-del-2"}
        r = self.client.post("/api/m3u8/delete", cookies=cookies,
                             headers={bridge_server.CSRF_HEADER: "csrf-del-2"},
                             json={"task_id": "portal2"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertNotIn("portal2", state._M3U8_TASKS)
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.isdir(d))

    # --- 归档目录浏览 -------------------------------------------------
    def test_ext_dirs_requires_token(self):
        r = self.client.get("/api/ext/m3u8/dirs?path=/")
        self.assertEqual(r.status_code, 401)

    def test_ext_dirs_reuses_openlist_dirs(self):
        from unittest.mock import AsyncMock
        fake = AsyncMock(return_value={"ok": True, "path": "/onedrive",
                                       "dirs": [{"name": "剧集", "path": "/onedrive/剧集"}]})
        with patch("routers.extension.openlist_dirs", new=fake):
            r = self.client.get("/api/ext/m3u8/dirs?path=/onedrive", headers=self.h)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["dirs"][0]["path"], "/onedrive/剧集")
        self.assertEqual(fake.await_args.args, ("/onedrive",))

    # --- submit 的 remote_dir ---------------------------------------
    def test_ext_submit_rejects_bad_remote_dir(self):
        # 公网 IP 字面量可过 SSRF；非法归档目录必须在落任务前就 400
        r = self.client.post("/api/ext/m3u8/submit", headers=self.h,
                             json={"url": "http://93.184.216.34/x.m3u8",
                                   "remote_dir": "/"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("归档目录", r.json()["message"])
        self.assertEqual(state._M3U8_TASKS, {})

    def test_ext_submit_passes_and_echoes_remote_dir(self):
        from unittest.mock import AsyncMock
        fake = AsyncMock(return_value={
            "task": {"id": "t-1", "state": "queued", "remote_dir": "/onedrive/剧集"},
            "duplicate": False})
        with patch("routers.extension.m3u8_submit", new=fake):
            r = self.client.post("/api/ext/m3u8/submit", headers=self.h,
                                 json={"url": "http://93.184.216.34/x.m3u8",
                                       "remote_dir": "/onedrive/剧集"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["remote_dir"], "/onedrive/剧集")
            self.assertEqual(fake.await_args.args[3], "/onedrive/剧集")
            # 别名 archive_dir 等价
            r2 = self.client.post("/api/ext/m3u8/submit", headers=self.h,
                                  json={"url": "http://93.184.216.34/x.m3u8",
                                        "archive_dir": "/ali/剧集"})
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(fake.await_args.args[3], "/ali/剧集")


if __name__ == "__main__":
    unittest.main(verbosity=2)
