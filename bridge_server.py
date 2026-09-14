# -*- coding: utf-8 -*-
"""
bridge_server.py — Telegram Files Bridge 极简主入口与向后兼容装配层
=====================================================================
符合 P3 架构治理规范，将单体解构为 core/、services/、routers/ 三层架构。
本文件作为微内核入口 (< 300 行)，负责：
1. 100% 兼容符号重导出 (Symbol Re-exporting) 与 Mock 自动同步
2. 全局中间件挂载 (安全头、防暴力破解、CSRF 门禁)
3. 9 大领域子路由装配挂载
4. 静态资源托管与 Cache-Control 优化
5. 应用全局生命周期管理 (WS Relay、自动归档巡检、FloodWait 保护)
"""
import os
import sys
import types
import time
import asyncio
from typing import Any, Dict, List, Optional
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------
# 1. 核心层与业务服务层 100% 兼容导出
# ---------------------------------------------------------------------
from core import *
from services import *
from routers import (
    auth_router,
    dashboard_router,
    tasks_router,
    browse_router,
    library_router,
    archive_router,
    subscriptions_router,
    system_router,
    api_router,
)


# ---------------------------------------------------------------------
# 2. 静态资源托管类
# ---------------------------------------------------------------------
class CachedStaticFiles(StaticFiles):
    """带 Cache-Control 响应头的静态资源服务。"""
    async def get_response(self, path: str, scope: Any):
        response = await super().get_response(path, scope)
        if 200 <= response.status_code < 300:
            response.headers.setdefault("Cache-Control", "public, max-age=86400")
        return response


# ---------------------------------------------------------------------
# 3. FastAPI 应用实例初始化与全局中间件
# ---------------------------------------------------------------------
app = FastAPI(title="Telegram Files Bridge", docs_url=None, redoc_url=None)

# 开启智能 GZip 传输压缩（页面、JS、CSS 与 JSON 传输体积骤降 75%+，排除流媒体以保护 HTTP 206 Range 契约）
from starlette.middleware.gzip import GZipMiddleware

class SelectiveGZipMiddleware:
    def __init__(self, app, minimum_size: int = 500):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            # /preview 返回 JPEG 二进制，不压缩（原 /api/media/stream 已随本地播放器移除）
            if path.startswith("/preview/"):
                await self.app(scope, receive, send)
                return
        await self.gzip(scope, receive, send)

app.add_middleware(SelectiveGZipMiddleware, minimum_size=500)

# 静态目录挂载
if os.path.isdir(STATIC_DIR):
    app.mount("/static", CachedStaticFiles(directory=STATIC_DIR), name="static")

# 全局安全与门禁中间件
# Starlette 中间件是「后注册者在外层」：最后注册的最先执行、最后处理响应。
# 因此要让 security_headers 包裹住 portal_auth_gate（这样门禁直接返回的
# 302/403/401 响应也会带上 CSP 等安全头），必须先注册 portal_auth_gate。
app.middleware("http")(portal_auth_gate)
app.middleware("http")(security_headers)

# ---------------------------------------------------------------------
# 4. 挂载 9 大领域子路由
# ---------------------------------------------------------------------
app.include_router(dashboard_router)
app.include_router(auth_router)
app.include_router(tasks_router)
app.include_router(browse_router)
app.include_router(library_router)
app.include_router(archive_router)
app.include_router(subscriptions_router)
app.include_router(system_router)
app.include_router(api_router)

# ---------------------------------------------------------------------
# 5. 应用全局生命周期管理
# ---------------------------------------------------------------------
_relay_task: Optional[asyncio.Task] = None
_auto_archive_task: Optional[asyncio.Task] = None
_flood_wait_timer_task: Optional[asyncio.Task] = None
_bot_command_task: Optional[asyncio.Task] = None
_watch_task: Optional[asyncio.Task] = None


@app.on_event("startup")
async def _startup():
    global _relay_task, _auto_archive_task, _bot_command_task, _watch_task
    # 状态持久化文件的加载已在 core.state 模块装载时完成（state.py 末尾的
    # _flood_wait_load/_archive_load/_subs_load/... 七连调用），此处不再重复执行。
    # 旧代码在这里又跑了一遍同样的 7 次加载：既产生重复的「已恢复…」日志行，
    # 也是 FloodWait 双定时器竞态的源头之一。
    #
    # 但有一个必须补的动作：_flood_wait_load() 在导入期运行时还没有事件循环，
    # 里面的 _ensure_flood_wait_timer() 会因 get_running_loop() 抛 RuntimeError
    # 而被静默跳过。若进程是在冷却期内被重启的，active=True 却没有任何定时器在跑
    # → 调度永久挂起，直到下次显式触发 FloodWait 才恢复。这里拿到运行循环后补启动。
    _ensure_flood_wait_timer()

    # 启动后台异步中继与循环任务
    _relay_task = asyncio.create_task(ws_relay_loop())
    asyncio.create_task(BACKEND.refresh_bootstrap_status())
    _auto_archive_task = asyncio.create_task(_auto_archive_loop())
    # TG Bot 命令长轮询（/ck /yd /st /err /help）：未配置 Bot 时内部自动挂起等待
    _bot_command_task = asyncio.create_task(bot_command_loop())
    # 频道监听：按订阅规则上的 watch 开关盯住会话，新消息自动入队下载
    _watch_task = asyncio.create_task(watch_loop())


@app.on_event("shutdown")
async def _shutdown():
    global _relay_task, _auto_archive_task, _flood_wait_timer_task, _bot_command_task, _watch_task
    if _relay_task is not None and not _relay_task.done():
        _relay_task.cancel()
        try:
            await _relay_task
        except asyncio.CancelledError:
            pass
    if _auto_archive_task is not None and not _auto_archive_task.done():
        _auto_archive_task.cancel()
        try:
            await _auto_archive_task
        except asyncio.CancelledError:
            pass
    if _flood_wait_timer_task is not None and not _flood_wait_timer_task.done():
        _flood_wait_timer_task.cancel()
        try:
            await _flood_wait_timer_task
        except asyncio.CancelledError:
            pass
    if _bot_command_task is not None and not _bot_command_task.done():
        _bot_command_task.cancel()
        try:
            await _bot_command_task
        except asyncio.CancelledError:
            pass
    if _watch_task is not None and not _watch_task.done():
        _watch_task.cancel()
        try:
            await _watch_task
        except asyncio.CancelledError:
            pass

    await BACKEND.close()
    for t in list(_ARCHIVE_TASKS.values()):
        t.cancel()
    for c in (_openlist_client, _openlist_upload_client):
        try:
            await c.aclose()
        except Exception:
            pass


# ---------------------------------------------------------------------
# 6. 单测 Mock 深度透传兼容模块
# ---------------------------------------------------------------------
import routers.auth as _r_auth
import routers.dashboard as _r_dashboard
import routers.tasks as _r_tasks
import routers.browse as _r_browse
import routers.library as _r_library
import routers.archive as _r_archive
import routers.subscriptions as _r_subscriptions
import routers.system as _r_system
import routers.api as _r_api
import services.openlist_service as _s_openlist
import services.watermark_service as _s_watermark
import services.notification_service as _s_notification
import services.backup_service as _s_backup
import services.doctor_service as _s_doctor
import services.retrieve_service as _s_retrieve
import services.archive_service as _s_archive
import services.subscription_service as _s_subscription
import services.media_service as _s_media
import services.task_service as _s_task
import services.browse_service as _s_browse
import core.config as _c_config
import core.state as _c_state
import core.logging as _c_logging
import core.auth as _c_auth
import core.backend as _c_backend
import core.templates as _c_templates

_ALL_DEPENDENT_MODULES = [
    _r_auth, _r_dashboard, _r_tasks, _r_browse, _r_library,
    _r_archive, _r_subscriptions, _r_system, _r_api,
    _s_openlist, _s_watermark, _s_notification, _s_backup,
    _s_doctor, _s_retrieve, _s_archive, _s_subscription,
    _s_media, _s_task, _s_browse,
    _c_config, _c_state, _c_logging, _c_auth, _c_backend, _c_templates
]


class _BridgeCompatibilityModule(types.ModuleType):
    """透传 patch 到依赖模块并实时代理单例状态的动态兼容模块"""
    def __getattribute__(self, key: str) -> Any:
        if key.startswith("__"):
            return super().__getattribute__(key)
        if key in _c_state.__dict__:
            return getattr(_c_state, key)
        return super().__getattribute__(key)

    def __setattr__(self, key: str, value: Any) -> None:
        super().__setattr__(key, value)
        for mod in _ALL_DEPENDENT_MODULES:
            if hasattr(mod, key):
                try:
                    setattr(mod, key, value)
                except Exception:
                    pass



sys.modules[__name__].__class__ = _BridgeCompatibilityModule


if __name__ == "__main__":
    import sys
    if "--restore-session" in sys.argv:
        idx = sys.argv.index("--restore-session")
        if idx + 1 < len(sys.argv):
            target_file = sys.argv[idx + 1]
            try:
                res = restore_session_backup(target_file)
                print(f"[RESTORE OK] {res['message']}")
                sys.exit(0)
            except Exception as e:
                print(f"[RESTORE FAIL] {e}", file=sys.stderr)
                sys.exit(1)
    import uvicorn
    uvicorn.run("bridge_server:app", host=BRIDGE_HOST, port=BRIDGE_PORT, reload=False)

