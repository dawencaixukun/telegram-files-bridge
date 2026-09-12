# -*- coding: utf-8 -*-
"""
core — 系统底层核心包
====================
提供配置、状态单例、日志存储、凭据网关、后端客户端与模板环境。
保证所有内部变量与工具函数 100% 导出兼容。
"""
import importlib

_SUBMODULES = [
    "core.config",
    "core.state",
    "core.logging",
    "core.auth",
    "core.backend",
    "core.templates",
]

__all__ = []

for _mod_name in _SUBMODULES:
    _mod = importlib.import_module(_mod_name)
    for _k in dir(_mod):
        if not (_k.startswith("__") and _k.endswith("__")):
            globals()[_k] = getattr(_mod, _k)
            if _k not in __all__:
                __all__.append(_k)
