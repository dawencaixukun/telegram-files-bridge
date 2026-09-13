# -*- coding: utf-8 -*-
"""
tests — 全量自动化测试套件
========================
整合 19 大测试模块，自动配置工作区根路径至 sys.path。

【测试数据隔离】
本文件由 unittest discover 在所有测试模块之前导入，是唯一可靠的
"导入顺序无关" 隔离点。

背景：core/config.py 的 APP_ROOT_DIR 是导入期一次性求值的模块常量。
一旦任何模块先导入了 core.config，后续测试再设置 TG_DATA_DIR 就完全失效。
而 discover 按字母序导入测试模块（test_account_health_page 等排在
test_subscriptions_e2e 之前），这些模块在导入时就会拉入 bridge_server →
core.config，导致 test_subscriptions_e2e 的临时目录重定向失效，
其 _subs_save() / _archive_save() 直接写入生产 app-data 目录，
造成 .subscriptions.json / .archive_jobs.json 被 mock 数据污染
（曾观察到 telegramId=100、chatTitle="纪录片频道"、"重试.mp4" 等残留，
以及 OpenList 侧出现近 5000 条 f<N>.mp4 测试条目）。

因此在这里、且在导入任何应用模块之前，把 TG_DATA_DIR 指向独立临时目录。
"""
import atexit
import os
import shutil
import sys
import tempfile

_WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, _WORKSPACE_ROOT)

# --------------------------------------------------------------------------
# 生产数据目录护栏：测试进程绝不允许写入真实 app-data
# --------------------------------------------------------------------------
_PROD_DATA_DIR = os.environ.get("TG_DATA_DIR_PROD_GUARD", "/root/tg-files/app-data")

if not os.environ.get("TG_DATA_DIR"):
    _TEST_DATA_DIR = tempfile.mkdtemp(prefix="tg-bridge-tests-")
    os.environ["TG_DATA_DIR"] = _TEST_DATA_DIR
    atexit.register(shutil.rmtree, _TEST_DATA_DIR, ignore_errors=True)
elif os.path.abspath(os.environ["TG_DATA_DIR"]) == os.path.abspath(_PROD_DATA_DIR):
    raise RuntimeError(
        f"测试隔离失败：TG_DATA_DIR 指向生产数据目录 {_PROD_DATA_DIR!r}。"
        "测试会覆盖真实数据，已中止。请改用临时目录。"
    )
