# -*- coding: utf-8 -*-
"""
tests/test_archive_concurrency_backoff.py
========================================
回归测试：「本地在存归档并发过头，有些要手动重试、不会自动重试」。

线上实测（34vps .archive_jobs.json 563 条）：
  * 单轮自动归档 sweep 会把所有「已下载待归档」一次性入队（实测峰值 35 个），
    每个 job 立刻 create_task，前端读到的「归档中」等于入队数而非真实并发；
  * 真实上传并发其实一直是 _archive_sem=2，但它硬编码、不可调；
  * 失败重试**没有任何退避**：30s 一轮的 sweep 立刻重新入队，
    _AUTO_ARCHIVE_MAX_ATTEMPTS=3 的额度在一两分钟内烧光（实测「云端核验未完成」
    4 条、113.mp4 5 次），之后只能手动点重试。

本文件锁定四条契约：
1. 并发额度可配（BRIDGE_ARCHIVE_CONCURRENCY），默认仍是安全值 2。
2. 排队中的 job 保持 queued，绝不冒充 uploading —— 状态必须等于真实资源占用。
3. 失败重试有指数退避（30s → 2min → 8min），不许一口气烧光额度。
4. 单轮 sweep 有入队上限，余下任务下一轮继续（不丢任务，只摊开）。
"""
import ast
import asyncio
import os
import re
import unittest
from unittest.mock import patch

import bridge_server
from services.archive_service import (
    _AUTO_RETRY_BACKOFF, _archive_retry_due, _archive_sweep_batch_limit,
)


class TestConcurrencyConfigurable(unittest.TestCase):
    """并发额度必须可配，且默认值不能再回到硬编码。"""

    def test_concurrency_is_env_configurable(self):
        import core.state as st
        self.assertEqual(st._ARCHIVE_CONCURRENCY,
                         max(1, int(os.environ.get("BRIDGE_ARCHIVE_CONCURRENCY", "2")
                                    or 2)))
        self.assertGreaterEqual(st._ARCHIVE_CONCURRENCY, 1)

    def test_semaphore_matches_configured_value(self):
        import core.state as st
        # asyncio.Semaphore 的初始值没有公开属性，用 acquire 次数验证
        async def probe():
            sem = st._archive_sem
            got = 0
            for _ in range(st._ARCHIVE_CONCURRENCY + 3):
                if sem.locked() or sem._value == 0:
                    break
                await sem.acquire()
                got += 1
            for _ in range(got):
                sem.release()
            return got
        self.assertEqual(asyncio.run(probe()), st._ARCHIVE_CONCURRENCY)

    def test_exposed_in_public_config(self):
        pub = bridge_server._archive_config_public()
        self.assertIn("concurrency", pub)
        import core.state as st
        self.assertEqual(pub["concurrency"], st._ARCHIVE_CONCURRENCY)


class TestQueuedIsNotUploading(unittest.TestCase):
    """排队阶段不得把状态写成 uploading（否则前端看到几十个「归档中」）。"""

    def _worker_source(self):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "services", "archive_service.py")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_state_set_inside_semaphore_guard(self):
        src = self._worker_source()
        seg = src[src.index("async def _archive_worker"):]
        seg = seg[:seg.index("async with _archive_sem")]
        self.assertNotIn('job["state"] = "uploading"', seg,
                         "拿到信号量之前不能改成 uploading —— 那是排队中的任务")

    def test_queued_state_visible_to_frontend(self):
        pill = bridge_server._ARCH_PILL
        self.assertIn("queued", pill)
        self.assertEqual(pill["queued"][1], "排队中")


class TestRetryBackoff(unittest.TestCase):
    """失败重试必须退避，不能下一轮 sweep 就立刻重来。"""

    def test_backoff_is_increasing(self):
        self.assertEqual(len(_AUTO_RETRY_BACKOFF), 3)
        self.assertEqual(_AUTO_RETRY_BACKOFF,
                         tuple(sorted(_AUTO_RETRY_BACKOFF)),
                         "退避窗口必须单调递增")
        self.assertGreater(_AUTO_RETRY_BACKOFF[1], _AUTO_RETRY_BACKOFF[0] * 2)

    def test_first_failure_waits_before_retry(self):
        import time as _t
        job = {"updated_at": _t.time()}   # 刚刚失败
        self.assertFalse(_archive_retry_due(job, attempts=1),
                         "刚失败的任务不能被下一轮 sweep 立刻重试")

    def test_retry_allowed_after_window(self):
        import time as _t
        job = {"updated_at": _t.time() - _AUTO_RETRY_BACKOFF[0] - 1}
        self.assertTrue(_archive_retry_due(job, attempts=1))

    def test_later_attempts_wait_longer(self):
        import time as _t
        waited = _AUTO_RETRY_BACKOFF[0] + 1
        job = {"updated_at": _t.time() - waited}
        self.assertTrue(_archive_retry_due(job, attempts=1))
        self.assertFalse(_archive_retry_due(job, attempts=3),
                         "第 3 次要等更久，不能沿用第 1 次的窗口")

    def test_legacy_record_without_timestamp_is_retried(self):
        self.assertTrue(_archive_retry_due({}, attempts=1),
                        "没有时间戳的历史记录不能被永远卡住")

    def test_backoff_capped_by_last_tier(self):
        import time as _t
        job = {"updated_at": _t.time() - _AUTO_RETRY_BACKOFF[-1] - 1}
        self.assertTrue(_archive_retry_due(job, attempts=99),
                        "超出档位后按最后一档计算，不能越等越久到永不重试")


class TestSweepBatchLimit(unittest.TestCase):
    """单轮 sweep 入队必须有上限。"""

    def test_limit_derived_from_concurrency(self):
        import core.state as st
        self.assertEqual(_archive_sweep_batch_limit(), max(4, st._ARCHIVE_CONCURRENCY * 4))

    def test_env_override(self):
        with patch.dict(os.environ, {"BRIDGE_ARCHIVE_SWEEP_BATCH": "7"}):
            self.assertEqual(_archive_sweep_batch_limit(), 7)

    def test_at_least_one(self):
        with patch.dict(os.environ, {"BRIDGE_ARCHIVE_SWEEP_BATCH": "0"}):
            self.assertGreaterEqual(_archive_sweep_batch_limit(), 1)

    def test_sweep_breaks_at_limit(self):
        """sweep 源码里必须有到上限就 break 的分支。"""
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "services", "archive_service.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        seg = src[src.index("async def _auto_archive_sweep"):]
        seg = seg[:seg.index("return enqueued")]
        self.assertIn("_archive_sweep_batch_limit()", seg,
                      "sweep 必须按上限截断，否则单轮又能堆几百个任务")
        self.assertIn("break", seg)


if __name__ == "__main__":
    unittest.main()
