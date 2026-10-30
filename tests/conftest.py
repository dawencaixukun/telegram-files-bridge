# -*- coding: utf-8 -*-
"""
tests/conftest.py — 测试环境配置与公共 Fixtures
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import bridge_server
from fastapi.testclient import TestClient
import pytest


@pytest.fixture(autouse=True)
def _isolate_browse_memory():
    """每个用例前后快照/恢复网盘浏览记忆（.browse_memory.json 的内存态）。

    浏览位置记忆是进程级全局：一个用例访问了某目录，裸 /explorer 在后续
    用例里就会被 302 续播到那里 —— TestClient 跟随重定向时不会重发
    per-request cookies，导致「登录后 200」类断言偶发拿到登录页
    （合跑必现、单跑通过）。autouse 快照恢复彻底消除这种顺序耦合。
    """
    import core.state as cs
    saved = dict(cs._BROWSE_MEMORY)
    saved_recent = dict(cs._BROWSE_MEMORY.get("recent") or {})
    yield
    cs._BROWSE_MEMORY.clear()
    cs._BROWSE_MEMORY.update(saved)
    cs._BROWSE_MEMORY["recent"] = saved_recent


def get_test_client() -> TestClient:
    """获取标准的测试客户端"""
    return TestClient(bridge_server.app)


@pytest.fixture(autouse=True)
def _isolate_throttle_state():
    """每个用例前后快照/恢复 TG 节流的内存态（配置/预算/队列）。

    同 _isolate_browse_memory 的理由：节流是进程级全局。某个用例把日预算
    调到 1 或用掉额度后不还原，后续用例里的 /browse/download、/submit、
    缩略图补图就会被"预算耗尽"意外拦下 —— 单跑通过、合跑失败。
    """
    import core.state as cs
    saved_cfg = dict(cs._THROTTLE_CONFIG)
    saved_scope = dict(cs._THROTTLE_CONFIG.get("scope") or {})
    saved_state = dict(cs._THROTTLE_STATE)
    saved_queue = dict(cs._THROTTLE_QUEUE)
    yield
    cs._THROTTLE_CONFIG.clear()
    cs._THROTTLE_CONFIG.update(saved_cfg)
    cs._THROTTLE_CONFIG["scope"] = saved_scope
    cs._THROTTLE_STATE.clear()
    cs._THROTTLE_STATE.update(saved_state)
    cs._THROTTLE_QUEUE.clear()
    cs._THROTTLE_QUEUE.update(saved_queue)
