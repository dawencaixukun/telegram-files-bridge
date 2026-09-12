# -*- coding: utf-8 -*-
"""
routers — 系统表现层路由集合包
==============================
装配 9 大业务领域子路由，对外统一导出。
"""
from routers.auth import router as auth_router
from routers.dashboard import router as dashboard_router
from routers.tasks import router as tasks_router
from routers.browse import router as browse_router
from routers.library import router as library_router
from routers.archive import router as archive_router
from routers.subscriptions import router as subscriptions_router
from routers.system import router as system_router
from routers.api import router as api_router

__all__ = [
    "auth_router",
    "dashboard_router",
    "tasks_router",
    "browse_router",
    "library_router",
    "archive_router",
    "subscriptions_router",
    "system_router",
    "api_router",
]
