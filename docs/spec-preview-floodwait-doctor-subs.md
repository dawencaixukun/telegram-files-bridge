# 4 项系统级进阶特性技术架构与接口规范文档（P0）

**版本**：v1.0.0  
**状态**：定稿发布（Requirements Approved）  
**关联任务**：`t1` (Requirements 制定技术规范) → `t2` (HTTP Range 视频流式预览实现) → `t3` (Telegram FloodWait 智能冷却实现) → `t4` (System Doctor 自检面板实现) → `t5` (订阅高级变量与规则优先级实现) → `t6` (自动化测试验证) → `t7` (架构与代码审查) → `t8` (系统安全渗透审计) → `t9` (全链路部署与验收)  
**责任角色**：全栈与系统工程师 (`engineer`)、测试验证工程师 (`verifier`)、架构与代码审查员 (`code_reviewer`)、安全审计专家 (`security_auditor`)  

---

## 1. 概述与整体目标

本项目旨在构建工业级、高吞吐、高可用的 Telegram 媒体转存与多网盘智能归档平台。在完成基础下载、本地在存与 OpenList 归档主流程之后，针对生产运行环境下视频在线即时播放、Telegram 平台风控限流、多组件故障排查、个性化目录整理四项核心高频诉求，制定本规范：

1. **网页内 HTTP Range 视频在线预览（边下边播）**：
   - 严格遵循 RFC 7233 规范，实现 HTTP 206 Partial Content 流式响应；
   - 支持正在下载过程中（in-progress）文件的“边下边播”动态切片，无需等待大文件（数 GB）全部下载完成即可在前端直接拖拽播放；
   - 严密的句柄生命周期管理与异步生成器模式，防止并发播放场景下的文件描述符（FD）泄漏与内存暴涨；
   - 严格的路径安全校验，彻底杜绝任意文件读取与目录遍历逃逸。

2. **Telegram FloodWait 智能冷却与排队倒计时**：
   - 全面感知 TDLib `code: 420 / FLOOD_WAIT_X` 及 Telegram Bot API HTTP 429 报错；
   - 建立账号级/系统级自动挂起状态机，将受阻任务平滑置入冷却等待，杜绝因高频重试引发的账号封禁；
   - 后台轻量单例定时器精确倒计时，归零后自动唤醒队列继续下载，实现完全无人值守自愈；
   - 前端集成沙漏 ⏳ 动画交互与秒级动态倒计时，与 SSE 实时状态同步。

3. **System Doctor 系统健康与依赖自检面板**：
   - 构建轻量、非阻塞、高并行的多组件探针体系，覆盖：**Java 后端 (TG-Files API)**、**TDLib 客户端会话**、**OpenList 网盘引擎**、**VPS 本地存储与高低水位**；
   - 并发异步探测，全局 3.0 秒硬超时保护，单组件卡死不影响系统整体诊断；
   - 敏感凭据与内网拓扑脱敏输出，防止密码、JWT Token、Telegram Key 及内网 IP 泄露；
   - 提供现代化玻璃拟态自检面板，呈现组件健康红绿灯、延时耗时与一键修复建议。

4. **订阅目录模板高级变量（{resolution}/{ext}/{chat_title}）与规则优先级匹配**：
   - 扩展目录模板渲染引擎，新增 `{resolution}`（视频分辨率）、`{ext}`（文件后缀扩展名）、`{chat_title}`（清洗后的频道名称）三大高级动态变量；
   - 支持视频元数据智能提取与智能回退，确保目录命名兼具规范性与美观度；
   - 引入规则优先级（`priority` 权重）与特异度打分机制，打破字典无序匹配，实现“高优规则优先、精确频道优先、通配符保底”的确定性匹配算法；
   - 前端提供模板变量快捷输入标签与实时预览（Live Preview）组件。

---

## 2. 系统整体架构与交互拓扑

```
+----------------------------------------------------------------------------------------------------+
|                                    Web 现代控制台 (前端 UI 界面)                                      |
|                                                                                                    |
|  +---------------------------+  +----------------------------+  +-------------------------------+  |
|  |   HTML5 视频播放器弹窗    |  |    FloodWait 沙漏倒计时     |  |    System Doctor 自检看板     |  |
|  | (HTTP 206 边下边播/拖拽)  |  | (SSE 实时推送 / 自动续跑)  |  | (Java/TDLib/OpenList/存储自检)|  |
|  +-------------+-------------+  +--------------+-------------+  +---------------+---------------+  |
+----------------|-------------------------------|--------------------------------|------------------+
                 | Range: bytes=start-end        | GET /api/telegram/flood-status | GET /api/doctor/check
                 v                               v                                v
+----------------------------------------------------------------------------------------------------+
|                                   FastAPI 核心桥接服务 (bridge_server.py)                            |
|                                                                                                    |
|  [ 1. HTTP 206 流式传输引擎 ] <==================================================+                 |
|       ├─ RFC 7233 范围解析器 (Range: bytes=0-1048575 / bytes=1024-)              |                 |
|       ├─ 动态边下边播截断器 (os.path.getsize 实时写入探测 & Moov 原子透传)       |                 |
|       ├─ 资源句柄安全发生器 (try...finally: f.close() 防 FD 泄漏 & 64KB 缓冲块)   |                 |
|       └─ 安全沙箱白名单门禁 (_safe_id / base64 uid / APP_ROOT_DIR 边界校验)       |                 |
|                                                                                  |                 |
|  [ 2. FloodWait 智能调度器 ] <===================================================+                 |
|       ├─ 异常正则识别器 (TDLib 420 FLOOD_WAIT_X / Bot API 429 retry_after)       |                 |
|       ├─ 状态机持久化 (.flood_wait_state.json，账号级隔离 & 最大冷却时间合并)     |                 |
|       ├─ 异步调度唤醒器 (asyncio.create_task 后台单例定时器，到期自动续跑)        |                 |
|       └─ 实时事件分发器 (SSE 广播 flood_wait_update，前端沙漏秒级平滑刷新)        |                 |
|                                                                                  |                 |
|  [ 3. System Doctor 探测引擎 ]                                                   |                 |
|       ├── 并发探测器 (asyncio.gather, 3.0 秒硬超时，非阻塞非等待)                  |                 |
|       ├── 探针 1: Java 后端 (TG-Files API HTTP / Ping / Session RTT)             |                 |
|       ├── 探针 2: TDLib 会话 (连接态 / 授权态 / 账号活跃度 / 风控状态)           |                 |
|       ├── 探针 3: OpenList 网盘 (服务可用性 / JWT 鉴权 / 挂载列表 / 读写探测)     |                 |
|       ├── 探针 4: VPS 本地存储 (读写权限 / 磁盘可用空间 / 85% 高水位熔断状态)     |                 |
|       └── 脱敏保护层 (JWT Bearer Token / 密码 / Telegram API Key / 内网 IP 脱敏)  |                 |
|                                                                                  |                 |
|  [ 4. 订阅高级模板与优先级引擎 ]                                                 |                 |
|       ├─ 高级变量解析 ({resolution} / {ext} / {chat_title} + 正则/元数据探测)    |                 |
|       ├─ 路径净化安全阀 (_sub_clean_seg 防穿越、禁忌字符过滤、64位段截断)         |                 |
|       └─ 确定性优先级排序器 (Priority DESC -> Specificity DESC -> CreatedAt ASC) |                 |
+----------------------------------------------------------------------------------+-----------------+
       |                                   |                               |                  |
       v (Unix Socket / HTTP :8123)        v (TDLib Client Session)        v (HTTP :5244)     v (Local Disk)
+-----------------------------+ +-----------------------------+ +--------------------+ +--------------------+
|    tg-files-api (Java)      | |   TDLib Native Core Engine  | | OpenList 网盘系统  | | VPS 宿主机本地存储 |
|   (Vert.x 后端与文件元数据) | |  (Telegram 协议风控与限流)  | | (WebDAV/云端存储)  | | (APP_ROOT_DIR)     |
+-----------------------------+ +-----------------------------+ +--------------------+ +--------------------+
```

---

## 3. 特性一：网页内 HTTP Range 视频在线预览（边下边播）

> ⚠️ **已废弃（2026-09-10）**：本特性已按用户要求从项目**整体移除**。
> 端点 `/api/media/info`、`/api/media/stream` 与前端 `__playLocalVideo` 弹窗、
> 复制直链、PotPlayer/VLC 唤起按钮均已删除。本节仅为历史设计记录，
> **不代表当前实现**，请勿据此重新实现。

### 3.1 核心原理与 RFC 7233 协议规范

现代浏览器 HTML5 `<video>` 播放器在渲染音视频时，严重依赖 HTTP `Range` 头实现快速拖拽跳转（Seek）与分片按需拉取。完整实现规范如下：

1. **请求头解析规范**：
   - 客户端携带 `Range: bytes=START-END` 请求特定区间；
   - 支持常见 3 种格式：
     - `bytes=0-1048575`：请求前 1MB 数据（常见于播放器读取 MP4 `moov` 元数据原子）；
     - `bytes=1048576-`：请求从 1MB 开始至文件末尾的全部数据；
     - `bytes=-524288`：请求文件末尾 512KB 数据。
   - 若客户端未携带 `Range` 头，支持以完整文件（HTTP 200）或默认流式起始分片响应，且必须声明 `Accept-Ranges: bytes`。

2. **响应头标准契约（HTTP 206 Partial Content）**：
   - 响应状态码：`206 Partial Content`；
   - `Content-Range: bytes START-END/TOTAL`（如 `Content-Range: bytes 0-1048575/10485760`）；
   - `Content-Length: CHUNK_SIZE`（`CHUNK_SIZE = END - START + 1`）；
   - `Accept-Ranges: bytes`；
   - `Content-Type: video/mp4`（根据文件扩展名与魔数自适应，兜底 `application/octet-stream`）；
   - `Cache-Control: public, max-age=3600`（对已完成文件开启浏览器缓存；对边下边播进行中文件使用 `no-cache`）。

3. **异常与边界状态处理（HTTP 416 Range Not Satisfiable）**：
   - 若客户端请求的起始偏移量 `START >= TOTAL` 或 `START > END`，返回 `416` 状态码；
   - 响应头必须携带 `Content-Range: bytes */TOTAL`；
   - 严禁向客户端发送超过物理文件大小的数据。

### 3.2 "边下边播" (Play While Downloading) 机制

针对正在从 Telegram 下载的视频（状态为 `downloading`）：
1. **动态边界探测**：
   - TDLib 下载视频时，逐步写入本地临时/目标文件。
   - 流式服务每次接收到 `Range` 请求时，通过 `os.path.getsize(filepath)` 动态检测当前磁盘已写入的实际字节数 `current_written`。
   - `TOTAL` 字段处理策略：
     - 采用任务元数据中的真实预估总大小 `size_bytes`；
     - 有效范围上界限制为：`effective_end = min(requested_end, current_written - 1)`；
     - 若客户端请求的 `START` 尚未被下载写入（`START >= current_written`），返回 HTTP 416 或让客户端按需等待刷新。
2. **MP4 头部（Moov Atom）支持**：
   - Telegram 传输的大多数流式视频采用 FastStart 模式（`moov` 原子位于文件头部）；
   - 只要视频头部前几十 KB 至数 MB 下载完成，浏览器即可解析出时长、音轨与视频编码，立即开始流畅播放；
   - 前端播放器 UI 显著标识：“⚡ 正在边下边播 · 已缓冲 XX%”。

### 3.3 句柄安全、内存防暴涨与路径防逃逸

1. **防文件句柄泄漏（FD Leak Prevention）**：
   - 采用 Python 异步生成器与上下文管理器：
   ```python
   async def file_chunk_generator(path: str, start: int, length: int, chunk_size: int = 65536):
       with open(path, "rb") as f:
           f.seek(start)
           remaining = length
           while remaining > 0:
               read_len = min(chunk_size, remaining)
               data = f.read(read_len)
               if not data:
                   break
               remaining -= len(data)
               yield data
   ```
   - 无论客户端主动暂停、拖拽 Seek 取消连接还是发生网络中断，生成器外层的 `with open(...)` 或 `try...finally: f.close()` 保证文件描述符即刻释放，绝不长期驻留。
2. **内存保护**：
   - 单次只缓冲 `chunk_size`（默认 64KB，最高 256KB），绝不在内存中加载整个视频，并发 100 路预览内存开销保持在数 MB 级。
3. **路径逃逸过滤（Anti Directory Traversal）**：
   - 必须基于 `uniqueId`（严格校验 `[A-Za-z0-9_=-]{4,160}`）索引回查本地文件路径；
   - 若传入直接文件路径，必须通过 `os.path.abspath` 解析后，断言 `resolved_path.startswith(APP_ROOT_DIR)` 或已授权的下载根目录，拒绝一切含 `../`、`..\\`、`%2e%2e` 或绝对路径参数，违者直接返回 HTTP 403 Forbidden。

### 3.4 核心 API 端点契约

#### 1) 视频流式点播/边下边播端点：`GET /api/video/stream/{unique_id}`
- **方法**：`GET`
- **路径参数**：`unique_id`（文件唯一标识符）
- **请求头**：`Range: bytes=0-1048575`（可选）
- **响应头示例**：
  ```http
  HTTP/1.1 206 Partial Content
  Accept-Ranges: bytes
  Content-Range: bytes 0-1048575/52428800
  Content-Length: 1048576
  Content-Type: video/mp4
  Cache-Control: no-cache
  ```

#### 2) 视频元数据与播放探测端点：`GET /api/video/info/{unique_id}`
- **响应示例**：
  ```json
  {
    "ok": true,
    "data": {
      "uniqueId": "AQADAgADx6cxG...",
      "filename": "sample_video.mp4",
      "mimeType": "video/mp4",
      "totalBytes": 52428800,
      "downloadedBytes": 15728640,
      "downloadStatus": "downloading",
      "isStreamingReady": true,
      "playWhileDownloading": true,
      "streamUrl": "/api/video/stream/AQADAgADx6cxG..."
    }
  }
  ```

### 3.5 前端交互规范

1. **操作入口**：
   - 在本地在存页（`/library/local`）的网格视图与表格视图的操作栏中，对视频文件新增“▶ 预览”按钮；
   - 在任务列表页（`/tasks`）中，对状态为 `downloading`（已下载 > 1MB）或 `completed` 的视频任务，操作栏增加“▶ 边下边播 / 播放”按钮。
2. **播放器模态弹窗（#localVideoModal）**：
   - 采用现代化深色半透明遮罩与圆角浮窗，集成 `<video controls playsinline preload="metadata">`；
   - 弹窗顶部显示视频文件名与实时播放源标记；
   - 弹窗底部提供快捷按钮：
     - **复制播放直链**：复制当前全路径 URL，便于粘贴至手机或第三方应用；
     - **外部播放器直调**：支持 `potplayer://{stream_url}` 与 `vlc://{stream_url}` 协议直接呼起本地播放器；
     - **关闭**：释放播放器并暂停视频元素，防止后台产生静默网络流量。

---

## 4. 特性二：Telegram FloodWait 智能冷却与排队倒计时

### 4.1 异常感知与捕获模型

Telegram 服务端为防止滥用，会在账号短时间内请求过密时抛出 FloodWait 限流。系统需在以下两个层级精确感知：

1. **TDLib 异常特征识别**：
   - 响应 JSON 结构：
     ```json
     {"@type": "error", "code": 420, "message": "FLOOD_WAIT_15"}
     ```
   - 正则表达式：`r"(?:FLOOD_WAIT_|retry after )(\d+)"`（忽略大小写）；
   - 提取数字作为必须强制冷却的秒数 `wait_seconds`。
2. **Telegram Bot API 异常识别**：
   - HTTP 状态码：`429 Too Many Requests`；
   - 响应体：
     ```json
     {"ok": false, "error_code": 429, "description": "Too Many Requests: retry after 30", "parameters": {"retry_after": 30}}
     ```
   - 优先提取 `parameters.retry_after`，兜底正则提取 `description` 中的整数。

### 4.2 账号级与系统级挂起状态机

```
              [ 收到正常请求 / 任务调度 ]
                         |
                         v
              +---------------------+
              |    NORMAL (正常)    |
              +---------------------+
                         |
                         | 捕获 FLOOD_WAIT_X 异常
                         v
              +---------------------+
              | FLOOD_WAIT (挂起中) | <--- 并发触发时: wait_until = max(wait_until, new_until)
              +---------------------+
                         |
                         | 后台定时器倒计时归零 (remaining <= 0)
                         v
              +---------------------+
              | AUTO_RESUME (唤醒)  |
              +---------------------+
                         |
                         | 自动重新激活调度器 / 重新入队下载
                         v
                    (回到 NORMAL)
```

1. **数据模型与持久化**：
   - 状态持久化文件：`APP_ROOT_DIR/.flood_wait_state.json`（权限 `0600`），保证服务意外重启时冷却信息不丢失；
   - 结构定义：
     ```json
     {
       "accounts": {
         "default": {
           "active": true,
           "waitSeconds": 45,
           "triggeredAt": 1725600000.0,
           "cooldownUntil": 1725600045.0,
           "reason": "FLOOD_WAIT_45",
           "affectedTasksCount": 3
         }
       },
       "globalCooling": true,
       "maxCooldownUntil": 1725600045.0
     }
     ```
2. **并发防击穿合并**：
   - 若同一账号在冷却期间再次触发限流告警，采用 `cooldown_until = max(cooldown_until, now + new_wait)` 延长冷却，严禁把时间缩短；
   - 对后续进入的下载任务，调度器直接拦截并设为 `flood_wait` 挂起态，绝不向 Telegram 发送任何无效网络请求。

### 4.3 异步调度与自动续跑引擎

1. **轻量后台调度器**：
   - 采用单例 `asyncio.Task` 维持后台倒计时循环，无需引入外部重量级 MQ；
   - 每 1 秒轮询一次活跃冷却状态，更新内存中的 `remaining_seconds`；
   - 倒计时归零时：
     1. 重置 `active = false`；
     2. 自动检索所有处于 `flood_wait` 状态的任务；
     3. 恢复任务状态为 `pending` 或直接调用 `resume_download()` 续跑；
     4. 通过 SSE 向前端广播恢复通知，记录系统审计日志。

### 4.4 状态同步与 API 契约

#### 1) 冷却状态查询端点：`GET /api/telegram/flood-status`
- **响应示例**：
  ```json
  {
    "ok": true,
    "data": {
      "isCooling": true,
      "remainingSeconds": 28,
      "cooldownUntil": 1725600045.0,
      "totalWaitSeconds": 45,
      "account": "default",
      "reason": "FLOOD_WAIT_45",
      "affectedTasks": 2,
      "message": "Telegram 账号触发风控保护，正在安全冷却中，预计 28 秒后自动恢复"
    }
  }
  ```

#### 2) 管理员强制解除冷却端点：`POST /api/telegram/flood-reset`
- **说明**：仅供管理员在确认 Telegram 服务端已解封时人工重置；
- **响应**：`{"ok": true, "message": "已强制重置 FloodWait 状态并唤醒挂起任务"}`。

#### 3) SSE 实时推送流：`/sse/events`
- 新增事件类型：`event: flood_wait_update`
  ```json
  {"isCooling": true, "remainingSeconds": 28, "cooldownUntil": 1725600045.0}
  ```
- 冷却结束事件：`event: flood_wait_cleared`
  ```json
  {"isCooling": false, "message": "FloodWait 冷却已完成，任务已自动恢复调度"}
  ```

### 4.5 前端沙漏倒计时交互规范

1. **顶部全局沙漏横幅**：
   - 当检测到 `isCooling == true` 时，页面顶部滑入吸顶橙黄色告警条：
     `⏳ Telegram 风控保护生效中 · 智能挂起冷却中 00:28 · 倒计时结束后将全自动续跑`；
   - 内置 CSS 沙漏旋转动画，秒级平滑递减；
2. **任务列表状态胶囊**：
   - 处于挂起的任务，状态胶囊渲染为带有沙漏图标的橙色样式：`<span class="pill warn"><span class="p-dot"></span>⏳ 冷却中 00:28</span>`；
3. **零秒自动恢复与静默刷新**：
   - 当倒计时归零，横幅自动渐隐并切换为绿色提示“✅ 冷却结束，已自动恢复下载队列”，任务列表自动执行 htmx / fetch 局部刷新，无需用户手动按 F5。

---

## 5. 特性三：System Doctor 系统健康与依赖自检面板

### 5.1 四大核心组件探测规范

System Doctor 必须全面覆盖系统运转的四大核心依赖，对各组件进行真实链路级探活：

| 探测目标 | 探测方式与链路 | 健康阈值 (Healthy) | 预警阈值 (Warning) | 严重错误 (Critical) |
| :--- | :--- | :--- | :--- | :--- |
| **Java 后端** (`tg-files-api`) | HTTP GET `/health` 或 `/`，附带后端鉴权 Session | 响应 200，RTT < 100ms | 响应 200，100ms <= RTT < 1000ms | 连接拒绝、HTTP 5xx、超时 > 2.5s |
| **TDLib 会话** | POST `/telegram/api/getAuthorizationState` | `authorizationStateReady`，账号正常登录 | 存在非致命警告，或处于 FloodWait 冷却 | `authorizationStateClosed`，或客户端未创建 |
| **OpenList 网盘** | GET `/api/me` 校验令牌 + GET `/api/fs/list` 探测挂载 | 令牌有效且至少挂载 1 个可用存储网盘 | 令牌有效但挂载列表为空 | 服务不可达、HTTP 401 令牌失效、502 网关错误 |
| **VPS 本地存储** | `APP_ROOT_DIR` 写入/读取临时测试文件 + `shutil.disk_usage` | 读写正常，磁盘使用率 < 85% | 读写正常，85% <= 磁盘使用率 < 95% | 磁盘只读无写权限，或使用率 >= 95% (熔断) |

### 5.2 并发无阻探针与 3 秒超时契约

1. **并发探测架构**：
   - 严禁串行探测（串行累加易导致接口超时）；
   - 使用 `asyncio.gather` 同时并发启动 4 项探测协程；
2. **严格单项与总体超时**：
   - 每个子探针施加 `asyncio.wait_for(probe(), timeout=2.5)` 保护；
   - 探针内部任何网络异常、DNS 错误、连接超时均在子协程内被吞吐捕获并转换为标准结构化异常条目；
   - 整体接口保证在 **3.0 秒内必定返回** 完整诊断报文，绝不发生前端请求挂起现象。

### 5.3 敏感信息安全脱敏契约

Doctor 面板涉及系统内部关键依赖与凭据，**严禁向前端泄漏未脱敏的敏感信息**：
1. **凭据掩码**：
   - 密码、Secret、Private Key 一律使用固定掩码 `******`；
   - JWT / Authorization Bearer Token 仅保留前 4 位和后 4 位，中间以 `...` 代替（如 `eyJ1...9F8A`）；
   - Telegram API Hash / Session Key 完全屏蔽；
2. **网络拓扑与目录脱敏**：
   - 隐藏真实的内网私有 IP（如 `192.168.x.x` 或 `10.x.x.x` 转换为 `127.0.0.1` 或抽象标识 `backend-service`）；
   - 隐藏宿主机绝对敏感根路径，统一映射为通用占位符或相对路径。

### 5.4 结构化自检 API 契约

#### 1) 综合健康诊断端点：`GET /api/doctor/check`
- **响应体规范**：
  ```json
  {
    "ok": true,
    "timestamp": 1725600100.0,
    "overallStatus": "healthy",
    "totalLatencyMs": 48.5,
    "components": {
      "javaBackend": {
        "name": "Java 后端核心 (tg-files-api)",
        "status": "healthy",
        "latencyMs": 12.4,
        "message": "服务连接畅通，会话认证有效",
        "details": {
          "endpoint": "http://backend-service:8123",
          "authenticated": true,
          "version": "0.4.0"
        }
      },
      "tdlib": {
        "name": "TDLib 客户端会话",
        "status": "healthy",
        "latencyMs": 15.2,
        "message": "Telegram 会话已就绪 (authorizationStateReady)",
        "details": {
          "state": "authorizationStateReady",
          "activeAccount": "user_***21",
          "isFloodWait": false
        }
      },
      "openlist": {
        "name": "OpenList 云端网盘引擎",
        "status": "healthy",
        "latencyMs": 18.1,
        "message": "网盘引擎在线，已挂载 3 个云端存储",
        "details": {
          "endpoint": "http://openlist-service:5244",
          "tokenValid": true,
          "username": "admin",
          "mountsCount": 3
        }
      },
      "localStorage": {
        "name": "VPS 本地文件存储",
        "status": "healthy",
        "latencyMs": 2.8,
        "message": "存储读写正常，磁盘使用率 42.6%（水位正常）",
        "details": {
          "writable": true,
          "totalSpaceHuman": "100.0 GB",
          "freeSpaceHuman": "57.4 GB",
          "usedPercent": 42.6,
          "isWatermarkMeltdown": false
        }
      }
    },
    "recommendations": []
  }
  ```

#### 2) 轻量存活 Ping 端点：`GET /api/doctor/ping`
- **响应**：`{"ok": true, "pong": true, "timestamp": 1725600100.0}`

### 5.5 前端自检面板交互规范

1. **入口布局**：
   - 控制台顶栏导航右侧工具区增设“🩺 System Doctor”状态徽章按钮（红/黄/绿呼吸灯小圆点）；
   - 设置页（`/settings`）新增独立“系统健康与依赖诊断”专区；
2. **面板交互特征**：
   - 现代化磨砂玻璃卡片布局，清晰列出 4 项核心组件；
   - 卡片内标明组件名称、延迟耗时（如 `12ms`）、状态胶囊与诊断说明；
   - 异常高亮与修复指引：若 OpenList 令牌过期，卡片显示红框并提供“前往重新登录 OpenList”一键跳转按钮；
   - **一键重新自检**：防抖限制 2 秒，点击触发脉冲旋转动画；
   - **复制脱敏报告**：一键生成标准 Markdown 诊断文本，方便用户汇报排查问题。

---

## 6. 特性四：订阅目录模板高级变量与规则优先级匹配

### 6.1 模板扩充变量规范

当前订阅系统支持的基础变量为：`{source}`, `{type}`, `{YYYY}`, `{MM}`, `{DD}`, `{YYYY-MM}`。本次升级新增以下三大高级变量：

1. **`{resolution}`（视频分辨率）**：
   - **提取算法**：
     1. 首先尝试从 `FileRecord` 的 `width` 与 `height`（若后端透传）计算，如 `1920x1080` → `1080p`，`3840x2160` → `4k`；
     2. 其次通过精准正则表达式扫描文件名和描述文本：
        - `(?i)\b(4k|2160p)\b` → `4k`；
        - `(?i)\b(1080p|1080i)\b` → `1080p`；
        - `(?i)\b(720p)\b` → `720p`；
        - `(?i)\b(480p|360p)\b` → 匹配项转小写；
        - `(?i)\b(\d{3,4})[xX](\d{3,4})\b` → 根据高度映射为常见档位（>=2160 为 4k，>=1080 为 1080p，>=720 为 720p）；
     3. 若非视频类型或未检测到分辨率，平滑回退为安全占位符 `unknown`。
2. **`{ext}`（文件后缀扩展名）**：
   - **提取算法**：
     1. 从文件名中提取最后一个 `.` 之后的字母数字串：`filename.rsplit(".", 1)[-1]`；
     2. 转换为全小写，剔除非法字符（仅放行 `[a-z0-9]`，长度限制在 1-10 位）；
     3. 若无扩展名，基于 MIME 类型推断（如 `video/mp4` → `mp4`），仍无法推断则保底回退为 `bin`。
3. **`{chat_title}`（清洗后的频道/群组标题）**：
   - 与原有 `{source}` 保持语义明确分离，确保取自真实的 Telegram 频道/群组 Title；
   - 经由 `_sub_clean_seg` 严格清洗，剔除特殊字符，截断至 64 字符。

#### 路径安全净化（`_sub_clean_seg`）
- 针对所有注入变量，严密执行以下过滤流程：
  1. 替换 Windows/Linux/网盘禁忌字符 `[\\/:*?"<>|\x00-\x1f]` 为下划线或空格；
  2. 压缩连续空白，去除首尾空白与点号（防止 `..` 形成目录穿越）；
  3. 截断最大长度为 64 字节；
  4. 最终生成的目录路径通过 `_archive_norm_dir` 规范化，确保必定以 `/` 开头且绝不含路径跳转段。

### 6.2 规则优先级排序与匹配算法

当前规则匹配仅依赖 Python 字典迭代，无法满足复杂多规则多频道过滤的业务场景。引入确定性优先级排序匹配引擎：

1. **规则数据模型字段扩充**：
   - `priority: int`：优先级权重（默认 `0`，可设置范围 `-100` 至 `1000`，数值越大优先级越高）；
   - `name: str`：规则备注名称；
   - `mediaTypes: List[str]`：媒体类型过滤白名单（如 `["video"]`，默认 `["*"]` 全匹配）；
2. **特异度打分算法（Specificity Scoring）**：
   - 精确指定 `chatId`（如 `-10019827364`）：特异度基础分 `100`；
   - 通配符全匹配（`chatId == "*"` 或未限定）：特异度基础分 `10`；
   - 指定了媒体类型过滤（而非 `*`）：特异度加 `20` 分；
3. **多因子确定性排序算法**：
   - 对所有启用的订阅规则进行严格复合排序：
     $$\text{SortKey}(R) = (-\text{priority}, -\text{specificity}, \text{createdAt}, \text{id})$$
   - **匹配执行流**：
     1. 任务到达后，提取 `telegramId`, `chatId`, `ftype`, `filename`, `metadata`；
     2. 遍历已排序的规则列表；
     3. 检查当前规则是否同时命中：
        - 账号匹配：`rule.telegramId == task.telegramId`（或通配）；
        - 频道匹配：`rule.chatId == task.chatId` 或 `rule.chatId == "*"`；
        - 类型匹配：`rule.mediaTypes` 包含 `task.type` 或包含 `*`；
     4. **首个全条件命中的规则即刻命中并返回，后续规则熔断跳过**；
     5. 若所有规则均未命中，回退至全局默认归档规则。

### 6.3 API 契约与配置变更

#### 1) 订阅规则创建/更新端点：`POST /api/subscriptions/rule`
- **请求体（JSON）**：
  ```json
  {
    "telegramId": 12345678,
    "chatId": "-1001234567890",
    "chatTitle": "4K Movie Channel",
    "priority": 100,
    "dirTemplate": "/Movies/{YYYY}/{chat_title}/{resolution}",
    "deleteLocal": true,
    "policy": "skip",
    "enabled": true
  }
  ```

#### 2) 模板实时预览演算端点：`POST /api/subscriptions/preview-template`
- **请求体（JSON）**：
  ```json
  {
    "dirTemplate": "/归档/{chat_title}/{YYYY-MM}/{resolution}/{ext}",
    "sample": {
      "chatTitle": "科技资讯",
      "filename": "WWDC2024_Highlights_1080p.mp4",
      "type": "video",
      "width": 1920,
      "height": 1080
    }
  }
  ```
- **响应体**：
  ```json
  {
    "ok": true,
    "previewDir": "/归档/科技资讯/2024-09/1080p/mp4",
    "variables": {
      "chat_title": "科技资讯",
      "resolution": "1080p",
      "ext": "mp4",
      "YYYY-MM": "2024-09"
    }
  }
  ```

### 6.4 前端交互规范

1. **规则管理列表**：
   - 订阅规则表格增加“优先级”列标签（如 `<span class="badge blue">优先级 100</span>`）；
   - 支持表格按优先级实时排列，提供直接输入或上下步进按钮调整优先级；
2. **规则编辑弹窗与模板输入框**：
   - 目录模板输入框下方增设一键插入标签芯片：
     `{resolution}`、`{ext}`、`{chat_title}`、`{source}`、`{YYYY-MM}`；
   - 点击芯片自动将变量插入到光标所在位置；
   - 实时预览区域根据当前模板与模拟样本，动态渲染出真实最终落盘路径，变量错误时即时红色报错提示。

---

## 7. 异常处理、性能指标与边界防护

### 7.1 异常处理矩阵

| 场景分类 | 触发条件 | 系统行为 | 响应码/事件 | 恢复策略 |
| :--- | :--- | :--- | :--- | :--- |
| **Range 超出边界** | `start >= file_size` | 返回 416 且附带 `Content-Range: bytes */total` | HTTP 416 | 客户端收到后重设请求范围至合理区间 |
| **边下边播网络断开** | 播放器提前关闭/中断 | 触发生成器 finally 块，即刻关闭文件句柄 | 释放 FD | 无需人工介入，避免资源泄漏 |
| **路径逃逸攻击** | 请求携带 `../` 或非法 uid | 拦截请求，记录安全审计警报 | HTTP 403 / 400 | 安全沙箱阻断 |
| **FloodWait 频繁触发** | 短期内接收多次 420 报错 | 自动合并取最大冷却到期时间戳，延长倒计时 | SSE 推送更新 | 冷却归零后自动重跑 |
| **Doctor 单组件宕机** | 如 OpenList 服务断开 | 探针 2.5s 超时隔离，其他组件正常汇报 | HTTP 200 (部分 warning) | 给出针对性组件修复建议，不拖慢整体响应 |
| **模板未知变量** | 输入非法变量如 `{foo}` | 拒绝保存或返回解析错误 | HTTP 400 | 前端红字高亮未知变量 |

### 7.2 性能基准指标

- **HTTP 206 首包响应延迟**：<= 30ms（基于本地磁盘预读与高效异步生成器）；
- **边下边播内存占用**：单连接缓冲内存 <= 256KB，支撑并发 100 路在线播放；
- **System Doctor 探测总耗时**：<= 3.0s（4 项协程并发执行与硬超时熔断）；
- **模板变量与优先级匹配开销**：<= 1ms（百条规则极速纯内存运算）。

---

## 8. 实施计划与质量门禁映射

为保障上述 4 项特性的工业级落地，团队严格遵循分工协作机制：

- **阶段一 (t1 Requirements，当前任务)**：完成本技术架构与契约规范文档，确立全部接口、数据模型与前端规范；
- **阶段二 (t2 Implementation 核心实现)**：
  - 模块 1：HTTP Range 边下边播后端生成器、路由代理与前端播放器弹窗实现；
  - 模块 2：Telegram FloodWait 异常捕获、状态机持久化、自动续跑定时器与前端沙漏组件实现；
  - 模块 3：System Doctor 四大组件探测器、非阻塞异步并发调度与脱敏接口实现；
  - 模块 4：订阅高级变量 `{resolution}/{ext}/{chat_title}` 提取算法、优先级排序引擎与前端交互升级；
- **阶段三 (t3/t4 Verification 自动化测试验证)**：编写自动化测试用例，覆盖 Range 206/416、FloodWait 状态转换与续跑、Doctor 3s 超时隔离与脱敏、模板优先级匹配等；
- **阶段四 (Review & Security Audit 架构与安全审计)**：进行架构规范与安全渗透审计（FD 泄漏、防越权、防路径穿越、防 DoS）。
