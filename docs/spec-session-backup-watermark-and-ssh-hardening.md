# TG Session 异地加密冷备、磁盘动态高低水位熔断与 SSH 凭据加固技术规范（P0）

**版本**：v1.0.0  
**状态**：定稿发布（Requirements Approved）  
**关联任务**：`t1` (Requirements 制定技术规范) → `t2` (Session 加密冷备与还原实现) → `t3` (磁盘高低水位熔断实现) → `t4` (SSH 密钥认证加固) → `t5` (测试验证) → `t6` (代码审查) → `t7` (安全审计) → `t8` (生产集成)  
**责任角色**：全栈与系统工程师 (`engineer`)  

---

## 1. 概述与设计目标

为了保证分布式 Telegram 资源转存服务在极端场景下的高可用性、数据完整性与主机基础设施安全，本规范统一确立以下三大核心能力的技术实现标准：

1. **Telegram Session 异地自动化加密快照与秒级解密还原**：
   - 将 TDLib 登录态核心凭证与系统关键配置做精细化打包，剥离无关的多媒体缓存。
   - 采用国际标准 `AES-256-GCM` 强对称加密，自动化推送到同机/异地 OpenList 云端网盘冷备目录。
   - 制定 1 分钟（实测秒级）解密解包还原协议，支持灾难时快速自愈。
   - 在 Web 管理台提供备份状态健康指示灯与一键即时快照操作。

2. **本地磁盘高低水位动态熔断与应急清理机制**：
   - 杜绝传统绝对值（如固定 5GB）阈值在不同规模存储卷下的失真问题，全面采用动态百分比水位。
   - 实施 **85% 高水位熔断**：在所有下载入口拦截新请求，安全置入 `waiting_disk` 挂起队列，不影响正在运行的下载。
   - 实施 **75% 低水位自动唤醒**：当磁盘占用回落至 75% 以下时，FIFO 自动恢复挂起任务的下载调度。
   - 实施 **被动应急清理**：在高水位告警时，按 FIFO 策略安全释放已成功归档到网盘的最早本地文件。

3. **VPS 部署通道全面 SSH 密钥认证改造**：
   - 彻底剥离 `.vps-conn/vps.json` 中的明文口令字段，杜绝凭据泄露。
   - 重构 `deploy.js` 与 `exec.js`，严格强制使用本地标准 SSH 密钥（ED25519/RSA）免密连接 VPS。
   - 强化本地凭据与私钥目录权限（0600 / ACL 用户隔离），实现端到端免密安全部署。

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
|  /root/.ssh/authorized_keys (受保护)                                                     |
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

## 3. Telegram Session 加密冷备与秒级还原协议规范

### 3.1 会话文件扫描与打包范围（白名单策略）
为避免以往将数 GB 的媒体文件打入备份导致磁盘暴涨，备份模块严格采用**白名单扫描打包**机制：

- **核心包含项（必须备份）**：
  - TDLib 登录态凭据：`account/*/td.binlog`
  - TDLib 数据库与索引：`account/*/db.sqlite*`（含 sqlite, sqlite-wal, sqlite-shm）
  - 账户凭据与应用密钥：`.backend_creds`、`.bridge_secret`、`.openlist_auth`
  - 业务规则与归档日志：`.subscriptions.json`、`.archive_jobs.json`、`.archive_config.json`
- **排除项（严禁打包）**：
  - `account/*/videos/*`、`account/*/photos/*`、`account/*/documents/*`
  - `account/*/thumbnails/*`、`account/*/temp/*`
  - `downloads/*`、`logs/*`、`*.tar.gz` 历史备份文件
- **一致性保证**：
  - 打包前检查 `td.binlog` 或 `db.sqlite` 必须存在且非空，否则判定数据源异常并终止备份。

### 3.2 强对称加密与封装格式规范 (AES-256-GCM)
为确保异地存储安全，备份包严禁明文外泄。采用认证加密算法 `AES-256-GCM`：

#### 密文封装格式协议（二进制流结构）
```
+----------------+----------------+----------------+-------------------------------+----------------+
| Magic (8 Byte) | Salt (16 Byte) | Nonce (12 Byte)|     Ciphertext (可变长)        | Tag (16 Byte)  |
|  "TGSNAP01"    | PBKDF2 随机盐   | AES-GCM IV     | tar.gz 压缩流的 AES-256 密文   | GCM 认证校验标签 |
+----------------+----------------+----------------+-------------------------------+----------------+
```

1. **Magic Header**：固定 `8 字节`，ASCII 字符串 `b"TGSNAP01"`，用于快速识别备份文件类型与版本。
2. **Salt**：`16 字节` 高强度密码学安全随机字节（`secrets.token_bytes(16)`）。
3. **Nonce**：`12 字节` 标准 GCM 初始化向量（`secrets.token_bytes(12)`）。
4. **Key 派生协议**：
   - 根材料来源：优先读取环境变量 `TG_SESSION_BACKUP_KEY`，若未提供则读取数据目录受保护密钥 `APP_ROOT_DIR/.bridge_secret`（0600权限）。
   - 算法：`PBKDF2-HMAC-SHA256`，迭代次数 `100,000` 次，派生出 `32 字节 (256-bit)` 密钥。
5. **Tag**：`16 字节` GCM 认证标签（AEAD）。任何密文被篡改或密码不正确，Tag 校验将在解密首阶段失败并抛出异常，100% 杜绝脏数据被还原。

### 3.3 OpenList 异地冷备上传规范
1. **上传路径**：
   - 根目录：`/TG-Backups/`
   - 文件名格式：`tg-session-{YYYYMMDD-HHMMSS}.tar.gz.enc`
2. **目录前置建置**：
   - 在上传前调用 `POST /api/fs/mkdir`（参数 `{"path": "/TG-Backups"}`），确保目标目录就绪。
3. **流式上传**：
   - 继承项目既有 `PUT /api/fs/put` 管道，以 1MB 内存分块异步推流至 OpenList，不把整个加密大文件堆积在内存。
4. **备份轮转机制**：
   - 云端保留最近 **7 份** 快照，调用 OpenList `POST /api/fs/list` 巡检 `/TG-Backups/`，超期自动调用 `POST /api/fs/remove` 淘汰旧包。
5. **完整性双向校验**：
   - 上传完成后，调用 `POST /api/fs/get` 核验云端文件的存在性与 `size` 大小一致性。

### 3.4 1 分钟解密解包还原协议 (Disaster Recovery Protocol)
当 Telegram 出现会话失效、文件损坏或新机迁移时，执行以下标准化秒级自愈还原：

```
[开始还原]
    |
    v
1. 校验源备份文件 Magic Header (前 8 字节必须为 b"TGSNAP01")
    |---> 不匹配 -> 抛出 MagicInvalidError 终止
    v
2. 提取 16B Salt 与 12B Nonce，派生 AES-256 密钥
    |
    v
3. 执行 AES-256-GCM AEAD 认证解密
    |---> Tag 校验失败 / 密文损坏 -> 抛出 DecryptionAuthError 终止
    v
4. 将解密出来的流解压至临时目录 (例如 /tmp/tg_restore_xxx)
    |
    v
5. 关键完整性校验：检查解压产物必须包含 td.binlog 或 db.sqlite
    |---> 校验不通过 -> 抛出 IncompleteSessionError 终止
    v
6. 原子置换：安全将核心文件复制并覆盖至 APP_ROOT_DIR
    |
    v
7. 刷新并重建应用凭据，重载 TDLib 客户端
    |
    v
[还原完成（全流程实测 < 15 秒）]
```

### 3.5 API 接口设计

#### 1. 触发手动冷备：`POST /api/session/backup`
- **鉴权**：需要 Session 登录 Cookie + `X-CSRF-Token` 头。
- **返回数据契约**：
  ```json
  {
    "ok": true,
    "filename": "tg-session-20260906-103000.tar.gz.enc",
    "size": 1572864,
    "remotePath": "/TG-Backups/tg-session-20260906-103000.tar.gz.enc",
    "sha256": "3a8f1...",
    "backupTime": "2026-09-06T10:30:00Z",
    "message": "会话加密快照已成功上传至 OpenList 异地冷备目录"
  }
  ```

#### 2. 查询冷备状态与指示灯：`GET /api/session/backup/status`
- **鉴权**：系统会话鉴权。
- **返回数据契约**：
  ```json
  {
    "ok": true,
    "status": "ok",           // "ok": 24小时内有成功备份; "warn": 超过24小时; "err": 备份失败/无备份
    "dotClass": "ok",         // 前端指示灯样式类
    "label": "冷备正常",       // 状态文案
    "lastBackupTime": "2026-09-06 10:30:00",
    "lastBackupSize": "1.5 MB",
    "lastBackupName": "tg-session-20260906-103000.tar.gz.enc",
    "remoteDir": "/TG-Backups/",
    "backupCount": 3,
    "message": "最近异地快照正常，支持秒级解密自愈"
  }
  ```

---

## 4. 本地磁盘高低水位动态熔断保护与调度规范

### 4.1 百分比动态水位算法
摒弃传统的绝对可用容量，基于应用所在分区计算真实使用率：
$$\text{usage\_percent} = \left( \frac{\text{used\_bytes}}{\text{total\_bytes}} \right) \times 100\%$$

- **高水位熔断阈值 ($H$)**：默认 **85.0%**（支持在设置中配置，范围 75%~95%）。
- **低水位唤醒阈值 ($L$)**：默认 **75.0%**（支持在设置中配置，范围 60%~80%），且必须满足 $L \le H - 5.0\%$，预留至少 5%~10% 的回滞迟滞区间（Hysteresis），防止在阈值边缘产生高频抖动。

### 4.2 熔断拦截与 `waiting_disk` 调度状态机

```
                      +-------------------+
                      |   用户提交新下载   |
                      +---------+---------+
                                |
                                v
                   [当前磁盘使用率 >= 85%?]
                     /                \
            [是: 熔断触发]          [否: 正常放行]
                  /                      \
                 v                        v
        +------------------+     +-------------------+
        |  置为 waiting_disk|     | 后端开始下载任务   |
        |  挂起排队，不落盘 |     +---------+---------+
        +--------+---------+               |
                 |                         v
                 |               [下载完成并归档成功]
                 |                         |
                 |                         v
                 |               [磁盘水位回落至 < 75%?]
                 |                         |
                 +<------------------------+
                 |
                 v
        [按 FIFO 唤醒挂起任务]
                 |
                 v
        +-------------------+
        | 恢复调度执行下载   |
        +-------------------+
```

1. **拦截点注入**：
   - `/browse/download`（文件浏览页批量下载）
   - `/submit`（链接批量提交）
   - `/api/tg/quick-download`（快捷直投下载）
2. **挂起持久化**：
   - 处于 `waiting_disk` 的任务在系统内存与 `_PENDING_DISK_TASKS` 队列中保留，持久化至 `APP_ROOT_DIR/.waiting_disk.json`。
   - 任务列表（`/tasks`）和表格 partials 增加状态标签：显示黄色胶囊标签 `磁盘挂起`，鼠标悬浮显示 `本地磁盘使用率达 85%，任务已安全挂起，回落至 75% 自动唤醒`。
3. **低水位自动唤醒引擎**：
   - 后台守护巡检线程（每 30 秒执行一次），以及任意文件归档/删除完成后被动触发。
   - 当检测到使用率 $< 75\%$ 时，按提交顺序逐个出队唤醒，恢复调用 `BACKEND.start_download_multiple`。若在唤醒过程中磁盘再度达到 85%，立刻中止唤醒并保持其余任务挂起。

### 4.3 应急被动清理策略 (Emergency Cleanup)
- **触发条件**：磁盘使用率 $\ge 85\%$ 时，且开启了 `diskAutoClean`。
- **清理约束（零数据丢失防御）**：
  1. 必须在 `_ARCHIVE_JOBS` 中查证该文件 `state == "done"`（已 100% 上传到 OpenList）。
  2. 本地文件存在且未被标记 `local_deleted`。
  3. 严禁删除任何未成功归档的半成品文件、下载中文件或系统配置文件。
- **释放顺序**：按照归档时间戳升序（最老文件优先 FIFO）。
- **退出条件**：磁盘使用率降至 $75\%$ 以下，或所有已归档文件已全部清理完毕。

---

## 5. VPS 部署通道 SSH 密钥认证与凭据加固规范

### 5.1 凭据存储收敛与明文剥离
- **现有问题**：`.vps-conn/vps.json` 包含明文口令 `"password": "..."`，存在严重安全隐患。
- **加固标准**：
  1. 彻底从 `.vps-conn/vps.json` 中删除 `"password"` 键。
  2. 配置文件只保留网络与身份元数据：
     ```json
     {
       "host": "your-vps-ip",
       "port": 22,
       "user": "root",
       "privateKeyPath": "~/.ssh/id_ed25519"
     }
     ```
  3. 本地私钥文件权限保护：
     - POSIX 环境：`chmod 600 ~/.ssh/id_ed25519`
     - Windows 环境：收敛 ACL，仅当前用户具备完全控制权。

### 5.2 `deploy.js` 与 `exec.js` 统一鉴权改造规范
- **实现逻辑**：
  ```javascript
  const fs = require('fs');
  const path = require('path');
  const os = require('os');

  function resolvePrivateKey(conf) {
    // 优先级 1: 环境变量注入
    if (process.env.VPS_SSH_KEY) return process.env.VPS_SSH_KEY;
    if (process.env.VPS_SSH_KEY_PATH) return fs.readFileSync(process.env.VPS_SSH_KEY_PATH, 'utf8');

    // 优先级 2: conf 中的配置路径（支持 ~ 展开）
    let keyPath = conf.privateKeyPath;
    if (keyPath) {
      if (keyPath.startsWith('~')) {
        keyPath = path.join(os.homedir(), keyPath.slice(1));
      }
      if (fs.existsSync(keyPath)) return fs.readFileSync(keyPath, 'utf8');
    }

    // 优先级 3: 默认标准用户路径
    const defaultEd25519 = path.join(os.homedir(), '.ssh', 'id_ed25519');
    if (fs.existsSync(defaultEd25519)) return fs.readFileSync(defaultEd25519, 'utf8');
    const defaultRsa = path.join(os.homedir(), '.ssh', 'id_rsa');
    if (fs.existsSync(defaultRsa)) return fs.readFileSync(defaultRsa, 'utf8');

    throw new Error('未检测到可用的 SSH 私钥（ED25519/RSA），严禁使用明文密码连接！');
  }
  ```
- **连接调用**：
  ```javascript
  conn.connect({
    host: conf.host,
    port: conf.port || 22,
    username: conf.user,
    privateKey: resolvePrivateKey(conf),
    readyTimeout: 20000,
  });
  ```
- **安全红线**：禁止在代码中保留任何密码认证 fallback；私钥解析失败直接快速失败（Fail-Fast）。

---

## 6. 测试与验证策略 (Quality Gate Contract)

### 6.1 单元与集成测试套件规划
| 测试套件文件 | 覆盖模块 | 核心验证点 |
|---|---|---|
| `test_session_backup.py` | 会话快照、AES-GCM加密、解密还原 | 1. 模拟 `td.binlog` 完整性校验；<br>2. 密文篡改拦截（Tag 不匹配时抛出异常）；<br>3. 模拟会话损坏后 1 分钟解密还原，无缝恢复登录态。 |
| `test_disk_watermark.py` | 高低水位熔断与唤醒 | 1. 模拟磁盘达到 85%，验证 `/browse/download`、`/submit` 被拦截并返回 `waiting_disk`；<br>2. 验证任务列表展现挂起状态；<br>3. 模拟水位降至 75%，验证挂起任务自动唤醒调度。 |
| `.vps-conn/exec.js` | SSH 密钥认证通道 | 执行 `node .vps-conn/exec.js "echo SSH_KEY_OK"`，完全剥离密码字段，100% 凭 ED25519 私钥秒级连通。 |

### 6.2 验收指标矩阵
- **Session 还原耗时**：端到端解密与还原完成时间 $< 30$ 秒（指标上限 60 秒）。
- **Session 密文强度**：AES-256-GCM，密码学随机盐与随机 IV，抗篡改。
- **磁盘熔断反应**：高水位判定延迟 $< 100\text{ms}$，拦截率 100%。
- **免密部署稳定性**：部署脚本无密码输入提示，全自动热更新与服务重启成功率 100%。

---

## 7. 模块演进与后续交付路线

- **t1 (当前任务)**：发布本技术规范文档，完成团队评审。
- **t2 (下阶段实现)**：编写 `bridge_server.py` 会话快照、AES-256-GCM 加密、OpenList 上传及前端 `account.html`/`settings.html` 状态灯与还原脚本。
- **t3 (下阶段实现)**：编写 `bridge_server.py` 动态百分比水位调度、`waiting_disk` 队列、低水位唤醒与 settings 配置。
- **t4 (下阶段实现)**：重构 `.vps-conn/deploy.js`、`exec.js` 与 `vps.json`，完成私钥加固并清理所有明文密码。
- **t5~t8**：测试套件验证、代码审查、安全专项审计、生产热部署。
