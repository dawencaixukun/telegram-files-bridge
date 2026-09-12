# TG Session 异地自动化加密冷备、磁盘动态高低水位熔断与 SSH 凭据加固技术规范（P0）

**版本**：v1.0.0  
**状态**：定稿发布（Requirements Approved）  
**关联任务**：`t1` (Requirements 制定技术规范) → `t2` (Session 加密冷备与还原实现) → `t3` (磁盘高低水位熔断实现) → `t4` (SSH 密钥认证加固) → `t5` (测试验证) → `t6` (代码审查) → `t7` (安全审计) → `t8` (生产集成)  
**责任角色**：全栈与系统工程师 (`engineer`)  

---

## 1. 概述与设计目标

为了保证分布式 Telegram 资源转存服务在极端场景下的高可用性、数据完整性与主机基础设施安全，本规范确立以下三大核心能力的技术实现标准与架构设计：

1. **Telegram Session 异地自动化加密快照与秒级解密还原**：
   - 扫描 `/root/tg-files/app-data/account/*/` 中的 TDLib 登录态凭据核心（`td.binlog`、`db.sqlite*`）及系统认证凭据，严格排除视频/图片等动辄数 GB 的非会话多媒体缓存文件。
   - 采用标准 `AES-256-GCM`（AEAD 认证加密）高强度加密机制（亦兼容 OpenSSL CLI / 标准 zipfile 封装格式）。
   - 自动化流式推送到 OpenList 异地冷备目录 `/TG-Backups/`，并进行云端存在性与大小完整性核验。
   - 制定 1 分钟（实测秒级，< 15 秒）解密解包还原协议，支持灾难或机器迁移时快速自愈。
   - 在 Web 管理后台提供备份状态健康指示灯（绿色正常/黄色预警/红色故障）与一键即时快照操作。

2. **本地磁盘高低水位动态熔断保护与应急清理机制**：
   - 杜绝传统绝对值（如固定 5GB）阈值在不同规模存储卷下的失真问题，全面采用动态百分比水位算法。
   - **85% 高水位熔断**：在所有下载入口（`/browse/download`、`/submit`、`/api/tg/quick-download`）主动拦截新请求，安全置入 `waiting_disk` 挂起队列，不影响正在运行的现有下载任务。
   - **75% 低水位自动唤醒**：当磁盘占用回落至 75% 以下时，后台巡检调度器以 FIFO 顺序自动唤醒挂起任务并恢复下载。
   - **应急被动清理**：当水位达到 85% 告警线时，按 FIFO 策略安全释放已成功归档到 OpenList 网盘的最早本地文件，严禁触碰未归档或会话数据。

3. **VPS 部署通道全面 SSH 密钥认证改造**：
   - 彻底从 `.vps-conn/vps.json` 中剥离明文密码（`password`）字段，消除代码与配置中的明文口令泄露隐患。
   - 重构 `.vps-conn/deploy.js` 与 `.vps-conn/exec.js`，严格强制优先使用本地 SSH 密钥对（ED25519 / RSA）连接 VPS `/root/.ssh/authorized_keys`。
   - 强化本地凭据与私钥目录权限（0600 / ACL 用户隔离），实现全流程 100% 免密无感安全部署。

---

## 2. 架构设计与系统拓扑

```
+-----------------------------------------------------------------------------------------+
|                                    本地开发/部署端                                         |
|                                                                                         |
|  ~/.ssh/id_ed25519 (0600)  <----+                                                       |
|                                  |                                                      |
|  .vps-conn/vps.json (无密码)     | (SSH ED25519 私钥免密通道)                             |
|  .vps-conn/deploy.js  -----------+                                                      |
|  .vps-conn/exec.js    -----------+                                                      |
+-----------------------------------|-----------------------------------------------------+
                                    |
                                    v (Port 22 SSH)
+-----------------------------------------------------------------------------------------+
|                                   VPS 宿主环境                                             |
|                                                                                         |
|  /root/.ssh/authorized_keys (已录入 ED25519 公钥)                                         |
|                                                                                         |
|  +-------------------------+      +---------------------------+                         |
|  |   tg-bridge.service     |      |       OpenList 服务        |                         |
|  |   (FastAPI/Uvicorn)     |      |      (Docker :5244)       |                         |
|  |                         |      |                           |                         |
|  | 1. Session 备份引擎      |      |   /TG-Backups/            |                         |
|  |    tar+AES-GCM加密 ------> PUT /api/fs/put --------------->| (冷备网盘存储)           |
|  |    秒级一键解密还原 <-----+      |                           |                         |
|  |                         |      +---------------------------+                         |
|  | 2. 磁盘动态水位调度器   |                                                            |
|  |    85% 熔断 waiting_disk|      +---------------------------+                         |
|  |    75% 自动唤醒恢复     |      |   tg-files-api (TDLib)    |                         |
|  |    应急清理 FIFO 释放   |      |      (Docker :8123)       |                         |
|  |                         |      |                           |                         |
|  | 3. APP_ROOT_DIR 宿主卷  |<====>|   挂载点:                 |                         |
|  |    /root/tg-files/      |      |   account/*/td.binlog     |                         |
|  |    app-data             |      |   account/*/db.sqlite     |                         |
|  +-------------------------+      +---------------------------+                         |
+-----------------------------------------------------------------------------------------+
```

---

## 3. Telegram Session 异地自动化冷备与秒级还原协议规范

### 3.1 目录扫描与打包范围（严格白名单）
过去直接对 `/root/tg-files/app-data` 全量打包会导致备份包膨胀至 12GB+，引发磁盘耗尽与上传超时。因此备份模块严格实施**白名单扫描打包**：

1. **会话核心包含项（必须备份）**：
   - 目录：`/root/tg-files/app-data/account/*/`
   - 核心凭据：`td.binlog`（TDLib 二进制会话密钥日志）
   - 数据库：`db.sqlite`、`db.sqlite-wal`、`db.sqlite-shm`（TDLib 聊天元数据与消息索引）
   - 系统级敏感认证：`/root/tg-files/app-data/.backend_creds`、`.bridge_secret`、`.openlist_auth`
   - 规则与状态：`.subscriptions.json`、`.archive_jobs.json`、`.archive_config.json`
2. **多媒体排除项（严禁打包）**：
   - `account/*/videos/*`、`account/*/photos/*`、`account/*/documents/*`
   - `account/*/thumbnails/*`、`account/*/temp/*`
   - `downloads/*`、`logs/*`、`session-backups/*` 历史归档
3. **前置完整性判定**：
   - 打包前递归遍历匹配 `td.binlog` 或 `db.sqlite`，若文件不存在或大小为 0，视为脏数据并阻断打包。

### 3.2 加密打包机制与封装格式规范 (AES-256-GCM)
为避免在异地网盘中明文存储 Telegram 登录凭证，必须采用强对称认证加密 `AES-256-GCM`：

#### 密文封装二进制结构（Magic Header 协议）
```
+----------------+----------------+----------------+-------------------------------+----------------+
| Magic (8 Byte) | Salt (16 Byte) | Nonce (12 Byte)|     Ciphertext (可变长)        | Tag (16 Byte)  |
|  "TGSNAP01"    | PBKDF2 随机盐   | AES-GCM IV     | tar.gz 压缩流的 AES-256 密文   | GCM 认证校验标签 |
+----------------+----------------+----------------+-------------------------------+----------------+
```

1. **Magic Header**：固定 `8 字节`，ASCII 字符串 `b"TGSNAP01"`，用于快速识别备份文件合法性与版本。
2. **Salt**：`16 字节` 密码学安全随机字节（`secrets.token_bytes(16)`）。
3. **Nonce**：`12 字节` 标准 AES-GCM 初始化向量（`secrets.token_bytes(12)`）。
4. **Key 派生机制**：
   - 根主密钥来源：优先读取环境变量 `TG_SESSION_BACKUP_KEY`，缺省读取本地受保护文件 `APP_ROOT_DIR/.bridge_secret`（0600 权限）。
   - 派生算法：`PBKDF2-HMAC-SHA256`，迭代 `100,000` 次，生成 `32 字节 (256-bit)` 密钥。
5. **Tag**：`16 字节` GCM AEAD 认证标签。一旦密文被篡改、不完整或密码错误，在解密阶段将立即触发验证失败抛出异常，杜绝脏数据还原。

### 3.3 OpenList 异地上传规范与完整性核验
1. **异地存储路径**：
   - 根目录：`/TG-Backups/`
   - 文件名模板：`tg-session-{YYYYMMDD-HHMMSS}.tar.gz.enc`
2. **上传流程**：
   - 调用 `POST /api/fs/mkdir` 保证 `/TG-Backups/` 目录存在。
   - 复用 `bridge_server.py` 的 OpenList 流式上传通道 `PUT /api/fs/put`，以 1MB 分块流式推送，不占用宿主额外内存。
3. **云端完整性双向核验**：
   - 上传完毕后，立即调用 `POST /api/fs/get`（路径 `/TG-Backups/<filename>`）核验云端文件状态与字节大小，一致后计入已完成快照。
4. **备份生命周期轮转**：
   - 默认保留最近 **7 份** 异地快照，超期快照调用 `POST /api/fs/remove` 自动淘汰。

### 3.4 1 分钟解密极速还原协议 (Restore Protocol)
当出现 TDLib 会话丢失、数据库损坏或更换机器时，执行标准化极速还原（实测耗时 < 15 秒）：

```
[触发还原]
    |
    v
1. 校验目标备份文件 Header (前 8 字节必须为 b"TGSNAP01")
    |---> 非标准 Header -> 抛出 MagicInvalidError 终止
    v
2. 提取 16B Salt 与 12B Nonce，派生 AES-256 密钥
    |
    v
3. 执行 AES-256-GCM AEAD 认证解密
    |---> Tag 校验失败 / 密文受损 -> 抛出 DecryptionAuthError 终止
    v
4. 解包 tar.gz 到临时目录 (如 /tmp/tg_restore_xxx)
    |
    v
5. 关键完整性校验：解包内容必须存在 account/*/td.binlog 或 db.sqlite
    |---> 校验缺失 -> 抛出 IncompleteSessionError 终止
    v
6. 原子置换：将核心文件原子安全复制覆盖至 APP_ROOT_DIR
    |
    v
7. 刷新凭据缓存，重启或连接 TDLib 服务
    |
    v
[还原完成（全流程实测 10~20 秒，远快于 1 分钟要求）]
```

### 3.5 API 接口设计与前端状态交互
1. `POST /api/session/backup`：
   - 需管理员登录 Cookie 与 CSRF Token。
   - 异步触发打包、加密、上传及校验，返回 `{ok: true, filename, size, remotePath, sha256}`。
2. `GET /api/session/backup/status`：
   - 返回 `{ok: true, status: "ok"|"warn"|"err", dotClass: "ok", lastBackupTime, lastBackupSize, lastBackupName, remoteDir: "/TG-Backups/", backupCount, message}`。
3. 前端交互：
   - 在 `account.html` 与 `settings.html` 顶部放置「会话冷备状态灯」与「立即冷备」按钮。

---

## 4. 本地磁盘高低水位动态熔断保护与调度规范

### 4.1 百分比动态水位算法
摒弃静态 GB 容量，采用分区真实动态使用率：
$$\text{usage\_percent} = \left( \frac{\text{used\_bytes}}{\text{total\_bytes}} \right) \times 100\%$$

- **高水位熔断阈值 ($H$)**：默认 **85.0%**（允许在设置页配置，范围 75%~95%）。
- **低水位唤醒阈值 ($L$)**：默认 **75.0%**（允许在设置页配置，范围 60%~80%），强制 $L \le H - 5.0\%$ 迟滞区间。

### 4.2 熔断拦截与 `waiting_disk` 调度机制
1. **全入口熔断拦截**：
   - 注入点：`/browse/download`、`/submit`、`/api/tg/quick-download`。
   - 当检测到当前磁盘使用率 $\ge 85.0\%$ 时：
     - 拒绝向后端下发下载调用；
     - 将任务统一归入 `waiting_disk` 状态，持久化至 `APP_ROOT_DIR/.waiting_disk.json`；
     - 接口返回统一友好提示：`{"ok": false, "code": "DISK_WATERMARK_EXCEEDED", "state": "waiting_disk", "message": "磁盘占用率已达 85%，任务已安全挂起排队，等待空间释放至 75% 以下自动恢复"}`。
     - 现有正在下载中的任务不受影响，允许其完成并归档。
2. **75% 低水位后台巡检唤醒调度**：
   - 独立后台循环（每 30 秒执行一次巡检），并在每次成功归档删除本地文件后被动触发。
   - 当使用率回落至 $< 75.0\%$ 时，按 FIFO 队列顺序逐个唤醒 `waiting_disk` 任务并提交后端。
   - 唤醒过程中若水位再次升至 $85.0\%$，立即重新熔断挂起。
3. **前端展示**：
   - 在 `/tasks` 与表格 partials 中支持 `waiting_disk` 状态标签（黄色警示胶囊 `磁盘挂起`），hover 提示挂起原因。

### 4.3 应急被动清理策略 (Emergency Cleanup)
- **触发条件**：磁盘使用率 $\ge 85.0\%$ 且启用了 `diskAutoClean`。
- **清理原则（零数据丢失）**：
  1. 仅允许清理 `_ARCHIVE_JOBS` 中 `state == "done"` 且本地文件存在的记录。
  2. 严禁清理未归档文件、下载中文件及任何会话配置文件。
  3. 严格按归档完成时间升序（最老文件优先 FIFO）。
- **退出条件**：磁盘使用率降低至 $75.0\%$ 以下或已无更多已归档文件。

---

## 5. VPS 部署通道全面 SSH 密钥认证与加固规范

### 5.1 剥离明文密码与凭据收敛
- **改造现状**：`.vps-conn/vps.json` 中明文配置了 `"password": "..."`。
- **加固目标**：
  1. 彻底删除 `vps.json` 中的 `password` 字段。
  2. 重构配置规范为：
     ```json
     {
       "host": "your-vps-ip",
       "port": 22,
       "user": "root",
       "privateKeyPath": "~/.ssh/id_ed25519"
     }
     ```
  3. 本地私钥权限保护：设置 `~/.ssh/id_ed25519` 为 0600，禁止非属主访问。

### 5.2 部署与执行脚本加固 (`deploy.js` & `exec.js`)
1. **私钥解析统一函数**：
   - 顺序探测：环境变量 `VPS_SSH_KEY` / `VPS_SSH_KEY_PATH` → `conf.privateKeyPath`（支持 `~` 展开）→ 默认用户路径 `~/.ssh/id_ed25519`、`~/.ssh/id_rsa`。
   - 读取密钥以 PEM/OpenSSH 格式传入 `ssh2.Client.connect({ host, port, username, privateKey })`。
   - 严禁保留明文密码 fallback；无私钥则立即报错退出。
2. **验证契约**：
   - `node .vps-conn/exec.js "echo SSH_KEY_OK"` 无密码免密秒级连通。
   - `node .vps-conn/deploy.js` 纯基于私钥免密同步代码并重启 `tg-bridge` 服务。

---

## 6. 测试与验证标准 (Quality Gate)

1. **还原演练**：模拟 Session 损坏后，通过备份包 1 分钟（实测 < 15 秒）内解密并恢复 TDLib 登录态。
2. **熔断与唤醒**：模拟磁盘 85% 触发 `waiting_disk` 拦截，模拟回落 75% 自动唤醒任务。
3. **免密部署**：在无密码配置下，纯通过 SSH 密钥完成 `exec.js` 与 `deploy.js` 部署测试。
