# -*- coding: utf-8 -*-
"""
tests/test_bot_commands.py
====================================
TG Bot 交互命令与远程操控服务回归测试：

覆盖能力：
  1. 输入框 `/` 命令菜单注册（setMyCommands + setChatMenuButton，共 9 条命令）
  2. 状态查看：/ck、/yd（done 不罗列）、/st、/err、/help
  3. 可操作上传：/up（全部待归档、指定文件、指定目标网盘目录）
  4. 可操作重试：/retry（重试失败归档上传 + 重试失败 TG 下载 + 永久失败熔断提示）
  5. 可操作设定上传网盘路径：/dir（查看与点选 OpenList 目录、设定 defaultDir、一键同步订阅规则路径）
  6. 链接直投下载：/dl 与直接向 Bot 发送 t.me 链接
  7. 内联按钮点击交互（callback_query：上传、换目录、重试、删除失败记录、取消上传、切换自动归档等）
  8. 通知卡片内联按钮（下载完成、归档失败、归档成功、磁盘告警均带 <=64B 的 callback_data 按钮）
  9. 安全契约：仅响应白名单 chatId（message 与 callback_query 均拦截陌生人），HTML 转义防注入。
"""
import os
import sys
import time
import tempfile
import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.state as core_state
import bridge_server
from services.bot_command_service import (
    BOT_COMMANDS,
    build_ck_reply, build_yd_reply, build_st_reply, build_err_reply,
    build_up_reply, build_retry_reply, build_dir_reply, build_dl_reply,
    build_notify_download_kb, build_notify_archive_failed_kb,
    register_bot_commands, _cb_put,
    HELP_TEXT, _handle_update, remember_archive_error,
)


def _mk_task(uid, status="download", progress=50, speed_label="1.0 MB/s",
             filename="v.mp4", size="10 MB", loaded="5 MB", err="",
             dl_status=None, local_path="—"):
    if dl_status is None:
        dl_status = "completed" if status in ("downloaded", "archived", "upload") else "downloading"
    return {
        "_unique_id": uid, "id": uid, "time": "09-10 15:00", "source": "ChatA",
        "msg_id": 1, "filename": filename, "size": size, "status": status,
        "loaded": loaded, "progress": progress, "progress_label": "已下载",
        "local_path": local_path, "source_url": "", "error_msg": err, "stages": [],
        "_telegram_id": "1", "_chat_id": "100", "_file_id": "200",
        "_download_status": dl_status,
        "_size_bytes": 10 * 1024 * 1024,
        "speed_label": speed_label,
    }


def _job(jid, state, fname="a.mp4", prog=50, err="", local_path="/tmp/a.mp4"):
    return {
        "id": jid, "unique_id": "u-" + jid, "filename": fname,
        "raw_filename": fname,
        "state": state, "progress": prog,
        "local_path": local_path,
        "remote_path": f"/onedrive/x/{fname}", "remote_dir": "/onedrive/x",
        "size_bytes": 1024, "error": err,
        "created_at": time.time() - 100, "updated_at": time.time(),
        "archived_at": 0.0,
    }


class TestBotCommandReplies(unittest.TestCase):
    def test_ck_shows_active_and_done_summary(self):
        """/ck：进行中带进度条与速率，完成区带完整性与体积汇总。"""
        tasks = [
            _mk_task("live", status="download", progress=55, speed_label="2.0 MB/s"),
            _mk_task("done1", status="downloaded", progress=100),
            _mk_task("arch1", status="archived", progress=100),
        ]
        async def run():
            with patch("services.task_service.tasks_all", new=AsyncMock(return_value=tasks)), \
                 patch("bridge_server.tasks_all", new=AsyncMock(return_value=tasks)):
                return await build_ck_reply()
        text = asyncio.run(run())
        self.assertIn("下载任务进度", text)
        self.assertIn("55%", text, "应渲染文本进度条百分比")
        self.assertIn("2.0 MB/s", text, "应显示实时速率")
        self.assertIn("已下载待归档：<b>1</b>", text)
        self.assertIn("已归档：<b>1</b>", text)

    def test_ck_empty(self):
        """/ck：无任务时给友好空态。"""
        async def run():
            with patch("services.task_service.tasks_all", new=AsyncMock(return_value=[])), \
                 patch("bridge_server.tasks_all", new=AsyncMock(return_value=[])):
                return await build_ck_reply()
        self.assertIn("没有", asyncio.run(run()))

    def test_yd_excludes_done_jobs(self):
        """/yd：只罗列 queued/uploading/failed，done 不出现（用户明确要求）。"""
        core_state._ARCHIVE_JOBS.clear()
        try:
            core_state._ARCHIVE_JOBS["j1"] = _job("j1", "uploading", fname="正在传.mp4", prog=72)
            core_state._ARCHIVE_JOBS["j2"] = _job("j2", "queued", fname="排队中.mp4")
            core_state._ARCHIVE_JOBS["j3"] = _job("j3", "failed", fname="失败.mp4", err="HTTP 507")
            core_state._ARCHIVE_JOBS["j4"] = _job("j4", "done", fname="已归档.mp4")
            core_state._ARCHIVE_JOBS["j5"] = _job("j5", "done", fname="已归档2.mp4")
            text = asyncio.run(build_yd_reply())
            self.assertIn("正在传.mp4", text)
            self.assertIn("72%", text)
            self.assertIn("排队中.mp4", text)
            self.assertIn("失败.mp4", text)
            self.assertNotIn("已归档.mp4", text, "done 任务不得罗列")
            self.assertNotIn("已归档2.mp4", text)
            self.assertIn("2</b>", text, "应提示历史已归档数量")
        finally:
            core_state._ARCHIVE_JOBS.clear()

    def test_yd_all_done_friendly_empty(self):
        """/yd：全部归档完成时提示空队列且带历史计数。"""
        core_state._ARCHIVE_JOBS.clear()
        try:
            core_state._ARCHIVE_JOBS["j1"] = _job("j1", "done")
            core_state._ARCHIVE_JOBS["j2"] = _job("j2", "done")
            text = asyncio.run(build_yd_reply())
            self.assertIn("没有进行中或失败", text)
            self.assertIn("<b>2</b>", text)
        finally:
            core_state._ARCHIVE_JOBS.clear()

    def test_st_overview(self):
        """/st：包含速率/任务计数/磁盘/FloodWait 区块。"""
        tasks = [_mk_task("live", status="download", speed_label="3.0 MB/s")]
        async def run():
            with patch("services.task_service.tasks_all", new=AsyncMock(return_value=tasks)), \
                 patch("bridge_server.tasks_all", new=AsyncMock(return_value=tasks)), \
                 patch("services.task_service._upload_speed_snapshot", new=AsyncMock(return_value=1024.0)):
                return await build_st_reply()
        text = asyncio.run(run())
        self.assertIn("系统总览", text)
        self.assertIn("下载：", text)
        self.assertIn("上传：", text)
        self.assertIn("磁盘", text)

    def test_err_lists_recent_failures(self):
        """/err：归档失败快照按时间倒序展示。"""
        core_state._ARCHIVE_JOBS.clear()
        remember_archive_error({"filename": "a.mp4", "remote_path": "/d/a.mp4",
                                "error": "容量超限", "updated_at": time.time()})
        try:
            text = build_err_reply()
            self.assertIn("a.mp4", text)
            self.assertIn("容量超限", text)
        finally:
            from services.bot_command_service import _RECENT_ARCHIVE_ERRORS
            _RECENT_ARCHIVE_ERRORS.clear()


class TestBotMenuAndOperations(unittest.TestCase):
    def setUp(self):
        self.orig_arch_cfg = dict(core_state._ARCHIVE_CONFIG)
        self.orig_sub_rules = dict(core_state._SUB_RULES)
        core_state._ARCHIVE_JOBS.clear()

    def tearDown(self):
        core_state._ARCHIVE_CONFIG.clear()
        core_state._ARCHIVE_CONFIG.update(self.orig_arch_cfg)
        core_state._SUB_RULES.clear()
        core_state._SUB_RULES.update(self.orig_sub_rules)
        core_state._ARCHIVE_JOBS.clear()

    def test_register_bot_commands_menu(self):
        """输入框输入 / 弹出菜单：验证 setMyCommands 与 setChatMenuButton 均正确调用。"""
        cmd_names = [c["command"] for c in BOT_COMMANDS]
        for expected in ("up", "retry", "dir", "dl", "ck", "yd", "st", "err", "help"):
            self.assertIn(expected, cmd_names)

        calls = []
        class FakeClient:
            async def post(self, url, json=None, timeout=None):
                calls.append((url, json))
                m = MagicMock(status_code=200)
                m.json.return_value = {"ok": True}
                return m

        ok = asyncio.run(register_bot_commands(FakeClient(), "123:ABC", "8652569586"))
        self.assertTrue(ok)
        endpoints = [u.rsplit("/", 1)[-1] for u, _ in calls]
        self.assertIn("setMyCommands", endpoints)
        self.assertIn("setChatMenuButton", endpoints)

    def test_up_command_enqueues_unarchived_files(self):
        """/up：把本地已下载待归档文件立即入队上传网盘，并支持指定目标目录。"""
        with tempfile.NamedTemporaryFile(suffix=".mp4") as tf:
            tf.write(b"x" * 1024)
            tf.flush()
            core_state._ARCHIVE_CONFIG["defaultDir"] = "/onedrive/yello"
            tasks = [_mk_task("u_up_1", status="downloaded", filename="demo_video.mp4", local_path=tf.name)]

            async def fake_worker(job):
                return None

            async def run():
                with patch("services.task_service.tasks_all", new=AsyncMock(return_value=tasks)), \
                     patch("services.archive_service._archive_worker", new=fake_worker), \
                     patch("services.bot_command_service._archive_save"):
                    return await build_up_reply("/onedrive/custom_dir")

            text, kb = asyncio.run(run())
            self.assertIn("已加入网盘上传队列", text)
            self.assertIn("demo_video.mp4", text)
            self.assertIn("/onedrive/custom_dir", text)
            self.assertEqual(len(core_state._ARCHIVE_JOBS), 1)
            job = next(iter(core_state._ARCHIVE_JOBS.values()))
            self.assertEqual(job["remote_dir"], "/onedrive/custom_dir")
            self.assertEqual(job["unique_id"], "u_up_1")

    def test_retry_command_retries_failed_archive_and_download(self):
        """/retry：同时重试可恢复的失败归档任务与失败下载任务，并拦截本地文件已丢的永久失败。"""
        with tempfile.NamedTemporaryFile(suffix=".mp4") as tf:
            tf.write(b"x" * 2048)
            tf.flush()
            core_state._ARCHIVE_JOBS["j_ok"] = _job(
                "j_ok", "failed", fname="retry_me.mp4", err="504 Gateway Timeout", local_path=tf.name
            )
            core_state._ARCHIVE_JOBS["j_missing"] = _job(
                "j_missing", "failed", fname="gone.mp4", err="本地文件不存在", local_path="/nonexistent/gone.mp4"
            )
            failed_dl_task = _mk_task("u_dl_fail", status="failed", dl_status="error", filename="dl_err.mp4")

            async def fake_worker(job):
                return None

            async def run():
                with patch("services.task_service.tasks_all", new=AsyncMock(return_value=[failed_dl_task])), \
                     patch("services.archive_service._archive_worker", new=fake_worker), \
                     patch("services.bot_command_service._archive_save"), \
                     patch.object(bridge_server.BACKEND, "start_download", new=AsyncMock(return_value={"ok": True})) as mock_dl:
                    text, kb = await build_retry_reply("all")
                    self.assertEqual(mock_dl.call_count, 1)
                    return text, kb

            text, kb = asyncio.run(run())
            self.assertIn("retry_me.mp4", text)
            self.assertIn("dl_err.mp4", text)
            self.assertIn("gone.mp4", text)
            self.assertEqual(core_state._ARCHIVE_JOBS["j_ok"]["state"], "queued")
            self.assertEqual(core_state._ARCHIVE_JOBS["j_missing"]["state"], "failed")

    def test_dir_command_sets_default_and_syncs_rules(self):
        """/dir：支持直接设定默认上传路径，并支持一键同步到收藏夹等订阅规则。"""
        core_state._ARCHIVE_CONFIG["defaultDir"] = "/onedrive/old"
        core_state._SUB_RULES["r1"] = {
            "id": "r1", "chatId": "8652569586", "chatTitle": "收藏 (Saved Messages)",
            "enabled": True, "dirTemplate": "/onedrive/tg-archive/{chat_title}/{YYYY-MM}",
        }
        async def run():
            with patch("services.bot_command_service._archive_config_save"), \
                 patch("services.bot_command_service._subs_save"):
                # 1. 仅设置默认路径，应提示规则存在差异并给出一键同步按钮
                t1, kb1 = await build_dir_reply("/onedrive/yello")
                self.assertEqual(core_state._ARCHIVE_CONFIG["defaultDir"], "/onedrive/yello")
                self.assertIn("/onedrive/yello", t1)
                self.assertIn("收藏", t1)
                # 2. all 模式同步更新规则路径
                t2, _ = await build_dir_reply("all /onedrive/yello")
                self.assertEqual(core_state._SUB_RULES["r1"]["dirTemplate"], "/onedrive/yello")
                self.assertIn("已同步更新", t2)
        asyncio.run(run())

    def test_callback_query_execution_and_security(self):
        """callback_query：验证内联按钮点击可设定目录、清理失败记录，且陌生人点击被拦截。"""
        core_state._ARCHIVE_JOBS["j_fail"] = _job("j_fail", "failed", fname="bad.mp4", err="timeout")
        answered = []
        edited = []

        class FakeClient:
            async def post(self, url, json=None, timeout=None):
                if url.endswith("/answerCallbackQuery"):
                    answered.append(json)
                elif url.endswith("/editMessageText") or url.endswith("/sendMessage"):
                    edited.append((url, json))
                m = MagicMock(status_code=200)
                m.json.return_value = {"ok": True}
                return m

        async def run():
            client = FakeClient()
            # 1. 陌生人点击 callback_query 被完全忽略
            stranger_upd = {
                "update_id": 10,
                "callback_query": {
                    "id": "cb1",
                    "message": {"message_id": 99, "chat": {"id": "99999"}},
                    "data": "act:dismiss_all",
                }
            }
            await _handle_update(client, "tok", "10086", stranger_upd)
            self.assertEqual(answered, [])
            self.assertIn("j_fail", core_state._ARCHIVE_JOBS)

            # 2. 管理员点击清理失败记录按钮
            admin_upd = {
                "update_id": 11,
                "callback_query": {
                    "id": "cb2",
                    "message": {"message_id": 99, "chat": {"id": "10086"}},
                    "data": "act:dismiss_all",
                }
            }
            with patch("services.bot_command_service._archive_save"):
                await _handle_update(client, "tok", "10086", admin_upd)
            self.assertEqual(len(answered), 1)
            self.assertNotIn("j_fail", core_state._ARCHIVE_JOBS)

            # 3. 管理员点击设定路径按钮 sd:<key>
            dk = _cb_put("/onedrive/movies_2026")
            dir_upd = {
                "update_id": 12,
                "callback_query": {
                    "id": "cb3",
                    "message": {"message_id": 100, "chat": {"id": "10086"}},
                    "data": f"sd:{dk}",
                }
            }
            with patch("services.bot_command_service._archive_config_save"):
                await _handle_update(client, "tok", "10086", dir_upd)
            self.assertEqual(core_state._ARCHIVE_CONFIG["defaultDir"], "/onedrive/movies_2026")

        asyncio.run(run())

    def test_notification_inline_keyboards_stay_under_64_bytes(self):
        """通知卡片的内联按钮 callback_data 即使面对超长路径/UID 也严格 <= 64 字节。"""
        long_uid = "unique_id_" + "x" * 120
        kb1 = build_notify_download_kb({"_unique_id": long_uid, "filename": "极长文件名.mp4"})
        for row in kb1["inline_keyboard"]:
            for btn in row:
                self.assertLessEqual(len(btn["callback_data"].encode("utf-8")), 64)

        kb2 = build_notify_archive_failed_kb({"id": "a1b2c3d4e5f6", "filename": "失败.mp4"})
        for row in kb2["inline_keyboard"]:
            for btn in row:
                self.assertLessEqual(len(btn["callback_data"].encode("utf-8")), 64)

    def test_direct_tme_link_triggers_download(self):
        """直接向 Bot 发送 t.me 链接（无需打 /dl）即可触发解析下载。"""
        sent = []
        async def fake_reply(client, token, cid, text, reply_markup=None):
            sent.append((cid, text, reply_markup))

        async def run():
            with patch("services.bot_command_service._send_reply", new=fake_reply), \
                 patch("services.watermark_service._disk_guard_or_enqueue_links", new=AsyncMock(return_value=None)), \
                 patch("services.task_service._resolve_links_to_files", new=AsyncMock(return_value=(2, ""))):
                upd = {"update_id": 20, "message": {"chat": {"id": "10086"}, "text": "https://t.me/c/123456/789"}}
                await _handle_update(object(), "tok", "10086", upd)
        asyncio.run(run())
        self.assertEqual(len(sent), 1)
        self.assertIn("已提交下载队列", sent[0][1])
        self.assertIn("2", sent[0][1])


class TestBotCommandSecurity(unittest.TestCase):
    def _upd(self, chat_id, text):
        return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}

    def test_stranger_chat_is_ignored(self):
        """白名单外的 chatId 发命令：不回复、不暴露 Bot 存在。"""
        sent = []
        async def fake_send(client, token, cid, text):
            sent.append((cid, text))
        async def run():
            client = object()
            return await _handle_update(client, "tok", "10086", self._upd("999", "/ck"))
        asyncio.run(run())
        self.assertEqual(sent, [], "陌生人命令必须被忽略")

    def test_allowed_chat_gets_reply(self):
        """白名单 chatId 的命令获得回复。"""
        sent = []
        async def fake_reply(client, token, cid, text):
            sent.append((cid, text))
        async def run():
            with patch("services.bot_command_service._send_reply", new=fake_reply), \
                 patch("services.task_service.tasks_all", new=AsyncMock(return_value=[])), \
                 patch("bridge_server.tasks_all", new=AsyncMock(return_value=[])):
                client = object()
                return await _handle_update(client, "tok", "10086", self._upd(10086, "/help"))
        asyncio.run(run())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "10086")
        self.assertIn("帮助", sent[0][1])

    def test_unknown_command_hint(self):
        """未识别命令：白名单内给礼貌提示。"""
        sent = []
        async def fake_reply(client, token, cid, text):
            sent.append(text)
        async def run():
            with patch("services.bot_command_service._send_reply", new=fake_reply):
                client = object()
                return await _handle_update(client, "tok", "10086", self._upd("10086", "/foobar"))
        asyncio.run(run())
        self.assertEqual(len(sent), 1)
        self.assertIn("/foobar", sent[0])
        self.assertIn("/help", sent[0])

    def test_html_escaping_in_reply(self):
        """文件名含 HTML 特殊字符时转义，不破坏 parse_mode。"""
        core_state._ARCHIVE_JOBS.clear()
        remember_archive_error({"filename": "<b>&evil</b>.mp4", "remote_path": "/d/<x>",
                                "error": "e<r>", "updated_at": time.time()})
        try:
            text = build_err_reply()
            self.assertIn("&lt;b&gt;", text)
            self.assertNotIn("<b>&evil", text)
        finally:
            from services.bot_command_service import _RECENT_ARCHIVE_ERRORS
            _RECENT_ARCHIVE_ERRORS.clear()


if __name__ == "__main__":
    unittest.main()
