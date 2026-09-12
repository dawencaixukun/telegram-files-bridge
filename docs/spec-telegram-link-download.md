# Telegram 消息链接一键解析直投下载规范文档（P0）

**版本**：v1.0.0  
**状态**：已定稿（Requirements Passed）  
**关联任务**：`t1` (Requirements) → `t2` (Implementation) → `t3` (Verification) → `t4` (Code Review) → `t5` (Security Audit) → `t6` (Integration)  
**作者**：全栈开发工程师（engineer）

---

## 1. 概述与业务背景

本功能旨在解决用户在 Telegram 客户端中浏览到音视频/文档等大文件时，需手动逐层进入对应频道翻找或手动逐个下载的低效流程。通过提供「一键解析 Telegram 消息链接 → 提取媒体元数据 → 确认直投下载 → 联动自动归档」的闭环能力，极大提升文件归档与下载体验。

---

## 2. Telegram 消息链接格式规范与正则解析引擎

### 2.1 支持的 URL 格式定义
Telegram 官方消息链接主要分为两类核心形态，以及可能附带的参数与 Topic 扩展：

1. **公开频道/超级群组链接 (Public Channel / Supergroup)**
   - 格式：`https://t.me/<username>/<messageId>`
   - 别名域名：`https://telegram.me/<username>/<messageId>`, `https://telegram.dog/<username>/<messageId>`
   - 示例：`https://t.me/tech_news/12345`
   - Topic/Forum 支持：`https://t.me/<username>/<topicId>/<messageId>`
   - 附带参数：`https://t.me/<username>/<messageId>?single`

2. **私有频道/私有超级群组链接 (Private Channel / Supergroup)**
   - 格式：`https://t.me/c/<chatId>/<messageId>`
   - 示例：`https://t.me/c/1827364521/987`
   - Topic/Forum 支持：`https://t.me/c/<chatId>/<topicId>/<messageId>`
   - 内部对应：`chatId` 为正整数（通常 5~20 位数字），TDLib 映射实际 Chat ID 为 `-100<chatId>`。

### 2.2 提取字段定义
| 字段名 | 类型 | 说明 | 示例 |
|---|---|---|---|
| `linkType` | `str` | 链接类型：`"public"` 或 `"private"` | `"private"` |
| `chatIdentifier`| `str` | 标识符：公开链接为 `username`，私密链接为纯数字 `chatId` | `"1827364521"` 或 `"tech_news"` |
| `topicId` | `Optional[int]` | 话题 ID（若存在） | `1024` 或 `None` |
| `messageId` | `int` | 目标消息 ID | `987` |
| `canonicalUrl` | `str` | 规范化后的标准 URL（去除查询参数，强制 `https://t.me/`） | `"https://t.me/c/1827364521/987"` |

### 2.3 规范化正则表达式规范（安全抗 ReDoS）
严禁使用包含指数级回溯的多层嵌套通配符。输入前执行安全截断（最大 512 字符）与空白清理。

```python
import re
from urllib.parse import urlsplit
from typing import Optional, Dict, Any

ALLOWED_TG_DOMAINS = {
    "t.me",
    "telegram.me",
    "telegram.dog"
}

# 1. 规范私有链接：t.me/c/<chat_id>/[<topic_id>/]<message_id>
RE_PRIVATE_LINK = re.compile(
    r"^(?:https?://)?(?:[a-zA-Z0-9-]+\.)*(t\.me|telegram\.me|telegram\.dog)/c/(\d{5,20})(?:/(\d+))?/(\d{1,12})(?:\?.*)?$",
    re.IGNORECASE
)

# 2. 规范公开链接：t.me/<username>/[<topic_id>/]<message_id>
RE_PUBLIC_LINK = re.compile(
    r"^(?:https?://)?(?:[a-zA-Z0-9-]+\.)*(t\.me|telegram\.me|telegram\.dog)/([a-zA-Z0-9_]{4,32})(?:/(\d+))?/(\d{1,12})(?:\?.*)?$",
    re.IGNORECASE
)
```

解析规则流程：
1. 链接预处理：去除前后空白，长度校验（10 ~ 512 字符）。
2. 使用 `urllib.parse.urlsplit` 抽取 Host，严格校验 Host 是否位于 `ALLOWED_TG_DOMAINS`（或以其为结尾且以 `.` 分隔），杜绝非白名单或包含 `@` 用户认证语法的伪造 URL。
3. 优先匹配私有频道正则；若未命中则匹配公开频道正则。
4. 提取对应的 `chatId`/`username` 与目标 `messageId`（若有 topicId，尾段作为真实 messageId）。
5. 组装规范化 URL。

---

## 3. API 接口规范：`/api/tg/resolve-link`

### 3.1 接口基础契约
- **请求方式**：`POST`
- **路径**：`/api/tg/resolve-link`
- **Content-Type**：`application/json`
- **认证要求**：Portal Session Cookie (`dsh_portal_session`) 必选
- **安全防线**：
  - 双重提交 CSRF 校验：Header `X-CSRF-Token` 必须与 Cookie 匹配。
  - 防 SSRF 白名单过滤：禁止解析任何非 Telegram 官方域名的 URL。
  - 请求频率限制（Rate Limit）：单会话 30 次/分钟，防扫描打垮 TDLib。

### 3.2 请求参数 (Request Body)
```json
{
  "link": "https://t.me/c/1827364521/987",
  "telegramId": 1
}
```
| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `link` | `string` | 是 | Telegram 消息链接，支持公开或私密链接 |
| `telegramId` | `int` | 否 | 指定解析所用的 TG 账号客户端 ID；不传则自动选择当前已登录且状态健康的账号 |

### 3.3 成功响应 (Response Body 200 OK)
```json
{
  "ok": true,
  "code": "SUCCESS",
  "message": "解析成功",
  "data": {
    "link": "https://t.me/c/1827364521/987",
    "canonicalUrl": "https://t.me/c/1827364521/987",
    "linkType": "private",
    "chatIdentifier": "1827364521",
    "messageId": 987,
    "telegramId": 1,
    "files": [
      {
        "fileId": 108273,
        "uniqueId": "AgADBAAD-q0x...",
        "filename": "极客精选_042期.mp4",
        "size": 524288000,
        "sizeHuman": "500.0 MB",
        "mimeType": "video/mp4",
        "fileType": "video",
        "chatId": -1001827364521,
        "chatTitle": "技术资料归档群",
        "messageId": 987,
        "date": 1725510000,
        "telegramId": 1,
        "isAlreadyDownloaded": false,
        "isAlreadyArchived": false
      }
    ]
  }
}
```

### 3.4 异常响应定义
| HTTP Status | `code` | 触发场景 | 客户端提示 |
|---|---|---|---|
| `400` | `INVALID_LINK_FORMAT` | 链接格式不符合 Telegram 消息规范 | 请输入有效的 Telegram 消息链接 (如 t.me/c/xxx/123) |
| `400` | `SSRF_BLOCKED` | 域名不在白名单内或尝试指向内网/恶意 IP | 链接域名不合法，仅支持 Telegram 官方消息链接 |
| `401` | `UNAUTHORIZED` | 未携带有效 Session Cookie 或登录失效 | 会话已过期，请重新登录管理台 |
| `403` | `CSRF_FAILED` | CSRF Token 缺失或不匹配 | 请求校验失败，请刷新页面后重试 |
| `404` | `NO_FILES_FOUND` | 消息有效但该消息不包含可供下载的文件/媒体 | 该消息不包含可下载的文件或媒体附件 |
| `409` | `TG_ACCOUNT_UNAVAILABLE` | 系统无可用在线 Telegram 账号 | 当前无在线的 Telegram 账号，请先完成账号登录 |
| `502` | `TG_BACKEND_ERROR` | TDLib 后端解析超时或权限不足（未加群等） | 解析失败：账号未加入该私有群或该消息已被撤回 |

---

## 4. 前端交互设计与确认弹窗模型

### 4.1 顶栏快捷输入区（Top Bar Quick Entry）
- **位置**：管理后台顶栏导航（`templates/base.html`）右侧操作区，位于「提交下载」按钮旁。
- **展示形式**：
  - 采用流线型胶囊输入组件，带 TG 图标、输入框与回车解析图标按钮。
  - 占位提示：`粘贴 t.me 消息链接直接下载...`
  - 支持快捷键触发：全站按下 `Ctrl+K` 或 `/` 快捷聚焦输入框。
- **即时响应**：
  - 用户粘贴完成后回车或点击按钮，输入框右侧变为加载 Spinner。
  - 客户端即时前置正则校验：若非合规链接，直接触发红框动画并显示 Toast「请输入有效的 Telegram 消息链接」。

### 4.2 直投解析确认弹窗（Direct Download Modal）
- **弹窗设计（Glassmorphism 拟态风格）**：
  - 弹窗头部：`直投下载确认` + 关闭按钮（支持 Esc / 点击遮罩退出）。
  - **媒体卡片区**：
    - 图标：根据 `fileType`（video / document / audio / photo）显示对应彩色文件图标。
    - 文件名：大号字，支持长文件名自动省略与悬浮 Tooltip。
    - 元数据标签：`大小: 500 MB` · `来源: 技术资料归档群` · `消息 ID: #987` · `账号: Account #1`。
  - **调度配置区**：
    - [复选框] **下载完成后自动归档到云端**（默认勾选）。
    - 联动目录选择：展开目标云端网盘路径（记忆上次选择目录，如 `/阿里云盘/tg-archive/`）。
    - 冲突处理选项：覆盖 / 跳过已存在。
  - **防重复提示**：
    - 若 `isAlreadyDownloaded` 为 true，黄色警告胶囊提示「本地已存同文件」，提供「重新下载」与「直接归档」快捷选择。
  - **底部动作条**：
    - `[取消]`（Ghost 按钮）
    - `[立即直投下载 ⏎]`（Primary 渐变按钮，带下载图标，回车触发）。

---

## 5. 直接下载与自动归档调度流架构

### 5.1 整体调度时序
```
[用户顶栏输入链接]
       │
       ▼
[前端 POST /api/tg/resolve-link]
       │
       ├─ (白名单与格式校验) ───[不合规]──> 返回 400
       ▼
[BackendClient.resolve_link(tg_id, link)]
       │
       ▼ (TDLib GetMessageLinkInfo)
[解析得 {chatId, messageId, fileId, uniqueId, filename, size}]
       │
       ▼
[返回解析结果 JSON] ──> 前端展示「直投确认弹窗」
       │
       ▼ [用户确认「立即直投下载」]
[前端 POST /api/tg/direct-download]
       │
       ├─ 1. 构建下载负载 files: [{telegramId, chatId, messageId, fileId}]
       ├─ 2. 调用 POST /files/start-download-multiple
       ├─ 3. 若勾选「自动归档」:
       │     创建 auto_archive 预定调度记录，绑定 uniqueId 与 targetDir
       ├─ 4. 清理后端缓存 BackendClient._cache.clear()
       ├─ 5. 触发任务事件，广播至 SSE / 任务队列，右上角弹出 Toast「已加入下载队列」
       ▼
[后台下载工作流推进]
       │
       ▼ (任务下载完成 status = completed)
[触发 _sub_on_job_finished 或 auto_archive 检查]
       │
       ├─ 若存在预定自动归档标记:
       │     自动入队 POST /archive/start，流式上传至 OpenList 指定云盘
       │     流转归档状态：排队 -> 上传中 xx% -> 已归档
       ▼
[归档完成，更新 Library 状态，发布系统通知]
```

### 5.2 状态持久化与容错设计
1. **自动归档调度记录持久化**：
   - 调度配置写入磁盘 `_AUTO_ARCHIVE_RECORDS` 文件（`0600` 权限保护），即使 bridge 服务重启，下载完成后的归档动作不会丢失。
2. **下载去重保护**：
   - 根据 `uniqueId` 和 `fileId` 进行排队检测，防止高频重复直投对 TDLib 和带宽造成雪崩。

---

## 6. 安全门禁与防护规范总结

| 安全威胁 | 应对防护机制 | 验收标准 |
|---|---|---|
| **SSRF (服务端请求伪造)** | 严格限定 URL 协议为 `https://` / `http://`；Host 精准限定于白名单集合；禁止 IP 直连与私有网段；禁止解析包含认证凭据 `@` 的畸形 URL | 任意内网 IP、非 TG 官方域名请求均被拦截拒绝（400） |
| **ReDoS (正则回溯拒绝服务)** | 限制输入文本长度（<= 512 字节）；采用线性或具备严密边界锚定的无歧义正则表达式 | 超长/特制恶意畸形链接解析耗时 <= 1ms |
| **CSRF (跨站请求伪造)** | 全站启用双重提交 Cookie 防护；POST 接口强制校验请求头 `X-CSRF-Token` | 无有效 CSRF Header 的 POST 请求一律返回 403 |
| **越权与账号探测** | 仅已认证后台会话可调用；后端调用 TDLib 时严格使用系统已知 Session；不回显任何内部系统敏感异常堆栈 | 未登录请求返回 401；隐藏后端敏感 Traceback |
| **大并发资源耗尽** | 接口增加会话级令牌桶限流；并发任务受 `asyncio.Semaphore` 管控 | 恶意高频请求触发 429 速率限制 |

---
*文档编制完成，符合 Task t1 要求，为后续 Task t2 (实现) 与 Task t3 (测试) 提供严密准则。*
