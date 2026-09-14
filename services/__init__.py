# -*- coding: utf-8 -*-
"""
services — 系统业务领域服务层
==============================
包含 OpenList 网盘、磁盘水位保护、消息通知、Session 备份、健康检查探针、
云端取回、归档引擎、订阅规则、流媒体与任务聚合服务。
保证所有业务方法与内部辅助函数 100% 导出兼容。
"""
import importlib

_SUBMODULES = [
    "services.openlist_service",
    "services.watermark_service",
    "services.notification_service",
    "services.backup_service",
    "services.doctor_service",
    "services.retrieve_service",
    "services.archive_service",
    "services.subscription_service",
    "services.media_service",
    "services.task_service",
    "services.browse_service",
    "services.bot_command_service",
    "services.watch_service",
]

__all__ = []

for _mod_name in _SUBMODULES:
    _mod = importlib.import_module(_mod_name)
    for _k in dir(_mod):
        if not (_k.startswith("__") and _k.endswith("__")):
            globals()[_k] = getattr(_mod, _k)
            if _k not in __all__:
                __all__.append(_k)
