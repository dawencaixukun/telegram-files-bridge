# 安全与运维审查报告 — /d/webapi（TG 视频下载与归档管理系统）

- 审查人：security-reviewer（AgentTeams code-review-v2）
- 审查日期：2026-09-02
- 审查范围：`.vps-conn/` 运维脚本、生产 VPS 部署架构、TG 账户 session 存储与备份、凭据泄露面、部署/回滚风险
- 数据来源：本地代码审读 + 通过 `vexec.js` 对生产 VPS 的**只读**勘查（未做任何写操作）

---

## 1. 概述

项目为 TG 视频下载与归档管理系统。生产部署位于 **生产 VPS**：

- 后端：docker 容器 `tg-files-api`（`ghcr.io/jarvis2f/telegram-files:latest`，Vert.x + TDLib），仅绑定 `127.0.0.1:8123`（正确，未对外暴露）。
- 前端适配层：systemd 服务 `tg-bridge.service`，uvicorn 运行 `bridge_server.py`，**绑定 `0.0.0.0:8000`**（对外暴露，自带 tf_portal 门禁 cookie）。
- 其他公网容器/端口：`infinite-canvas`(3000)、`openlist`(5244/5245)、`cli-proxy-api`(8317)、caddy(80/443)。

**核心结论（先说重点）：**

1. **TG 账户 session（活跃账户）当前没有任何备份**。现存 `app-backup-20260901-001618.tar.gz` 只打包了 `app/` 代码目录，**不含 `app-data/`（session 所在目录）**。`stale-accounts-backup/` 只含 7 个已停用的旧账户，**不含活跃账户 `503bb50c-…`**。
2. VPS 上存在多处**明文凭据**：root SSH 口令（本地 `vps.json`）、管理台 admin 密码与 bootstrap 码（`bootstrap.sh`/`cleanup.sh`/`diag.sh`）、Telegram API ID/HASH（docker 环境变量）。
3. SSH 配置 `PermitRootLogin yes` + `PasswordAuthentication yes`，root 可直接密码登录，爆破面大。
4. 部署脚本为「先删后写 / 直接覆盖」，**无自动备份、无回滚**；个别操作（vpush 的 `rm -f`）在误用时可能直接删掉生产文件。

**在改动 VPS 后端之前，必须先执行第 3 节的 session 备份命令（当前无备份，风险极高）。**

---

## 2. 发现列表

### 2.1 高危

#### F1. TG 账户 session 无任何备份（活跃账户）
- **位置**：VPS `/root/tg-files/app-data/account/503bb50c-fe26-4aeb-8cec-b98af8f57c0b/`（`db.sqlite` ~104MB + `db.sqlite-wal` + `db.sqlite-shm` + `td.binlog`）
- **问题**：active account 的 TDLib session（含登录态、文件索引）仅存于这一处。现存备份 `app-backup-20260901-001618.tar.gz` 经 tar 内容核查只含 `app/` 代码，**不含 app-data**；`stale-accounts-backup/` 的 7 个账户不含活跃账户。VPS 无 crontab（`crontab: command not found`），无任何定时备份；GCP 快照无法确认（无快照权限）。
- **风险**：任何对 `/root/tg-files/app-data` 的误删/覆盖/容器重建失误都会**永久丢失 TG 登录态**，需重新扫码登录所有频道。
- **建议**：立即按第 3 节做一次性全量备份 + 建立定时备份；备份存放至少一份**非本 VPS**（拉回本地/对象存储）。

#### F2. root SSH 明文口令落盘（本地）
- **位置**：`.vps-conn/vps.json:2-5`（`host: your-vps-ip, user: root, password: your-password`）
- **问题**：root 口令明文存储，文件权限 `644`（世界可读，`-rw-r--r--`）。`.gitignore` 已排除该文件（第 6 行，正确），当前目录也非 git 仓库，故无 git 历史泄露；但**任何能读该工作区的进程/人都可直接拿到 root**。
- **风险**：本地机器被入侵 = VPS root 沦陷；VPS 侧 `PermitRootLogin yes` + `PasswordAuthentication yes` 放大了口令泄露后果。
- **建议**：① 尽快轮换该口令；② vps.json 权限收紧为 `600`，或改用本机 SSH key（`ssh-keygen` + `ssh-copy-id`）后禁用密码登录（VPS 端 `PasswordAuthentication no`）；③ 本地 `.vps-conn/vps.json` 用环境变量/`~/.ssh/config` 替代。

#### F3. SSH 允许 root 密码登录
- **位置**：VPS `/etc/ssh/sshd_config`（`PermitRootLogin yes`、`PasswordAuthentication yes`）与 `/etc/ssh/sshd_config.d/60-cloudimg-settings.conf`
- **问题**：公网 22 端口 + root + 密码登录组合，暴露暴力破解面。
- **风险**：口令若泄露/被爆破，VPS（含全部 TG session、admin 凭据、docker 数据）直接沦陷。
- **建议**：改为 key-only 登录：`PermitRootLogin prohibit-password` + `PasswordAuthentication no`；用 `fail2ban` 或限制 SSH 来源 IP。

#### F4. 管理台 admin 凭据与 bootstrap 码明文落盘（VPS）
- **位置**：VPS `/root/tg-files/bootstrap.sh:2`（`CODE="your-bootstrap-token"`、`"admin"/"your-password"`）、`cleanup.sh:3`、`diag.sh:4`（均 644）
- **问题**：后端管理台默认 admin 密码与一次性 bootstrap 码以明文写在 644 脚本中，任何能读 `/root/tg-files` 的用户可拿到。
- **风险**：管理台被未授权登录 → 可删除 telegram 频道/会话（对应「会丢 session 的操作」）；密码是弱口令且已落盘。
- **建议**：① 立即改掉管理台 admin 密码；② bootstrap 码用后即弃（一次性）；③ 脚本权限 `chmod 700`，密码改从环境变量/密钥管理读取；④ 清理历史 shell 脚本中的明文。

#### F5. 部署脚本先删后写 / 直接覆盖，无备份回滚
- **位置**：`.vps-conn/vpush.js:31`（`rm -f <remote>` 后分块写）、`.vps-conn/pushall.js:53`（`sftp.fastPut` 直接覆盖）、`.vps-conn/vput.js:13`
- **问题**：vpush 先 `rm -f` 远端文件再 base64 分块写入（注释自述「即使 rm 失败也不会把新内容拼在旧文件尾部」——**意图是防脏写，但若远端路径误指、或写入中断，远端文件即丢失且无备份**）。pushall/vput 直接覆盖，无 `.bak`、无版本化。
- **风险**：部署失败/路径失误 = 生产代码被删/被截断，且**没有回滚手段**（仅靠手工 tar 快照，且该快照不含 session）。
- **建议**：① vpush 改为「先备份再写」：`cp <remote> <remote>.bak`（或先 SFTP 下载旧文件到本地）；② 部署前在 VPS 生成带时间戳的全量 tar（含 app-data）；③ 每次改 bridge_server.py 前先执行第 3 节备份命令。

#### F6. bridge 前端绑定 0.0.0.0:8000 直连公网，无 TLS
- **位置**：VPS `/etc/systemd/system/tg-bridge.service`（`ExecStart=…uvicorn bridge_server:app --host 0.0.0.0 --port 8000`）；`bridge_server.py:47`（`BRIDGE_HOST` 默认 127.0.0.1，但 systemd 显式覆盖为 0.0.0.0）
- **问题**：8000 端口监听 `0.0.0.0`，caddy（80/443）未反代 8000（Caddyfile 只反代 8317/5244/3000），即 **bridge 面板直连公网且无 TLS**。cookie 的 `Secure` 标志依赖 `BRIDGE_SECURE_COOKIE=1`（`bridge_server.py:49`），systemd 服务未设置该环境变量。
- **风险**：面板凭 tf_portal cookie 明文传输，可被中间人窃取会话；面板暴露在公网增加被扫描/探测面。
- **建议**：① 让 caddy 反代 `http://127.0.0.1:8000` 并启用 HTTPS，bridge 改回只监听 127.0.0.1；② systemd 设置 `BRIDGE_SECURE_COOKIE=1`；③ 收紧 VPS 防火墙（iptables INPUT 当前 policy ACCEPT，未做入站限制）。

#### F7. Telegram API 凭据明文进 docker 环境变量
- **位置**：VPS `start-backend.sh`（`docker run … -e TELEGRAM_API_ID=… -e TELEGRAM_API_HASH=…`）；`docker inspect tg-files-api` 确认 env 明文含 `TELEGRAM_API_ID=31235942`、`TELEGRAM_API_HASH=636189b…`
- **问题**：TG API 密钥以明文环境变量注入，且 `start-backend.sh` 通过命令行参数传递（`/proc/<pid>/cmdline`/bash history 可见）。
- **风险**：拿到 VPS 文件系统/进程列表者即获 TG 应用密钥。
- **建议**：改用 `docker run --env-file`（权限 600）+ 不落 bash history；密钥存本机 secrets 管理。

### 2.2 中危

#### F8. 活跃 session 目录权限偏宽
- **位置**：VPS `/root/tg-files/app-data/account/503bb50c-…/`（`drwxr-x---`，组可读；`db.sqlite`/`db.sqlite-wal`/`db.sqlite-shm` 为 `-rw-r--r--` 世界可读；`td.binlog` 为 `-rw-------`，正确）
- **问题**：`db.sqlite` 及其 wal/shm 世界可读（644），任何同机用户可读 TG 账户数据库。
- **风险**：同机非 root 用户（其他容器进程、被攻破的 web 进程）可读 TG session 数据。
- **建议**：`chmod 600` 目录下所有 sqlite 文件（或 `chmod 700` 目录）；确认 `tg-files-api` 仅 root 启动；`docker inspect` 的挂载为 bind mount（`/root/tg-files/app-data:/app/data`），保持目录归属 root。

#### F9. 部署过程无自动化与一致性校验
- **位置**：`.vps-conn/` 全部脚本
- **问题**：手工执行、无 deploy 脚本整合、无上传后校验（md5/sha256）、无健康检查回滚。
- **风险**：上传半截文件后 bridge 起不来，人工才发现。
- **建议**：加 `sha256sum` 比对 + 部署后 `curl http://127.0.0.1:8000/healthz` + 失败自动回滚上一版 tar。

#### F10. 无版本控制
- **位置**：工作区 `/d/webapi` 不是 git 仓库（`git rev-parse` 失败）
- **问题**：代码变更无历史、无 tag，`bridge_server.py` 的每次修改无法追溯。
- **风险**：回滚只能靠手工 tar，且易漏文件。
- **建议**：初始化 git（注意 `.gitignore` 已正确排除 `vps.json`），关键版本打 tag；VPS 端维护 `releases/` 目录存放历史 tar。

### 2.3 低危 / 备注

- `stale-accounts-backup/` 内 7 个账户的 `db.sqlite` 有部分缺 `-wal`/`-shm`（可能非一致快照），仅作参考，不应视为可靠备份。
- `auth.tar` / `auth2.tar` / `fix.tar` / `frontend.tar` 为早期手工代码包，内容陈旧，不建议用作回滚源。
- `.bridge_secret`（VPS app-data 下）权限 `600`（正确）；bridge 的 admin 凭据仅存进程内存（`set_credentials`），不落盘（正确）。
- 8123 后端仅绑定 loopback（正确）；caddy 已启用（有 TLS 能力）。
- 活跃账户 `db.sqlite` 有 `-wal`（未 checkpoint），热备份时需用 SQLite 一致性方式（见 3 节）。

---

## 3. TG session 备份清单与步骤（在改动后端前必须执行）

### 3.1 备份对象（VPS 路径）
```
/root/tg-files/app-data/                      ← 整个数据目录（含 account/* session、data.db、.bridge_secret）
  ├── account/<uuid>/td.binlog                ← TDLib 认证/会话日志（关键）
  ├── account/<uuid>/db.sqlite[-wal|-shm]     ← 账户数据库（含文件索引）
  ├── data.db[-wal|-shm]                      ← 系统数据库
  ├── logs/                                   ← 日志（可选）
  └── .bridge_secret                          ← bridge 门禁密钥（一并备份，避免重启后 cookie 全失效）
```

### 3.2 一次性全量备份（推荐：容器停止式，保证 WAL 一致）

在 VPS 上执行（通过 `node .vps-conn/vexec.js` 或直接 SSH）：

```bash
# 1) 停止后端容器（短暂停机，TG 下载/归档暂停几秒~几十秒）
docker stop tg-files-api

# 2) 创建备份目录并打包（含 account session + 数据库 + 门禁密钥）
mkdir -p /root/tg-files/session-backups
TS=$(date +%Y%m%d-%H%M%S)
tar czf /root/tg-files/session-backups/tg-sessions-$TS.tar.gz \
    -C /root/tg-files app-data

# 3) 校验备份内容完整（必须有 td.binlog 与 db.sqlite）
tar tzf /root/tg-files/session-backups/tg-sessions-$TS.tar.gz | grep -E 'td.binlog|db.sqlite' | head

# 4) 重启容器
docker start tg-files-api

# 5) 记录文件大小供后续比对
ls -lh /root/tg-files/session-backups/tg-sessions-$TS.tar.gz
```

### 3.3 拉取一份到本机（异地备份，强烈建议）

在本地工作区执行（把 VPS 备份包下载到本地 `.vps-conn` 之外的目录，如 `D:/webapi/backups/`）：

```bash
# 方案 A：用 scp（Windows Git Bash 自带）
scp root@your-vps-ip:/root/tg-files/session-backups/tg-sessions-*.tar.gz /d/webapi/backups/

# 方案 B：临时用 node + ssh2 写个小脚本，或直接改用 vput 的反向（本地无现成 vget，可临时用 scp）
```

> ⚠️ 拉取后**将本地备份目录权限收紧**（`chmod 700`），避免再次落盘泄露。

### 3.4 定时备份（防止未来再裸奔）

在 VPS 添加 crontab（当前 crontab 未安装，先 `apt install cron`）或 systemd timer：

```bash
# 每日 03:30 停机式备份 + 保留最近 7 份
30 3 * * * docker stop tg-files-api && \
  mkdir -p /root/tg-files/session-backups && \
  tar czf /root/tg-files/session-backups/tg-sessions-$(date +\%Y\%m\%d-\%H\%M\%S).tar.gz -C /root/tg-files app-data && \
  docker start tg-files-api && \
  ls -t /root/tg-files/session-backups/tg-sessions-*.tar.gz | tail -n +8 | xargs -r rm -f
```

### 3.5 每次改动后端前的「快照式」备份（最小操作）

```bash
node .vps-conn/vexec.js "docker stop tg-files-api && cp -a /root/tg-files/app-data /root/tg-files/app-data.bak-$(date +%s) && docker start tg-files-api"
```

---

5. **重新 bootstrap 覆盖 admin 凭据**：`bootstrap.sh` 用固定 CODE + `your-password`，在已初始化后会失败，但若在未初始化状态执行会重设凭据（不直接删 session，但改变控制面）。

以下操作**可能或必然丢失 TG session**，执行前必须先备份：

1. **`rm -rf /root/tg-files/app-data`** 或对 app-data 目录做覆盖/移动（任何 `mv`/`rm`/解压覆盖到 app-data）。
2. **vpush.js 把远端路径误指向 app-data**：`vpush.js:31` 会先 `rm -f <remote>`——若 `remote=/root/tg-files/app-data/account/<uuid>/td.binlog` 之类，session 直接被删且无备份。
3. **`docker rm -f tg-files-api` 后重建但挂载路径写错/漏挂**：`start-backend.sh` 用 bind mount，正常重建**不会**删 app-data；但若重建时 `-v` 路径写错、或误执行 `docker volume prune`/`docker system prune -af --volumes`（对 bind mount 无影响，但删除目录本身会丢）。
4. **管理台/API 层删除 telegram 账户**：`/api/telegram/<id>/delete`（cleanup.sh 即遍历删除 unauthorized 账户，若误判活跃账户会删 session 目录 → 触发 TDLib 重新登录）。
5. **重新 bootstrap 覆盖 admin 凭据**：`bootstrap.sh` 用固定 CODE + `Admin@123456`，在已初始化后会失败，但若在未初始化状态执行会重设凭据（不直接删 session，但改变控制面）。
6. **对活跃 session 目录做 `chown`/`chmod -R` 到其他用户**：导致容器进程（root）可读写没问题，但若改成非 root 属主且容器降权会失败。

---

## 5. 总体结论

| 维度 | 评级 | 说明 |
|------|------|------|
| TG session 备份 | 🔴 **缺失** | 活跃账户无任何备份，改动后端前必须先备份（第 3 节命令） |
| 凭据管理 | 🔴 **差** | root 口令/管理台密码/TG API 密钥多处明文，SSH 允许 root 密码登录 |
| 部署/回滚 | 🟠 **中危** | 先删后写、无自动备份、无一致性校验、无版本控制 |
| 暴露面 | 🟠 **中危** | 8000 直连公网无 TLS；防火墙入站未收紧；后端 8123 绑定正确 |
| 安全基线（门禁/CSRF/内存凭据/密钥权限） | 🟢 **良好** | bridge 有签名 cookie + CSRF + 登录限流，admin 凭据不落盘，`.bridge_secret` 600 |

**必须立即处理（P0）：**
1. **执行第 3 节 session 全量备份 + 拉取异地副本**（在任何人改动 VPS 后端之前）。
2. 轮换 VPS root 口令，`vps.json` 权限收紧为 600，SSH 改 key-only。
3. 修改管理台 admin 密码，清理脚本中明文密码与 bootstrap 码。

**建议尽快处理（P1）：** caddy 反代 8000 并启用 HTTPS + `BRIDGE_SECURE_COOKIE=1`；部署脚本加备份+校验+回滚；VPS 建立定时备份与防火墙规则。

**总体判断**：系统功能基线尚可（后端隔离、门禁到位），但**运维侧在「凭据明文」与「无 session 备份」两处属于高危欠账**。在完成第 3 节备份与 P0 项整改前，不建议对 VPS 后端做任何修改。
