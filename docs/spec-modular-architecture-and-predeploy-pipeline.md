# P3 工程治理与长期维护：领域模块化物理拆解、tests套件整合与 Pre-deploy 门禁自动化流水线架构设计规范

**版本**：v1.0.0  
**状态**：定稿发布（Requirements Approved）  
**责任角色**：系统架构与全栈工程师 (`engineer`)  
**关联任务**：`t1` (架构设计与规范文档) → `t2` (物理拆解 core/ 与 services/) → `t3` (物理拆解 routers/ 与入口装配)  

---

## 1. 背景与工程治理目标

### 1.1 现状与痛点分析
当前核心入口文件 `bridge_server.py` 已膨胀至 **9,358 行代码**，单文件汇集了：
1. 全局配置、路径解析、格式化函数与常量；
2. 门户 Session 签名、CSRF 门禁中间件、防暴力破解与 IP 限流；
3. 全局单例状态字典（`_ARCHIVE_JOBS`、`_WAITING_DISK_TASKS`、`_SUB_RULES`、`_OPENLIST`、`_FLOOD_WAIT` 等）及文件持久化；
4. 内存环形日志缓冲 `LogStore` 与 SSE 事件广播；
5. Java 后端异步 HTTP/WebSocket 客户端及断线重连 Relay；
6. OpenList 网盘交互、目录探针与直接下载直链解析；
7. 文件自动归档引擎、分片重试、批量任务管理与失败诊断；
8. 云端取回（Retrieve）工作流与后台下载任务；
9. 动态目录模板引擎与订阅自动归档轮询调度；
10. HTTP 206 范围请求媒体流媒体切片分发与缩略图自愈；
11. Telegram 机器人通知与 Saved Messages 消息派发；
12. TDLib Session 加密冷备、AES-256-GCM 编解码与秒级灾难恢复；
13. VPS 磁盘动态百分比高低水位（85%/75%）熔断与应急文件释放；
14. System Doctor 全链路健康检查探针；
15. 超过 40 个页面渲染（Jinja2）与 JSON API 路由。

同时，根目录下散落了 **19 个 `test_*.py` 独立测试脚本**（共计 8,700+ 行测试代码），测试用例发现路径分散，缺乏统一的工程化门禁自动化流水线，发布部署依赖人工干预，存在误操作风险。

### 1.2 治理目标
1. **领域模块化物理拆解**：将 9,358 行单体彻底解构为职责清晰的高内聚、低耦合三层架构（`core/`、`services/`、`routers/`），并在 `bridge_server.py` 中轻量组装；
2. **100% 行为与接口兼容（零破坏性变更）**：
   - 保证 `uvicorn bridge_server:app` 启动行为 100% 一致；
   - 保持所有 HTTP / SSE / WebSocket 对外路由、请求/响应协议完全不变；
   - 保持既有 19 个测试套件中所有对 `bridge_server.*` 的全局变量读写、方法调用与 `unittest.mock.patch` 100% 兼容；
3. **测试套件归拢至 `tests/`**：将根目录下散落的 19 个测试用例文件全部规整至 `tests/` 目录，支持 `python -m unittest discover -s tests -p "test_*.py"` 一键发现执行，保持与历史用例兼容；
4. **本地一键 Pre-deploy 门禁自动化流水线**：构建四阶段前置发布门禁脚本（`scripts/pre_deploy.py` 与 `scripts/pre_deploy.sh`）：
   - **Stage 1 (Lint/Syntax/DAG)**：代码静态语法编译检查与模块单向依赖 DAG 无循环断言；
   - **Stage 2 (Unit/Regression Tests)**：全量自动化回归测试套件执行，断言 100% 绿色通过；
   - **Stage 3 (Security Audit)**：安全凭据脱敏审计、明文密码扫描与路由门禁合规检查；
   - **Stage 4 (Deploy & Restart)**：纯基于 SSH 密钥的自动化比对打包部署与 VPS 服务热重启验证。

---

## 2. 领域模块化三层架构与无循环依赖 DAG 设计

### 2.1 物理目录与模块规划

```
D:\webapi/
├── bridge_server.py          # 极简组装主入口、生命周期事件挂载与向后兼容导出层 (< 300 行)
├── core/                     # 核心基础层 (无业务逻辑，零反向依赖)
│   ├── __init__.py
│   ├── config.py             # 配置常量、路径安全校验、格式化函数、常量定义
│   ├── state.py              # 全局单例状态字典、线程/异步锁、文件持久化 load/save
│   ├── auth.py               # Session Token 签名验证、CSRF 校验、IP 限流、安全中间件
│   ├── logging.py            # LogStore 环形缓冲、日志持久化回放、Broadcaster 事件广播
│   ├── backend.py            # BackendClient 异步 HTTP 客户端、WS Relay 客户端、凭据存储
│   └── templates.py          # Jinja2 模板引擎配置、自定义过滤器、_ctx 上下文生成器
├── services/                 # 业务领域服务层 (依赖 core/，禁止依赖 routers/)
│   ├── __init__.py
│   ├── openlist_service.py   # OpenList 登录、挂载探针、文件上传、直链解析
│   ├── archive_service.py    # 归档引擎、去重检查、工作流调度、失败重试分类
│   ├── retrieve_service.py   # 云端文件取回引擎、流式写入、任务进度管理
│   ├── subscription_service.py # 目录命名模板引擎、订阅规则匹配、自动归档巡检
│   ├── media_service.py      # HTTP 206 范围媒体流传输、MIME 识别、缩略图自愈
│   ├── notification_service.py # Telegram Bot / Saved Messages 多通道通知派发
│   ├── backup_service.py     # TDLib Session 备份快照、AES-256 加解密、异地冷备
│   ├── doctor_service.py     # System Doctor 四大探针并发检测与 2.5s 超时隔离
│   └── watermark_service.py  # 磁盘 85%/75% 动态水位监控、应急文件释放与任务挂起/唤醒
├── routers/                  # 表现层路由 (按业务领域拆分，依赖 services/ 与 core/)
│   ├── __init__.py
│   ├── auth.py               # /login, /logout, /init, /auth/password
│   ├── dashboard.py          # /, /health
│   ├── tasks.py              # /tasks, /tasks/{task_id}, /partials/tasks, /task/retry, /task/cancel, /submit
│   ├── browse.py             # /browse, /partials/browse-files, /browse/download
│   ├── library.py            # /library/local, /library/cloud, /partials/local-files, 删除与取回接口
│   ├── archive.py            # /archive/start, /archive/status, /archive/cancel, /api/archive/*
│   ├── subscriptions.py      # /subscriptions, /api/subscriptions/*
│   ├── system.py             # /settings, /logs, /account, /openlist/*, /tg-login/*, /api/system/doctor 等
│   └── api.py                # /api/search, /api/files/check-dedup, /api/tg/*
├── tests/                    # 测试套件归拢目录 (整合 19 个 test_*.py 文件)
│   ├── __init__.py
│   ├── conftest.py           # 公共 TestClient 与 Fixtures
│   ├── test_advanced_features.py
│   ├── test_deep_features.py
│   ├── test_library_enhancements.py
│   ├── ... (所有 19 个完整测试文件)
├── scripts/                  # 运维与自动化流水线脚本
│   ├── pre_deploy.py         # 本地一键 Pre-deploy 门禁校验流水线主程序
│   └── pre_deploy.sh         # Bash 门禁触发包装器
├── .vps-conn/                # VPS SSH 免密通道配置与远程部署工具
│   ├── vps.json              # 剥离明文密码的 VPS 配置
│   ├── deploy.js             # 纯私钥 SFTP 增量部署与服务热重启脚本
│   └── exec.js               # 纯私钥远程执行脚本
├── static/                   # 静态前端资源 (css, js, icons)
└── templates/                # Jinja2 HTML 模板
```

### 2.2 严格单向导入 DAG（杜绝循环依赖）

```
                     +---------------------------------------+
                     |            bridge_server.py           |
                     +-------------------+-------------------+
                                         |
                                         v
                     +---------------------------------------+
                     |                routers/               |
                     |  (auth, dashboard, tasks, browse,     |
                     |   library, archive, subs, system, api)|
                     +-------------------+-------------------+
                                         |
                                         v
                     +---------------------------------------+
                     |               services/               |
                     |  (openlist, archive, retrieve, subs,  |
                     |   media, notify, backup, doctor, disk)|
                     +-------------------+-------------------+
                                         |
                                         v
                     +---------------------------------------+
                     |                 core/                 |
                     |  (config -> state -> logging ->       |
                     |   auth -> backend -> templates)       |
                     +---------------------------------------+
```

**依赖流向红线约束**：
1. **禁止逆向依赖**：
   - `core/` 严禁 `import services.*` 或 `import routers.*` 或 `import bridge_server`；
   - `services/` 严禁 `import routers.*` 或 `import bridge_server`；
   - `routers/` 严禁 `import bridge_server`；
2. **禁止横向循环**：
   - `routers/` 各子模块之间相互独立，禁止路由互相交叉导入；公共逻辑全部下沉至 `services/`；
   - `services/` 各服务之间若存在协作，遵循确定性的上层服务依赖下层服务模型：
     - `subscription_service` → `archive_service`
     - `archive_service` → `openlist_service`, `watermark_service`, `notification_service`
     - `retrieve_service` → `openlist_service`
     - `doctor_service` → `openlist_service`, `core.backend`

---

## 3. 核心模块详细设计与职责划分

### 3.1 `core/` 基础核心层

| 模块文件 | 包含核心符号与职责 | 依赖项 |
| :--- | :--- | :--- |
| `core/config.py` | 全局基础配置参数、常量与纯函数：<br>• 端口与网络地址：`BRIDGE_HOST`, `BRIDGE_PORT`, `TG_API_URL`, `TG_WS_URL`<br>• 鉴权凭据常量：`OPENLIST_BASE_URL`, `PORTAL_USER`, `PORTAL_PASS`, `PORTAL_COOKIE`, `PORTAL_TTL`<br>• 目录与限额：`APP_ROOT_DIR`, `BASE_DIR`, `TG_API_METHOD_WHITELIST`, `_SESSION_BACKUP_MAX_KEEP`<br>• 纯格式化与安全函数：`_fmt_size`, `_fmt_time`, `_fmt_dur`, `_human_to_bytes`, `_sum_human`, `_mask_secret`, `_mask_token`, `_clean_archive_filename`, `_is_safe_subpath`, `_resolve_host_local_path`, `_norm_remote_path` | 仅 Python 标准库 (`os`, `sys`, `re`, `pathlib` 等) |
| `core/state.py` | 全局单例状态字典、线程/异步安全锁与磁盘 JSON 持久化：<br>• 单例状态：`_ARCHIVE_JOBS`, `_WAITING_DISK_TASKS`, `_SUB_RULES`, `_OPENLIST`, `_ALERTS`, `_ARCHIVE_CFG`, `_NOTIFY_CFG`, `_SESSION_BACKUP_STATUS`, `_FLOOD_WAIT`, `_RETRIEVE_JOBS`, `_TASK_CACHE`, `_LOGIN_FAILURES`<br>• 锁与同步对象：`_archive_lock`, `_subs_lock`, `_disk_lock`, `_openlist_lock`<br>• 持久化方法：`_flood_wait_save/load`, `_waiting_disk_save/load`, `_archive_load/save`, `_subs_load/save`, `_notify_config_load/save`, `_openlist_load/save/clear`<br>• FloodWait 定时器与控制：`_trigger_flood_wait`, `_is_flood_wait_active`, `_reset_flood_wait`, `_flood_wait_timer_loop` | `core.config` |
| `core/logging.py` | 统一日志存储、缓冲与实时推送：<br>• `LogStore`：内存定长环形缓冲 + 磁盘持久化追加 + 历史日志查询与级别过滤<br>• `_LogStoreHandler`：标准 Python `logging` 处理器，桥接根日志与 `LogStore`<br>• `Broadcaster`：用于 SSE `/sse/logs` 与 `/sse/tasks` 客户端连接池管理与异步广播 | `core.config` |
| `core/auth.py` | 身份认证凭据签名、防暴破风控与网关中间件：<br>• `_portal_secret()`, `_sign()`, `_make_portal_token()`, `_verify_portal_token()`<br>• IP 访问风控：`_ip_in_networks`, `_client_ip`, `_prune_login_failures`, `_login_blocked`, `_record_login_failure`, `_clear_login_failures`<br>• 端点白名单：`_is_public()`, `_init_gate_open()`, `_is_initialized()`, `_mark_initialized()`<br>• 核心中间件：`security_headers()`, `portal_auth_gate()`, `_set_portal_cookie()` | `core.config`, `core.logging` |
| `core/backend.py` | Java 后端通信与 TDLib 接口封装：<br>• `BackendClient` 类：处理所有与 `:8123` 的异步 HTTP 通信、Cookie 维持与自动重连<br>• 单例实例：`BACKEND = BackendClient(TG_API_URL)`<br>• 后端凭据落盘与恢复：`_save_backend_credentials`, `_load_backend_credentials`<br>• WebSocket Relay 引擎：`_ws_consume`, `ws_relay_loop`, `_ws_reconnect_loop`, `_ws_cookie_header` | `core.config`, `core.state`, `core.logging` |
| `core/templates.py` | Jinja2 模板渲染集成：<br>• 模板全局过滤器挂载 (`fmt_size`, `fmt_time`, `fmt_dur`, `stages`)<br>• 上下文注入辅助函数 `_ctx()`：包含导航高亮、告警摘要、版本与未读消息状态 | `core.config`, `core.auth`, `core.state`, `core.logging` |

---

### 3.2 `services/` 业务引擎服务层

| 服务模块 | 核心职责与封装接口 | 协作依赖 |
| :--- | :--- | :--- |
| `services/openlist_service.py` | OpenList 网盘集成服务：<br>• `_openlist_api_login()` / `_openlist_verify_token()` / `_openlist_status()`<br>• `_openlist_ready()` / `_openlist_token()` / `_openlist_relogin()`<br>• `_openlist_mkdir_tree()` / `_openlist_exists()` / `_openlist_put_once()`<br>• `openlist_dirs()` / `_openlist_direct_url()` / `openlist_stream_url()` | `core.config`, `core.state`, `core.logging` |
| `services/archive_service.py` | 归档工作流调度引擎：<br>• 任务状态查询与去重：`_archive_registry_lookup`, `_check_file_dedup`<br>• 任务执行核心：`_archive_worker()`, `_archive_public()`, `_archive_state_of()`<br>• 批量归档与重试：`_classify_archive_error`, `api_archive_failed_summary`, `_safe_delete_local_path`<br>• 自动归档扫描：`archive_sweep()` | `core.*`, `services.openlist_service`, `services.watermark_service`, `services.notification_service` |
| `services/retrieve_service.py` | 云端反向取回服务：<br>• `_retrieve_worker()`：将 OpenList 云端文件以分片流式下载回本地 VPS<br>• `_retrieve_public()`：公开状态视图脱敏<br>• 任务取消与清理：`retrieve_cancel()`, `retrieve_status()` | `core.*`, `services.openlist_service` |
| `services/subscription_service.py` | 自动化订阅与规则引擎：<br>• 路径模板渲染：`_render_dir_template()` (支持 `{source}`, `{YYYY-MM}`, `{chat_title}`, `{resolution}`, `{ext}` 等清洗及防路径穿越注入)<br>• 规则管理：`_sub_rules_sorted()`, `_sub_match_rule()`, `_sub_bump()`, `_sub_public()`<br>• 后台巡检循环：`_auto_archive_sweep()`, `_auto_archive_loop()` | `core.*`, `services.archive_service` |
| `services/media_service.py` | 媒体预览与流式切片分发服务：<br>• HTTP 206 范围请求解析与生成器：`_parse_http_range()`, `_stream_file_generator()`<br>• MIME 嗅探与媒体信息定位：`_detect_media_mime()`, `_resolve_media_record()`<br>• 缩略图自愈机制：`_heal_thumbnail()` | `core.config`, `core.state`, `core.backend`, `core.logging` |
| `services/notification_service.py` | 多通道实时消息通知服务：<br>• 通知派发总线：`_dispatch_notification()`<br>• 通道实现：Telegram Bot API (`_send_via_bot`) 与 Saved Messages (`_send_via_saved_messages`)<br>• 业务事件触发器：`notify_download_completed`, `notify_archive_success`, `notify_archive_failed`, `notify_disk_watermark_alert` | `core.config`, `core.state`, `core.logging`, `core.backend` |
| `services/backup_service.py` | VPS 会话快照异地冷备引擎：<br>• 会话目录安全扫描与过滤：`_scan_session_files()`<br>• 归档打包与 AES-256-GCM AEAD 加解密：`_create_session_archive_bytes()`, `_encrypt_session_payload()`, `_decrypt_session_payload()`<br>• 本地轮转与 OpenList 异地冷备上传：`_rotate_local_backups()`, `_upload_session_backup_to_openlist()`<br>• 一键解密还原命令：`restore_session_backup()` | `core.config`, `core.state`, `core.logging`, `services.openlist_service` |
| `services/doctor_service.py` | System Doctor 全链路健康检查探针服务：<br>• 并发探针：`_doctor_probe_java_backend()`, `_doctor_probe_tdlib()`, `_doctor_probe_openlist()`, `_doctor_probe_local_storage()`<br>• 探针容灾与超时隔离：单项探针 2.5s 超时切断，并发 `asyncio.gather(*probes, return_exceptions=True)` 严格保证总耗时 < 3.0s<br>• 诊断聚合：`_run_system_doctor_check()` | `core.config`, `core.state`, `core.backend`, `services.openlist_service` |
| `services/watermark_service.py` | VPS 磁盘动态水位保护服务：<br>• 水位指标采集：`_get_disk_free_gb()`, `_get_disk_usage_percent()`<br>• 85% 高水位熔断拦截与任务入队：`_is_disk_high_watermark_exceeded()`, `_enqueue_waiting_disk_files/links()`<br>• 75% 低水位自动唤醒：`_is_disk_low_watermark_reached()`, `_check_and_wake_waiting_disk_tasks()`<br>• 应急清理：`_disk_guard_check()` 释放最早已归档的本地文件 | `core.config`, `core.state`, `core.logging`, `services.notification_service` |

---

### 3.3 `routers/` 表现层路由模块

按业务领域拆分，每个子路由采用 `APIRouter()` 进行定义，独立维护路由前缀与业务处理：

1. `routers/auth.py`：`/login` (GET/POST), `/logout` (POST), `/init` (GET/POST), `/auth/password` (POST)；
2. `routers/dashboard.py`：`/` (GET 仪表盘视图), `/health` (GET 存活探针)；
3. `routers/tasks.py`：`/tasks` (GET 列表), `/tasks/{task_id}` (GET 详情), `/partials/tasks` (局部刷新), `/tasks` (POST 提交), `/task/retry`, `/task/cancel`, `/submit` (GET/POST)；
4. `routers/browse.py`：`/browse` (GET 资源浏览), `/partials/browse-files`, `/browse/download` (POST 触发转存)；
5. `routers/library.py`：`/library/local`, `/library/cloud`, `/partials/local-files`, `/library/local/delete`, `/api/local/delete`, `/library/cloud/delete`, `/library/cloud/clear-missing`, `/library/cloud/retrieve`, `/library/cloud/retrieve/status`, `/library/cloud/retrieve/cancel`, `/library/cloud/files`；
6. `routers/archive.py`：`/archive/start`, `/archive/status`, `/archive/cancel`, `/archive/batch`, `/api/archive/failed-summary`, `/api/archive/failed`, `/api/archive/retry-failed`, `/api/archive/batch-retry`, `/archive/config` (GET/POST), `/archive/sweep` (POST)；
7. `routers/subscriptions.py`：`/subscriptions` (GET 规则页), `/api/subscriptions` (GET/POST), `/api/subscriptions/rule`, `/api/subscriptions/update`, `/api/subscriptions/reorder`, `/api/subscriptions/preview-template`, `/api/subscriptions/delete`, `/api/subscriptions/run`；
8. `routers/system.py`：`/settings` (GET/POST), `/settings/load`, `/settings/save`, `/logs`, `/api/logs`, `/api/logs.txt`, `/sse/logs`, `/sse/tasks`, `/account`, `/profile`, `/alerts/read`, `/openlist/status`, `/openlist/login`, `/openlist/logout`, `/openlist/dirs`, `/openlist/direct-url`, `/openlist/stream-url`, `/tg-login` (GET/POST 各步骤), `/api/system/doctor`, `/api/doctor/check`, `/api/system/doctor/ping`, `/api/session/backup` (POST), `/api/session/backup/status` (GET), `/api/disk/watermark/status` (GET), `/api/disk/wake` (POST), `/api/notify/config` (GET/POST), `/api/notify/test` (POST)；
9. `routers/api.py`：`/api/search`, `/api/search/aggregate`, `/api/files/check-dedup`, `/api/tg/resolve-link`, `/api/tg/quick-download`, `/api/tg/floodwait/status`, `/api/tg/floodwait/reset`, `/preview/{telegram_id}/{unique_id}`。（原 `/api/media/info`、`/api/media/stream` 本地流式播放端点已随「边下边播」特性整体移除。）

---

## 4. 主入口 `bridge_server.py` 轻量装配与向后兼容设计

### 4.1 轻量装配主结构 (< 300 行)
`bridge_server.py` 仅保留以下生命周期与应用装配逻辑：
```python
# bridge_server.py 伪代码示意
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from core.config import BRIDGE_HOST, BRIDGE_PORT, STATIC_DIR
from core.auth import security_headers, portal_auth_gate
from core.state import _openlist_load, _archive_load, _notify_config_load, _subs_load, _waiting_disk_load, _flood_wait_load
from core.backend import ws_relay_loop
from services.subscription_service import _auto_archive_loop
from core.state import _flood_wait_timer_loop
from routers import (
    auth_router, dashboard_router, tasks_router, browse_router,
    library_router, archive_router, subscriptions_router,
    system_router, api_router
)

app = FastAPI(title="Telegram Files Bridge", docs_url=None, redoc_url=None)

# 1. 静态资源挂载
app.mount("/static", CachedStaticFiles(directory=STATIC_DIR), name="static")

# 2. 全局中间件挂载
app.middleware("http")(security_headers)
app.middleware("http")(portal_auth_gate)

# 3. 领域子路由包含
app.include_router(dashboard_router)
app.include_router(auth_router)
app.include_router(tasks_router)
app.include_router(browse_router)
app.include_router(library_router)
app.include_router(archive_router)
app.include_router(subscriptions_router)
app.include_router(system_router)
app.include_router(api_router)

# 4. 生命周期管理
@app.on_event("startup")
async def _startup():
    _openlist_load()
    _archive_load()
    _notify_config_load()
    _subs_load()
    _waiting_disk_load()
    _flood_wait_load()
    asyncio.create_task(ws_relay_loop())
    asyncio.create_task(_auto_archive_loop())
    asyncio.create_task(_flood_wait_timer_loop())

@app.on_event("shutdown")
async def _shutdown():
    ...
```

### 4.2 100% 兼容导出层（Symbol Re-exporting）
既有测试用例（如 `test_library_enhancements.py`、`test_deep_features.py`、`test_advanced_features.py`）大量使用：
- `import bridge_server`
- `bridge_server.BACKEND` / `bridge_server._ARCHIVE_JOBS` / `bridge_server._WAITING_DISK_TASKS` / `bridge_server._OPENLIST`
- `bridge_server.PORTAL_COOKIE` / `bridge_server._make_portal_token()`
- `patch.object(bridge_server.BACKEND, "telegram_api", ...)`
- `patch("bridge_server._openlist_ready", ...)`

为了确保重构过程中及重构完成后零测试用例破坏，`bridge_server.py` 在模块顶部显式 re-export 所有底层变量和函数引用：
```python
# 核心状态与单例（保证内存对象同一性）
from core.config import *
from core.state import *
from core.auth import *
from core.logging import *
from core.backend import *
from core.templates import *
from services.openlist_service import *
from services.archive_service import *
from services.retrieve_service import *
from services.subscription_service import *
from services.media_service import *
from services.notification_service import *
from services.backup_service import *
from services.doctor_service import *
from services.watermark_service import *
```
这样既保证了 `uvicorn bridge_server:app` 启动完全透明，又保证了所有测试代码无感知，平滑过渡。

---

## 5. 测试套件归拢至 `tests/` 方案

### 5.1 目录迁移映射表
将根目录下 19 个散落测试文件统一平移至 `tests/` 目录：
- `./test_adv_features_arch_review_t7.py` → `tests/test_adv_features_arch_review_t7.py`
- `./test_advanced_features.py` → `tests/test_advanced_features.py`
- `./test_architecture_review_t7.py` → `tests/test_architecture_review_t7.py`
- `./test_archive_status_dedup.py` → `tests/test_archive_status_dedup.py`
- `./test_deep_features.py` → `tests/test_deep_features.py`
- `./test_disk_watermark.py` → `tests/test_disk_watermark.py`
- `./test_e2e_security_review.py` → `tests/test_e2e_security_review.py`
- `./test_frontend_browse.py` → `tests/test_frontend_browse.py`
- `./test_frontend_cloud.py` → `tests/test_frontend_cloud.py`
- `./test_frontend_local.py` → `tests/test_frontend_local.py`
- `./test_library_enhancements.py` → `tests/test_library_enhancements.py`
- `./test_link_download.py` → `tests/test_link_download.py`
- `./test_security_audit.py` → `tests/test_security_audit.py`
- `./test_security_audit_t5.py` → `tests/test_security_audit_t5.py`
- `./test_security_audit_t7.py` → `tests/test_security_audit_t7.py`
- `./test_security_audit_t8.py` → `tests/test_security_audit_t8.py`
- `./test_session_backup.py` → `tests/test_session_backup.py`
- `./test_ssh_hardening.py` → `tests/test_ssh_hardening.py`
- `./test_subscriptions_e2e.py` → `tests/test_subscriptions_e2e.py`

### 5.2 兼容性与运行方式保障
1. 新增 `tests/__init__.py`，使 `tests` 成为合法测试包；
2. 统一测试执行入口：
   ```bash
   python -m unittest discover -s tests -p "test_*.py"
   ```
3. 在 `tests/conftest.py`（或测试基类模块）中自动注入工作区根目录到 `sys.path`，确保 `import bridge_server`, `from core import ...`, `from services import ...` 无论从任何路径执行都能稳定解析；
4. 修复当前已知测试环境小缺陷（如 `_doctor_probe_tdlib` 对 `BACKEND.telegram_api` 的调用适配，以及 SSH 部署超时优化），确保测试集 100% 绿色全过。

---

## 6. 本地一键 Pre-deploy 门禁自动化流水线设计

构建统一的 Pre-deploy 自动化质量门禁流水线脚本：
- `scripts/pre_deploy.py` (跨平台 Python 门禁核心实现)
- `scripts/pre_deploy.sh` (POSIX/Git-Bash 便捷一键执行脚本)

```
+-----------------------------------------------------------------------------------------+
|                    Local One-Click Pre-deploy Quality Pipeline                          |
|                                                                                         |
|  [Stage 1] 语法与单向 DAG 静态检查                                                      |
|     ├── py_compile: bridge_server.py, core/*.py, services/*.py, routers/*.py, tests/*.py|
|     └── AST Import DAG Check: core 零反向导入，services 不得导 routers，杜绝循环依赖     |
|                                     │ PASS                                              |
|                                     ▼                                                   |
|  [Stage 2] 全量测试套件自动化回归检验                                                   |
|     ├── python -m unittest discover -s tests -p "test_*.py"                             |
|     └── 断言 220+ 测试用例 100% 成功，0 Failures, 0 Errors, 0 Regressions                |
|                                     │ PASS                                              |
|                                     ▼                                                   |
|  [Stage 3] 安全凭据审计与权限门禁校验                                                   |
|     ├── .vps-conn/vps.json 严格核验：严禁出现 password / passwd 明文口令                |
|     ├── 敏感代码静态扫描：无 eval/exec、无 shell=True 注入、无硬编码密钥                |
|     └── 路由权限门禁核查：所有新增/拆分子路由均包含在 portal_auth_gate 与 CSRF 保护范围内   |
|                                     │ PASS                                              |
|                                     ▼                                                   |
|  [Stage 4] 自动化增量部署与热重启验证                                                   |
|     ├── 校验待部署清单（包含拆分后的 core/, services/, routers/ 模块目录）              |
|     ├── 调用 node .vps-conn/deploy.js 通过 SSH 私钥 SFTP 增量推送至 VPS /root/tg-files/app|
|     └── 远程执行 systemctl restart tg-bridge，断言服务热重启成功并返回 ACTIVE              |
+-----------------------------------------------------------------------------------------+
```

### 6.1 四阶段门禁规范定义

#### 阶段一：静态语法编译与架构 DAG 校验 (Stage 1: Lint, Syntax & DAG Audit)
- **输入**：全部 Python 源码及测试代码；
- **校验内容**：
  1. 执行 `python -m py_compile <all_files>`，确保无任何 SyntaxError、IndentationError 或 Python 3.14 语法兼容性问题；
  2. 使用 `ast` 模块解析所有文件的 `import` 与 `from ... import` 依赖关系图；
  3. 严格断言 DAG 规则：
     - `core/` 下所有文件不得依赖 `services`、`routers` 或 `bridge_server`；
     - `services/` 下所有文件不得依赖 `routers` 或 `bridge_server`；
     - `routers/` 各文件间不得交叉导入；
     - 整个包依赖有向图不存在任何环路（Acyclicity Assertion）。
- **失败策略**：输出具体违规导入路径及行号，立即终止流水线。

#### 阶段二：全量测试套件回归检验 (Stage 2: Full Automated Test Suite)
- **输入**：`tests/` 目录下全部 19 个测试套件；
- **校验内容**：
  1. 调用 `python -m unittest discover -s tests -p "test_*.py"`；
  2. 统计测试总数（预计 220+ 用例）、耗时及结果；
  3. 断言 `failures == 0` 且 `errors == 0`。
- **失败策略**：打印详细 Traceback 和失败用例清单，立即终止流水线。

#### 阶段三：安全凭据审计与端点防护核查 (Stage 3: Security & Credential Gate)
- **输入**：项目全部配置文件与源码；
- **校验内容**：
  1. 深度扫描 `.vps-conn/vps.json`，断言绝对不含 `password` / `passwd` 键；
  2. 源码特征扫描：确保不存在明文写入的云存储 API Token、Bot Token，确保无非法的动态执行语法；
  3. 路由安全矩阵核查：核查 `core.auth.portal_auth_gate` 中 `_is_public()` 白名单，确保所有管理与业务子路由（`/browse/*`, `/library/*`, `/archive/*`, `/subscriptions/*`, `/api/*` 等）均在受保护受审计范围。
- **失败策略**：输出安全告警与文件定位，拒绝发布。

#### 阶段四：安全增量部署与热重启验证 (Stage 4: Automated Deploy & Service Verification)
- **输入**：更新后的 `.vps-conn/deploy.js` 文件部署映射列表；
- **校验内容**：
  1. 自动同步更新 `deploy.js` 中的 `FILES` 数组，加入 `core/`, `services/`, `routers/` 各新增模块文件；
  2. 调用 `node .vps-conn/deploy.js`，纯基于 SSH 私钥（ED25519/RSA）向 VPS SFTP 增量推送；
  3. 监控 VPS 远端执行 `systemctl restart tg-bridge`，断言返回状态码 0 并输出 `tg-bridge RESTARTED SUCCESSFULLY`；
  4. 支持 `--skip-deploy` 参数用于本地预检模式，只有明确提供 `--deploy` 时才执行远程推送。

---

## 7. 演进与实施步骤分解

1. **Task t1（当前阶段）**：完成本架构与流水线设计规范文档编写与定稿审议；
2. **Task t2**：
   - 物理抽取 `core/`（`config.py`, `state.py`, `logging.py`, `auth.py`, `backend.py`, `templates.py`）；
   - 物理抽取 `services/`（`openlist_service.py`, `archive_service.py`, `retrieve_service.py`, `subscription_service.py`, `media_service.py`, `notification_service.py`, `backup_service.py`, `doctor_service.py`, `watermark_service.py`）；
   - 在 `bridge_server.py` 中维持兼容导出，运行测试检验 core/services 的正确性与无环依赖；
3. **Task t3**：
   - 物理抽取 `routers/`（`auth.py`, `dashboard.py`, `tasks.py`, `browse.py`, `library.py`, `archive.py`, `subscriptions.py`, `system.py`, `api.py`）；
   - 重构 `bridge_server.py` 为极简组装器；
   - 归拢测试文件至 `tests/`；
   - 编写 `scripts/pre_deploy.py` 与 `scripts/pre_deploy.sh`，并在 `.vps-conn/deploy.js` 中更新待部署模块清单；
   - 运行 Pre-deploy 全流程流水线验证，确保质量门禁 100% 绿色全过。
