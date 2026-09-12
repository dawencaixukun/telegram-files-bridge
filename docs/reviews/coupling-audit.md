# 前后端耦合度审计报告

- 日期：2026-09-02
- 范围：`bridge_server.py`（前端适配层）⇄ `telegram-files 0.4.0`（Java/Vert.x+TDLib 后端）+ `preview_server.py` + `templates/ + static/`
- 方法：全量静态盘点接触面（HTTP/WS/文件系统/env/协议常量），结合当日线上事件（后端重启 → 401 → 自愈）实测降级行为

## 总体结论

**传输层、浏览器侧、数据存储三方面解耦良好；风险集中在"后端内部协议细节"的深度绑定 —— bridge 实质上是后端的第二个官方前端，与后端版本 0.4.0 强绑定。**

| 维度 | 结论 | 证据 |
|---|---|---|
| 浏览器 ⇄ 后端 | ✅ 完全解耦 | 模板/JS 中无任何后端地址或 `/api/*` 直连（grep 为空），浏览器只与 bridge 通信（JSON + SSE） |
| 进程/部署 | ✅ 解耦 | 独立 systemd 服务，仅靠 `TG_API_URL` 环境变量指向后端；bridge 可独立重启/部署（当日验证多次） |
| 数据存储 | ✅ 解耦 | bridge 不读后端 `db.sqlite`/`account/`/`data.db`，只在共享 app-data 目录写自己的 3 个标记文件（`.bridge_secret`/`.bridge_initialized`/`.backend_creds`，均 0600） |
| 实时事件 | ✅ 协议翻译层 | 后端 WebSocket → bridge → 浏览器 SSE（`ws_relay_loop`），断线指数退避重连（封顶 60s） |
| 后端宕机降级 | ✅ 页面不白屏 | 83 处异常处理；后端不可达时页面仍 200 + 空态 + 日志告警（当日 401 事件实测） |
| 前端预览 | ✅/⚠️ | `preview_server` 纯 mock、零后端调用（解耦证明）；但与 bridge 的数据翻译是**两套独立实现**，靠人肉对齐 |
| 后端内部协议 | ⚠️ **深耦合** | 见下节风险清单 |

## 接触面清单

### 1. HTTP API（21 个端点，全部走 `TG_API_URL`）
```
/auth/bootstrap  /auth/bootstrap/status  /auth/login  /auth/logout
/auth/password   /auth/session
/telegrams       /telegram/create        /telegram/{tg}/delete
/telegram/{tg}/chats
/telegram/{tg}/chat/{ch}/files          /telegram/{tg}/chat/{ch}/files/count
/telegram/{tg}/chat/0/files
/telegram/api/{method}                  ← TDLib 裸方法透传（TG 登录向导用）
/files            /files/start-download-multiple
/tasks            /tasks/{task_id}      /task/retry       /task/cancel
```

### 2. WebSocket（1 个）
`/api/ws?telegramId=…` → bridge 中继为 `/sse/tasks`、`/sse/logs`。每账号一条连接，指数退避重连。

### 3. 文件系统（共享 app-data 目录）
| 路径 | 归属 | bridge 操作 |
|---|---|---|
| `app-data/account/*/`、`data.db` | 后端（TG session） | **只读不写、不打开**（备份脚本除外） |
| `app-data/.bridge_secret` / `.bridge_initialized` / `.backend_creds` | bridge | 读写（0600） |
| 磁盘用量 | 系统 | `shutil.disk_usage` 只读 |

### 4. 环境变量
`TG_API_URL`、`TG_DATA_DIR`、`BRIDGE_PORT` — 三项即全部部署契约。

## ⚠️ 深耦合风险清单（后端升级必查）

| # | 耦合点 | 位置 | 断裂后果 |
|---|---|---|---|
| R1 | **TDLib AuthorizationState 构造器码** ×8 硬编码（TG_WAIT_PHONE=306402531 等，注释明言"与 TdApi.java 一致"） | bridge_server.py ~2274 | TDLib 升级改码 → TG 登录向导状态机失效 |
| R2 | **EventPayload 事件类型码** -1..6（来自 EventPayload.java / 官方前端 WebSocketMessageType） | ~1466 | 日志/任务实时事件解析错乱 |
| R3 | **FileRecord 字段映射**（逐字段注释钉在 FileRecord.java 0.4.0；downloadStatus 枚举钉在 FileRecord.java:33-39） | ~786 | 后端改字段 → 任务/文件库数据错列或丢失 |
| R4 | **nginx 路由知识**：WS 走 `/ws`（无 `/api` 前缀），bridge 手工剥 `/api` 拼 ws:// URL | ~156 | 后端代理结构变化 → 实时事件全断 |
| R5 | **WS 认证机制**：需后端 session cookie（tf_admin/tf_csrf），bridge 手工携带 | ~1573 | 后端改认证 → WS 永远握手失败（HTTP 自愈、WS 哑火） |
| R6 | 任务无后端主键：bridge 用 `uniqueId` 的 sha1 前 48bit 造稳定 id | ~798 | 后端若改 uniqueId 语义 → 任务身份漂移 |
| R7 | preview_server 与 bridge **双实现**数据形状（无共享 import） | preview_server.py | mock 与真实契约漂移，预览失真 |

## 降级行为（实测）

- 后端 401/502/宕机：页面照常 200（空态/错误态文案），`list_all_files` 自动回退逐聊天列举，WS 中继退避重连不刷握手
- 后端会话失效：`_relogin` 用持久化凭据自愈（2026-09-02 修复并验证）
- 已知残留：后端不可达时顶栏状态点是否准确变红（`tg_state`）建议覆盖 502 场景做一次人工确认

## 建议

1. **契约集中化**：把 R1/R2/R3 的硬编码常量抽到 `contracts.py` 单一模块，并加启动期契约探测（后端可达时取一个样本记录校验），不匹配即醒目告警 —— 目前契约只存在于注释里，散落各处
2. **消灭双实现**：`preview_server` 改为 import bridge 的翻译函数，mock 只构造原始 FileRecord —— R7 根治
3. **后端升级回归清单**：升级 telegram-files 版本时，本报告 R1–R6 逐项人工回归
4. **长期**：推动后端暴露稳定任务语义（taskId 主键、下载状态机 REST 化），bridge 逐步去除 TDLib 构造器码解析，降级为纯 REST 消费者
