# -*- coding: utf-8 -*-
"""
tests/test_tg_throttle.py
=========================
TG 请求节流（防封号随机节奏 + 每日预算 + 跨日自动续跑）回归测试。

覆盖矩阵：
1. 档位预设：默认档位必须是「平衡档」（用户拍板），四档参数与说明齐备，
   切换档位按预设回填并发/间隔/日预算。
2. 覆盖范围默认 4 项全开（浏览页/链接直投/频道监听/缩略图补图），
   取回下载不纳入；关闭某项后该入口直通。
3. 小批量直通：≤ 并发上限时不出队、不延迟、原样返回 None（调用方一次性提交）。
4. 超并发分批：先下发并发上限个，其余进队列；队列落盘 0600；任务列表可见
   waiting_throttle 行（与磁盘挂起区分）。
5. 每日预算：放行计数累计；耗尽时整批挂起（suspended=True）而**不是**丢弃，
   并给出含次日钟点的提示。
6. 跨日续跑：跨天后（模拟到点）挂起自动解除并恢复放行。
7. 缩略图补图：计入同一预算、份额上限 40%、额度不足时明确拒绝；
   关闭覆盖范围后恒放行。
8. 接口：/api/throttle/status|config|wake|queue/clear 鉴权与行为。
9. 取消：从节流队列出队取消。
"""
import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge_server as B
import core.state as st
import services.throttle_service as T


def setUpModule():
    """暂停后台自动放行，使「入队条数 / 放行条数」断言确定。

    生产路径不打开这个开关；队列与预算照常走真实逻辑（那才是要测的部分）。
    """
    T.throttle_pause_dispatch()


def tearDownModule():
    T.throttle_resume_dispatch()


class TestThrottleDefaults(unittest.TestCase):
    def setUp(self):
        self._cfg = json.loads(json.dumps(st._THROTTLE_CONFIG))
        self._state = json.loads(json.dumps(st._THROTTLE_STATE))
        self._queue = dict(st._THROTTLE_QUEUE)
        self._tmp = tempfile.mkdtemp(prefix="throttle-defaults-")
        self._orig_queue_file = st._THROTTLE_QUEUE_FILE
        st._THROTTLE_QUEUE_FILE = os.path.join(self._tmp, ".throttle_queue.json")
        st._THROTTLE_QUEUE.clear()
        T._apply_profile("balanced")
        st._THROTTLE_CONFIG["enabled"] = True

    def tearDown(self):
        st._THROTTLE_CONFIG.clear()
        st._THROTTLE_CONFIG.update(self._cfg)
        st._THROTTLE_STATE.clear()
        st._THROTTLE_STATE.update(self._state)
        st._THROTTLE_QUEUE.clear()
        st._THROTTLE_QUEUE.update(self._queue)
        st._THROTTLE_QUEUE_FILE = self._orig_queue_file
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_default_profile_is_balanced(self):
        """用户拍板：默认档位 = 平衡档（并发 2~4 / 间隔 6~25 秒 / 日预算 200）。"""
        self.assertEqual(st._THROTTLE_DEFAULT_PROFILE, "balanced")
        cfg = T.throttle_config_public()
        self.assertEqual(cfg["profile"], "balanced")
        self.assertEqual(cfg["concurrencyMax"], 4)
        self.assertEqual(cfg["intervalMin"], 6.0)
        self.assertEqual(cfg["intervalMax"], 25.0)
        self.assertEqual(cfg["dailyBudget"], 200)

    def test_four_profiles_available_with_descriptions(self):
        """四档预设必须齐备且带可读说明（设置页直接渲染）。"""
        cfg = T.throttle_config_public()
        self.assertEqual(set(cfg["profiles"]), {"conservative", "robust", "balanced", "unlimited"})
        for key, p in cfg["profiles"].items():
            self.assertTrue(p["label"], key)
            self.assertTrue(p["desc"], key)
            self.assertLessEqual(p["intervalMin"], p["intervalMax"], key)
            self.assertLessEqual(p["concurrencyMin"], p["concurrencyMax"], key)

    def test_scope_covers_four_paths_by_default(self):
        """覆盖范围默认含浏览页/链接直投/监听/缩略图四项；取回下载不在范围内。"""
        cfg = T.throttle_config_public()
        self.assertTrue(cfg["scope"]["browse"])
        self.assertTrue(cfg["scope"]["submit"])
        self.assertTrue(cfg["scope"]["api"])
        self.assertTrue(cfg["scope"]["watch"])
        self.assertTrue(cfg["scope"]["thumb"])
        # 取回走 OpenList 不碰 TG，恒不纳入
        self.assertNotIn("retrieve", cfg["scope"])
        for kind in ("browse", "submit", "api", "watch", "thumb"):
            self.assertTrue(T.throttle_enabled_for(kind), kind)

    def test_switch_profile_backfills_params(self):
        ok, _ = T.throttle_save_config({"profile": "conservative"})
        self.assertTrue(ok)
        cfg = T.throttle_config_public()
        self.assertEqual(cfg["profile"], "conservative")
        self.assertEqual(cfg["intervalMin"], 30.0)
        self.assertEqual(cfg["dailyBudget"], 40)

    def test_save_config_rejects_unknown_profile(self):
        ok, msg = T.throttle_save_config({"profile": "does-not-exist"})
        self.assertFalse(ok)
        self.assertIn("未知档位", msg)

    def test_save_config_clamps_out_of_range_values(self):
        """越界参数必须钳制，不能原样落库（防前端绕过）。"""
        T.throttle_save_config({"concurrencyMax": 999, "intervalMin": -5,
                                "dailyBudget": -3, "resumeHour": 99})
        cfg = T.throttle_config_public()
        self.assertEqual(cfg["concurrencyMax"], 20)
        self.assertEqual(cfg["intervalMin"], 0.0)
        self.assertEqual(cfg["dailyBudget"], 0)
        self.assertEqual(cfg["resumeHour"], 23)

    def test_disabled_throttle_bypasses_every_path(self):
        st._THROTTLE_CONFIG["enabled"] = False
        for kind in ("browse", "submit", "api", "watch", "thumb"):
            self.assertFalse(T.throttle_enabled_for(kind), kind)
        files = [{"fileId": i} for i in range(1, 30)]
        self.assertIsNone(asyncio.run(T.throttle_plan_submit(files, "browse")),
                          "节流关闭时必须直通（返回 None）")


class TestThrottlePlanning(unittest.TestCase):
    def setUp(self):
        self._cfg = json.loads(json.dumps(st._THROTTLE_CONFIG))
        self._state = json.loads(json.dumps(st._THROTTLE_STATE))
        self._queue = dict(st._THROTTLE_QUEUE)
        self._tmp = tempfile.mkdtemp(prefix="throttle-plan-")
        self._orig_queue_file = st._THROTTLE_QUEUE_FILE
        st._THROTTLE_QUEUE_FILE = os.path.join(self._tmp, ".throttle_queue.json")
        st._THROTTLE_QUEUE.clear()
        T._apply_profile("balanced")
        st._THROTTLE_CONFIG["enabled"] = True
        st._THROTTLE_STATE.update({"date": T._throttle_today(), "used": 0,
                                   "thumbUsed": 0, "submitted": 0,
                                   "lastSubmitAt": 0.0, "lastWakeAt": 0.0})

    def tearDown(self):
        st._THROTTLE_CONFIG.clear()
        st._THROTTLE_CONFIG.update(self._cfg)
        st._THROTTLE_STATE.clear()
        st._THROTTLE_STATE.update(self._state)
        st._THROTTLE_QUEUE.clear()
        st._THROTTLE_QUEUE.update(self._queue)
        st._THROTTLE_QUEUE_FILE = self._orig_queue_file
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _files(self, n):
        return [{"telegramId": 1, "chatId": 2, "messageId": 100 + i, "fileId": 500 + i}
                for i in range(n)]

    def test_small_batch_passes_through(self):
        """≤ 并发上限的小批量直通：不排队、不延迟，并计入当日用量。"""
        plan = asyncio.run(T.throttle_plan_submit(self._files(3), "browse"))
        self.assertIsNone(plan)
        self.assertEqual(len(st._THROTTLE_QUEUE), 0)
        self.assertEqual(T._used_today(), 3)

    def test_batch_over_concurrency_is_split(self):
        """超并发上限：先下发 4 个（平衡档），其余排队，并落盘。"""
        plan = asyncio.run(T.throttle_plan_submit(self._files(10), "browse"))
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan["submit"]), 4)
        self.assertEqual(plan["queued"], 6)
        self.assertFalse(plan["suspended"])
        self.assertEqual(len(st._THROTTLE_QUEUE), 6)
        self.assertTrue(os.path.exists(st._THROTTLE_QUEUE_FILE))
        if os.name == "posix":
            # Windows 上 os.open 的 mode 位不生效（无 POSIX 权限位），只校验 POSIX
            self.assertEqual(os.stat(st._THROTTLE_QUEUE_FILE).st_mode & 0o777, 0o600)
        self.assertIn("节流队列", plan["message"])

    def test_immediate_dispatch_capped_by_remaining_budget(self):
        """立即下发数同时受「并发上限」与「剩余预算」约束。

        历史缺陷（回归）：只看并发上限时，预算 2 + 一次提交 6 个会当场下发 4 个，
        把当天预算击穿 —— 用户设定的上限完全失效。
        """
        st._THROTTLE_CONFIG["dailyBudget"] = 2
        st._THROTTLE_STATE.update({"used": 0, "thumbUsed": 0})
        plan = asyncio.run(T.throttle_plan_submit(self._files(6), "browse"))
        self.assertEqual(len(plan["submit"]), 2, "不得超出剩余预算")
        self.assertEqual(plan["queued"], 4)
        self.assertEqual(T._used_today(), 2)
        self.assertTrue(T._budget_exhausted())

    def test_queued_rows_appear_in_task_list(self):
        """排队的任务必须在任务列表可见，且状态与「磁盘挂起」区分开。"""
        asyncio.run(T.throttle_plan_submit(self._files(7), "browse"))
        rows = T.throttle_pending_rows()
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertEqual(r["status"], "waiting_throttle")
            self.assertIn("_unique_id", r)
            self.assertTrue(r["error_msg"])

    def test_queued_row_explains_remaining_wait(self):
        """排队行要说明还要等多久，而不是一句笼统的「排队中」。"""
        st._THROTTLE_STATE["nextReleaseAt"] = time.time() + 120
        st._THROTTLE_QUEUE["t1"] = {
            "id": "t1", "filename": "a.mp4", "payload": {"fileId": 1},
            "created_at": time.time(), "due_at": 0.0, "suspended": False,
            "status": "waiting_throttle", "uniqueId": "",
        }
        row = T.throttle_pending_rows()[0]
        self.assertIn("秒后放行", row["error_msg"])

    def test_suspended_row_explains_next_day_resume(self):
        """预算挂起的行必须说明「次日自动续跑」，用户才知道不用手动干预。"""
        st._THROTTLE_CONFIG["dailyBudget"] = 1
        st._THROTTLE_STATE["used"] = 1
        asyncio.run(T.throttle_plan_submit(self._files(1), "browse"))
        row = T.throttle_pending_rows()[0]
        self.assertIn("次日", row["error_msg"])
        self.assertIn("自动续跑", row["error_msg"])

    def test_budget_exhausted_suspends_instead_of_dropping(self):
        """预算耗尽：整批挂起（不丢弃、不报错），提示含次日钟点。"""
        st._THROTTLE_CONFIG["dailyBudget"] = 5
        st._THROTTLE_STATE["used"] = 5
        plan = asyncio.run(T.throttle_plan_submit(self._files(3), "submit"))
        self.assertIsNotNone(plan)
        self.assertEqual(plan["submit"], [])
        self.assertTrue(plan["suspended"])
        self.assertEqual(plan["queued"], 3)
        self.assertEqual(len(st._THROTTLE_QUEUE), 3)
        self.assertTrue(all(w["suspended"] for w in st._THROTTLE_QUEUE.values()))
        self.assertIn("次日", plan["message"])

    def test_thumb_budget_share_capped_at_40_percent(self):
        """缩略图最多用掉 40% 日预算，不能把下载额度吃光。

        记账不变量：thumbUsed 是 used 的子集（每张补图同时 +used +thumbUsed）。
        """
        st._THROTTLE_CONFIG["dailyBudget"] = 100
        st._THROTTLE_STATE["used"] = 40
        st._THROTTLE_STATE["thumbUsed"] = 40
        self.assertEqual(T._thumb_budget_left(), 0, "份额用满后拒绝继续补图")
        self.assertFalse(T.throttle_thumb_allow())
        # 总预算仍有余量（60 个）可放行下载
        self.assertEqual(T._budget_left(), 60)
        self.assertEqual(T._used_today(), 40)

    def test_thumb_allow_consumes_budget(self):
        st._THROTTLE_CONFIG["dailyBudget"] = 100
        st._THROTTLE_STATE.update({"used": 0, "thumbUsed": 0})
        self.assertTrue(T.throttle_thumb_allow())
        self.assertEqual(st._THROTTLE_STATE["thumbUsed"], 1)
        self.assertEqual(st._THROTTLE_STATE["used"], 1)

    def test_thumb_disabled_scope_always_allows(self):
        st._THROTTLE_CONFIG["scope"]["thumb"] = False
        st._THROTTLE_CONFIG["dailyBudget"] = 1
        st._THROTTLE_STATE.update({"used": 1, "thumbUsed": 1})
        self.assertTrue(T.throttle_thumb_allow(), "未纳入范围时必须恒放行")
        self.assertEqual(st._THROTTLE_STATE["used"], 1, "不纳入范围时不应记账")

    def test_cancel_removes_from_queue(self):
        asyncio.run(T.throttle_plan_submit(self._files(6), "browse"))
        victim = list(st._THROTTLE_QUEUE.values())[0]
        hit = T.throttle_cancel(task_id=victim["id"])
        self.assertIsNotNone(hit)
        self.assertEqual(len(st._THROTTLE_QUEUE), 1)

    def test_clear_empties_queue(self):
        asyncio.run(T.throttle_plan_submit(self._files(6), "browse"))
        removed = T.throttle_clear()
        self.assertEqual(removed, 2)
        self.assertEqual(len(st._THROTTLE_QUEUE), 0)


class TestThrottleDispatch(unittest.TestCase):
    def setUp(self):
        self._cfg = json.loads(json.dumps(st._THROTTLE_CONFIG))
        self._state = json.loads(json.dumps(st._THROTTLE_STATE))
        self._queue = dict(st._THROTTLE_QUEUE)
        self._tmp = tempfile.mkdtemp(prefix="throttle-dispatch-")
        self._orig_queue_file = st._THROTTLE_QUEUE_FILE
        st._THROTTLE_QUEUE_FILE = os.path.join(self._tmp, ".throttle_queue.json")
        st._THROTTLE_QUEUE.clear()
        T._apply_profile("balanced")
        st._THROTTLE_CONFIG["enabled"] = True
        st._THROTTLE_STATE.update({"date": T._throttle_today(), "used": 0,
                                   "thumbUsed": 0, "submitted": 0,
                                   "lastSubmitAt": 0.0, "lastWakeAt": 0.0})

    def tearDown(self):
        st._THROTTLE_CONFIG.clear()
        st._THROTTLE_CONFIG.update(self._cfg)
        st._THROTTLE_STATE.clear()
        st._THROTTLE_STATE.update(self._state)
        st._THROTTLE_QUEUE.clear()
        st._THROTTLE_QUEUE.update(self._queue)
        st._THROTTLE_QUEUE_FILE = self._orig_queue_file
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _files(self, n, base=900):
        return [{"telegramId": 1, "chatId": 2, "messageId": base + i, "fileId": base + i}
                for i in range(n)]

    def test_dispatch_releases_queued_entry(self):
        """到期条目按随机节奏放行：提交给后端、出队、计入预算。"""
        asyncio.run(T.throttle_plan_submit(self._files(6), "browse"))   # 4 下发 + 2 排队
        self.assertEqual(len(st._THROTTLE_QUEUE), 2)
        st._THROTTLE_STATE["nextReleaseAt"] = 0.0     # 上一轮间隔已过
        started = []

        async def fake_start(payload):
            started.append(payload)
            return {"ok": True}

        with patch.object(B.BACKEND, "start_download_multiple", new=fake_start):
            released = asyncio.run(T.throttle_dispatch_pending())
        self.assertEqual(released, 1, "自动节奏单轮只放 1 个，避免瞬间成串")
        self.assertEqual(len(started), 1)
        self.assertEqual(len(st._THROTTLE_QUEUE), 1)
        self.assertEqual(T._used_today(), 5)

    def test_dispatch_skips_when_suspended_and_budget_gone(self):
        """预算耗尽期间不放行任何排队任务。"""
        st._THROTTLE_CONFIG["dailyBudget"] = 2
        st._THROTTLE_STATE["used"] = 2
        asyncio.run(T.throttle_plan_submit(self._files(3), "browse"))
        self.assertTrue(all(w["suspended"] for w in st._THROTTLE_QUEUE.values()))
        called = []

        async def fake_start(payload):
            called.append(payload)

        with patch.object(B.BACKEND, "start_download_multiple", new=fake_start):
            released = asyncio.run(T.throttle_dispatch_pending(force=True))
        self.assertEqual(released, 0)
        self.assertEqual(called, [])

    def test_random_gate_spaces_out_releases(self):
        """随机间隔闸门：非 force 放行必须隔一个随机间隔，不能一轮把队列全放完。"""
        st._THROTTLE_CONFIG["intervalMin"] = 3600.0
        st._THROTTLE_CONFIG["intervalMax"] = 3600.0
        asyncio.run(T.throttle_plan_submit(self._files(2), "browse"))   # 入队 2（cap=4 时直通）
        self.assertEqual(len(st._THROTTLE_QUEUE), 0)
        # 直接构造两条到期条目
        for i in range(2):
            st._THROTTLE_QUEUE[f"t{i}"] = {
                "id": f"t{i}", "filename": f"f{i}.mp4", "payload": {"fileId": 70 + i},
                "created_at": time.time() + i, "due_at": 0.0, "suspended": False,
                "status": "waiting_throttle", "uniqueId": "",
            }
        started = []

        async def fake_start(payload):
            started.append(payload)

        with patch.object(B.BACKEND, "start_download_multiple", new=fake_start):
            first = asyncio.run(T.throttle_dispatch_pending())
            second = asyncio.run(T.throttle_dispatch_pending())   # 闸门未到期 → 0
        self.assertEqual(first, 1)
        self.assertEqual(second, 0, "间隔未到必须不放行")
        self.assertEqual(len(st._THROTTLE_QUEUE), 1)

    def test_force_wake_respects_concurrency_cap(self):
        """手动放行跳过随机间隔，但仍受并发上限约束（不是一次倒完）。"""
        st._THROTTLE_CONFIG["intervalMin"] = 3600.0
        st._THROTTLE_CONFIG["intervalMax"] = 3600.0
        for i in range(8):
            st._THROTTLE_QUEUE[f"t{i}"] = {
                "id": f"t{i}", "filename": f"f{i}.mp4", "payload": {"fileId": 60 + i},
                "created_at": time.time() + i, "due_at": 0.0, "suspended": False,
                "status": "waiting_throttle", "uniqueId": "",
            }
        started = []

        async def fake_start(payload):
            started.append(payload)

        with patch.object(B.BACKEND, "start_download_multiple", new=fake_start):
            released = asyncio.run(T.throttle_dispatch_pending(force=True))
        self.assertEqual(released, 4, "一次最多放 concurrencyMax 个")
        self.assertEqual(len(st._THROTTLE_QUEUE), 4)

    def test_next_day_resumes_suspended_queue(self):
        """跨日（预算重置）后挂起自动解除并恢复放行 —— 用户要求的「次日自动跑」。"""
        st._THROTTLE_CONFIG["dailyBudget"] = 2
        st._THROTTLE_CONFIG["resumeHour"] = 0
        st._THROTTLE_STATE["used"] = 2          # 今天额度已用完
        st._THROTTLE_STATE["date"] = T._throttle_today()
        st._THROTTLE_STATE["nextReleaseAt"] = 0.0
        asyncio.run(T.throttle_plan_submit(self._files(2), "browse"))
        self.assertEqual(len(st._THROTTLE_QUEUE), 2, "额度用完 → 全部挂起")
        self.assertTrue(all(w["suspended"] for w in st._THROTTLE_QUEUE.values()))

        # 模拟「已经是第二天」：把预算归属日改成昨天，used 仍为满额
        yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        st._THROTTLE_STATE["date"] = yesterday
        started = []

        async def fake_start(payload):
            started.append(payload)

        with patch.object(B.BACKEND, "start_download_multiple", new=fake_start):
            released = asyncio.run(T.throttle_dispatch_pending(force=True))
        self.assertEqual(released, 2, "跨日后必须自动续跑")
        self.assertEqual(len(started), 1, "成批放行 = 一次后端调用")
        self.assertEqual(len(started[0]["files"]), 2)
        # 跨日重置：今日用量从 0 重新计，放行 2 个后为 2
        self.assertEqual(T._used_today(), 2)
        self.assertEqual(st._THROTTLE_STATE["date"], T._throttle_today())

    def test_dispatch_keeps_entries_when_backend_fails(self):
        """后端提交失败时条目留在队列（下次重试），不能静默丢弃。"""
        asyncio.run(T.throttle_plan_submit(self._files(6), "browse"))
        before = len(st._THROTTLE_QUEUE)

        async def boom(payload):
            raise RuntimeError("TDLib down")

        with patch.object(B.BACKEND, "start_download_multiple", new=boom):
            released = asyncio.run(T.throttle_dispatch_pending(force=True))
        self.assertEqual(released, 0)
        self.assertEqual(len(st._THROTTLE_QUEUE), before)

    def test_queue_survives_reload(self):
        """队列落盘后能恢复（bridge 重启不丢挂起任务）。"""
        asyncio.run(T.throttle_plan_submit(self._files(6), "browse"))
        self.assertEqual(len(st._THROTTLE_QUEUE), 2)
        st._THROTTLE_QUEUE.clear()
        st._throttle_queue_load()
        self.assertEqual(len(st._THROTTLE_QUEUE), 2)


class TestThrottleEndpoints(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(B.app)
        self._cfg = json.loads(json.dumps(st._THROTTLE_CONFIG))
        self._state = json.loads(json.dumps(st._THROTTLE_STATE))
        self._queue = dict(st._THROTTLE_QUEUE)
        self._tmp = tempfile.mkdtemp(prefix="throttle-api-")
        self._orig_queue_file = st._THROTTLE_QUEUE_FILE
        st._THROTTLE_QUEUE_FILE = os.path.join(self._tmp, ".throttle_queue.json")
        st._THROTTLE_QUEUE.clear()
        T._apply_profile("balanced")
        st._THROTTLE_CONFIG["enabled"] = True
        token = B._make_portal_token()
        csrf = "csrf-throttle-verifier-7777"
        self.cookies = {B.PORTAL_COOKIE: token, B.CSRF_COOKIE: csrf}
        self.headers = {B.CSRF_HEADER: csrf}

    def tearDown(self):
        st._THROTTLE_CONFIG.clear()
        st._THROTTLE_CONFIG.update(self._cfg)
        st._THROTTLE_STATE.clear()
        st._THROTTLE_STATE.update(self._state)
        st._THROTTLE_QUEUE.clear()
        st._THROTTLE_QUEUE.update(self._queue)
        st._THROTTLE_QUEUE_FILE = self._orig_queue_file
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_status_requires_auth(self):
        r = self.client.get("/api/throttle/status")
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.json().get("ok"))

    def test_status_shape(self):
        r = self.client.get("/api/throttle/status", cookies=self.cookies)
        self.assertEqual(r.status_code, 200)
        s = r.json()["status"]
        for key in ("budget", "used", "remaining", "exhausted", "queuedCount",
                    "nextResumeAt", "resumeHour", "profile"):
            self.assertIn(key, s)
        self.assertEqual(s["profile"], "balanced")

    def test_config_get_exposes_profiles_and_scope_labels(self):
        r = self.client.get("/api/throttle/config", cookies=self.cookies)
        self.assertEqual(r.status_code, 200)
        cfg = r.json()["config"]
        self.assertIn("balanced", cfg["profiles"])
        self.assertEqual(len(cfg["scopeLabels"]), 5)

    def test_config_post_requires_csrf(self):
        r = self.client.post("/api/throttle/config", json={"profile": "robust"},
                             cookies=self.cookies)
        self.assertEqual(r.status_code, 403)
        r2 = self.client.post("/api/throttle/config", json={"profile": "robust"},
                              cookies=self.cookies, headers=self.headers)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(st._THROTTLE_CONFIG["profile"], "robust")

    def test_wake_endpoint_reports_released_count(self):
        files = [{"telegramId": 1, "chatId": 2, "messageId": 700 + i, "fileId": 700 + i}
                 for i in range(6)]
        asyncio.run(T.throttle_plan_submit(files, "browse"))

        async def fake_start(payload):
            return {"ok": True}

        with patch.object(B.BACKEND, "start_download_multiple", new=fake_start):
            r = self.client.post("/api/throttle/wake", json={},
                                 cookies=self.cookies, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        # 手动放行跳过随机间隔，但受并发上限约束（平衡档 4 个），队列只剩 2 个
        self.assertEqual(r.json()["released"], 2)
        self.assertEqual(r.json()["status"]["queuedCount"], 0)

    def test_clear_endpoint_empties_queue(self):
        files = [{"telegramId": 1, "chatId": 2, "messageId": 800 + i, "fileId": 800 + i}
                 for i in range(8)]
        asyncio.run(T.throttle_plan_submit(files, "browse"))
        self.assertGreater(len(st._THROTTLE_QUEUE), 0)
        r = self.client.post("/api/throttle/queue/clear", json={},
                             cookies=self.cookies, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(st._THROTTLE_QUEUE), 0)


class TestThrottleSettingsUi(unittest.TestCase):
    """设置页的节流卡片必须**简约**：只留档位选择与主开关，细节收进高级设置。

    两轮历史缺陷：
    1. 首版三组控件用 Alpine x-for 渲染，接口慢/失败时首屏只剩空 select，
       用户以为功能没做出来 —— 所以档位与覆盖范围必须 SSR 直出。
    2. 后来把并发/间隔/预算/续跑钟点/5 项范围全部平铺成一屏数字输入框，
       用户反馈「太复杂」。现在收敛为：主开关 + 4 个档位按钮（点即生效）
       + 折叠的「高级设置」，精细参数只在展开后出现。
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(B.app)
        token = B._make_portal_token()
        self.cookies = {B.PORTAL_COOKIE: token, B.CSRF_COOKIE: "csrf-settings-5555"}

    def test_settings_page_renders_throttle_card(self):
        r = self.client.get("/settings", cookies=self.cookies)
        self.assertEqual(r.status_code, 200)
        h = r.text
        self.assertIn("TG 请求节流（防封号）", h)
        self.assertIn("throttleSettingsCard", h)

    def test_all_four_profiles_listed_in_html(self):
        """四档必须 SSR 直出（x-for 版本首屏为空，用户以为没做出来）。"""
        r = self.client.get("/settings", cookies=self.cookies)
        h = r.text
        for label in ("保守", "稳健", "平衡", "只加间隔"):
            self.assertIn(label, h, label)

    def test_scope_options_live_inside_advanced_section(self):
        """覆盖范围仍然可配，但必须收在「高级设置」折叠区里，不再平铺一屏。"""
        r = self.client.get("/settings", cookies=self.cookies)
        h = r.text
        self.assertIn("高级设置", h)
        # 折叠区起点必须在 5 个范围开关之前（即：它们都在 details 内）
        adv = h.index("高级设置")
        for label in ("浏览页", "链接提交", "接口直投", "频道监听", "缩略图补图"):
            self.assertIn(label, h, label)
            self.assertGreater(h.index(label), adv, f"{label} 应在高级设置之后")
        self.assertIn("取回下载", h, "必须说明取回下载为何不纳入")

    def test_no_flat_numeric_wall(self):
        """主界面（高级设置之前）不得出现并发/间隔/预算这类数字输入框。"""
        r = self.client.get("/settings", cookies=self.cookies)
        h = r.text
        adv = h.index("高级设置")
        head = h[:adv]
        self.assertNotIn('x-model.number="cfg.concurrencyMin"', head,
                         "并发输入框不该出现在主界面")
        self.assertNotIn('x-model.number="cfg.intervalMin"', head,
                         "间隔输入框不该出现在主界面")
        self.assertNotIn('x-model.number="cfg.dailyBudget"', head,
                         "预算输入框不该出现在主界面")

    def test_profile_click_saves_immediately(self):
        """点档位即生效：主界面没有「保存节流设置」按钮，档位按钮直接调 save。"""
        r = self.client.get("/settings", cookies=self.cookies)
        h = r.text
        self.assertIn("pickProfile(", h, "档位按钮应调用 pickProfile（点即保存）")
        adv = h.index("高级设置")
        self.assertNotIn("保存节流设置", h[:adv],
                         "主界面不该再要求用户额外点保存")

    def test_tasks_page_has_throttle_status_bar(self):
        r = self.client.get("/tasks", cookies=self.cookies)
        self.assertEqual(r.status_code, 200)
        self.assertIn("throttleStatusBar", r.text)
        self.assertIn("TG 节流", r.text)


class TestThrottleWiring(unittest.TestCase):
    """提交入口接线：/browse/download 与 /api/tg/quick-download 必须受节流约束。"""

    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(B.app)
        self._cfg = json.loads(json.dumps(st._THROTTLE_CONFIG))
        self._state = json.loads(json.dumps(st._THROTTLE_STATE))
        self._queue = dict(st._THROTTLE_QUEUE)
        self._tmp = tempfile.mkdtemp(prefix="throttle-wiring-")
        self._orig_queue_file = st._THROTTLE_QUEUE_FILE
        st._THROTTLE_QUEUE_FILE = os.path.join(self._tmp, ".throttle_queue.json")
        st._THROTTLE_QUEUE.clear()
        T._apply_profile("balanced")
        st._THROTTLE_CONFIG["enabled"] = True
        st._THROTTLE_STATE.update({"date": T._throttle_today(), "used": 0,
                                   "thumbUsed": 0, "submitted": 0,
                                   "lastSubmitAt": 0.0, "lastWakeAt": 0.0})
        token = B._make_portal_token()
        csrf = "csrf-throttle-wiring-6666"
        self.cookies = {B.PORTAL_COOKIE: token, B.CSRF_COOKIE: csrf}
        self.headers = {B.CSRF_HEADER: csrf}

    def tearDown(self):
        st._THROTTLE_CONFIG.clear()
        st._THROTTLE_CONFIG.update(self._cfg)
        st._THROTTLE_STATE.clear()
        st._THROTTLE_STATE.update(self._state)
        st._THROTTLE_QUEUE.clear()
        st._THROTTLE_QUEUE.update(self._queue)
        st._THROTTLE_QUEUE_FILE = self._orig_queue_file
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_browse_download_split_when_over_concurrency(self):
        files = [{"telegramId": 1, "chatId": 2, "messageId": 900 + i,
                  "fileId": 900 + i, "uniqueId": "uid_%d" % i, "filename": "f%d.mp4" % i}
                 for i in range(9)]
        with patch.object(B, "_check_file_dedup",
                          new=AsyncMock(return_value={"duplicate": False})), \
             patch.object(B, "_disk_guard_or_enqueue", new=AsyncMock(return_value=None)), \
             patch.object(B.BACKEND, "start_download_multiple",
                          new=AsyncMock(return_value={"ok": True})) as mock_start:
            r = self.client.post("/browse/download", json={"files": files, "force": True},
                                 cookies=self.cookies, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 4, "只立即下发并发上限个")
        self.assertEqual(data["queuedCount"], 5)
        mock_start.assert_awaited_once()
        self.assertEqual(len(st._THROTTLE_QUEUE), 5)

    def test_browse_download_suspended_when_budget_exhausted(self):
        st._THROTTLE_CONFIG["dailyBudget"] = 1
        st._THROTTLE_STATE["used"] = 1
        files = [{"telegramId": 1, "chatId": 2, "messageId": 950,
                  "fileId": 950, "uniqueId": "uid_sus", "filename": "sus.mp4"}]
        with patch.object(B, "_check_file_dedup",
                          new=AsyncMock(return_value={"duplicate": False})), \
             patch.object(B, "_disk_guard_or_enqueue", new=AsyncMock(return_value=None)), \
             patch.object(B.BACKEND, "start_download_multiple",
                          new=AsyncMock(return_value={"ok": True})) as mock_start:
            r = self.client.post("/browse/download", json={"files": files, "force": True},
                                 cookies=self.cookies, headers=self.headers)
        data = r.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["code"], "THROTTLE_SUSPENDED")
        self.assertEqual(data["state"], "waiting_throttle")
        mock_start.assert_not_awaited()
        self.assertEqual(len(st._THROTTLE_QUEUE), 1)
        self.assertTrue(list(st._THROTTLE_QUEUE.values())[0]["suspended"])


if __name__ == "__main__":
    unittest.main()
