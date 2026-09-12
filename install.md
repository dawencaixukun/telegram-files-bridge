# Telegram Files Bridge 系统部署与生产运维指南 (install.md)

本文档是为自动化运维 Agent、系统工程师及运维人员编写的生产级部署与全生命周期运维指南。系统已完成三层解耦架构重构（`core` / `services` / `routers`），本文档涵盖 **Linux VPS** 与 **Windows** 环境下的完整安装配置、服务常驻、网络安全加固、双向 Telegram Bot 交互、自动化备份自愈及故障排查体系。

---

## 1. 系统架构与服务拓扑

系统由微内核网关服务、下载核心引擎容器、网盘聚合挂载服务及可选反向代理层协同工作：

```
+---------------------------------------------------------------------------------------+
|                                外部用户 / 浏览器 (HTTPS/WSS/SSE)                      |
+-------------------------------------------+-------------------------------------------+
                                            |
                                            v (80 / 443)
+---------------------------------------------------------------------------------------+
|                    反向代理层 (Caddy / Nginx) [生产强烈推荐]                           |
|      - TLS 终止、WebSocket 升级、SSE 禁用缓冲 (proxy_buffering off)、静态资源长缓存    |
+-------------------------------------------+-------------------------------------------+
                                            |
                                            v (反代至 127.0.0.1:8000)
+---------------------------------------------------------------------------------------+
|              tg-bridge 服务网关 (Python 3.10+ / FastAPI / Uvicorn / Jinja2)            |
|   - 核心微内核入口: bridge_server.py                                                  |
|   - 架构分层:                                                                         |
|     • core/     : 配置、HMAC/CSRF 门禁、Vert.x 后端通信、LogStore 环形日志落盘        |
|     • services/ : 12 大领域服务 (任务调度、测速、归档、备份还原、水位、风控、Bot交互等)|
|     • routers/  : 9 大解耦子路由 (dashboard, auth, tasks, browse, library, system 等) |
|   - 默认监听: 127.0.0.1:8000                                                          |
+-------------------+---------------------------------------+---------------------------+
                    |                                       |
                    v (HTTP & WS :8123)                     v (REST API :5244)
+---------------------------------------+   +-------------------------------------------+
| tg-files-api (TDLib/Vert.x 核心容器)   |   | openlist 网盘挂载服务 (Go 容器/进程)        |
| - 镜像: ghcr.io/jarvis2f/telegram-files|   | - 端口: 5244                              |
| - 职责: TDLib 账户状态、下载任务执行  |   | - 职责: 异地冷备存储、网盘归档 (115/阿里等)|
| - 数据目录挂载: APP_ROOT_DIR          |   | - 异地冷备目录: /TG-Backups/              |
+---------------------------------------+   +-------------------------------------------+
                    |                                       ^
                    | (读写共享卷)                          | (双向交互长轮询)
+-------------------+-----------------------------------+   |
| 宿主机数据持久化目录 (APP_ROOT_DIR)                   |   | +-----------------------+
| 默认 Linux: /root/tg-files/app-data                   |   +-| Telegram Bot 命令交互 |
| 默认 Windows: C:\tg-data\app-data                     |     | /ck /yd /st /err /help|
|   ├── account/<account_id>/    <- TDLib 授权密钥本体  |     +-----------------------+
|   │     └── td.binlog          <- (授权核心，严禁误删)|
|   ├── session-backups/         <- 本地加密快照冷备目录|
|   ├── .backend_creds           <- Java 后端认证凭据   |
|   ├── .bridge_secret           <- Portal 密钥(0600)   |
|   ├── .bridge_initialized      <- 首启完成标记        |
|   ├── .subscriptions.json      <- 频道订阅归档规则    |
|   ├── .archive_jobs.json       <- 云端归档任务队列    |
|   ├── .archive_config.json     <- 归档与清理配置      |
|   ├── .openlist_auth           <- OpenList 登录令牌   |
|   ├── .notify_config.json      <- Telegram 通知与配置 |
|   ├── .waiting_disk.json       <- 磁盘水位挂起任务    |
|   ├── .flood_wait_state.json   <- TDLib 防风控冷却状态|
|   └── logs/                    <- 运行日志持久化目录  |
+-------------------------------------------------------+
```

---

## 2. 环境变量与配置规范

`tg-bridge` 通过环境变量进行全局配置，若未指定则自动回退至缺省值：

| 环境变量名 | 默认值 | 类型 | 说明 | 生产推荐设置 |
| :--- | :--- | :--- | :--- | :--- |
| `BRIDGE_HOST` | `127.0.0.1` | String | Bridge 服务监听地址 | 配合反代设为 `127.0.0.1`；无反代直连可设为 `0.0.0.0` |
| `BRIDGE_PORT` | `8000` | Int | Bridge 服务 HTTP 端口 | `8000` |
| `TG_DATA_DIR` | `/root/tg-files/app-data` | Path | 宿主机应用数据根目录 (`APP_ROOT_DIR`) | Linux: `/root/tg-files/app-data`<br>Windows: `C:\tg-data\app-data` |
| `TG_API_URL` | `http://127.0.0.1:8123/api` | URL | TDLib 后端核心 API 地址 | `http://127.0.0.1:8123/api` |
| `OPENLIST_URL` | `http://127.0.0.1:5244` | URL | OpenList 网盘服务地址 | `http://127.0.0.1:5244` |
| `BRIDGE_SECURE_COOKIE` | `""` | Bool(0/1) | Cookie `Secure` 标记 | 生产环境启用 HTTPS 时必须设置为 `1` |
| `BRIDGE_TRUSTED_PROXIES` | `""` | String | 受信任反代 IP 清单（逗号分隔） | 例如 `127.0.0.1,10.0.0.0/8` |
| `BRIDGE_CACHE_TTL` | `8` | Float | 任务与列表数据短缓存（秒） | `8` |
| `BRIDGE_WS_DELAY` | `3` | Float | WebSocket 重连等待间隔（秒） | `3` |
| `BRIDGE_LOGIN_LIMIT` | `10` | Int | 登录防暴破窗口最大尝试次数 | `10` |
| `BRIDGE_LOGIN_WINDOW` | `300` | Float | 登录防暴破统计窗口（秒） | `300` (5分钟) |
| `BRIDGE_AUTO_ARCHIVE_INTERVAL` | `30` | Float | 订阅频道自动归档巡检间隔（秒） | `30` ~ `60` |
| `TG_SESSION_BACKUP_KEY` | *(空)* | String | Session 备份加密主密钥（留空自动使用 `.bridge_secret`） | 生产可选自定义 32 位随机字符 |
| `TG_SESSION_BACKUP_REMOTE_DIR` | `/TG-Backups` | Path | OpenList 异地备份保存目标目录 | `/TG-Backups` |
| `TG_SESSION_BACKUP_LOCAL_DIR` | *(空)* | Path | 本地冷备存放目录（留空为 `{TG_DATA_DIR}/session-backups`） | 推荐独立挂载目录如 `/data/tg-backups` |
| `TG_BACKUP_CONTAINER` | `tg-files-api` | String | 还原会话时需停写的 Docker 容器名 | `tg-files-api` |
| `TG_BACKUP_PRERESTORE` | `1` | Bool(0/1) | 还原操作前是否自动生成回滚快照 | `1`（强制开启回滚保护） |
| `PYTHONUNBUFFERED` | `1` | Bool(0/1) | 禁用 Python 标准输出缓冲 | 容器与 Systemd 环境设为 `1` |

---

## 3. Linux VPS 部署流程 (Ubuntu / Debian / CentOS / Rocky)

### 3.1 系统前置依赖安装

在 VPS 上执行（以 root 或具备 sudo 权限的用户执行）：

```bash
# Ubuntu / Debian
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git curl tar docker.io docker-compose-plugin

# CentOS / Rocky Linux / RHEL / AlmaLinux
dnf install -y python3 python3-pip git curl tar docker docker-compose-plugin
systemctl enable --now docker
```

### 3.2 规划并创建工作目录与权限加固

```bash
# 1. 创建应用代码、数据持久化与网盘目录
mkdir -p /root/tg-files/app
mkdir -p /root/tg-files/app-data
mkdir -p /root/tg-files/session-backups
mkdir -p /root/tg-files/openlist-data

# 2. 权限安全加固 (防止其他系统本地进程窥探 TDLib 会话密钥与认证凭据)
chmod 700 /root/tg-files
chmod 700 /root/tg-files/app-data
chmod 700 /root/tg-files/session-backups
```

### 3.3 启动后端核心与 OpenList (Docker Compose 方式)

在 `/root/tg-files/docker-compose.yml` 写入如下编排配置：

```yaml
version: "3.8"

services:
  # TDLib 下载引擎后端 (Java / Vert.x)
  tg-files-api:
    image: ghcr.io/jarvis2f/telegram-files:latest
    container_name: tg-files-api
    restart: always
    network_mode: "host"
    environment:
      - SERVER_PORT=8123
      - APP_DATA_DIR=/app/data
      # 如有申请专属 Telegram API 凭据可在下方指定，否则采用镜像默认值
      # - TELEGRAM_API_ID=your_api_id
      # - TELEGRAM_API_HASH=your_api_hash
    volumes:
      - /root/tg-files/app-data:/app/data

  # OpenList 网盘聚合挂载系统
  openlist:
    image: openlistteam/openlist:latest
    container_name: openlist
    restart: always
    network_mode: "host"
    volumes:
      - /root/tg-files/openlist-data:/data
```

启动容器集群并验证服务探针：

```bash
cd /root/tg-files
docker compose up -d

# 验证后端探针响应 (返回 HTTP 200 或状态 JSON 即为正常)
curl -s http://127.0.0.1:8123/api/bootstrap/status || echo "Waiting for backend..."
curl -s http://127.0.0.1:5244/ping || echo "Waiting for openlist..."
```

### 3.4 部署 tg-bridge 代码与 Python 虚拟环境

将项目源码部署至 `/root/tg-files/app`：

```bash
cd /root/tg-files/app

# 创建并激活隔离的 Python 虚拟环境
python3 -m venv venv
source venv/bin/activate

# 升级 pip 并安装生产锁版依赖
pip install --upgrade pip
pip install -r requirements.txt
```

### 3.5 配置 Systemd 守护进程 (tg-bridge.service)

创建系统服务描述文件 `/etc/systemd/system/tg-bridge.service`：

```ini
[Unit]
Description=Telegram Files Bridge Web Gateway
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/tg-files/app
Environment="PATH=/root/tg-files/app/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
Environment="TG_DATA_DIR=/root/tg-files/app-data"
Environment="TG_API_URL=http://127.0.0.1:8123/api"
Environment="OPENLIST_URL=http://127.0.0.1:5244"
Environment="BRIDGE_HOST=127.0.0.1"
Environment="BRIDGE_PORT=8000"
Environment="BRIDGE_SECURE_COOKIE=1"
Environment="TG_BACKUP_CONTAINER=tg-files-api"
Environment="TG_BACKUP_PRERESTORE=1"
Environment="BRIDGE_AUTO_ARCHIVE_INTERVAL=30"
Environment="PYTHONUNBUFFERED=1"

# 生产环境启动命令 (单 worker 事件循环，支持 WebSocket 与代理头透传)
ExecStart=/root/tg-files/app/venv/bin/uvicorn bridge_server:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers

Restart=always
RestartSec=5s
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

加载并启动服务守护：

```bash
systemctl daemon-reload
systemctl enable --now tg-bridge.service

# 查看服务状态与最近日志
systemctl status tg-bridge.service --no-pager
journalctl -u tg-bridge.service -f -n 50
```

### 3.6 配置反向代理与 HTTPS

生产环境强烈建议配置反向代理层以提供 TLS/SSL 终止、WebSocket 长连接与 SSE 无缓冲流式传输。

#### 方案 A：Caddy 配置（推荐，自动获取 Let's Encrypt 证书）

编辑 `/etc/caddy/Caddyfile`（将 `yourdomain.com` 替换为真实域名）：

```caddyfile
yourdomain.com {
    encode gzip zstd

    # tg-bridge 门户反向代理
    reverse_proxy 127.0.0.1:8000 {
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
}
```

重新加载 Caddy：
```bash
systemctl reload caddy
```

#### 方案 B：Nginx 配置（支持 SSE 禁用缓冲与 WebSocket 透传）

编辑 `/etc/nginx/sites-available/tg-bridge` 或 `/etc/nginx/conf.d/tg-bridge.conf`：

```nginx
server {
    listen 80;
    server_name yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;

    client_max_body_size 500M;

    # 全局代理头
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # 主网关反向代理 (支持 WebSocket)
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }

    # SSE 日志与任务流推送（必须禁用代理缓冲，否则前台无法实时刷新）
    location /sse/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding off;
        proxy_read_timeout 86400s;
    }
}
```

测试并重载 Nginx：
```bash
nginx -t && systemctl reload nginx
```

---

## 4. Windows 环境部署流程

Windows 环境适用于本地快速开发、无 Docker 纯前端预览调试，或在 Windows Server 主机上部署。

### 4.1 环境准备
1. 安装 **Python 3.10+**（推荐 3.11 或 3.12，安装时勾选 `Add python.exe to PATH`）；
2. 安装 **Git for Windows**；
3. 安装 **Docker Desktop for Windows**（用于运行 Java 后端与 OpenList）。

### 4.2 场景 A：纯前端 Mock 预览模式（零容器依赖）
仅用于开发调试前端界面或查看视觉交互，无需运行 Java 核心与 TDLib：

```powershell
# 1. 打开 PowerShell 进入工作区根目录
cd "D:\webapi"

# 2. 创建并激活虚拟环境
python -m venv venv
.\venv\Scripts\Activate.ps1

# 3. 安装依赖包
pip install -r requirements.txt

# 4. 启动前端 Mock 预览服务
uvicorn preview_server:app --host 127.0.0.1 --port 8000 --reload
```
打开浏览器访问：`http://127.0.0.1:8000/`

---

### 4.3 场景 B：Windows 完整系统运行模式（连接真实后端）

#### 步骤 1：规划本地数据持久化目录
```powershell
New-Item -ItemType Directory -Force -Path "C:\tg-data\app-data"
New-Item -ItemType Directory -Force -Path "C:\tg-data\session-backups"
New-Item -ItemType Directory -Force -Path "C:\tg-data\openlist-data"
```

#### 步骤 2：启动 Docker 容器 (PowerShell)
```powershell
# 启动 TDLib 下载核心 (将 C:\tg-data\app-data 映射进容器)
docker run -d `
  --name tg-files-api `
  --restart always `
  -p 8123:8123 `
  -v C:\tg-data\app-data:/app/data `
  -e SERVER_PORT=8123 `
  -e APP_DATA_DIR=/app/data `
  ghcr.io/jarvis2f/telegram-files:latest

# 启动 OpenList 网盘聚合服务 (可选)
docker run -d `
  --name openlist `
  --restart always `
  -p 5244:5244 `
  -v C:\tg-data\openlist-data:/data `
  openlistteam/openlist:latest
```

#### 步骤 3：编写 Windows 启动脚本 `run_bridge.bat`
在项目根目录下创建 `run_bridge.bat`：

```bat
@echo off
title Telegram Files Bridge
cd /d %~dp0

set "TG_DATA_DIR=C:\tg-data\app-data"
set "TG_API_URL=http://127.0.0.1:8123/api"
set "OPENLIST_URL=http://127.0.0.1:5244"
set "BRIDGE_HOST=127.0.0.1"
set "BRIDGE_PORT=8000"
set "BRIDGE_SECURE_COOKIE=0"
set "TG_BACKUP_CONTAINER=tg-files-api"
set "TG_BACKUP_PRERESTORE=1"
set "PYTHONUNBUFFERED=1"

echo [*] Starting Telegram Files Bridge on http://%BRIDGE_HOST%:%BRIDGE_PORT% ...
call .\venv\Scripts\activate.bat
python -m uvicorn bridge_server:app --host %BRIDGE_HOST% --port %BRIDGE_PORT%
pause
```

双击 `run_bridge.bat` 或在终端中执行 `.\run_bridge.bat` 即可启动。

#### 步骤 4：Windows 守护进程常驻（NSSM 方案）
若在 Windows Server 生产环境常驻运行：
```powershell
# 1. 安装 nssm 工具
winget install NSSM

# 2. 注册为系统系统服务
nssm install TgBridge "D:\webapi\venv\Scripts\python.exe" "-m uvicorn bridge_server:app --host 0.0.0.0 --port 8000"
nssm set TgBridge AppDirectory "D:\webapi"
nssm set TgBridge AppEnvironmentExtra "TG_DATA_DIR=C:\tg-data\app-data" "TG_API_URL=http://127.0.0.1:8123/api" "OPENLIST_URL=http://127.0.0.1:5244" "PYTHONUNBUFFERED=1"

# 3. 启动并配置随系统自启
nssm set TgBridge Start SERVICE_AUTO_START
nssm start TgBridge
```

---

## 5. 初次安装引导 (Bootstrap) 与核心业务配置

启动服务后，使用浏览器访问管理台：`http://<SERVER_IP>:8000/`：

### 5.1 步骤 1：首启初始化向导 (`/init`)
- 首次进入系统会自动重定向至 `/init`；
- **配对码验证 (Bootstrap Code)**：输入 Java 后端在日志中打印的初始引导配对码；
- **管理员设置**：配置管理员用户名与高强度密码（切勿使用弱口令）；
- 初始化成功后，系统将在数据目录生成 `.bridge_secret`（0600 权限）与 `.bridge_initialized`。

### 5.2 步骤 2：安全凭据与会话门禁
- 登录认证通过带 HMAC-SHA256 签名的 `tf_portal` HttpOnly Cookie 维持会话（有效期 7 天）；
- 核心修改与提交请求严格执行 `tf_portal_csrf` CSRF Token 头校验 (`X-CSRF-Token`)；
- 系统自动对 `/auth/login` 与 `/init` 实施防暴力破解滑动窗口限制（默认 300 秒内最多 10 次）。

### 5.3 步骤 3：Telegram 账号登录向导 (`/tg-login`)
在控制台进入 TG 登录向导：
1. **输入手机号**（带国际区号，如 `+1234567890`）；
2. **填写验证码**：输入 Telegram 客户端或短信收到的验证码；
3. **二步验证 (2FA)**：若账号开启了密码保护，输入二步验证密码；
4. 登录成功后，活跃认证密钥将落盘至 `APP_ROOT_DIR/account/<UUID>/td.binlog`。

### 5.4 步骤 4：OpenList 网盘连接与归档配置 (`/settings`)
1. 进入 **设置 (Settings)** -> **OpenList 网盘配置**；
2. 填写 OpenList 地址（默认 `http://127.0.0.1:5244`）、用户名与密码，点击验证并保存；
3. 在 **云端归档设置** 中配置默认归档路径（如 `/115网盘/Telegram归档`），开启「归档成功后自动删除本地源文件」以节约 VPS 磁盘。

---

## 6. Telegram Bot 双向交互命令服务

系统内置了 Telegram Bot 异步长轮询交互服务（基于 `services/bot_command_service.py`），无需打开 Web 控制台即可通过 Telegram 手机端实时掌握任务进度与系统状态。

### 6.1 交互命令集速查

| 命令 | 名称 | 响应内容与特性 |
| :--- | :--- | :--- |
| `/ck` | **任务进度与完整性** | 查看当前进行中的下载任务（实时速率、动态进度条 ▮▮▮▯▯、已下载大小）、最近已完成任务校验状态及等待队列 |
| `/yd` | **云端归档监控** | 实时查看网盘归档队列：上传中（含已传体积与耗时）、排队中以及失败待重试的任务清单 |
| `/st` | **系统状态一屏速读** | 一屏概览：磁盘分区使用率与水位红线、FloodWait 限流冷却倒计时、下载/上传实时总带宽、各状态任务计数 |
| `/err` | **归档失败排查诊断** | 展示最近 5~8 条归档失败记录的文件名、目标远端目录、具体报错细节及自愈修复建议 |
| `/help` | **命令帮助** | 打印当前可用的命令清单与说明手册 |

### 6.2 安全模型与配置规范
- **白名单严格过滤**：Bot 命令服务严格校验发信者的 `chat_id`，陌生用户发给 Bot 的消息一律静默丢弃；
- **只读非阻塞聚合**：Bot 查询复用 Bridge 内存缓存，单次查询强制 6 秒超时降级，保证 Bot 响应永不卡死；
- **敏感凭据脱敏**：对外回复的所有文本统一通过安全正则清洗，自动脱敏 Bot Token 与网络密钥。

在 Web 管理后台 **设置** -> **Telegram 通知与 Bot 配置** 中填入 `Bot Token` 与 `Chat ID` 即可自动激活长轮询监听。

---

## 7. 自动化订阅、目录模板与云端检索

### 7.1 频道订阅与自动归档规则
在 **订阅管理 (/subscriptions)** 页面中，可创建自动化下载与归档规则：
- **监控来源**：指定 Telegram 频道 ID、群组或私聊会话；
- **智能目录模板**：支持按命名模板动态渲染云端目录层级。

可用模板变量：
- `{source}`：频道或聊天来源名称
- `{chat_title}`：聊天会话标题
- `{type}`：媒体文件类型分类 (`video`, `document`, `photo`)
- `{resolution}`：视频分辨率等级 (`4k`, `1080p`, `720p`, `480p`)
- `{ext}`：文件扩展名 (`mp4`, `mkv`)
- `{YYYY}`, `{MM}`, `{DD}`, `{YYYY-MM}`：发布日期时间维度

*示例模板*：`/阿里云盘/TG归档/{chat_title}/{YYYY-MM}/{resolution}`

### 7.2 云端文件检索与本地回取
- **全局聚合搜索** (`/api/search/aggregate`)：一键对本地在存与已归档至 OpenList 的历史文件进行全量模糊检索；
- **云端回取至本地** (`/library/cloud/retrieve`)：在「云端归档」页面可选中已归档文件，一键回取下载到本地磁盘供临时调用，支持实时进度查询与取消。

---

## 8. 生产容灾与 Session 异地冷备恢复协议

### 8.1 为什么必须备份 Session？
Telegram 活跃登录态核心为 `APP_ROOT_DIR/account/*/td.binlog`（TDLib 授权密钥本体）。若该文件丢失或损坏，所有频道下载连接将永久断开，必须人工重新扫码认证。

#### 白名单备份（约 11 MB）
系统仅打包认证与核心配置：
- `account/*/td.binlog`：TDLib 核心授权密钥（**命脉**）；
- `data.db` (+wal/shm)：服务持久化数据库（含管理账号）；
- `.backend_creds`、`.bridge_secret`、`.openlist_auth`、`.session_backup_key`：凭据与密钥；
- `.subscriptions.json`、`.archive_config.json`、`.archive_jobs.json`、`.notify_config.json`：配置与规则。

#### 刻意排除项（节约 99% 体积）
- `account/*/db.sqlite*`：TDLib 消息缓存（实测 ~500MB，TDLib 连接后会自动重建）；
- `account/*/videos/`：TG 下载的媒体本体（实测 ~8GB，已由归档程序传至云端）；
- `account/*/temp/`：临时转码分片（实测 ~470MB）。

### 8.2 停写容器安全还原编排 (Container-Safe Restore Protocol)
为防止运行中的 TDLib 进程因持有文件句柄而将旧数据静默回写，系统执行严格的容器协同自愈流程：

```
+-----------------------------------------------------------------------------------+
| 1. 校验备份包魔数 (TGSNAP01 加密包 或 1f 8b 明文包) 与完整性                     |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| 2. 自动生成「还原前回滚快照」(TG_BACKUP_PRERESTORE=1)，确保存量状态随时可撤销     |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| 3. 自动停止后端写容器 (docker stop tg-files-api)，解除 td.binlog 文件占用锁        |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| 4. PBKDF2 派生密钥 + AES-256-GCM 解密，按白名单精准覆盖还原至 APP_ROOT_DIR (0600) |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| 5. 自动重启后端容器 (docker start tg-files-api)，验证 TDLib 恢复在线并回测健康探针|
+-----------------------------------------------------------------------------------+
```

### 8.3 备份与还原触发方式

#### 方式 1：Web 控制台图形化操作
- **创建备份**：进入 **设置** -> **Session 备份与灾难恢复**，点击 **立即创建加密快照**；
- **一键还原**：在备份历史列表中点击 **安全还原**，系统自动完成停写、解密、覆盖与重启。

#### 方式 2：调用 REST API
```bash
# 触发异地加密备份
curl -X POST "http://127.0.0.1:8000/api/session/backup" \
     -H "Cookie: tf_portal=<YOUR_TOKEN>" \
     -H "X-CSRF-Token: <YOUR_CSRF_TOKEN>"

# 触发安全还原
curl -X POST "http://127.0.0.1:8000/api/session/restore" \
     -H "Cookie: tf_portal=<YOUR_TOKEN>" \
     -H "X-CSRF-Token: <YOUR_CSRF_TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{"name": "tg-session-20260912-120000.tar.gz.enc", "origin": "local", "confirm": true}'
```

#### 方式 3：CLI 离线一键命令行还原（服务器迁移/灾难恢复）
在全新安装的机器或容器未启动状态下，执行原生命令行秒级解密恢复：
```bash
# 激活 Python 虚拟环境后执行
python bridge_server.py --restore-session /path/to/tg-session-20260912-120000.tar.gz.enc
```

---

## 9. 智能防风控 (FloodWait) 与动态磁盘水位自愈

### 9.1 TDLib FloodWait 智能防风控退避机制
当高频提交下载或频繁调用 Telegram API 触发风控时：
1. 系统自动捕获 `FLOOD_WAIT_X` 异常并解析秒数；
2. 自动进入风控熔断退避状态，所有新建请求平滑入队等待；
3. Web 界面右上方与 Bot `/st` 展示冷却倒计时徽标；
4. 倒计时结束后自动无缝恢复下载，避免账号遭 Telegram 永久冻结；
5. 紧急情况可通过 API 手动强制重置状态：`POST /api/tg/floodwait/reset`。

### 9.2 本地磁盘高低水位熔断与唤醒机制
1. **85% 高水位熔断**：当 VPS 磁盘使用率达到 85% 时，系统自动熔断拦截新下载任务，将其挂入 `waiting_disk` 等待队列，并通过 Telegram Bot/Saved Messages 触发告警；
2. **75% 低水位唤醒**：后台巡检线程检测到文件归档删除、磁盘回落至 75% 以下时，按 FIFO 时间序自动将挂起任务唤醒重投；
3. **手动唤醒**：清理磁盘后可调用接口立即唤醒：`POST /api/disk/wake`。

### 9.3 System Doctor 全链路探针诊断
系统内置 SLA < 3.0s 的四路并发健康检查探针 (`GET /api/system/doctor`)：
- **Java 后端核心**：检查 Vert.x 响应延迟与通信链路；
- **TDLib 客户端会话**：采用「`getAuthorizationState` 主探针 + `list_telegrams` 授权状态二次确认」的双信号机制，杜绝偶发网络抖动误报；
- **OpenList 网盘通信**：验证令牌有效性、挂载驱动状态与往返时延；
- **本地存储水位**：实时计算应用分区可用 GB 与使用率百分比。

---

## 10. 自动化代码门禁与增量发布 (CI/CD Pre-deploy Pipeline)

项目内置了四阶段 Pre-deploy 质量与安全自动化发布流水线：

```bash
# 本地执行 Stage 1-3 质量与安全门禁检测（免 VPS 部署）
python scripts/pre_deploy.py --skip-deploy

# 或直接运行 Shell 脚本
bash scripts/pre_deploy.sh --skip-deploy
```

### 门禁流水线执行阶段说明：
- **[Stage 1] Lint, Syntax & Architecture DAG Audit**：
  对全量 95+ 个 Python 源文件进行 AST 编译与单向依赖 DAG 断言，确保 `core` 零反向、`services` 零跨层、`routers` 零横向依赖；
- **[Stage 2] Unit & Regression Tests**：
  自动发现并运行 `tests/test_*.py` 下 36 个测试套件，执行全部 380+ 项自动化回归测试用例，断言 100% 绿色通过；
- **[Stage 3] Security, Credentials & CSRF Gate Audit**：
  扫描 `.vps-conn/vps.json` 杜绝明文凭据，强制断言本地 SSH 私钥凭据（ED25519/RSA），校验路由门禁白名单合规性；
- **[Stage 4] Pure SSH Private Key Deploy & Hot-restart**：
  若未携带 `--skip-deploy`，流水线将基于 SSH2 SFTP 增量比对文件指纹，快速同步源码至 VPS `/root/tg-files/app`，并触发 `systemctl restart tg-bridge` 平滑热重启与探针回测。

---

## 11. 生产日常运维指令速查表

| 运维目标 | Linux VPS (Bash / Systemd) | Windows (PowerShell) |
| :--- | :--- | :--- |
| **启动 Bridge 服务** | `systemctl start tg-bridge` | `.\run_bridge.bat` 或 `nssm start TgBridge` |
| **停止 Bridge 服务** | `systemctl stop tg-bridge` | `Ctrl + C` 或 `nssm stop TgBridge` |
| **重启 Bridge 服务** | `systemctl restart tg-bridge` | `nssm restart TgBridge` |
| **查看实时运行日志** | `journalctl -u tg-bridge -f -n 100` | `Get-Content logs\bridge.log -Wait -Tail 100` |
| **服务基础健康探针** | `curl -s http://127.0.0.1:8000/health` | `Invoke-RestMethod http://127.0.0.1:8000/health` |
| **Doctor 全链路健康诊断** | `curl -s http://127.0.0.1:8000/api/system/doctor` | `Invoke-RestMethod http://127.0.0.1:8000/api/system/doctor` |
| **查询磁盘水位状态** | `curl -s http://127.0.0.1:8000/api/disk/watermark/status` | `Invoke-RestMethod http://127.0.0.1:8000/api/disk/watermark/status` |
| **手动唤醒磁盘挂起任务** | `curl -X POST http://127.0.0.1:8000/api/disk/wake` | `Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/disk/wake` |
| **查询 FloodWait 风控状态** | `curl -s http://127.0.0.1:8000/api/tg/floodwait/status` | `Invoke-RestMethod http://127.0.0.1:8000/api/tg/floodwait/status` |
| **强制重置 FloodWait 状态** | `curl -X POST http://127.0.0.1:8000/api/tg/floodwait/reset` | `Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/tg/floodwait/reset` |
| **查看实时传输测速** | `curl -s http://127.0.0.1:8000/api/speeds` | `Invoke-RestMethod http://127.0.0.1:8000/api/speeds` |
| **创建加密 Session 备份** | `curl -X POST http://127.0.0.1:8000/api/session/backup` | *(建议在管理控制台设置页一键创建)* |
| **查询冷备健康状态** | `curl -s http://127.0.0.1:8000/api/session/backup/status` | `Invoke-RestMethod http://127.0.0.1:8000/api/session/backup/status` |
| **Telegram Bot 手机速查** | 在 Telegram 给 Bot 发送 `/st`、`/ck` 或 `/yd` | 手机端 Telegram 随时随地查阅 |

---

## 12. 常见故障排查树 (Troubleshooting)

### 12.1 启动失败：`Address already in use` (端口 8000 冲突)
- **Linux**:
  ```bash
  ss -tulpn | grep 8000
  kill -9 <PID>
  ```
- **Windows**:
  ```powershell
  Get-NetTCPConnection -LocalPort 8000 | Select-Object OwningProcess
  Stop-Process -Id <PID> -Force
  ```

### 12.2 后端通信异常：`HTTP 502 / BackendConnectionError`
- **检查步骤**：
  1. 确认 `tg-files-api` 容器正常运行：`docker ps | grep tg-files-api`；
  2. 验证本地 8123 端口连通性：`curl -v http://127.0.0.1:8123/api/bootstrap/status`；
  3. 检查 Java 后端日志输出：`docker logs --tail 100 tg-files-api`。

### 12.3 磁盘高水位保护熔断：`Disk usage exceeds 85%`
- **现象**：新建下载任务提示失败，或任务进入 `waiting_disk` 挂起状态。
- **处置方案**：
  1. 系统处于保护状态，避免磁盘打满导致操作系统崩溃；
  2. 进入管理后台「本地在存」清理已归档视频，或清理系统无用 Docker 镜像（`docker system prune -a`）；
  3. 当磁盘占用回落至 75% 以下，系统将自动唤醒任务；亦可调用 `POST /api/disk/wake` 手动恢复。

### 12.4 TDLib 接口限流：`FLOOD_WAIT_X`
- **现象**：提交下载时提示 `FLOOD_WAIT`。
- **机制与恢复**：
  1. 属于 Telegram 官方服务器防刷保护，系统已自动进入退避保护模式；
  2. 在 Web 控制台右上角或 Bot 发送 `/st` 查看解封剩余秒数；
  3. 倒计时结束后任务会自动续跑；切勿在封禁期频繁重试。

### 12.5 Session 还原后仍提示未登录（静默失败）
- **根因**：还原会话文件时未停止 `tg-files-api` 容器，运行中的 TDLib 进程因持有旧缓存而将还原的新密钥再次回写覆盖。
- **排查与解决**：
  1. 确保配置了正确的容器环境变量：`TG_BACKUP_CONTAINER=tg-files-api`；
  2. 使用 Web 控制台自带的「安全还原」或通过 API 调用，系统会自动调用 Docker 停写并重启容器；
  3. 离线手工还原前请务必先执行 `docker stop tg-files-api`。

### 12.6 SSE 日志流卡顿或无法实时刷新
- **根因**：Nginx 等反向代理开启了 `proxy_buffering`，导致服务器端发送事件 (SSE) 被缓存在代理层直到达到 buffer 阈值才外发。
- **解决**：参考 3.6 节在反代配置中针对 `/sse/` 路由增加 `proxy_buffering off;`、`proxy_cache off;` 与 `chunked_transfer_encoding off;`。

### 12.7 Telegram Bot 长轮询无响应或报错
- **检查步骤**：
  1. 确认已在管理后台 `/settings` 正确配置了 `Bot Token` 与 `Chat ID`；
  2. 确认 VPS 可以直连 `api.telegram.org:443`；若在受限网络需配置代理；
  3. 确认未在其他平台为该 Bot 注册 Webhook（Telegram 规定 Webhook 与 `getUpdates` 长轮询互斥，若已注册需先调用 `deleteWebhook`）。
