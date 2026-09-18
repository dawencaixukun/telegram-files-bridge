# Telegram Files Bridge 部署与运维指南

面向 Linux VPS 生产部署与日常运维，Windows 开发调试见第 5 节。

## 1. 架构

```
浏览器/Telegram Bot
   │ HTTPS/WSS/SSE（生产建议反代）
   v
tg-bridge 网关 (FastAPI, 127.0.0.1:8000)
   ├─ core/     配置、认证/CSRF、后端通信、日志
   ├─ services/ 任务/归档/备份/水位/风控/Bot 等领域服务
   ├─ routers/  9 个子路由（dashboard/auth/tasks/browse/library/archive/subscriptions/system/api）
   ├─ tg-files-api (Java/TDLib 容器, :8123) —— 下载执行、账号会话
   └─ openlist  (Go 容器, :5244) —— 网盘归档/冷备
```

数据目录 `TG_DATA_DIR`（默认 `/root/tg-files/app-data`）关键内容：
`account/*/td.binlog`（TDLib 授权密钥，**严禁误删**）、`.backend_creds`、`.bridge_secret`、
`.subscriptions.json`、`.archive_jobs.json`、`.openlist_auth`、`.notify_config.json`、`.waiting_disk.json`、`logs/`。

## 2. 环境变量

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `127.0.0.1` / `8000` | 监听地址与端口；无反代直连才设 `0.0.0.0` |
| `TG_DATA_DIR` | `/root/tg-files/app-data` | 应用数据根目录 |
| `TG_API_URL` | `http://127.0.0.1:8123/api` | TDLib 后端 API |
| `OPENLIST_URL` | `http://127.0.0.1:5244` | OpenList 地址 |
| `BRIDGE_SECURE_COOKIE` | 空 | 启用 HTTPS 后设 `1` |
| `BRIDGE_TRUSTED_PROXIES` | 空 | 受信任反代 IP，如 `127.0.0.1` |
| `BRIDGE_CACHE_TTL` | `8` | 任务列表缓存秒数 |
| `BRIDGE_LOGIN_LIMIT` / `BRIDGE_LOGIN_WINDOW` | `10` / `300` | 登录防爆破 |
| `BRIDGE_AUTO_ARCHIVE_INTERVAL` | `30` | 订阅归档巡检间隔（秒） |
| `TG_SESSION_BACKUP_KEY` | 空 | 备份加密主密钥，留空用 `.bridge_secret` |
| `TG_SESSION_BACKUP_REMOTE_DIR` | `/TG-Backups` | OpenList 备份目录 |
| `TG_SESSION_BACKUP_LOCAL_DIR` | 空 | 本地冷备目录，留空 `{TG_DATA_DIR}/session-backups` |
| `TG_BACKUP_CONTAINER` | `tg-files-api` | 还原时需停写的容器名 |
| `TG_BACKUP_PRERESTORE` | `1` | 还原前自动生成回滚快照 |

## 3. Linux VPS 部署

### 3.1 依赖与目录

```bash
# Debian/Ubuntu
apt-get update -y && apt-get install -y python3 python3-pip python3-venv git curl tar docker.io docker-compose-plugin
# CentOS/Rocky
dnf install -y python3 python3-pip git curl tar docker docker-compose-plugin && systemctl enable --now docker

mkdir -p /root/tg-files/app /root/tg-files/app-data /root/tg-files/session-backups /root/tg-files/openlist-data
chmod 700 /root/tg-files /root/tg-files/app-data /root/tg-files/session-backups
```

### 3.2 后端容器（docker-compose.yml）

```yaml
version: "3.8"
services:
  tg-files-api:
    image: ghcr.io/jarvis2f/telegram-files:latest
    container_name: tg-files-api
    restart: always
    network_mode: "host"
    environment:
      - SERVER_PORT=8123
      - APP_DATA_DIR=/app/data
      # - TELEGRAM_API_ID=your_api_id
      # - TELEGRAM_API_HASH=your_api_hash
    volumes:
      - /root/tg-files/app-data:/app/data
  openlist:
    image: openlistteam/openlist:latest
    container_name: openlist
    restart: always
    network_mode: "host"
    volumes:
      - /root/tg-files/openlist-data:/data
```

```bash
cd /root/tg-files && docker compose up -d
curl -s http://127.0.0.1:8123/api/bootstrap/status   # 后端探针
curl -s http://127.0.0.1:5244/ping                   # openlist 探针
```

### 3.3 tg-bridge 代码与虚拟环境

```bash
cd /root/tg-files/app
python3 -m venv venv && source venv/bin/activate
pip install --upgrade pip && pip install -r requirements.txt
```

### 3.4 Systemd 服务（/etc/systemd/system/tg-bridge.service）

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
ExecStart=/root/tg-files/app/venv/bin/uvicorn bridge_server:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers
Restart=always
RestartSec=5s
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now tg-bridge
journalctl -u tg-bridge -f -n 50
```

### 3.5 反向代理与 HTTPS（生产推荐）

Caddy（自动证书）：

```caddyfile
yourdomain.com {
    encode gzip zstd
    reverse_proxy 127.0.0.1:8000
}
```

Nginx（SSE 必须禁缓冲，否则页面日志流不实时）：

```nginx
server {
    listen 443 ssl http2;
    server_name yourdomain.com;
    ssl_certificate     /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;
    client_max_body_size 500M;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400s;
    }
    location /sse/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400s;
    }
}
```

## 4. 初次安装引导

1. 浏览器打开 `http://<SERVER_IP>:8000/`，首启重定向 `/init`：输入后端日志中的配对码，设置管理员账号密码（生成 `.bridge_secret` 0600 与 `.bridge_initialized`）。
2. `/tg-login` 向导登录 Telegram：手机号 → 验证码 → 2FA 密码（如开启）；授权密钥落盘 `account/<UUID>/td.binlog`。
3. `/settings` 配置 OpenList 地址/账号并验证；设置云端归档默认目录，可开启「归档后删本地源文件」。

安全机制：HMAC 签名 Cookie 会话（7 天）、写操作 CSRF 头校验、登录防爆破（300s/10 次）。

## 5. Windows 环境（开发调试）

- **Mock 预览**（零容器）：`python -m venv venv` → `pip install -r requirements.txt` → `uvicorn preview_server:app --port 8000 --reload`
- **完整运行**：Docker Desktop 起 `tg-files-api`（`-v C:\tg-data\app-data:/app/data -e SERVER_PORT=8123`）与 openlist 后，设环境变量 `TG_DATA_DIR=C:\tg-data\app-data` 等再 `python -m uvicorn bridge_server:app`；常驻用 NSSM 注册服务。

## 6. Telegram Bot 命令

在 `/settings` 填 Bot Token 与 Chat ID 即激活长轮询（仅响应白名单 chat_id，回复自动脱敏）。

| 命令 | 内容 |
| :--- | :--- |
| `/ck` | 进行中任务进度、速率、最近完成与队列 |
| `/yd` | 云端归档队列：上传中/排队/失败 |
| `/st` | 磁盘水位、FloodWait 倒计时、总带宽、任务计数 |
| `/err` | 最近归档失败详情与修复建议 |
| `/help` | 命令清单 |

## 7. 订阅归档与目录模板

订阅页创建规则：监控指定频道/群组，新文件自动下载并归档。目录模板可用变量：
`{source}` `{chat_title}` `{type}`（video/document/photo）`{resolution}`（4k/1080p/720p/480p）`{ext}` `{YYYY}` `{MM}` `{DD}` `{YYYY-MM}`
示例：`/阿里云盘/TG归档/{chat_title}/{YYYY-MM}/{resolution}`

云端检索：`/api/search/aggregate` 聚合搜索本地+云端；`/library/cloud/retrieve` 一键回取已归档文件。

## 8. Session 备份与还原

备份对象：`td.binlog`（命脉）、`data.db`、各凭据/配置点文件；**排除** `db.sqlite`(~500MB)、`videos/`(~8GB)、`temp/`——TDLib 会自动重建，可节约 99% 体积（包约 11MB，AES-256-GCM 加密）。

还原流程（容器安全协议）：校验备份包 → 自动生成回滚快照 → `docker stop tg-files-api` 停写 → 解密覆盖 → 重启容器回测探针。**不停容器直接覆盖会被运行中的 TDLib 回写覆盖（静默失败）**。

三种触发方式：
- Web：设置 → Session 备份，创建/一键还原
- API：`POST /api/session/backup`、`POST /api/session/restore`（带 Cookie + `X-CSRF-Token`，restore 需 `"confirm": true`）
- CLI 离线还原（迁移/灾难恢复）：`python bridge_server.py --restore-session /path/to/xxx.tar.gz.enc`

## 9. 内置自愈机制

- **FloodWait 防风控**：捕获 `FLOOD_WAIT_X` 自动熔断退避，新任务平滑入队，倒计时结束自动恢复；状态见 Web 徽标或 Bot `/st`，强制重置 `POST /api/tg/floodwait/reset`。
- **磁盘水位**：85% 熔断（新任务挂入 `waiting_disk` 并告警），回落 75% 自动按 FIFO 唤醒；手动唤醒 `POST /api/disk/wake`。
- **Doctor 探针**：`GET /api/system/doctor` 四路并发检查（Java 后端 / TDLib 双信号 / OpenList / 本地存储），3 秒超时隔离。

## 10. Pre-deploy 质量门禁

```bash
python scripts/pre_deploy.py --skip-deploy   # Stage 1-3 本地门禁
python scripts/pre_deploy.py                 # 含 Stage 4：SSH 私钥增量部署 + 热重启
```

Stage 1 AST 编译与分层依赖审计；Stage 2 全量单元回归测试；Stage 3 凭据与路由门禁安全审计；Stage 4 SFTP 指纹增量同步 `/root/tg-files/app` 并重启服务。

## 11. 运维速查

| 目标 | Linux 命令 |
| :--- | :--- |
| 启/停/重启 | `systemctl start\|stop\|restart tg-bridge` |
| 实时日志 | `journalctl -u tg-bridge -f -n 100` |
| 健康探针 | `curl -s http://127.0.0.1:8000/health` |
| 全链路诊断 | `curl -s http://127.0.0.1:8000/api/system/doctor` |
| 磁盘水位状态/唤醒 | `GET/POST http://127.0.0.1:8000/api/disk/watermark/status` `/api/disk/wake` |
| FloodWait 状态/重置 | `GET/POST http://127.0.0.1:8000/api/tg/floodwait/status` `/api/tg/floodwait/reset` |
| 实时测速 | `curl -s http://127.0.0.1:8000/api/speeds` |
| Session 备份/状态 | `POST /api/session/backup`、`GET /api/session/backup/status` |
| 手机速查 | Telegram 给 Bot 发 `/st` `/ck` `/yd` |

Windows 侧对应 PowerShell 命令或 `nssm start|stop|restart TgBridge`。

## 12. 故障排查

- **端口占用**：`ss -tulpn | grep 8000` → `kill -9 <PID>`（Windows：`Get-NetTCPConnection -LocalPort 8000`）。
- **后端 502**：`docker ps | grep tg-files-api` → `curl -v http://127.0.0.1:8123/api/bootstrap/status` → `docker logs --tail 100 tg-files-api`。
- **磁盘 85% 熔断**：属保护状态；清理已归档视频或 `docker system prune -a`，回落 75% 自动恢复，或 `POST /api/disk/wake`。
- **FLOOD_WAIT_X**：Telegram 官方限流，系统已自动退避；`/st` 看剩余秒数，勿频繁重试。
- **还原后仍未登录**：还原时未停容器导致 TDLib 回写覆盖；确认 `TG_BACKUP_CONTAINER` 正确并用 Web/API「安全还原」，或手工先 `docker stop tg-files-api`。
- **SSE 不实时**：反代开了缓冲；对 `/sse/` 加 `proxy_buffering off`（见 3.5）。
- **Bot 无响应**：检查 Token/Chat ID 配置；VPS 能否直连 `api.telegram.org:443`；是否注册过 Webhook（与长轮询互斥，需 `deleteWebhook`）。
