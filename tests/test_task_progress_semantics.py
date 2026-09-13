# -*- coding: utf-8 -*-
"""回归测试：归档阶段不得把「上传进度」渲染成「已下载」。

历史缺陷（用户可见）：文件早已下载完成、正在归档上传时，任务列表的进度条
恒按「已下载 x%」渲染，而 x 实际取自归档上传 job 的 progress。
于是 1.6 GB 的文件在归档上传到 6% 时显示「已下载 6% / 1.6 GB」，
用户误判为下载失败或卡住。

同时锁定阶段时间线的三态：进行中(active) 必须与已完成(done) 可区分，
不能把上传中的阶段画成已完成。
"""
import unittest
from unittest.mock import patch
from core.templates import _stages, templates
from services.task_service import _to_task

SIZE = int(1.6 * 1024 ** 3)  # 1.6 GB


def _file_record(dl_status="completed", downloaded=None):
    return {
        "uniqueId": "UID-regression-1",
        "fileName": "极客飞船频道_第42期.mkv",
        "size": SIZE,
        "downloadStatus": dl_status,
        "downloadedSize": SIZE if downloaded is None else downloaded,
        "date": 1757300000000,
        "startDate": 1757400000,
        "completionDate": 1757402570,
        "chatTitle": "极客飞船频道",
        "messageId": 8841,
        "chatId": -100123,
    }


class _FakeRequest:
    scope = {"type": "http"}


class TestArchiveProgressSemantics(unittest.TestCase):
    def setUp(self):
        # 用真实全局归档表（_to_task 经 _archive_latest_raw_of 读取它），
        # 但必须用 patch.dict 隔离：直接 clear()/update() 会污染后续测试文件
        # （tests/test_archive_status_dedup.py 就是因此让 /library/cloud 断言
        #  在组合运行时随机失败）。patch.dict 在 exit 时精确还原原内容。
        import core.state as state
        self._state = state
        patcher = patch.dict(state._ARCHIVE_JOBS, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _put_job(self, state_name, progress):
        self._state._ARCHIVE_JOBS["job-reg-1"] = {
            "id": "job-reg-1",
            "unique_id": "UID-regression-1",
            "filename": "极客飞船频道_第42期.mkv",
            "size_bytes": SIZE,
            "remote_dir": "/阿里云盘/tg-archive",
            "remote_path": "/阿里云盘/tg-archive/极客飞船频道_第42期.mkv",
            "state": state_name,
            "progress": progress,
            "created_at": 1757410000,
            "updated_at": 1757410300,
            "archived_at": 0.0,
        }

    def test_uploading_must_not_be_labelled_as_download(self):
        """归档上传中：进度口径必须是「已上传」，下载进度仍为 100%。"""
        self._put_job("uploading", 6)
        t = _to_task(_file_record(), 1, {})

        self.assertEqual(t["status"], "upload")
        self.assertEqual(t["progress"], 6)
        self.assertEqual(t["progress_kind"], "upload")
        self.assertEqual(t["progress_label"], "已上传")
        self.assertEqual(t["loaded"], "6%")
        # 下载早已完成，不能被上传的 6% 污染
        self.assertEqual(t["download_progress"], 100)
        self.assertEqual(t["upload_progress"], 6)

    def test_uploading_template_no_longer_says_downloaded(self):
        """渲染真实模板：不得出现「已下载 6%」，必须是「已上传 6%」。"""
        self._put_job("uploading", 6)
        t = _to_task(_file_record(), 1, {})
        html = templates.get_template("partials/_tasks_table.html").render(
            {"request": _FakeRequest(), "tasks": [t]}
        )
        self.assertNotIn("已下载 6%", html)
        self.assertIn("已上传 6%", html)
        self.assertIn("1.6 GB", html)

    def test_timeline_upload_stage_is_active_not_done(self):
        """归档上传中：上传阶段必须是 active（进行中），不能是已完成。"""
        self._put_job("uploading", 6)
        t = _to_task(_file_record(), 1, {})
        by_name = {s["name"]: s for s in t["stages"]}

        self.assertEqual(by_name["下载"]["state"], "done")
        self.assertEqual(by_name["校验"]["state"], "done")
        self.assertEqual(by_name["上传"]["state"], "active")
        self.assertEqual(by_name["上传"]["dur"], "6%")
        self.assertEqual(by_name["归档"]["state"], "idle")
        # 上传阶段绝不能同时是「已完成」
        self.assertEqual(by_name["上传"]["done"], "no")

    def test_queued_archive_stage_is_active(self):
        """归档排队中：上传阶段应为进行中并标注「排队中」，而非已完成。"""
        self._put_job("queued", 0)
        t = _to_task(_file_record(), 1, {})
        by_name = {s["name"]: s for s in t["stages"]}
        self.assertEqual(by_name["上传"]["state"], "active")
        self.assertEqual(by_name["上传"]["dur"], "排队中")
        self.assertEqual(by_name["上传"]["done"], "no")

    def test_archived_all_stages_done(self):
        """归档完成：上传与归档阶段都应是已完成。"""
        self._put_job("done", 100)
        self._state._ARCHIVE_JOBS["job-reg-1"]["archived_at"] = 1757410500
        t = _to_task(_file_record(), 1, {})
        by_name = {s["name"]: s for s in t["stages"]}
        self.assertEqual(t["status"], "archived")
        self.assertEqual(by_name["上传"]["state"], "done")
        self.assertEqual(by_name["归档"]["state"], "done")

    def test_downloading_progress_still_labelled_download(self):
        """真实下载中：口径仍为「已下载」，且下载阶段是 active。"""
        t = _to_task(_file_record("downloading", int(SIZE * 0.42)), 1, {})
        self.assertEqual(t["status"], "download")
        self.assertEqual(t["progress_kind"], "download")
        self.assertEqual(t["progress_label"], "已下载")
        by_name = {s["name"]: s for s in t["stages"]}
        self.assertEqual(by_name["下载"]["state"], "active")
        self.assertEqual(by_name["校验"]["state"], "idle")
        self.assertEqual(by_name["上传"]["state"], "idle")

    def test_downloaded_not_archived(self):
        """下载完成但未归档：上传/归档应为未开始，下载/校验为已完成。"""
        t = _to_task(_file_record(), 1, {})
        self.assertEqual(t["status"], "downloaded")
        self.assertEqual(t["progress_label"], "已下载")
        self.assertEqual(t["download_progress"], 100)
        self.assertEqual(t["upload_progress"], 0)
        by_name = {s["name"]: s for s in t["stages"]}
        self.assertEqual(by_name["下载"]["state"], "done")
        self.assertEqual(by_name["上传"]["state"], "idle")

    def test_waiting_disk_stages_keep_legacy_shape(self):
        """挂起/冷却类任务的 stages 没有 state 字段，模板必须仍按 ghost 兜底渲染。"""
        legacy = [
            {"name": "等待磁盘空间", "done": "yes", "time": "21:00", "dur": "挂起中"},
            {"name": "下载媒体", "done": "no", "time": "—", "dur": "—"},
        ]
        html = templates.get_template("partials/_tasks_table.html").render(
            {"request": _FakeRequest(), "tasks": [{
                "id": 1, "status": "waiting_disk", "time": "21:00", "source": "x", "msg_id": 1,
                "filename": "a.mkv", "size": "1 GB", "loaded": "0 B", "progress": 0,
                "progress_label": "已下载", "progress_kind": "",
                "local_path": "—", "source_url": "", "error_msg": "",
                "stages": legacy, "_unique_id": "u", "_telegram_id": None,
            }]}
        )
        # 未开始阶段应渲染成 ghost（空心点）
        self.assertIn("tl-item ghost", html)
        self.assertNotIn("tl-item  active", html)

    def test_stages_have_state_key_for_all_records(self):
        """_stages 必须始终输出 state 字段，避免模板三态判断退化。"""
        for status in ("downloaded", "upload", "archived", "download", "pending", "failed"):
            for st in _stages(status, _file_record(), None):
                self.assertIn("state", st, "阶段 %s 缺少 state 字段" % st.get("name"))
                self.assertIn(st["state"], ("done", "active", "idle"))


if __name__ == "__main__":
    unittest.main()
