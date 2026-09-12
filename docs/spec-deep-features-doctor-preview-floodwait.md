# 4 项系统级进阶特性技术架构与接口规范文档

**文档名称**：系统级进阶特性技术规范（边下边播、FloodWait 智能冷却、System Doctor、订阅高级变量与优先级匹配）  
**文件归档**：`docs/spec-deep-features-doctor-preview-floodwait.md`  
**版本**：v1.0.0  
**状态**：定稿发布（Approved Specification）  
**责任角色**：全栈与系统工程师 (`engineer`)  

---

## 1. 概述与整体架构目标

为了将 Telegram 媒体文件转存与网盘智能归档平台进一步提升至工业级系统标准，针对实时音视频播放、高频风控规避、全链路故障诊断与精细化订阅整理等关键业务场景，统一制定本规范。规范涵盖四大系统级进阶特性：

1. **网页内 HTTP 206 视频流式预览与边下边播**：
   - 统一规范 `/api/media/stream` 接口，全面支持标准 HTTP `Range` 分片请求与 `206 Partial Content` 响应；
   - 建立安全非阻塞的异步文件分片流式发生器，确保并发流式读取时文件描述符（FD）瞬时释放，防止内存暴涨与句柄泄漏；
   - 支持**本地在存已完成文件**与**正在下载中（In-Progress）文件**的动态分片读取，利用 MP4 FastStart/Moov 首包特性实现前端即点即播与进度条拖拽播放；
   - 制定网页内端到端 HTML5 视频播放器弹窗规范与外部播放器（PotPlayer / VLC）流媒体直链调用协议。

2. **Telegram FloodWait 智能冷却与排队倒计时**：
   - 深度感知并智能捕获 TDLib `code: 420 / FLOOD_WAIT_(\d+)` 与 Telegram Bot API HTTP `429 Too Many Requests`；
   - 构建账号级/系统级请求自动挂起与恢复调度状态机，受阻任务自动切换为冷却排队保护态，并发限流时自动合并最大冷却时长；
   - 规范 `GET /api/tg/floodwait/status` 监控接口与管理员手动重置接口 `POST /api/tg/floodwait/reset`；
   - 规范前端沙漏 ⏳ 动态倒计时交互、实时 SSE 事件广播与冷却归零后的自动续跑调度。

3. **System Doctor 系统健康自检面板**：
   - 统一设计一键系统自检接口 `GET /api/system/doctor` 与轻量存活探测 `GET /api/system/doctor/ping`；
   - 深度覆盖四大系统依赖探针：
     - **Java 后端核心**（8123 端口 / WebSocket 链路 / 会话鉴权 / RTT 延时）；
     - **TDLib 客户端会话**（连接态 `connectionStateReady` / 授权态 / 账号状态 / 会话往返延时）；
     - **OpenList 云端网盘**（5244 端口可达性 / JWT 凭据有效性 / 云端挂载网盘连通性）；
     - **本地文件存储**（宿主机 `APP_ROOT_DIR` 目录读写延时 / 磁盘总容量与剩余空间 / 85% 高水位熔断状态）；
   - 并发异步探测，施加单项 2.5s、总体 3.0s 硬超时熔断保护，单组件挂起不拖慢整体诊断；
   - 制定严格的敏感信息脱敏契约（密码掩码、JWT 截断、IP 与私有绝对路径隐藏）。

4. **订阅目录模板高级变量与规则优先级匹配**：
   - 扩充订阅目录模板变量库，新增 `{resolution}`（视频分辨率）、`{ext}`（文件小写扩展名）、`{chat_title}`（清洗后的频道标题）三大高级动态变量；
   - 制定完善的分辨率与扩展名智能推导、正则提取及安全回退算法；
   - 引入订阅规则优先级权重（`priority` 字段）与特异度打分机制（精确 `chatId` 优先于通配符 `*`），实现确定性复合排序匹配算法：
     $$\text{SortKey} = (-\text{priority}, -\text{specificity}, \text{createdAt}, \text{id})$$
   - 规范 `POST /api/subscriptions/rule` 与模板实时预览演算接口 `POST /api/subscriptions/preview-template` 及前端交互。

---

## 2. 系统拓扑与核心调用时序

```
+---------------------------------------------------------------------------------------------------------+
|                                        Web 管理控制台 (前端 UI 视窗)                                       |
|                                                                                                         |
|  +-----------------------------+  +-------------------------------+  +-------------------------------+  |
|  |    HTML5 视频播放弹窗       |  |      FloodWait 倒计时沙漏     |  |    System Doctor 自检看板     |  |
|  |  (边下边播/Range拖拽/PotPlayer)| |   (SSE 动态流式广播/自动续跑)  |  |  (Java/TDLib/OpenList/存储自检)|  |
|  +--------------+--------------+  +---------------+---------------+  +---------------+---------------+  |
+-----------------|---------------------------------|----------------------------------|------------------+
                  | GET /api/media/stream           | GET /api/tg/floodwait/status     | GET /api/system/doctor
                  | (Range: bytes=start-end)        | SSE: flood_wait_update           | (3秒超时/脱敏结构化JSON)
                  v                                 v                                  v
+---------------------------------------------------------------------------------------------------------+
|                                     FastAPI 桥接核心服务 (bridge_server.py)                               |
|                                                                                                         |
|  [ 1. /api/media/stream 引擎 ] <======================================================+                 |
|       ├── HTTP Range 解析器 (RFC 7233 协议支持: bytes=start-end / start- / -suffix)   |                 |
|       ├── 边下边播动态探测器 (os.path.getsize 实时写入边界检测 & MP4 moov 首包直透)  |                 |
|       ├── 异步生成器流式传输 (64KB~256KB 分块输出, try...finally 严格释放 FD)         |                 |
|       └── 安全路径白名单校验 (_safe_id 过滤, 严防 ../ 逃逸, 限制 APP_ROOT_DIR)         |                 |
|                                                                                       |                 |
|  [ 2. FloodWait 智能冷却调度器 ] <====================================================+                 |
|       ├── TDLib 420 / 429 报错智能捕获 (FLOOD_WAIT_(\d+) 正则提取 wait_seconds)       |                 |
|       ├── 冷却状态机与持久化 (.flood_wait_state.json, 账号级隔离, 取最大冷却时间)     |                 |
|       ├── 异步续跑定时器 (asyncio.create_task 后台单例, 倒计时归零自动唤醒队列)        |                 |
|       └── SSE 广播推送引擎 (向前端推送倒计时秒数与自动刷新信号)                       |                 |
|                                                                                       |                 |
|  [ 3. System Doctor 诊断引擎 ]                                                        |                 |
|       ├── 并发无阻探测池 (asyncio.gather, 4 项探针并行, 2.5s 单项超时, 3.0s 全局熔断) |                 |
|       ├── 探针 1: Java Vert.x 后端 (8123 端口 HTTP GET /health + WebSocket 连通性)     |                 |
|       ├── 探针 2: TDLib 客户端会话 (getAuthorizationState + 账号登录状态 + RTT)        |                 |
|       ├── 探针 3: OpenList 网盘引擎 (5244 端口 /api/me 令牌校验 + /api/fs/list 挂载)  |                 |
|       ├── 探针 4: 本地磁盘与存储 (APP_ROOT_DIR 临时文件读写延时 + 85% 水位熔断状态)   |                 |
|       └── 敏感凭据脱敏器 (密码掩码, Token 截断保留前后4位, 内网 IP 隐匿)              |                 |
|                                                                                       |                 |
|  [ 4. 订阅高级变量与优先级匹配引擎 ]                                                  |                 |
|       ├── 高级变量提取 ({resolution} / {ext} / {chat_title} 正则/元数据推导)          |                 |
|       ├── 路径清洗安全阀 (_sub_clean_seg 防穿越, 过滤特殊字符, 截断 64 字符)          |                 |
|       └── 确定性规则优先级排序 (Priority DESC -> Specificity DESC -> CreatedAt ASC)  |                 |
+---------------------------------------------------------------------------------------+-----------------+
        |                                       |                               |                 |
        v (HTTP :8123 / WS)                     v (TDLib Native Session)        v (HTTP :5244)    v (Local FS)
+-------------------------------+       +-------------------------------+ +-------------------+ +-------------------+
|      Java 后端核心服务        |       |      TDLib 客户端会话         | |  OpenList 网盘系统| |  宿主机本地存储   |
|   (tg-files-api / Vert.x)     |       |   (Telegram 官方协议核心)     | | (多存储驱动与挂载)| |  (APP_ROOT_DIR)   |
+-------------------------------+       +-------------------------------+ +-------------------+ +-------------------+
```

---

## 3. 特性一：网页内 HTTP 206 视频流式预览与边下边播

> ⚠️ **已废弃（2026-09-10）**：本特性已按用户要求从项目**整体移除**。
> 相关端点 `/api/media/info`、`/api/media/stream` 已删除；`services/media_service.py`
> 中的范围解析/分片读取/媒体解析辅助函数已删除；前端的 `__playLocalVideo`、
> `__closeLocalVideo`、`__copyLocalStreamUrl`、`__openLocalExternalPlayer` 与
> `localVideoModal` 弹窗均已移除。本节仅作为历史设计记录保留，**不代表当前实现**，
> 请勿据此重新实现。云端归档页的 OpenList 在线播放属另一条链路，未受影响。

### 3.1 协议规范与 RFC 7233 契约

流式播放接口必须严格遵循 RFC 7233 标准：

1. **请求头解析与区间规范**：
   - 客户端携带 `Range: bytes=start-end` 请求特定字节区间；
   - 支持格式：
     - `bytes=0-1048575`：请求起始 1MB 数据（播放器解析视频元数据原子 `moov`）；
     - `bytes=1048576-`：请求从 1MB 开始至文件当前有效末尾；
     - `bytes=-524288`：请求尾部 512KB 数据；
   - 若未携带 `Range` 头，支持以完整文件响应（HTTP 200）或默认流式分片，并统一声明 `Accept-Ranges: bytes`。

2. **响应头标准契约（HTTP 206 Partial Content）**：
   - 响应状态码：`206 Partial Content`；
   - `Accept-Ranges: bytes`；
   - `Content-Range: bytes {start}-{end}/{total}`（如 `Content-Range: bytes 0-1048575/10485760`）；
   - `Content-Length: {length}`（`length = end - start + 1`）；
   - `Content-Type: video/mp4`（根据文件扩展名与魔数动态自适应，兜底 `application/octet-stream`）；
   - `Cache-Control: no-cache`（边下边播状态下必须声明无缓存，防止浏览器错误缓存未写完的文件片段）。

3. **越界与异常处理（HTTP 416 Range Not Satisfiable）**：
   - 若客户端请求的起始位置 `start >= total` 或 `start > end`，直接返回 `416` 状态码；
   - 响应头必须携带 `Content-Range: bytes */{total}`；
   - 严禁向客户端发送超过物理实际已写入边界的数据。

### 3.2 边下边播（In-Progress Streaming）与文件并发读写

针对正在由 TDLib 写入本地磁盘的视频任务（`downloadStatus == 'downloading'`）：
1. **写入边界动态检测**：
   - 服务端接收到 `/api/media/stream` 请求时，通过 `os.path.getsize(filepath)` 实时探测本地物理已写入字节数 `current_written`；
   - 若 `current_written == 0`，返回等待重试提示；
   - 有效分片截断：`effective_end = min(requested_end, current_written - 1)`；
   - 若客户端请求的起始位置 `start >= current_written`，返回 HTTP 416 或轻量等待；
2. **MP4 Moov 首包 FastStart 解析**：
   - Telegram 传输的主流视频采用 FastStart 模式（`moov` 原子位于文件前部）；
   - 只要前 1MB~5MB 下载落盘，HTML5 播放器即可解析出时长、分辨率与编码格式，立即进入边下边播状态。

### 3.3 异步生成器与句柄安全（防 FD 泄漏）

为防范高并发或用户频繁拖拽导致的连接中断及文件描述符（FD）泄漏：
1. **生成器模式**：
   ```python
   async def stream_file_chunks(file_path: str, start: int, length: int, chunk_size: int = 65536):
       with open(file_path, "rb") as f:
           f.seek(start)
           remaining = length
           while remaining > 0:
               read_len = min(chunk_size, remaining)
               chunk = f.read(read_len)
               if not chunk:
                   break
               remaining -= len(chunk)
               yield chunk
   ```
2. **连接中断瞬时回收**：
   - 在 FastAPI 中通过 `StreamingResponse(stream_file_chunks(...))` 返回；
   - 外层上下文管理器 `with open(...)` 保证在客户端主动断开、网络异常或读取结束时立即触发 `f.close()`，杜绝 FD 泄漏。
3. **安全沙箱与路径防逃逸**：
   - 参数仅接受合法的 `unique_id`（严格校验 `^[A-Za-z0-9_=-]{4,160}$`）；
   - 严禁传入任意相对路径，通过系统任务指纹库安全解析出物理路径，并断言路径严格落在授权的 `APP_ROOT_DIR` 内，违者返回 403 Forbidden。

### 3.4 核心 API 契约规范

#### 1) 媒体流式点播端点：`GET /api/media/stream`
- **请求参数**：
  - `unique_id`：文件唯一标识符（必填）；
  - `task_id`：任务 ID（可选）；
- **请求头**：`Range: bytes=0-1048575`（可选）；
- **响应头**：
  ```http
  HTTP/1.1 206 Partial Content
  Accept-Ranges: bytes
  Content-Range: bytes 0-1048575/52428800
  Content-Length: 1048576
  Content-Type: video/mp4
  Cache-Control: no-cache
  ```

#### 2) 媒体元数据查询端点：`GET /api/media/info`
- **请求参数**：`unique_id`；
- **响应体（JSON）**：
  ```json
  {
    "ok": true,
    "data": {
      "uniqueId": "AQADAgADx6cxG...",
      "filename": "sample_movie_1080p.mp4",
      "mimeType": "video/mp4",
      "totalBytes": 52428800,
      "downloadedBytes": 15728640,
      "downloadStatus": "downloading",
      "playWhileDownloading": true,
      "bufferedPercent": 30.0,
      "streamUrl": "/api/media/stream?unique_id=AQADAgADx6cxG..."
    }
  }
  ```

### 3.5 前端交互与播放器弹窗规范

1. **入口触发**：
   - 在本地在存页（`/library/local`）的网格与表格视图操作列增加“▶ 预览”按钮；
   - 在任务列表（`/tasks`）中，对下载中（已缓冲 > 1MB）或已完成的视频任务提供“▶ 边下边播”按钮；
2. **弹窗设计（#localVideoModal）**：
   - 现代深色磨砂遮罩，居中展示 HTML5 `<video controls playsinline preload="metadata">`；
   - 顶部显示视频标题与“⚡ 边下边播中 · 已下载 30%”动态徽章；
   - 底部工具栏提供：
     - **复制播放直链**：复制绝对 URL 至剪贴板；
     - **外部播放器唤起**：一键调用 `potplayer://{stream_url}` 或 `vlc://{stream_url}`；
     - **关闭**：立即销毁视频源，暂停播放并触发后端流式连接回收。

---

## 4. 特性二：Telegram FloodWait 智能冷却与排队倒计时

### 4.1 异常捕获模型

系统需全面捕获 Telegram 协议层与 Bot 接口层的限流报错：

1. **TDLib 报错捕获**：
   - 响应特征：`code: 420` 或 `code: 429`，错误信息符合正则：
     ```python
     FLOOD_WAIT_PATTERN = re.compile(r"(?:FLOOD_WAIT_|retry after )(\d+)", re.IGNORECASE)
     ```
   - 提取分组中的整数作为强制冷却秒数 `wait_seconds`；
2. **Telegram Bot API 报错捕获**：
   - HTTP 429 响应，提取 `parameters.retry_after` 或 `description` 中的整数秒；
3. **安全边界与时间上限**：
   - 若提取数值超过单次合理上限（如 > 86400 秒），记录致命告警，冷却时间上限设为 86400 秒。

### 4.2 账号级状态机与持久化

```
                      [ 正常任务调用 / 下载请求 ]
                                  |
                                  v
                      +-----------------------+
                      |     NORMAL (就绪)     |
                      +-----------------------+
                                  |
                                  | 捕获 FLOOD_WAIT_X 异常
                                  v
                      +-----------------------+
                      | FLOOD_WAIT (排队挂起) | <--- 并发多次限流: wait_until = max(current, new)
                      +-----------------------+
                                  |
                                  | 倒计时归零 (remaining <= 0)
                                  v
                      +-----------------------+
                      |  AUTO_RESUME (唤醒)   |
                      +-----------------------+
                                  |
                                  | 恢复调度队列 / 自动重放挂起任务
                                  v
                             (返回 NORMAL)
```

1. **持久化文件**：`APP_ROOT_DIR/.flood_wait_state.json`（权限 `0600`）；
2. **并发防击穿合并算法**：
   - 同一账号在未脱离冷却期前再次捕获限流信号，新的到期时间为：
     $$\text{cooldown\_until} = \max(\text{current\_until}, \text{now} + \text{wait\_seconds})$$
   - 杜绝因错误计算将冷却倒计时缩短；
   - 冷却期间进入的新任务自动被调度器置为 `flood_wait` 挂起状态，杜绝向上游发送无效高频请求。

### 4.3 异步续跑与自动恢复引擎

1. **单例后台定时器**：
   - 采用轻量级 `asyncio.create_task` 维护后台单例定时轮询任务（每 1 秒轮询一次）；
2. **自动恢复工作流**：
   - 当 `remaining_seconds <= 0` 时：
     1. 将状态机切换为恢复态，重置冷却标志位；
     2. 扫描所有状态为 `flood_wait` 的任务；
     3. 恢复任务调度状态至 `pending` 并触发重新下载；
     4. 触发 SSE 广播事件 `flood_wait_cleared`，通知前端静默局部刷新；
     5. 写入系统审计日志。

### 4.4 核心 API 契约规范

#### 1) 冷却状态查询接口：`GET /api/tg/floodwait/status`
- **响应体（JSON）**：
  ```json
  {
    "ok": true,
    "data": {
      "isCooling": true,
      "account": "default",
      "remainingSeconds": 45,
      "totalWaitSeconds": 60,
      "cooldownUntil": 1725600060.0,
      "reason": "FLOOD_WAIT_60",
      "suspendedTasksCount": 4,
      "message": "Telegram 触发风控限流，系统已安全挂起，正在冷却排队中"
    }
  }
  ```

#### 2) 管理员强制重置接口：`POST /api/tg/floodwait/reset`
- **说明**：支持管理员手动提前解除冷却态并唤醒排队任务；
- **响应体**：
  ```json
  {
    "ok": true,
    "message": "已成功强制清除 FloodWait 冷却状态，挂起任务已恢复调度"
  }
  ```

### 4.5 前端沙漏倒计时交互规范

1. **吸顶全局沙漏预警条**：
   - 当 `isCooling == true` 时，页面顶部滑出橙黄色悬浮条；
   - 显示沙漏 ⏳ 旋转微动画与实时动态倒计时（格式：`00:45`）；
   - 附带说明文案：“Telegram 触发风控冷却 · 任务已安全挂起 · 倒计时结束后全自动续跑”；
2. **任务表格状态胶囊**：
   - 处于挂起的任务在阶段列显示：`<span class="pill warn"><span class="p-dot"></span>⏳ 冷却中 00:45</span>`；
3. **零秒自愈无感刷新**：
   - 倒计时归零时，预警条自动变为绿色并渐隐消失，任务表格通过 htmx / SSE 自动局部刷新，无需人工刷新页面。

---

## 5. 特性三：System Doctor 系统健康自检面板

### 5.1 四大依赖探针规范

自检接口必须对系统依赖的基础组件进行真实网络与功能探测：

| 探测依赖项 | 探测指标与实现逻辑 | 正常标准 (Healthy) | 预警标准 (Warning) | 严重故障 (Critical) |
| :--- | :--- | :--- | :--- | :--- |
| **Java 后端核心** (`:8123`) | HTTP GET `/health` 存活探测 + WebSocket 握手状态 + 请求 RTT 延时 | 状态码 200，RTT < 100ms | 100ms <= RTT < 1000ms | 端口拒绝、HTTP 5xx、超时 > 2.5s |
| **TDLib 客户端会话** | 调用 `getAuthorizationState` 探测会话认证态与活跃账号延时 | 状态为 `authorizationStateReady` | 存在非致命警告，或处于 FloodWait 冷却 | 会话关闭、客户端未创建或无法通信 |
| **OpenList 云端网盘** (`:5244`) | GET `/api/me` 校验 JWT 令牌 + GET `/api/fs/list` 探测网盘挂载列表 | 令牌有效且挂载数量 >= 1 | 令牌有效但无挂载存储 | 服务不可达、令牌过期（401）、网关错误 |
| **VPS 本地存储** | `APP_ROOT_DIR` 读写测试文件延时 + 磁盘使用率与高低水位 | 读写正常，磁盘使用率 < 85% | 读写正常，85% <= 水位 < 95% | 磁盘无写权限，或使用率 >= 95% (熔断) |

### 5.2 异步并发与 3 秒硬超时隔离

1. **并发探测执行池**：
   - 采用 `asyncio.gather(probe_java(), probe_tdlib(), probe_openlist(), probe_storage(), return_exceptions=True)` 并发执行；
2. **单项与全局熔断保护**：
   - 单个探针执行 `asyncio.wait_for(probe, timeout=2.5)` 保护；
   - 遇到网络中断、DNS 解析失败或超时时，探针内部自行降级捕获，输出错误信息，严禁向外抛出未处理异常；
   - 整体接口执行耗时严格控制在 **3.0 秒以内**，确保前端诊断卡片秒级呈现。

### 5.3 敏感信息脱敏契约

为防范未授权信息泄露，接口必须对诊断结果进行严格脱敏：
- **凭据掩码**：密码、私钥、Telegram API Hash 一律脱敏为 `******`；
- **令牌截断**：JWT / Bearer Token 仅输出前 4 位和后 4 位，如 `eyJ1...F98A`；
- **网络拓扑与路径隐匿**：隐藏 VPS 内网私有 IP（如 `172.x.x.x` 或 `10.x.x.x` 隐匿为 `backend-service`），系统绝对路径隐藏为相对路径。

### 5.4 核心 API 契约规范

#### 1) 一键自检诊断接口：`GET /api/system/doctor`
- **响应体（JSON）**：
  ```json
  {
    "ok": true,
    "timestamp": 1725600000.0,
    "overallStatus": "healthy",
    "totalLatencyMs": 45.2,
    "report": {
      "javaBackend": {
        "name": "Java 后端核心 (tg-files-api)",
        "status": "healthy",
        "latencyMs": 12.3,
        "endpoint": "http://backend-service:8123",
        "message": "服务在线，HTTP 及 WebSocket 握手正常",
        "details": { "authenticated": true, "version": "0.4.0" }
      },
      "tdlib": {
        "name": "TDLib 客户端会话",
        "status": "healthy",
        "latencyMs": 14.1,
        "message": "Telegram 会话已就绪 (authorizationStateReady)",
        "details": { "activeAccount": "user_***88", "isFloodWait": false }
      },
      "openlist": {
        "name": "OpenList 云端网盘引擎",
        "status": "healthy",
        "latencyMs": 16.5,
        "endpoint": "http://openlist-service:5244",
        "message": "网盘服务正常，已挂载 3 个云端存储",
        "details": { "tokenValid": true, "mountCount": 3, "username": "admin" }
      },
      "localStorage": {
        "name": "VPS 本地存储与水位",
        "status": "healthy",
        "latencyMs": 2.3,
        "message": "本地磁盘读写正常，使用率 48.5%（水位正常）",
        "details": {
          "totalSpace": "100.0 GB",
          "freeSpace": "51.5 GB",
          "usedPercent": 48.5,
          "isWatermarkMeltdown": false
        }
      }
    },
    "recommendations": []
  }
  ```

#### 2) 轻量存活 Ping 接口：`GET /api/system/doctor/ping`
- **响应体**：`{"ok": true, "pong": true, "timestamp": 1725600000.0}`

### 5.5 前端自检看板交互规范

1. **入口呈现**：
   - 控制台顶栏右侧新增“🩺 System Doctor”状态徽章与呼吸灯；
   - 设置页（`/settings`）设立“系统健康自检”独立功能区；
2. **看板视觉交互**：
   - 4 块现代化卡片网格并排展示各组件健康状态、延时标签与健康指标；
   - 提供“一键重新体检”按钮（2 秒防抖控制），点击时卡片伴随脉冲扫描动画；
   - 提供“复制脱敏体检报告”按钮，一键将诊断数据格式化为 Markdown 方便排查。

---

## 6. 订阅目录模板高级变量与规则优先级匹配

### 6.1 高级变量扩展与解析算法

针对目录模板新增三大高级动态变量：

1. **`{resolution}`（视频分辨率）解析算法**：
   - **优先级推导流程**：
     1. 若文件记录中存在 `width` 与 `height` 元数据：
        - 高度 >= 2160 或 宽度 >= 3840 → 映射为 `4k`；
        - 高度 >= 1080 或 宽度 >= 1920 → 映射为 `1080p`；
        - 高度 >= 720 或 宽度 >= 1280 → 映射为 `720p`；
        - 高度 >= 480 → 映射为 `480p`；
     2. 若元数据缺失，基于文件名和标题执行正则模糊探测：
        ```python
        RES_REGEX = re.compile(r"\b(4k|2160p|1080p|1080i|720p|480p|360p)\b", re.IGNORECASE)
        ```
     3. 若仍无法识别或非视频类型，平滑回退为安全占位符 `unknown`。
2. **`{ext}`（文件小写扩展名）解析算法**：
   - 从文件名中提取最后一个 `.` 后的扩展名串，全部转为小写；
   - 过滤保留纯字母数字字符 `[a-z0-9]`，最长截断为 10 字符；
   - 若无扩展名，基于 `mimeType` 推断映射（如 `video/mp4` → `mp4`），兜底回退为 `bin`。
3. **`{chat_title}`（清洗后的频道名称）**：
   - 取来源频道的原始 Title；
   - 经 `_sub_clean_seg` 过滤全部禁忌字符（`\ / : * ? " < > |` 及控制字符），压缩空格并去除首尾点号，截断至 64 字符。
4. **既有变量兼容**：
   - 完整保持对 `{source}`, `{type}`, `{YYYY}`, `{MM}`, `{DD}`, `{YYYY-MM}` 的 100% 兼容支持。

### 6.2 规则优先级权重与特异度复合匹配算法

为解决多规则冲突并支持精细化分流，引入多因子确定性排序匹配引擎：

1. **规则数据模型字段扩充**：
   - `priority: int`：优先级权重（默认 `0`，可设置区间 `-100` 至 `1000`，数值越大越先匹配）；
   - `mediaFilter: str`：媒体过滤（如 `video`, `all`）；
2. **特异度计算算法（Specificity Score）**：
   - 精确指定 `chatId`（如 `-10012345678`）：特异度得分 `100`；
   - 通配符全匹配规则（`chatId == "*"`）：特异度得分 `10`；
   - 指定了具体媒体类型过滤（非 `all` 或 `*`）：特异度加 `20` 分；
3. **复合排序键（Deterministic Sort Key）**：
   - 对所有启用的规则按照以下键进行降序排列：
     $$\text{SortKey}(R) = (-\text{priority}, -\text{specificity}, \text{created\_at}, \text{id})$$
4. **匹配执行流程**：
   1. 当下载完成任务触发自动归档扫描时，提取任务所属账号、频道、类型与元数据；
   2. 按已排序的规则列表逐一执行条件校验；
   3. **首个全匹配命中的规则即刻接管归档路径渲染，后续规则熔断短路**；
   4. 若所有规则均未命中，安全回退至系统全局默认归档规则。

### 6.3 核心 API 契约规范

#### 1) 订阅规则增改接口：`POST /api/subscriptions/rule`
- **请求体（JSON）**：
  ```json
  {
    "telegramId": 12345678,
    "chatId": "-1001234567890",
    "chatTitle": "电影分享频道",
    "priority": 100,
    "dirTemplate": "/影视/{chat_title}/{resolution}/{ext}",
    "deleteLocal": true,
    "policy": "skip",
    "enabled": true
  }
  ```

#### 2) 模板实时演算预览接口：`POST /api/subscriptions/preview-template`
- **请求体（JSON）**：
  ```json
  {
    "dirTemplate": "/媒体/{chat_title}/{YYYY-MM}/{resolution}/{ext}",
    "sample": {
      "chatTitle": "4K纪录片频道",
      "filename": "Earth_Episode1_1080p.mkv",
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
    "previewDir": "/媒体/4K纪录片频道/2024-09/1080p/mkv",
    "variables": {
      "chat_title": "4K纪录片频道",
      "resolution": "1080p",
      "ext": "mkv",
      "YYYY-MM": "2024-09"
    }
  }
  ```

### 6.4 前端交互规范

1. **规则列表展示与排序控制**：
   - 规则表格增加“优先级”列标签（如 `<span class="badge blue">优先级 100</span>`）；
   - 支持上下微调按钮直接增减权重，表格按优先级实时响应重排；
2. **模板输入辅助组件**：
   - 在模板输入框下方提供快速插入芯片：`{resolution}`、`{ext}`、`{chat_title}`、`{source}`、`{YYYY-MM}`；
   - 点击芯片即时插入到光标位置；
   - 实时预览区域动态回显模拟演算路径，未知非法变量以红字清晰标出。

---

## 7. 实施计划与质量门禁映射

1. **Task t1 (Requirements，当前任务)**：定稿并发布本规范文档 `docs/spec-deep-features-doctor-preview-floodwait.md`；
2. **Task t2 (Implementation，核心全栈实现)**：
   - 落地 `/api/media/stream` 边下边播后端生成器与前端弹窗；
   - 落地 FloodWait 状态机持久化、自动续跑定时器与沙漏组件；
   - 落地 `GET /api/system/doctor` 4 组件异步探针与脱敏面板；
   - 落地订阅高级变量与多因子优先级匹配算法；
3. **Task t3 (Verification，自动化测试验证)**：编写全套单元与端到端测试用例，确保 100% 覆盖；
4. **Task t4 (Review & Security Audit)**：执行架构审查（FD 泄漏/内存占用）与安全审计（路径逃逸/鉴权脱敏/防 DoS）。
