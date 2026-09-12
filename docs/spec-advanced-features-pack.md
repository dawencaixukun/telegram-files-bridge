# 4 项高级产品特性技术架构与接口规范文档（P0）

**版本**：v1.0.0  
**状态**：定稿发布（Requirements Approved）  
**关联任务**：`t1` (Requirements 制定技术规范) → `t2` (下载防重与资产关联实现) → `t3` (Telegram通知引擎实现) → `t4` (跨库全局搜索与Ctrl+K实现) → `t5` (归档失败归类与批量重试实现) → `t6` (自动化测试验证) → `t7` (架构与代码评审) → `t8` (安全审计) → `t9` (生产环境部署与全链路验收)  
**责任角色**：全栈与系统工程师 (`engineer`)  

---

## 1. 概述与整体目标

为了进一步将本 TG 视频下载与网盘归档管理系统打造为工业级、自动化、智能化的一站式媒体转存平台，本规范针对用户在高频使用过程中最关键的效率痛点，统一设计并制定以下 4 项高级特性的技术规范与接口契约：

1. **全局 uniqueId 下载防重与智能关联网盘**：
   - 建立覆盖“当前任务队列 + 本地在存资产 + 云端网盘归档”的三层全局文件指纹库。
   - 以 `uniqueId`（TDLib 稳定远程标识符）为一阶精确防重，辅以 `(normalized_filename, size_bytes)` 为二阶内容指纹防重。
   - 在解析链接、批量投递、提交下载等各个入口实施智能查重拦截，对已存在资产提供状态提示与直达云端/本地入口，支持用户按需强制重下。

2. **Telegram 消息通知外发引擎（Bot/收藏夹双通道）**：
   - 构建非阻塞、高可靠的 Telegram 外发通知引擎，支持 **Telegram 独立 Bot** 与 **当前登录用户收藏夹 (Saved Messages)** 双通道独立/并行推送。
   - 涵盖四大核心生命周期事件模型：大文件下载完成（阈值控制）、网盘归档成功、网盘归档失败告警、磁盘高低水位熔断告警。
   - 采用精致 HTML 卡片排版，内嵌状态指示 Emoji、元数据网格（文件名、大小、耗时、速率、网盘路径）与可点击直达按钮。
   - 内建防 SSRF 白名单限制与 1 msg/s 令牌桶平滑限流队列。

3. **跨库全局聚合搜索（本地+云端+任务，支持 Ctrl+K 快捷面板）**：
   - 打破存储边界，提供单入口统一检索【任务队列】、【本地磁盘在存】、【云端网盘归档】三库资产。
   - 统一规范 API 响应三栏数据结构与多维度加权模糊打分匹配算法（支持拼音、大小写不敏感、多词 AND 匹配）。
   - 前端集成现代化玻璃拟态 Command Palette 弹窗，支持全局 `Ctrl+K`（Mac `Cmd+K`）即时呼出、键盘上下导航、回车激活、搜索防抖。

4. **归档失败智能归类引擎与一键批量重试**：
   - 针对网盘归档过程中的各类异构报错，构建正则与特征模式识别引擎，统一归入 6 大标准化故障类别：🔑 鉴权过期 (`AUTH_EXPIRED`)、💾 容量超限 (`QUOTA_EXCEEDED`)、⚠️ 文件冲突 (`FILE_CONFLICT`)、🌐 网络超时 (`NETWORK_TIMEOUT`)、📁 本地文件缺失 (`FILE_NOT_FOUND`)、❓ 未知错误 (`UNKNOWN_ERROR`)。
   - 提供归档失败聚合诊断看板（按类别统计、故障建议与恢复策略）。
   - 建立一键批量重试调度引擎，支持分类一键批量重放、自动凭据再刷新 (`_openlist_relogin`) 与强制覆盖冲突策略。

---

## 2. 系统整体架构与交互拓扑

```
+-----------------------------------------------------------------------------------------------+
|                                    Web 管理控制台 (前端 UI)                                      |
|                                                                                               |
|  +-------------------------+  +----------------------------+  +----------------------------+  |
|  |  快捷链接栏 / 模态提交   |  |   Ctrl+K 全局聚合搜索面板    |  |    云端/任务失败诊断看板    |  |
|  |  (防重拦截 & 智能提示)   |  |   (任务/本地/云端三栏展示)  |  |    (分类聚合 & 一键重试)    |  |
|  +------------+------------+  +--------------+-------------+  +--------------+-------------+  |
+---------------|------------------------------|-------------------------------|----------------+
                | HTTP / Fetch                 | GET /api/search/aggregate     | POST /api/archive/batch-retry
                v                              v                               v
+-----------------------------------------------------------------------------------------------+
|                               FastAPI 桥接核心服务 (bridge_server.py)                            |
|                                                                                               |
|  [ 1. 全局防重指纹库 ] <======================================================+                 |
|       ├─ uniqueId (TDLib remoteId) 精确索引                                    |                 |
|       └─ (norm_name, size) 内容哈希索引                                        |                 |
|            ├── 任务池 (tasks_all): 正在下载/排队中                               |                 |
|            ├── 本地池 (/library/local): 本地磁盘在存                             |                 |
|            └── 云端池 (_ARCHIVE_JOBS / OpenList): 已归档网盘资产                 |                 |
|                                                                              |                 |
|  [ 2. 消息通知外发引擎 ] ──(Async Queue / 1 msg/s)──+                        |                 |
|       ├── 事件分发器 (Event Dispatcher)               |                        |                 |
|       │    ├─ DOWNLOAD_COMPLETED (大文件>=50MB)      |                        |                 |
|       │    ├─ ARCHIVE_SUCCESS                        |                        |                 |
|       │    ├─ ARCHIVE_FAILED                         |                        |                 |
|       │    └─ DISK_WATERMARK_ALERT (85%熔断/75%唤醒) |                        |                 |
|       ├── 通道 A: Telegram Bot API (https://api.telegram.org 严格白名单)       |                 |
|       └── 通道 B: TDLib 登录态 Saved Messages (收藏夹直投)                      |                 |
|                                                                              |                 |
|  [ 3. 跨库全局聚合搜索引擎 ]                                                  |                 |
|       └── 并行检索 Task Store + Local Files + OpenList Cloud Archive         |                 |
|                                                                              |                 |
|  [ 4. 归档失败分类与重试引擎 ]                                                |                 |
|       ├── 智能正则分类器 (Token过期 / 容量超限 / 文件冲突 / 网络超时 / 本地缺失)  |                 |
|       └── 批量重试调度器 (自动 Relogin / 策略覆盖 / 指数退避)                     |                 |
+-----------------------------------------------------------------------------------------------+
       |                                       |                               |
       v (Unix Socket / HTTP :8123)            v (HTTP :5244)                  v (Telegram Cloud)
+-----------------------------+     +-----------------------------+     +-----------------------+
|    tg-files-api (TDLib)     |     |       OpenList 网盘引擎      |     |  Telegram Bot API &   |
|   (Telegram 协议核心服务)     |     |      (多网盘挂载与归档)       |     |   User Saved Messages |
+-----------------------------+     +-----------------------------+     +-----------------------+
```

---

## 3. 特性一：全局 uniqueId 下载防重与智能关联网盘

### 3.1 核心算法与防重层级

```python
# 查重流程伪代码示意
def check_duplicate(unique_id: str, filename: str, size_bytes: Optional[int]) -> DuplicateResult:
    # Level 1: 检查云端网盘已归档资产（最高优先级：无需重复下载和占用 VPS 磁盘）
    cloud_match = lookup_cloud_archive(unique_id, filename, size_bytes)
    if cloud_match:
        return DuplicateResult(duplicate=True, type="cloud", asset=cloud_match)
        
    # Level 2: 检查本地磁盘在存资产（次优先级：文件已在磁盘，无需重新拉取网络带宽）
    local_match = lookup_local_library(unique_id, filename, size_bytes)
    if local_match and local_file_exists_on_disk(local_match):
        return DuplicateResult(duplicate=True, type="local", asset=local_match)
        
    # Level 3: 检查当前任务队列（防止并发重复添加）
    task_match = lookup_in_flight_tasks(unique_id, filename, size_bytes)
    if task_match and task_match["status"] in ("downloading", "queued"):
        return DuplicateResult(duplicate=True, type="task", asset=task_match)
        
    return DuplicateResult(duplicate=False, type="none")
```

1. **唯一身份识别（Primary Key）**：
   - 以 TDLib 的 `uniqueId`（即 `FileRecord.uniqueId`，Base64 编码的远程唯一标识符）为唯一强比对键。
   - 该标识符在 Telegram 官方服务器端跨频道转发、跨会话引用全局唯一，不可篡改。
2. **内容特征指纹（Secondary Key）**：
   - 当 `uniqueId` 因消息历史或外部链接暂不可用时，采用 `(normalize_name(filename), size_bytes)` 复合比对。
   - `normalize_name`：转小写、剥离两端标点空白、去除常见转发前缀/后缀（如 `[copy]`, `(1)` 等）。
3. **关联资产动态判定**：
   - 云端已归档：通过 `_ARCHIVE_JOBS` 检验 `state == "done"` 且记录中远端路径有效，同时生成对应的 OpenList Web 直达浏览链接。
   - 本地在存：通过 `tasks_all()` 检验 `downloadStatus == "completed"` 且 `localPath` 在 VPS 宿主机磁盘上真实存在且大小大于 0。
   - 正在执行：任务处于 `downloadStatus == "downloading"` 或 `queued`，提取实时下载进度百分比与剩余预估。

### 3.2 接口契约规范

#### 1) 专用查重检测接口：`POST /api/files/check-dedup`
- **说明**：供前端提交下载、弹窗校验或快捷输入栏即时检测重复性。
- **请求方法**：`POST`
- **请求体（JSON）**：
```json
{
  "uniqueId": "AQADAgADx6cxG...",
  "filename": "Sample_Video_1080p.mp4",
  "size": 104857600,
  "link": "https://t.me/c/1827364521/987"
}
```
- **响应体（JSON）**：
```json
{
  "ok": true,
  "code": "SUCCESS",
  "data": {
    "duplicate": true,
    "duplicateType": "cloud",
    "matchedBy": "unique_id",
    "message": "该文件已在云端网盘归档中存在，无需重复下载",
    "asset": {
      "uniqueId": "AQADAgADx6cxG...",
      "filename": "Sample_Video_1080p.mp4",
      "size": 104857600,
      "sizeHuman": "100.0 MB",
      "status": "archived",
      "cloudPath": "/AliyunDrive/Media/Sample_Video_1080p.mp4",
      "drive": "AliyunDrive",
      "openlistUrl": "http://127.0.0.1:5244/AliyunDrive/Media/Sample_Video_1080p.mp4",
      "archivedAt": 1725280000,
      "localPath": null,
      "taskId": null,
      "downloadProgress": null
    },
    "actions": [
      {
        "type": "open_cloud",
        "label": "直达网盘查看",
        "url": "http://127.0.0.1:5244/AliyunDrive/Media/Sample_Video_1080p.mp4"
      },
      {
        "type": "force_download",
        "label": "忽略并重新下载",
        "action": "confirm_force"
      }
    ]
  }
}
```

#### 2) `/browse`、`/submit` 与直投接口防重联动：
- **前置查重与资产提示契约**：
  - `/browse` 频道文件浏览流：在 `_browse_rows` 中基于 `uniqueId`、`filename`、`size` 对齐三层指纹库，行数据内嵌 `is_archived`、`cloud_path`、`is_downloaded`、`local_path` 标记；
  - `/submit` 批量提交下载页面：在解析 links 时前置对比指纹库，若全部已归档或已下载则前端给予资产状态弹窗提示并标明位置，支持一键查看资产；
  - 顶栏直投输入栏 (`#quickLinkBar`) 与 `POST /api/tg/resolve-link`：返回的媒体文件数组中，每个对象统一注入 `dedup` 字段，携带云端/本地/任务归属与直达路径；
- `POST /api/tg/quick-download` 与 `POST /tasks`：
  - 新增参数：`force: bool = false`（默认 false）。
  - 若 `force == false` 且检测到已在云端或任务中，拒绝无意义重复下载，返回 HTTP 409 状态码及关联资产信息：
    ```json
    {
      "ok": false,
      "code": "DUPLICATE_ASSET",
      "message": "文件已存在于云端网盘归档中",
      "asset": { ... }
    }
    ```
  - 用户可在前端对话框确认“强制下载”，带上 `force: true` 重新提交。

---

## 4. 特性二：Telegram 消息通知外发引擎（Bot/收藏夹双通道）

### 4.1 双通道架构与外发协议

1. **通道一：Telegram 独立 Bot（标准服务通知）**：
   - 依赖项：`botToken`（如 `123456789:ABCdef...`）与 `chatId`（接收者用户 ID、群组 ID 或频道 ID）。
   - 通信协议：直接调用 Telegram 官方 Bot API `https://api.telegram.org/bot<botToken>/sendMessage`。
   - 格式：HTML 富文本，`parse_mode=HTML`，`disable_web_page_preview=true`。
2. **通道二：Telegram 账号收藏夹（Saved Messages 零配置通道）**：
   - 依赖项：无需用户另外申请 Bot，直接复用当前系统绑定的 TG 客户端账号。
   - 通信协议：通过 `BACKEND` 桥接向自身账号专属 `Saved Messages` 发送文本/卡片消息（调用 TDLib 的 `sendMessage`，目标 `chat_id = user_id`）。
3. **通道选择与冗余策略**：
   - 支持设置通知模式：`"both"`（双通道同时发送）、`"bot"`（仅 Bot）、`"saved_messages"`（仅收藏夹）、`"none"`（关闭）。

### 4.2 核心事件模型定义

| 事件类型 (`eventType`) | 触发时机 | 默认开启 | 过滤阈值 | 核心 payload |
|---|---|---|---|---|
| `DOWNLOAD_COMPLETED` | TDLib 下载任务完成时 | 是 | 单文件大小 $\ge 50\text{ MB}$（可配置） | `filename`, `size`, `elapsedSec`, `speedHuman`, `chatTitle`, `localPath` |
| `ARCHIVE_SUCCESS` | OpenList 上传完成 (`state=="done"`) | 是 | 无 | `filename`, `size`, `remotePath`, `drive`, `openlistUrl`, `elapsedSec` |
| `ARCHIVE_FAILED` | 归档失败 (`state=="failed"`) | 是 | 无 | `filename`, `remotePath`, `category`, `errorDetail`, `retryUrl` |
| `DISK_WATERMARK_ALERT`| 本地磁盘触达 85% 高水位或 75% 唤醒时 | 是 | 状态改变或每隔 30 分钟防抖 | `currentPercent`, `usedHuman`, `freeHuman`, `suspendedTasksCount`, `action` |

### 4.3 卡片排版与 HTML 模板设计

Telegram 官方 Bot API 与客户端均支持基础 HTML 标签。通知引擎统一规范以下 4 套专业卡片排版：

#### 1) 大文件下载完成卡片
```html
🎉 <b>【大文件下载完成】</b>
━━━━━━━━━━━━━━━━━━
📦 <b>文件名称：</b><code>{filename}</code>
📊 <b>文件大小：</b><code>{size_human}</code>
⏱️ <b>下载耗时：</b><code>{elapsed_str}</code>（均速: <code>{speed_human}</code>）
💬 <b>来源会话：</b><code>{chat_title}</code>
💾 <b>本地路径：</b><code>{local_path}</code>
━━━━━━━━━━━━━━━━━━
<i>💡 该文件已安全落地，等待归档转存。</i>
```

#### 2) 网盘归档成功卡片
```html
☁️ <b>【网盘归档成功】</b>
━━━━━━━━━━━━━━━━━━
📦 <b>文件名称：</b><code>{filename}</code>
📊 <b>文件大小：</b><code>{size_human}</code>
📁 <b>目标网盘：</b><code>{drive}</code>
📂 <b>存储路径：</b><code>{remote_path}</code>
⏱️ <b>上传耗时：</b><code>{elapsed_str}</code>
━━━━━━━━━━━━━━━━━━
🔗 <a href="{openlist_url}">点击直接在 OpenList 中查看文件</a>
```

#### 3) 归档失败预警卡片
```html
🚨 <b>【网盘归档失败告警】</b>
━━━━━━━━━━━━━━━━━━
📦 <b>文件名称：</b><code>{filename}</code>
📂 <b>目标路径：</b><code>{remote_path}</code>
⚠️ <b>故障类别：</b><b>{category_label}</b>
❌ <b>详细原因：</b><code>{error_detail}</code>
━━━━━━━━━━━━━━━━━━
<i>🔧 处理建议：{suggested_action}</i>
👉 <a href="{dashboard_retry_url}">前往管理台执行一键重试</a>
```

#### 4) 磁盘高低水位告警卡片
```html
⚠️ <b>【VPS 磁盘高水位熔断告警】</b>
━━━━━━━━━━━━━━━━━━
📈 <b>当前磁盘占用：</b><code>{current_percent}%</code>（阈值: <code>85%</code>）
💽 <b>剩余可用空间：</b><code>{free_human}</code> / <code>{total_human}</code>
⏸️ <b>调度状态：</b>新任务已自动安全置入挂起队列（{waiting_count} 个任务等待）
━━━━━━━━━━━━━━━━━━
<i>🚨 系统已启动自动应急清理与保护，请关注存储安全。</i>
```

### 4.4 安全与高可用防护

1. **严格防 SSRF 机制**：
   - 目标请求 URL 强制固化为 `https://api.telegram.org/bot<token>/sendMessage`。
   - 严禁通过请求参数动态拼接域名，拦截任何针对 `127.0.0.1`、私有 IP 段（RFC1918）或未知内网服务的代理转发。
2. **凭据脱敏存储**：
   - 配置文件保存在受保护的 `.notify_config.json`（权限 0600）。
   - 前端查询配置接口对 `botToken` 自动脱敏，仅展示前 6 位与后 4 位，如 `718293:******kL9a`。
3. **令牌桶与异步平滑发送队列**：
   - 发送操作全部置入后台 `asyncio.Queue` 异步工作线程，单条发送间隔强制 $\ge 1.0$ 秒，杜绝瞬间并发导致 TG 触发 HTTP 429 封禁。
   - 相同的磁盘告警实施 10 分钟本地防抖去重。

### 4.5 接口契约规范

- `GET /api/notify/config`：读取通知配置（含脱敏 Token、启用状态、事件勾选、最小体积）。
- `POST /api/notify/config`：保存通知配置。
- `POST /api/notify/test`：测试外发一条测试通知卡片，验证 Bot Token / Chat ID / 收藏夹通道连通性。

---

## 5. 特性三：跨库全局聚合搜索与 Ctrl+K 快捷面板

### 5.1 数据源全景覆盖

全局聚合搜索统一覆盖以下三大数据源：

1. **Tasks 任务库**：
   - 数据源：`tasks_all()` 内存与后端任务缓存。
   - 检索字段：`filename`, `chatTitle`, `_unique_id`, `status`。
   - 状态标识：`queued` (排队), `downloading` (下载中), `completed` (完成), `failed` (失败)。
2. **Local 本地资产库**：
   - 数据源：本地在存且落盘的文件列表。
   - 检索字段：`name`, `localPath`, `uniqueId`, `date_str`。
   - 状态标识：`downloaded` (已下载未归档), `archived` (已归档并在存)。
3. **Cloud 云端归档库**：
   - 数据源：`_cloud_archive_rows()` 及 OpenList 远端索引。
   - 检索字段：`filename`, `remote_path`, `drive`, `unique_id`。
   - 状态标识：`archived` (云端正常), `missing` (云端已删)。

### 5.2 聚合搜索算法设计

- **多条件匹配逻辑**：
  - 忽略大小写（Case-insensitive）。
  - 支持空格分词 AND 检索（例如输入 `"deb 2026"`，需同时命中包含 `deb` 和 `2026` 的项目）。
  - 匹配字段权重机制：
    - 文件名完全前缀匹配：权重 100
    - 文件名子串匹配：权重 80
    - 路径/频道名匹配：权重 50
    - uniqueId 精确匹配：权重 120
- **响应上限与性能约束**：
  - 单分类默认最多返回 10 条匹配项，总上限 30 条。
  - 内存级线性扫描在 10,000 条记录下实测耗时 $< 15\text{ms}$，响应极速。

### 5.3 接口契约规范：`GET /api/search` 与 `GET /api/search/aggregate`

- **请求方法**：`GET`（支持 `/api/search` 与 `/api/search/aggregate` 别名互通）
- **查询参数**：
  - `q`: 搜索关键词字符串（最小 1 字符，最大 128 字符，自动去除首尾空白）。
  - `limit`: 单分类最大返回条数（默认 10，上限 50）。
- **响应格式（JSON）**：
```json
{
  "ok": true,
  "query": "debian",
  "total": 5,
  "counts": {
    "tasks": 1,
    "local": 2,
    "cloud": 2
  },
  "results": {
    "tasks": [
      {
        "id": 1001,
        "uniqueId": "AQAD_xxx1",
        "filename": "debian-12.0.0-amd64.iso",
        "size": "629.1 MB",
        "status": "downloading",
        "progress": 55,
        "source": "Linux Mirror",
        "actionUrl": "/tasks"
      }
    ],
    "local": [
      {
        "uniqueId": "AQAD_xxx2",
        "filename": "debian-11.5.0-amd64.iso",
        "size": "580.0 MB",
        "localPath": "/root/tg-files/downloads/debian-11.5.0.iso",
        "date": "2026-09-01 12:00",
        "isArchived": false,
        "actionUrl": "/library/local"
      }
    ],
    "cloud": [
      {
        "id": "arch_77",
        "uniqueId": "AQAD_xxx3",
        "filename": "debian-10.0.0-amd64.iso",
        "size": "550.0 MB",
        "cloudPath": "/Aliyun/ISOs/debian-10.0.0-amd64.iso",
        "drive": "Aliyun",
        "archivedTime": "2026-08-28 10:15",
        "openlistUrl": "http://127.0.0.1:5244/Aliyun/ISOs/debian-10.0.0-amd64.iso",
        "actionUrl": "/library/cloud"
      }
    ]
  }
}
```

### 5.4 前端 Command Palette（Ctrl+K 快捷呼出面板）规范

1. **按键监听与呼出逻辑**：
   - 监听 `window` 级别键盘事件：
     ```javascript
     document.addEventListener('keydown', function(e) {
       if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
         e.preventDefault();
         window.__toggleGlobalSearch();
       }
       if (e.key === 'Escape' && window.__isGlobalSearchOpen()) {
         window.__closeGlobalSearch();
       }
     });
     ```
   - 顶栏搜索栏内嵌快捷提示：提供可视化的快捷胶囊 `[Ctrl K]`，点击直接呼出模态框。
2. **面板交互与无障碍规范**：
   - 自动聚焦：打开模态时，输入框自动获得焦点并选中文本。
   - 输入防抖：输入时进行 250ms 防抖处理，避免高频发信。
   - 选项卡过滤：支持查看【全部】、【任务队列】、【本地在存】、【云端网盘】。
   - 键盘上下选择：支持通过键盘 `ArrowUp` / `ArrowDown` 循环切换当前高亮结果，按 `Enter` 键直接执行跳转或打开云端直链。

---

## 6. 特性四：归档失败智能归类引擎与一键批量重试

### 6.1 错误模式识别与标准化分类矩阵

归档失败原因归一化引擎对异常捕获字符串执行严谨的模式匹配：

```python
RE_AUTH_ERR = re.compile(r"(?:401|403|token expired|unauthorized|invalid token|认证失败|凭证过期|OpenListAuthErr)", re.I)
RE_QUOTA_ERR = re.compile(r"(?:quota|out of space|disk full|storage limit|space full|容量不足|空间不足|配额超限|insufficient)", re.I)
RE_CONFLICT_ERR = re.compile(r"(?:already exists|conflict|409|同名文件|目标已存在|文件冲突|file exists|duplicate)", re.I)
RE_TIMEOUT_ERR = re.compile(r"(?:timeout|timed out|connection refused|connecterror|readtimeout|reset by peer|502|503|504|超时|网络不可达|断开)", re.I)
RE_MISSING_LOCAL = re.compile(r"(?:本地文件不存在|file not found|no such file|0 字节|已被移动)", re.I)
```

| 故障编码 (`category`) | 语义名称 | 匹配特征（正则） | 严重程度 | 建议处理方案 | 是否支持一键重试 |
|---|---|---|---|---|---|
| `AUTH_EXPIRED` | 🔑 鉴权过期 | `RE_AUTH_ERR` | 高 | 重新调用 OpenList 登录接口获取最新 Token 并存盘 | **是**（重试前自动 Relogin） |
| `QUOTA_EXCEEDED` | 💾 容量超限 | `RE_QUOTA_ERR` | 阻塞 | 网盘空间已满，需扩容网盘或切换其他归档目录 | 否（需先清理空间或更换目录） |
| `FILE_CONFLICT` | ⚠️ 文件冲突 | `RE_CONFLICT_ERR` | 中 | 网盘已有同名文件，重试时启用 `policy="overwrite"` | **是**（支持强制覆盖重试） |
| `NETWORK_TIMEOUT`| 🌐 网络超时 | `RE_TIMEOUT_ERR` | 中 | 网络偶发抖动或云端服务响应慢，延时重新调度 | **是**（直接重回排队） |
| `FILE_NOT_FOUND` | 📁 本地文件缺失| `RE_MISSING_LOCAL`| 高 | 本地磁盘原始文件已被删除，不可恢复 | 否（不可恢复终态） |
| `UNKNOWN_ERROR`  | ❓ 未知异常 | 其他未匹配错误 | 低 | 查看日志详情分析堆栈 | **是**（可选重试） |

### 6.2 接口契约规范

#### 1) 失败归档聚合诊断接口：`GET /api/archive/failed-summary`
- **说明**：汇总所有处于 `failed` 状态的归档任务，返回分类统计与诊断明细。
- **响应体（JSON）**：
```json
{
  "ok": true,
  "summary": {
    "totalFailed": 8,
    "categories": {
      "AUTH_EXPIRED": 2,
      "QUOTA_EXCEEDED": 0,
      "FILE_CONFLICT": 3,
      "NETWORK_TIMEOUT": 3,
      "FILE_NOT_FOUND": 0,
      "UNKNOWN_ERROR": 0
    }
  },
  "diagnostics": [
    {
      "jobId": "arch_20260906_01",
      "uniqueId": "AQAD_xxx",
      "filename": "Movie_Clip.mp4",
      "size": 524288000,
      "remotePath": "/Aliyun/Media/Movie_Clip.mp4",
      "category": "NETWORK_TIMEOUT",
      "categoryLabel": "网络超时",
      "rawError": "readtimeout: HTTPSConnectionPool(host='openlist', port=5244): Read timed out.",
      "failedAt": 1725281200,
      "retryCount": 1,
      "canRetry": true,
      "suggestedFix": "网络抖动引起，建议一键批量重试"
    }
  ]
}
```

#### 2) 一键批量重试接口：`POST /api/archive/retry-failed` 与 `POST /api/archive/batch-retry`
- **说明**：支持 `/api/archive/retry-failed` 与 `/api/archive/batch-retry` 别名互通。
- **请求体（JSON）**：
```json
{
  "category": "NETWORK_TIMEOUT",
  "jobIds": [],
  "forceOverwrite": false
}
```
- **请求参数说明**：
  - `category`：要重试的错误分类，可选 `"all"`、`"AUTH_EXPIRED"`、`"NETWORK_TIMEOUT"`、`"FILE_CONFLICT"` 等。
  - `jobIds`：可选指定的 Job ID 列表；若提供则只重试这些任务。
  - `forceOverwrite`：当为 `true` 时，若选中的任务为 `FILE_CONFLICT`，将其归档策略强制修改为 `overwrite`。
- **调度执行逻辑**：
  1. 权限与状态校验：仅状态为 `failed` 且 `category != "FILE_NOT_FOUND"` 的任务允许重试。
  2. 若选中的任务包含 `AUTH_EXPIRED`，在重试前主动触发 `await _openlist_relogin()` 获取新鲜 Token。
  3. 批量将符合条件的 job 状态重置为 `state = "queued"`，清空 `error = ""`，更新 `progress = 0`，`retry_count += 1`。
  4. 若启用 `forceOverwrite = True`，同步更新目标 job 的 `policy = "overwrite"`。
  5. 重新拉起后台归档调度协程 `_archive_run_one(job)`，平滑依次执行。
- **响应体（JSON）**：
```json
{
  "ok": true,
  "message": "成功重新调度 3 个归档任务",
  "data": {
    "retriedCount": 3,
    "skippedCount": 0,
    "retriedJobIds": ["arch_20260906_01", "arch_20260906_02", "arch_20260906_03"],
    "skippedJobs": []
  }
}
```

---

## 7. 安全防护、并发健壮性与边界处理规范

1. **SSRF（服务端请求伪造）严格防护**：
   - 针对 Telegram Bot API 请求，域名白名单强制限制为 `api.telegram.org`，协议必须为 `https`。
   - 彻底阻断任何利用通知 Webhook、自定义代理链接反向探测本地回环 `127.0.0.1`、`169.254.169.254`（云元数据）或内网网段的行为。
2. **凭据安全与脱敏**：
   - Telegram Bot Token 及 OpenList Token 存储在宿主机受控文件系统，文件模式强制设为 `0600`。
   - 任何涉及日志打印、前端模板渲染或 API 序列化的地方，严禁明文暴露完整的 Bot Token。
3. **高并发限流与防抖**：
   - Telegram Bot 发送限流：严格遵守每秒最多 1 条发送速率，使用 FIFO 异步队列缓冲。
   - 搜索接口防 DoS：查询关键字长度截断（最多 128 字符），限制单次返回条数（最多 50 条），防范正则表达式 ReDoS 攻击。
4. **幂等性与状态一致性**：
   - 批量重试接口必须具备幂等性，对于已经处于 `queued` 或 `uploading` 的任务直接跳过，杜绝重复并发上传同一文件导致网盘写入冲突。
   - 归档状态落盘使用原子写入与节流机制，避免突发高频 IO 造成 `.archive_jobs.json` 损坏。

---

## 8. 验收标准与交付物对照表

| 序号 | 特性模块 | 核心交付物 / 代码与模板变动点 | 验证指标 |
|---|---|---|---|
| 1 | 全局 uniqueId 防重与智能关联 | `bridge_server.py` (`POST /api/files/check-dedup`, `_archive_registry_lookup`, `/api/tg/quick-download`) | 能够正确识别云端、本地、任务中重复文件，返回关联资产，拦截非 force 重复提交 |
| 2 | Telegram 消息通知引擎 | `bridge_server.py` (`TelegramNotifier`, `_notify_queue`, `/api/notify/*`), `.notify_config.json` | Bot 与收藏夹双通道正常外发，HTML 卡片排版优雅，大文件完成/归档/告警事件准确触发 |
| 3 | 跨库全局聚合搜索与 Ctrl+K | `bridge_server.py` (`GET /api/search/aggregate`), `templates/base.html`, `static/js/app.js` | 键盘按 `Ctrl+K` 瞬间呼出居中玻璃拟态面板，三栏聚合展示搜索结果，支持上下键选择与跳转 |
| 4 | 归档失败分类与一键批量重试 | `bridge_server.py` (`/api/archive/failed-summary`, `/api/archive/batch-retry`), `templates/library_cloud.html` | 准确将各类报错归纳至 6 类故障，一键批量重试成功将任务重置入队并处理 Token/冲突策略 |
