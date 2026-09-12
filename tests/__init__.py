# -*- coding: utf-8 -*-
"""
tests — 全量自动化测试套件
========================
整合 19 大测试模块，自动配置工作区根路径至 sys.path。
"""
import os
import sys

_WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, _WORKSPACE_ROOT)
