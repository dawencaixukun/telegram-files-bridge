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

def get_test_client() -> TestClient:
    """获取标准的测试客户端"""
    return TestClient(bridge_server.app)
