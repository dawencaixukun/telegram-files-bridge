# 后端代码审查报告（bridge_server.py / preview_server.py / requirements.txt）

- 审查人：backend-reviewer（code-review-v2）
- 审查范围：`/d/webapi/bridge_server.py`（2482 行）、`/d/webapi/preview_server.py`（400 行）、`/d/webapi/requirements.txt`；附查 `.vps-conn/` 运维脚本与相关模板/JS 的对接点
- 审查时间：2026-09-02
- 重点专题：TG 账户 session 存储/序列化/刷新/丢失风险（团队红线）、认证与授权、注入/路径遍历/任意文件读写、SSRF、命令注入、并发与任务队列竞态、错误处理、敏感信息硬编码、日志泄露凭据、明显 bug

---

## 1. 概述

`bridge_server.py` 是一个纯桥接层（FastAPI），本身不保存任何 TG 登录数据：它持有的是**后端管理台会话**（httpx cookie jar：`tf_admin`/`tf_csrf`）与**门禁会话**（浏览器 `tf_portal` HMAC 签名 cookie），而 TG 账户的 TDLib 会话完全托管在真实 Java 后端（`telegram-files`，生产 VPS）。这决定了本项目的安全评估要分两层：

1. **bridge 自身**：整体安全意识明显高于一般项目 —— 路径段白名单校验（`_safe_id`）、HMAC 签名带过期 cookie、双提交 CSRF、SameSite=Lax、登录限流、`/init` 一次性标志、密码不落盘、日志不打印口令。没有发现可利用的 SQL 注入（无 SQL）、命令注入（无 subprocess/shell）、直接 SSRF（仅访问配置的后端）、模板 XSS 高风险点（Jinja2 默认转义 + `tojson`）。
2. **TG session 层**：这是团队红线。审查结论：**bridge 代码本身没有任何"主动删除 TG session"的执行路径**（`delete_telegram` 是死代码，未接路由），**但存在"改密 → 后端吊销全部会话 → 若 TDLib 数据未落盘则 TG 登录态丢失"的间接风险链**，且部署/升级脚本直接触碰生产环境。此风险必须由 captain 在改动后端前以"备份 + 验证后端行为"的方式消除。

**发现统计：High 3 · Medium 8 · Low 5，共 16 条。** 其中与 TG session 丢失直接相关 2 条（#1 改密链、#2 明文口令/部署），与"明显 bug"直接相关 1 条（#3 登录死锁）。

---

## 2. 按严重度分组的发现

### 2.1 高危（High）

#### H1. 修改管理台密码可能连带销毁 TG 登录会话（session 丢失风险链）
- **位置**：`bridge_server.py:2392-2411`（`auth_password`）；后端契约见同文件 2379-2381 注释
- **问题**：改密成功后，后端"**立即吊销该账号全部会话**并清空 `tf_admin`/`tf_csrf`"。而 TG 的 TDLib 客户端是**绑定在 HTTP session 上的**（见 `telegram_create` 注释 `sessionTelegramVerticles`，691-700 行）。bridge 改密后清空 cookie jar 并重登（2394-2399 行），拿到的是**新 session**；若旧 session 被吊销时后端把绑定其上的 TelegramVerticle/TDLib 认证状态一起销毁，则已登录的 TG 账户会掉线甚至需要重新扫码/验证码。
- **为什么是问题**：团队红线明确"tg 账户 sessions 不可丢失"。此路径是**管理员本人操作**即可触发的连带丢失，且 bridge 无法感知（它只关心管理台会话重登是否成功 `relog`）。
- **修复建议**：
  1. 改密前（及任何后端重启/升级前）先备份后端 TDLib 数据目录（VPS 上如 `/root/tg-files/` 下 session/数据库文件），用 `tar` 落盘到独立位置；
  2. 向后端维护者确认：吊销管理台 session 是否会销毁 TDLib 认证状态？若会，改密流程应先备份或后端改为"只吊销管理台认证、保留 TDLib 客户端"；
  3. `auth_password` 成功后应显式检查 `relog` 结果，若重登失败要告警，不能静默返回 `ok:true`（当前 2411 行返回 `{"ok": True, "relogin": relog}`，前端只看 `ok`）。

#### H2. 生产 VPS root 明文密码入库（.vps-conn/）
- **位置**：`.vps-conn/vps.json:5`（`"password": "your-password"`），被 `vexec.js`/`vput.js`/`vpush.js`/`pushall.js` 全部直接读取
- **问题**：生产服务器 root 密码以明文存在于工作区文件，且这些脚本用**密码认证** SSH 连接。虽然 `.gitignore` 已排除该文件，但只要有人能读该目录（含未来误提交、备份、容器镜像、日志），即可拿到 VPS root。
- **为什么是问题**：一旦泄露 = 服务器完全沦陷，TG 数据/session 全部可被窃取或删除；明文口令也没有审计与吊销机制。
- **修复建议**：
  1. 立即改为 **SSH 密钥认证**（`privateKey` 用 ssh2 支持），`vps.json` 中只存 host/user/key 路径，口令字段删除；
  2. 若必须留密码，改从环境变量/密钥管理器注入，禁止落盘；并**轮换当前密码**（已暴露在代码审查记录中）；
  3. 给 `vps.json` 设置仅本用户可读权限（0600），确认 `.gitignore` 规则长期有效（建议加 `!.vps-conn/` 反向校验或在 CI 里做"禁止 secret 文件入库"检查）。

#### H3. `auth_login` 与 `_relogin` 共用非重入锁 → 死锁挂起（明显 bug）
- **位置**：`bridge_server.py:357`（`_login_lock = asyncio.Lock()`）、`482-485`（`auth_login` 持锁）、`373-389`（`_relogin` 也要锁）、`411-418`（`_request` 401 时调 `_relogin`）
- **问题**：`auth_login` 先 `async with self._login_lock` 再调 `_request("POST","/auth/login")`；若该请求返回 401 且 `retry_on_401=True`（默认），`_request` 会调用 `_relogin()`，而 `_relogin` 又 `async with self._login_lock` —— **`asyncio.Lock` 非重入**，同一协程第二次获取会永久等待 → 该登录请求**永远挂起**，占用连接/任务不释放。
- **为什么是问题**：触发条件并不罕见 —— 进程内已登录过（`_username` 非空）后，后端会话过期或密码输错一次，登录端点即卡死；多并发下还可能让事件循环堆积僵尸协程。属于确定性并发 bug。
- **修复建议**：
  1. `auth_login`/`auth_bootstrap` 调用 `_request` 时传 `retry_on_401=False`（登录本身的 401 就该原样上抛给调用方，不需要重登）；
  2. 或将 `_relogin` 改为不获取同一把锁（例如单独一把 `_relogin_lock`），或把锁拆成"登录写锁/请求锁"；
  3. 至少给 `_request` 的整体调用加超时（httpx 已设 10s 超时，但锁等待不在其中）。

### 2.2 中危（Medium）

#### M1. 登录限流可被 X-Forwarded-For 伪造绕过 + 失败字典无上限
- **位置**：`bridge_server.py:317-321`（`_client_ip` 直接取 `x-forwarded-for` 首个值）、`314-336`（`_login_failures`）
- **问题**：`_client_ip` 无条件信任客户端可伪造的 `X-Forwarded-For` 头；攻击者每次带不同伪造 IP 即可绕过 `LOGIN_RATE_LIMIT` 暴力破解登录/初始化。同时 `_login_failures` 字典键永不删除，伪造海量 IP 可造成进程内存缓慢增长。
- **为什么是问题**：限流是 `/login` 与 `/init` 的唯一抗爆破手段；被绕过 = 可无限尝试管理员口令。内存增长属次要 DoS。
- **修复建议**：限流键改用"反向代理实际连接地址"（配置 TrustedHost/可信代理后读 `request.client.host`，或让 nginx 覆盖且仅信任已知代理段的 XFF）；给字典加定期清理（如按窗口过期删除键）。

#### M2. `_bootstrap_status_cache` 永不刷新 → `/init` 门控退化
- **位置**：`bridge_server.py:504-514`（`refresh_bootstrap_status` 只在 startup 调一次，TTL 60s 过期后恒返回 None）、`1647-1659`（`_init_gate_open`）
- **问题**：60 秒后 `_bootstrap_status_cache()` 永远返回 `None`，`_init_gate_open` 从此只看本地 `.bridge_initialized` 标志。若后端数据库被重置（管理员表被清空，需重新 bootstrap），bridge 不会再打开 `/init` 门，或反之在本地标志丢失时错误开放。
- **为什么是问题**：初始化门控是安全边界；依赖一个永不刷新的缓存 = 行为随时间漂移，管理员无法感知后端需要重新初始化。
- **修复建议**：把 `_bootstrap_status_cache` 做成带过期自动重查（在 `_init_gate_open` 里异步刷新），或在每次访问 `/init` 时实时向后端查询（该端点本身是公开路由）。

#### M3. `telegram_api(method)` 路径拼接无白名单
- **位置**：`bridge_server.py:702-705`
- **问题**：`method` 直接拼进 URL path `f"/telegram/api/{method}"`，未做任何校验。当前所有调用方都是硬编码 TDLib 方法名（`SetAuthenticationPhoneNumber` 等），因此实际不可被外部控制；但一旦未来把方法名参数化（例如前端透传），`../`、`/` 等即可注入后端路径。
- **为什么是问题**：防御纵深缺失；且该方法理论上可调用 `logOut`/`deleteAccount` 等**会销毁 TG 会话**的 TDLib 方法，属于"离 session 丢失红线最近的可扩展点"。
- **修复建议**：加白名单（`{"SetAuthenticationPhoneNumber","CheckAuthenticationCode","CheckAuthenticationPassword", ...}`），只放行已知安全方法。

#### M4. `get_file_preview_url` 未对 telegram_id 做 `_safe_id` 校验（死代码 + 功能性缺陷）
- **位置**：`bridge_server.py:678-680`
- **问题**：项目所有其它 `telegram_id` 入 path 前都过 `_safe_id` 白名单（448-458），唯独此方法把 `telegram_id` 直接拼进 URL，仅对 `unique_id` 做了 `quote`。当前该方法**无任何调用方**（死代码），但一旦被前端 `<img src>`/视频预览引用：① telegram_id 含 `..` 可构成路径注入；② 拼接出的 URL 指向后端 `/api/{tg}/file/{uid}`，而浏览器没有后端 `tf_admin` cookie，预览请求必然 401 失败。
- **为什么是问题**：死代码掩盖了路径注入缺陷，且功能上不可用（预览图加载失败）。
- **修复建议**：要么删除该方法，要么补 `_safe_id` 校验并把预览改为经 bridge 代理（携带后端会话）再返回给浏览器。

#### M5. 浏览器 SSE 订阅无数量上限（内存增长）
- **位置**：`bridge_server.py:1589-1633`（`sse_logs`/`sse_tasks`）、`1312-1340`（Broadcaster）
- **问题**：每个 SSE 连接在 `HUB` 里注册一个 `asyncio.Queue`（maxsize=200），无订阅总数限制；已登录用户开多个标签页/连接即持续占用队列与内存。
- **为什么是问题**：单管理员场景风险有限，但 bridge 是共享部署（多访客），大量并发 SSE 可放大内存占用。
- **修复建议**：对订阅数/单 IP 连接数设上限，超限拒绝新 SSE（返回 429 或直接关闭）。

#### M6. 响应缺少安全头（CSP / X-Frame-Options / X-Content-Type-Options / HSTS）
- **位置**：`bridge_server.py:191-192`（app 定义），各路由未统一设置安全头
- **问题**：未设置 `Content-Security-Policy`、`X-Frame-Options`、`X-Content-Type-Options`、`Referrer-Policy` 等。模板内有大量内联脚本（Alpine/onclick），一旦某处模板渲染引入反射内容，无 CSP 兜底。
- **为什么是问题**：缺少纵深防御；XSS 的唯一防线是 Jinja2 转义与 `tojson`。
- **修复建议**：加中间件统一注入安全头（至少 `X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy`）；CSP 若因内联脚本难落地，可先加 `script-src 'self' 'unsafe-inline'` 逐步收紧。

#### M7. 错误信息向客户端/日志回显后端内部细节
- **位置**：`bridge_server.py:2148-2163`（`_tg_err`）、`1701-1712`（`init_submit` 分类提示）、`2018-2036`（`post_task` 等将 `_tg_err` 直接返回）
- **问题**：`_tg_err` 会把后端错误体（`err.message`/`err.code`）原样返回给前端并写入日志；`init_submit` 还区分 "NETWORK"/"LOCAL" 等部署拓扑。
- **为什么是问题**：向低信任客户端泄露后端内部错误结构、代码路径与部署细节，辅助进一步攻击。
- **修复建议**：对外统一为固定文案，完整错误仅进服务端日志（且日志中过滤 URL/令牌）。

#### M8. CSRF token 全站静态、不随会话轮换
- **位置**：`bridge_server.py:124-126`（`_csrf_token()` 由固定 secret 派生，所有用户共享同一值）
- **问题**：双提交机制本身正确（cookie 与 header 比对、SameSite=Lax、写端点强制校验、登录/初始化豁免合理），但 CSRF token 值对所有会话相同且登录不轮换（仅改密时轮换 secret）。
- **为什么是问题**：若任意会话通过 XSS 或日志泄露一次 token，该 token 长期有效并适用于所有会话；降低 CSRF 防护的会话隔离性。
- **修复建议**：token 绑定用户/会话（如 HMAC 中加入 username 或登录随机数），登录成功后重新签发。

### 2.3 低危（Low）

#### L1. `_build_tasks` 分页终止判断用首页长度
- **位置**：`bridge_server.py:917-931`
- **问题**：`len(all_files) < 500` 判断的是**第一页**长度而非当前页；`nxt == cursor` 能防死循环，但若首页恰为 500、后续页不足 500 且后端仍给 nextFromMessageId，会多拉页；反之若首页不足 500 直接跳过所有后续页。
- **影响**：任务聚合可能遗漏/多余分页，属功能瑕疵，不涉安全。
- **修复建议**：以当前页 `page` 长度与 `nxt` 双重判断。

#### L2. 管理员密码明文存于进程内存
- **位置**：`bridge_server.py:359-361`、`369-371`（`set_credentials`）、`1770`/`1695`（登录/初始化后保存）
- **问题**：为支持 401 自愈重登，管理台口令以明文保存在 `BackendClient` 内存属性。
- **影响**：不落盘，风险有限；进程内存 dump/崩溃转储可读。
- **修复建议**：可接受；如强化，改为后端签发长期 refresh token 或加密存储。

#### L3. `_portal_secret` 降级路径每次生成不同内存密钥
- **位置**：`bridge_server.py:80-96`
- **问题**：当 secret 文件读/写失败时，每次调用 `_portal_secret()` 都返回**不同的**临时密钥（且该函数只调用一次，但多 worker/重启场景下各进程密钥不一致，导致 cookie 互相失效）。注释已自述风险，属"显式暴露"的设计。
- **影响**：仅可用性（需要重新登录），无安全后果。
- **修复建议**：保持现状亦可；若上多 worker，改为共享密钥源。

#### L4. `delete_telegram` 为死代码，无 UI 入口（好事，但需防止未来接线）
- **位置**：`bridge_server.py:633-637`；全仓库 grep 无调用方
- **问题**：定义了一个"删除（CLOSED 僵尸）客户端"的后端调用但从未使用。若未来在账号管理页接线"删除账号"，将直接威胁 TG session 存活。
- **影响**：当前无风险；未来接线的隐患。
- **修复建议**：在接线前必须加二次确认 + 强制先备份 session；建议在代码注释中标注红线。

#### L5. `preview_server.py` 为纯 mock、无鉴权（预期行为）
- **位置**：`preview_server.py` 全文（路由无鉴权、SSE 持续伪造数据）
- **问题**：预览服务器不含真实数据/凭据，默认绑定 127.0.0.1，风险低；但若误以 `--host 0.0.0.0` 部署到公网且被误认为生产入口，存在误导。
- **影响**：低。
- **修复建议**：README/注释已说明用途，保持默认回环绑定即可。

---

## 3. TG 账户 session 专题（团队红线）

### 3.1 session 存哪里、谁在管
- **TG 会话数据**：由 Java 后端（telegram-files, TDLib）管理，位于生产 VPS 后端数据目录（`APP_ROOT_DIR` 默认 `/root/tg-files/app-data` 为 bridge 自身数据目录；TDLib 数据库/会话一般在后端容器/数据卷内）。**bridge 不存储、不序列化、不刷新 TG 会话**，只透传 TDLib 方法（`/telegram/api/*`）。
- **bridge 自身落盘数据**：仅 `.bridge_secret`（门禁密钥，0600）与 `.bridge_initialized` 标志（`bridge_server.py:73-75`），与 TG 会话无关。

### 3.2 可能的丢失路径（按风险排序）
1. **改管理台密码**（H1）→ 后端吊销全部 HTTP session → 若 TDLib 认证状态绑定在 session 内且未落盘，TG 登录态丢失。**最高优先验证项**。
2. **后端重启/升级/数据卷清理**（部署脚本 `pushall.js` 直接推送并 `rm -f` 远端文件）→ 若覆盖或清到 TDLib 数据目录，会话丢失。
3. **`telegram_api` 未来透传 `logOut`/`deleteAccount`**（M3）→ 直接登出/删除账号。
4. **`delete_telegram` 被接线**（L4）→ 删除客户端（当前死代码）。

### 3.3 当前代码层保障（正面）
- 浏览器登出（`bridge_server.py:2441-2449`）**不调用**后端 `/auth/logout`，避免杀掉共享后端会话/WS 中继 —— 正确。
- `_relogin` 自愈只在管理台会话层，不触碰 TG 客户端。
- 无任何 `shutil.rmtree`/文件删除逻辑指向后端数据目录。

---

## 4. 认证/授权/注入/并发/错误处理逐项结论

| 领域 | 结论 |
|---|---|
| 认证（门禁） | 良好：HMAC-SHA256 签名 + exp 覆盖签名 + nonce，密钥 0600 落盘，TTL 与 cookie 同步；改密轮换 secret 使旧 token 全部失效 |
| 授权 | 内页全部门禁；未登录数据接口 401、页面 302；`/sse`、`/partials`、写端点均在门禁内 |
| CSRF | 双提交正确；写端点强制校验；登录/初始化豁免合理（凭据首次建立）；小缺陷见 M8 |
| SQL 注入 | 无 SQL（无数据库），N/A |
| 路径遍历 | 主路径 `_safe_id` 白名单完善；缺陷仅 `get_file_preview_url`（M4，死代码） |
| 任意文件读写 | 无用户输入参与文件路径（仅 secret/flag 文件，路径来自 env 常量） |
| SSRF | 无直接 SSRF：只请求配置的后端；`resolve_link` 的 `link` 虽到后端，但 bridge 侧 `_parse_link` 用正则白名单限制为 `t.me/...`（1980-1995） |
| 命令注入 | 无 subprocess/shell（bridge）；运维脚本 `vpush.js` 用 `sh -c` 拼命令但 base64 块与单引号转义处理正确 |
| 并发/竞态 | 主要问题 = 登录死锁（H3）；其余缓存均为进程内、单 worker 下无明显竞态 |
| 错误处理 | 覆盖面好（大量 `except → log.warning`），但错误回显略多（M7） |
| 敏感信息 | 主要问题 = VPS 明文口令（H2）；bridge 内无硬编码密钥 |
| 日志泄露 | 登录/初始化不打印口令；WS 日志只含 telegramId；`_tg_err` 会向日志回写后端错误体（M7） |

---

## 5. 总体结论

**质量评估**：后端安全基线**良好**。代码对路径注入、CSRF、签名 cookie、门禁、限流、日志卫生都有系统性的防御意识，明显不是"裸奔"项目；审查**未发现**可直接利用的远程注入/越权主路径。主要问题集中在**运维与红线保护**（明文 root 口令、改密连带 TG 会话风险）与**一处理论上的确定性并发 bug**（登录死锁）。

**必须优先处理**（按团队红线排序）：
1. 改动任何 VPS 后端/重启/升级前，**先备份 TG 会话数据目录**（tar 到独立位置），并确认后端"吊销管理台会话"不会销毁 TDLib 认证数据；
2. 轮换并移除 `vps.json` 明文 root 口令（改 SSH 密钥）；
3. 修复 `auth_login`/`_relogin` 死锁（`retry_on_401=False`）。

**建议顺序**：H3（代码 bug，当日可修）→ H2（运维口令）→ H1（需与后端维护者确认 + 备份）→ M 类加固 → L 类按需。

---

*报告文件：`docs/reviews/backend-review.md`。*
