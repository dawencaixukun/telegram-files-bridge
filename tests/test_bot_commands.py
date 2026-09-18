# -*- coding: utf-8 -*-
"""
tests/test_bot_commands.py
====================================
TG Bot 交互命令服务回归测试：

新增能力：Bot 从「只外发通知」升级为「可双向交互」——
  /ck  下载任务进度与完整性
  /yd  云端归档上传任务（done 不罗列）
  /st  系统总览
  /err 最近归档失败
  /help 帮助

安全契约：
  1. 命令只响应配置的 chatId（白名单），陌生人消息一律忽略不回复；
  2. 回复文本经 HTML 转义（文件名含 <>& 不破坏 parse_mode）；
  3. /yd 不罗列已归档 done 的任务（用户明确要求）；
  4. 归档失败经 remember_archive_error 记录供 /err 展示。
"""
import os
import sys
import time
import unittest
import asyncio
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.state as core_state
import bridge_server
from services.bot_command_service import (
    build_ck_reply, build_yd_reply, build_st_reply, build_err_reply, _handle_update, remember_archive_error
)


def _mk_task(uid, status="download", progress=50, speed_label="1.0 MB/s",
             filename="v.mp4", size="10 MB", loaded="5 MB", err=""):
    return {
        "_unique_id": uid, "id": uid, "time": "09-10 15:00", "source": "ChatA",
        "msg_id": 1, "filename": filename, "size": size, "status": status,
        "loaded": loaded, "progress": progress, "progress_label": "已下载",
        "local_path": "—", "source_url": "", "error_msg": err, "stages": [],
        "_telegram_id": "1", "_size_bytes": 10 * 1024 * 1024,
        "speed_label": speed_label,
    }


def _job(jid, state, fname="a.mp4", prog=50, err=""):
    return {
        "id": jid, "unique_id": "u-" + jid, "filename": fname,
        "state": state, "progress": prog,
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
        remember_archive_error({"filename": "a.mp4", "remote_path": "/d/a.mp4",
                                "error": "容量超限", "updated_at": time.time()})
        try:
            text = build_err_reply()
            self.assertIn("a.mp4", text)
            self.assertIn("容量超限", text)
        finally:
            from services.bot_command_service import _RECENT_ARCHIVE_ERRORS
            _RECENT_ARCHIVE_ERRORS.clear()


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
