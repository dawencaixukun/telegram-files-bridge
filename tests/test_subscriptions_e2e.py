"""
test_subscriptions_e2e.py — 订阅归档路由（下载完成 → 自动归档 → 删本地）端到端验证
=====================================================================
覆盖五层：
  1. 目录命名模板引擎（变量渲染 / 非法输入 / 路径穿越 / 字符清洗）
  2. 订阅规则 0600 落盘与恢复 round-trip
  3. 规则匹配（启用/停用/跨聊天）
  4. 自动归档 sweep 决策（命中入队 / 幂等 / 取消尊重 / 失败重试上限 / 统计钩子）
  5. HTTP 层（门禁 302/401、CSRF 403、页面 200 渲染、API 入参校验）

运行：  python test_subscriptions_e2e.py
说明：  TG_DATA_DIR 指向临时目录，不触碰真实数据；sweep 测试用 monkeypatch
        替换 tasks_all / _openlist_ready / _archive_worker，不依赖真实后端。
"""
import asyncio
import json
import os
import sys
import tempfile
import time

# 隔离数据目录（.bridge_secret / logs.jsonl / .subscriptions.json 全进临时目录）
_TMP = tempfile.mkdtemp(prefix="subs-e2e-")
os.environ["TG_DATA_DIR"] = _TMP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_server as B  # noqa: E402
import httpx  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (name, detail))


# ---------------------------------------------------------------------
# 1. 目录命名模板引擎
# ---------------------------------------------------------------------
def test_template():
    print("[1] 目录命名模板引擎")
    ts = 1757000000.0  # 固定时间戳，期望值用同一 localtime 计算
    lt = time.localtime(ts)
    expect_ym = "%04d-%02d" % (lt.tm_year, lt.tm_mon)
    out = B._render_dir_template("/阿里云盘/tg/{source}/{YYYY-MM}",
                                 source="纪录片 放映室", ftype="video", ts=ts)
    check("变量渲染 {source}/{YYYY-MM}", out == "/阿里云盘/tg/纪录片 放映室/" + expect_ym, repr(out))

    out2 = B._render_dir_template("/盘/{YYYY}/{MM}/{DD}", ts=ts)
    check("变量渲染 {YYYY}/{MM}/{DD}",
          out2 == "/盘/%04d/%02d/%02d" % (lt.tm_year, lt.tm_mon, lt.tm_mday), repr(out2))

    check("未知变量 → None", B._render_dir_template("/a/{foo}") is None)
    check("非 / 开头 → None", B._render_dir_template("abc/{source}", source="x") is None)
    check("路径穿越 .. → None", B._render_dir_template("/a/../b") is None)
    check("空模板 → None", B._render_dir_template("") is None)

    dirty = B._render_dir_template("/d/{source}", source='a/b\\c:d*e?f"g<h>i|j')
    check("脏字符清洗后无分隔/禁忌字符",
          dirty is not None and not any(c in dirty.split("/d/", 1)[1] for c in '/\\:*?"<>|'),
          repr(dirty))
    check("空 source 兜底「未分类」",
          B._render_dir_template("/d/{source}", source="") == "/d/未分类")
    check("段首点号被剥离（防隐藏目录）",
          B._render_dir_template("/d/{source}", source="..机密..") == "/d/机密")

    out_adv = B._render_dir_template("/盘/{chat_title}/{resolution}/{ext}",
                                     source="我的频道", chat_title="纪录片频道",
                                     filename="nature_4k.mkv", ftype="video", ts=ts)
    check("高级变量 {chat_title}/{resolution}/{ext} 提取",
          out_adv == "/盘/纪录片频道/4k/mkv", repr(out_adv))

    out_res_fallback = B._render_dir_template("/盘/{resolution}/{ext}",
                                             filename="document_text", ftype="document", ts=ts)
    check("未知分辨率与后缀回退 {resolution}/{ext}",
          out_res_fallback == "/盘/unknown/bin", repr(out_res_fallback))


# ---------------------------------------------------------------------
# 2. 规则落盘与恢复
# ---------------------------------------------------------------------
def test_persistence():
    print("[2] 订阅规则落盘与恢复")
    rule = {
        "id": "r1", "telegramId": 100, "chatId": -1001234, "chatTitle": "纪录片频道",
        "enabled": True, "dirTemplate": "/阿里云盘/tg/{source}/{YYYY-MM}",
        "deleteLocal": True, "policy": "skip", "created_at": time.time(),
        "stats": {"enqueued": 0, "done": 0, "failed": 0, "last_hit_at": 0.0},
    }
    B._SUB_RULES["r1"] = rule
    B._subs_save()
    check("落盘文件存在", os.path.exists(B._SUBS_FILE))
    if os.name != "nt":
        check("文件权限 0600", (os.stat(B._SUBS_FILE).st_mode & 0o777) == 0o600)
    B._SUB_RULES.clear()
    B._subs_load()
    got = B._SUB_RULES.get("r1")
    check("恢复后规则完整",
          got is not None and got["chatTitle"] == "纪录片频道"
          and got["dirTemplate"] == rule["dirTemplate"] and got["deleteLocal"] is True)


# ---------------------------------------------------------------------
# 3. 规则匹配
# ---------------------------------------------------------------------
def test_match():
    print("[3] 规则匹配")
    task = {"_telegram_id": 100, "_chat_id": -1001234, "_unique_id": "u-x"}
    check("启用规则命中", (B._sub_match_rule(task) or {}).get("id") == "r1")
    B._SUB_RULES["r1"]["enabled"] = False
    check("停用规则不命中", B._sub_match_rule(task) is None)
    B._SUB_RULES["r1"]["enabled"] = True
    check("跨聊天不命中", B._sub_match_rule({"_telegram_id": 100, "_chat_id": 999}) is None)
    check("缺字段不命中", B._sub_match_rule({"_telegram_id": 100}) is None)
    pub = B._sub_public(B._SUB_RULES["r1"])
    check("公开视图带目录预览", pub["previewDir"].startswith("/阿里云盘/tg/纪录片频道/"), repr(pub["previewDir"]))

    # 优先级匹配验证
    B._SUB_RULES["prio_low"] = {
        "id": "prio_low", "telegramId": 100, "chatId": -1001234, "chatTitle": "低优",
        "enabled": True, "priority": 10, "dirTemplate": "/low/{source}",
        "deleteLocal": True, "policy": "skip", "created_at": time.time(),
    }
    B._SUB_RULES["prio_high"] = {
        "id": "prio_high", "telegramId": 100, "chatId": -1001234, "chatTitle": "高优",
        "enabled": True, "priority": 100, "dirTemplate": "/high/{source}",
        "deleteLocal": True, "policy": "skip", "created_at": time.time(),
    }
    matched_prio = B._sub_match_rule(task)
    check("高优先级规则优先命中", (matched_prio or {}).get("id") == "prio_high", repr(matched_prio))
    B._SUB_RULES.pop("prio_low", None)
    B._SUB_RULES.pop("prio_high", None)


# ---------------------------------------------------------------------
# 4. sweep 决策（monkeypatch 后端与上传）
# ---------------------------------------------------------------------
def _fake_task(uid, chat, dl="completed", lp="", fname="视频.mp4", tg=100):
    return {
        "id": 1, "filename": fname, "source": "纪录片频道", "status": "archived",
        "_download_status": dl, "_unique_id": uid, "_chat_id": chat, "_telegram_id": tg,
        "_type": "video", "_size_bytes": 1024, "_date_ts": 1757000000.0,
        "local_path": lp,
    }


async def test_sweep():
    print("[4] 自动归档 sweep 决策")
    B._ARCHIVE_JOBS.clear()
    B._ARCHIVE_TASKS.clear()
    B._reset_flood_wait()
    local_ok = os.path.join(_TMP, "ok.mp4")
    with open(local_ok, "wb") as f:
        f.write(b"x" * 16)

    tasks = [
        _fake_task("u1", -1001234, lp=local_ok),          # 命中 → 入队
        _fake_task("u2", -1001234, dl="downloading"),      # 未完成 → 跳过
        _fake_task("u3", 999),                             # 无规则 → 跳过
        _fake_task("u4", -1001234, lp=os.path.join(_TMP, "ghost.mp4")),  # 本地缺失 → 跳过
    ]
    done_jobs = []

    async def fake_tasks_all(force=False):
        return tasks

    async def fake_ready():
        return True

    async def fake_worker(job):
        job["state"] = "done"
        job["progress"] = 100
        done_jobs.append(job["id"])
        B._sub_on_job_finished(job)  # 模拟真实 worker finally 钩子

    orig = (B.tasks_all, B._openlist_ready, B._archive_worker)
    B.tasks_all, B._openlist_ready, B._archive_worker = fake_tasks_all, fake_ready, fake_worker
    try:
        n = await B._auto_archive_sweep()
        check("首轮仅 1 个入队", n == 1, "n=%s" % n)

        job = B._archive_latest_raw_of("u1")
        lt = time.localtime(1757000000.0)
        expect_dir = "/阿里云盘/tg/纪录片频道/%04d-%02d" % (lt.tm_year, lt.tm_mon)
        check("job 目录按消息日期渲染", job is not None and job["remote_dir"] == expect_dir,
              repr(job and job["remote_dir"]))
        check("job 标记 auto + 首次尝试", job["auto"] is True and job["auto_attempts"] == 1)
        check("job 关联规则", job["rule_id"] == "r1")
        check("job 远端路径拼接", job["remote_path"] == expect_dir + "/视频.mp4", repr(job["remote_path"]))

        await asyncio.gather(*B._ARCHIVE_TASKS.values(), return_exceptions=True)
        stats = B._SUB_RULES["r1"]["stats"]
        check("统计 enqueued=1 / done=1", stats["enqueued"] == 1 and stats["done"] == 1, repr(stats))

        n2 = await B._auto_archive_sweep()
        check("二轮幂等（done 不重复入队）", n2 == 0, "n=%s" % n2)

        # 用户手动取消 → 不再自动重试
        tasks.append(_fake_task("u5", -1001234, lp=local_ok, fname="取消.mp4"))
        B._ARCHIVE_JOBS["jc"] = {"id": "jc", "unique_id": "u5", "state": "cancelled",
                                 "created_at": time.time(), "auto": True, "auto_attempts": 1}
        check("取消尊重（不再入队）", await B._auto_archive_sweep() == 0)

        # 失败自动重试：attempts=2 → 第 3 次；attempts=3 → 不再重试
        tasks.append(_fake_task("u6", -1001234, lp=local_ok, fname="重试.mp4"))
        B._ARCHIVE_JOBS["jf2"] = {"id": "jf2", "unique_id": "u6", "state": "failed",
                                  "created_at": time.time(), "auto": True, "auto_attempts": 2}
        n6 = await B._auto_archive_sweep()
        j6 = B._archive_latest_raw_of("u6")
        check("失败第 2 次后 → 自动第 3 次", n6 == 1 and j6["auto_attempts"] == 3,
              "n=%s attempts=%s" % (n6, j6 and j6["auto_attempts"]))
        B._ARCHIVE_JOBS["jf3"] = {"id": "jf3", "unique_id": "u6", "state": "failed",
                                  "created_at": time.time() + 1, "auto": True, "auto_attempts": 3}
        check("重试上限 3 次 → 不再入队", await B._auto_archive_sweep() == 0)
    finally:
        B.tasks_all, B._openlist_ready, B._archive_worker = orig


# ---------------------------------------------------------------------
# 5. HTTP 层（门禁 / CSRF / 页面渲染 / API 校验）
# ---------------------------------------------------------------------
async def test_http():
    print("[5] HTTP 门禁与 API")
    transport = httpx.ASGITransport(app=B.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/subscriptions")
        check("未登录页面 → 302 /login", r.status_code == 302 and "/login" in r.headers.get("location", ""))
        r = await c.get("/api/subscriptions")
        check("未登录 API → 401 JSON", r.status_code == 401)

        cookies = {B.PORTAL_COOKIE: B._make_portal_token(), B.CSRF_COOKIE: "csrf-test"}
        r = await c.get("/subscriptions", cookies=cookies)
        check("已登录页面 → 200 渲染", r.status_code == 200 and "订阅归档" in r.text,
              "status=%s" % r.status_code)
        # Jinja tojson 默认 ensure_ascii=True：中文以 \uXXXX 形式嵌入，浏览器解析后正常
        esc = json.dumps("纪录片频道", ensure_ascii=True)[1:-1]
        check("页面含规则数据", esc in r.text)

        r = await c.post("/api/subscriptions", cookies=cookies, json={"chatId": 1})
        check("缺 CSRF 头 → 403", r.status_code == 403)

        h = {"X-CSRF-Token": "csrf-test"}
        r = await c.post("/api/subscriptions", cookies=cookies, headers=h,
                         json={"telegramId": 1, "chatId": 2, "dirTemplate": "/x/{source}"})
        body = r.json()
        check("非法聊天被拒", r.status_code == 200 and body.get("ok") is False, repr(body)[:120])

        r = await c.post("/api/subscriptions/run", cookies=cookies, headers=h, json={})
        body = r.json()
        check("手动扫描返回 ok（OpenList 未配置 → 0 入队）",
              r.status_code == 200 and body.get("ok") is True and body.get("enqueued") == 0,
              repr(body)[:120])

        r = await c.post("/api/subscriptions/delete", cookies=cookies, headers=h, json={"id": "ghost"})
        check("删除不存在规则 → ok False", r.json().get("ok") is False)


async def main():
    await test_sweep()
    await test_http()


import unittest


class TestSubscriptionsE2E(unittest.TestCase):
    def test_e2e_suite(self):
        test_template()
        test_persistence()
        test_match()
        asyncio.run(main())
        self.assertEqual(FAIL, 0)


if __name__ == "__main__":
    test_template()
    test_persistence()
    test_match()
    asyncio.run(main())
    print("\n===== 结果：%d 通过 / %d 失败 =====" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
