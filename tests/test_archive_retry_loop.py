# -*- coding: utf-8 -*-
"""回归测试：不可重试的归档失败必须被熔断，不得形成死循环。

用户发现的真实缺陷：
如果一个文件的本地原文件已经不存在了（被删除/移动），归档必然失败。
此时用户每点一次「重试」，就再跑一遍注定失败的归档——形成死循环。

根因（两层）：
1. `/api/archive/retry-failed` 的次数熔断写成
   `if retry_count >= MAX and not target_ids_set: continue`，
   即只要请求里带了 jobIds 就完全跳过熔断；而前端单项「重试」按钮
   (retryOne) 恰好总是带 jobIds —— 熔断形同虚设。
2. 更根本的是：这类错误属于**永久性**错误，重试本身就没有意义，
   不该只靠「次数」来兜底。

本测试锁定：
- 永久性错误（source_missing / remote_changed / remote_missing）无论是否指定
  jobIds 都不得被重试；
- 临时性错误仍可正常重试（不能误伤）；
- 诊断接口给出 retryable 标记，供前端置灰按钮。
"""
import time
import unittest

from fastapi.testclient import TestClient

import bridge_server
from core.state import _ARCHIVE_JOBS, _MAX_JOB_RETRIES
from core.auth import _BATCH_RETRY_REQUESTS
from services.archive_service import (
    _classify_archive_error, _is_retryable_archive_error, _PERMANENT_ARCHIVE_ERRORS,
)

JID = "job-not-retryable-test"
ERR_SOURCE_MISSING = "本地文件不存在或已被移动，且云端没有完整副本（云端 缺失 字节）"
# 复刻用户截图里的真实报错文本
ERR_ITEM_NOT_FOUND = ('All attempts fail: #1: {"code":"itemNotFound",'
                      '"message":"The resource could not be found."}')
ERR_RESOURCE_MODIFIED = ('All attempts fail: #1: {"code":"resourceModified","message":'
                         '"The resource has changed since the caller last read it; '
                         'usually an eTag mismatch"}')


class TestArchiveRetryLoop(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(bridge_server.app)
        self._saved = dict(_ARCHIVE_JOBS)
        self._csrf = "csrf-loop-regression"
        self.cookies = {
            bridge_server.PORTAL_COOKIE: bridge_server._make_portal_token(),
            bridge_server.CSRF_COOKIE: self._csrf,
        }
        self.headers = {bridge_server.CSRF_HEADER: self._csrf,
                        "Content-Type": "application/json"}
        # 本用例会密集调用 /api/archive/retry-failed（单项 + 重复点击），
        # 必须清掉跨用例共享的限流计数：否则会把 _BATCH_RETRY_LIMIT 的
        # 预算耗尽，导致后续安全审计用例拿到 429 而误判失败。
        _BATCH_RETRY_REQUESTS.clear()

    def tearDown(self):
        _ARCHIVE_JOBS.clear()
        _ARCHIVE_JOBS.update(self._saved)
        _BATCH_RETRY_REQUESTS.clear()
    def _put_job(self, err, state="failed", retry_count=0):
        _ARCHIVE_JOBS.clear()
        _ARCHIVE_JOBS[JID] = {
            "id": JID, "unique_id": "UID-LOOP-REG",
            "filename": "消失的文件.mkv", "raw_filename": "消失的文件.mkv",
            "size_bytes": 584 * 1024 * 1024,
            "local_path": r"D:\webapi\__definitely_missing__.mkv",
            "remote_dir": "/阿里云盘/tg-archive",
            "remote_path": "/阿里云盘/tg-archive/消失的文件.mkv",
            "policy": "overwrite", "delete_local": False,
            "state": state, "progress": 0, "error": err,
            "created_at": time.time(), "updated_at": time.time(), "archived_at": 0.0,
            "retry_count": retry_count,
        }

    def _retry(self, body):
        return self.client.post("/api/archive/retry-failed", json=body,
                                cookies=self.cookies, headers=self.headers).json()

    # ---------- 分类层 ----------

    def test_source_missing_is_permanent(self):
        code, label, fix = _classify_archive_error(ERR_SOURCE_MISSING)
        self.assertEqual(code, "source_missing")
        self.assertFalse(_is_retryable_archive_error(ERR_SOURCE_MISSING))
        self.assertIn("source_missing", _PERMANENT_ARCHIVE_ERRORS)

    def test_user_reported_errors_are_permanent(self):
        """用户截图里的 itemNotFound / resourceModified 都必须判为不可重试。"""
        for err in (ERR_ITEM_NOT_FOUND, ERR_RESOURCE_MODIFIED):
            with self.subTest(err=err[:40]):
                self.assertFalse(_is_retryable_archive_error(err))

    def test_transient_errors_stay_retryable(self):
        """临时性错误不能被误判成永久性，否则正常重试能力被砍掉。"""
        for err in ("令牌已失效，请重新登录", "OpenList 返回业务码 507", "连接超时 timeout"):
            with self.subTest(err=err):
                self.assertTrue(_is_retryable_archive_error(err))

    # ---------- 路由层：关键回归 ----------

    def test_retry_one_with_jobids_is_blocked(self):
        """核心回归：带 jobIds 的单项重试也必须被永久性错误拦住。

        修复前：熔断条件带了 `and not target_ids_set`，单项重试完全绕过，
        retry_count 会被无限推高（实测可达上限之外）。
        """
        self._put_job(ERR_SOURCE_MISSING)
        d = self._retry({"jobIds": [JID], "forceOverwrite": True})
        self.assertEqual(d.get("retriedCount"), 0, "永久性错误不得被重新调度")
        self.assertEqual(d.get("code"), "NOT_RETRYABLE")
        self.assertEqual(_ARCHIVE_JOBS[JID]["retry_count"], 0, "重试次数不得增长")
        self.assertEqual(_ARCHIVE_JOBS[JID]["state"], "failed", "状态不得被重置为 queued")

    def test_repeated_clicks_never_accumulate(self):
        """反复点击不得累积次数——这就是用户担心的死循环。"""
        self._put_job(ERR_SOURCE_MISSING)
        for _ in range(10):
            self._retry({"jobIds": [JID], "forceOverwrite": True})
        self.assertEqual(_ARCHIVE_JOBS[JID]["retry_count"], 0)
        self.assertEqual(_ARCHIVE_JOBS[JID]["state"], "failed")

    def test_retry_all_is_blocked(self):
        """一键重试全部同样拦住永久性错误。"""
        self._put_job(ERR_SOURCE_MISSING)
        d = self._retry({"category": "all", "forceOverwrite": True})
        self.assertEqual(d.get("retriedCount"), 0)
        self.assertEqual(d.get("code"), "NOT_RETRYABLE")
        self.assertIn("本地文件已丢失", d.get("message") or "")

    def test_retry_error_explains_why(self):
        """拦截必须给出原因与建议，而不是含糊的「未找到符合条件的任务」。"""
        self._put_job(ERR_SOURCE_MISSING)
        d = self._retry({"jobIds": [JID], "forceOverwrite": True})
        msg = d.get("message") or ""
        self.assertIn("无法重试", msg)
        self.assertIn("重新下载", msg)  # 明确告诉用户下一步怎么做

    def test_transient_error_still_retries(self):
        """临时性错误必须仍能重试（不能因本修复误伤）。"""
        self._put_job("令牌已失效，请重新登录")
        d = self._retry({"jobIds": [JID], "forceOverwrite": True})
        self.assertEqual(d.get("retriedCount"), 1)
        self.assertEqual(_ARCHIVE_JOBS[JID]["retry_count"], 1)

    def test_max_retries_still_enforced_with_jobids(self):
        """次数上限对显式指定 jobIds 的请求同样生效。"""
        self._put_job("连接超时 timeout", retry_count=_MAX_JOB_RETRIES)
        d = self._retry({"jobIds": [JID], "forceOverwrite": True})
        self.assertEqual(d.get("retriedCount"), 0)
        self.assertIn("最大重试次数", d.get("message") or "")

    def test_diagnostics_expose_retryable_flag(self):
        """/api/archive/failed 必须带 retryable，供前端把按钮置灰。"""
        self._put_job(ERR_SOURCE_MISSING)
        r = self.client.get("/api/archive/failed", cookies=self.cookies,
                            headers=self.headers).json()
        job = next(j for j in r["failedJobs"] if j["id"] == JID)
        self.assertIs(job.get("retryable"), False)
        self.assertEqual(job.get("category"), "source_missing")
        self.assertIn("本地文件已丢失", job.get("categoryLabel") or "")

    def test_transient_diagnostic_is_retryable(self):
        """临时性错误在诊断接口里必须标为可重试。"""
        self._put_job("连接超时 timeout")
        r = self.client.get("/api/archive/failed", cookies=self.cookies,
                            headers=self.headers).json()
        job = next(j for j in r["failedJobs"] if j["id"] == JID)
        self.assertIs(job.get("retryable"), True)


if __name__ == "__main__":
    unittest.main()
