# 前端代码审查报告 — TG 归档台

- 审查范围：`templates/`（含 `partials/`）全部 Jinja2 模板、`static/css/*.css`、`static/js/*.js`（含 vendor）、`preview_server.py` 前端渲染部分；并交叉核对 `bridge_server.py` 的前端契约（数据来源、CSRF 门禁、鉴权中间件、安全头）以准确判断前端风险。
- 审查时间：2026-09
- 审查人：frontend-reviewer（子代理）

## 1. 概述

项目前端为 **服务端渲染（Jinja2）+ htmx 局部刷新 + Alpine.js 交互 + 自托管图表库** 的经典方案，无 SPA、无构建链。整体安全基线**较高**：

- 所有模板变量走 Jinja2 默认 autoescape（FastAPI/Starlette `Jinja2Templates` 默认开启），未发现 `|safe` / `Markup` / `innerHTML` / `x-html` / `v-html` 等绕过点；
- 前端 JS 刻意用 `textContent` 注入 toast（`app.js`），日志/向导全部用 Alpine `x-text`，注释中明确"不能 innerHTML"；
- 所有写操作（提交下载、重试/取消、设置保存、改密、TG 登录向导、登出）统一带 `X-CSRF-Token` 头，与后端双提交校验（`tf_portal_csrf` cookie + header，`hmac.compare_digest`）配套；
- vendor（htmx 1.9.10 / Alpine 3.14.1 / Chart.js 4.4.1 / ApexCharts 3.45.2）全部**本地自托管**，无 CDN 供应链面（`base.html` 注释亦明示此意图）；
- 内页全部在后端 `portal_auth_gate` 门禁后（页面 302 → /login，数据/SSE 接口 401 JSON），`/login` `/init` 有限流。

**结论：未发现当前可直接利用的存储型/反射型 XSS 或鉴权绕过。** 主要问题集中在**潜在注入点（脚本上下文未用 tojson）、缺失安全响应头（点击劫持/无 CSP）、preview_server 无鉴权、若干逻辑/健壮性/可访问性缺陷**。建议优先修复 H1/M1/M2/M4，其余按优先级跟进。

## 2. 发现清单（按严重度）

### Blocker（0）

无。

---

### High（1）

#### H1. 内联 `<script>` 中任务 id 直接插值，未用 `tojson`（潜在存储型 XSS 注入点）

- **位置**：`templates/partials/_tasks_table.html:106`、`templates/task_detail.html:96`、`templates/partials/_tasks_table.html:39/61/64`、`templates/task_detail.html:81/82`
- **问题描述**：
  ```html
  <script>
    window.__TASK_META[{{ t.id }}] = { uniqueId: {{ t._unique_id | tojson }}, telegramId: {{ t._telegram_id | tojson }} };
  </script>
  ```
  在 `<script>` **脚本上下文**中，`{{ t.id }}` 直接裸插值（`task_detail.html:96` 同；`_tasks_table.html:39` `onclick="window.__toggleTask('task-{{ t.id }}')"`、`:61/64` `onclick="window.__retryTask({{ t.id }}, this)"`、`task_detail.html:81/82` 同样直接拼接）。Jinja 的 HTML autoescape **不保护脚本/属性内 JS 上下文**——`'`、`"`、`;`、`]`、`)` 等字符在脚本中仍是语法字符。一旦 `id` 变成字符串（含引号/分号），即可逃逸出对象字面量或事件处理器执行任意 JS。
- **为什么是问题**：
  1. 当前**恰好安全**：`bridge_server.py:_stable_task_id()` 强制把 id 压成 48 位 int，preview_server 也用 int 自增 id；`_unique_id/_telegram_id` 走了 `tojson`。但这是"靠巧合"而非"靠防御"。
  2. htmx `allowScriptTags=true`（默认）会在局部 swap 时**执行返回片段里的内联 script**——`_tasks_table.html` 尾部的这段脚本正是通过 `/partials/tasks` 与 `/tasks` POST 返回并由 htmx 执行的。一旦未来任一后端字段（如把 `_unique_id` 直接当 id 返回）或接口把非数字 id 带入，**一行改动即可演化为带管理员会话的存储型 XSS**。
  3. 模板中其他脚本数据（dashboard/account/logs 的图表数组、seed logs）都正确使用了 `|tojson`，唯独任务 id 系列漏了，属于不一致的易错模式。
- **修复建议**：脚本上下文统一改用 `{{ t.id | tojson }}`（或 `{{ t.id }}` 前先 `{% set id_int = t.id|int %}` 强制数字）；事件属性内不要拼 JS，改为 `data-*` 属性 + 事件委托（app.js 用 `dataset` 读取），彻底消除属性内 JS 拼接。示例：
  ```html
  <tr data-task-id="{{ t.id }}" data-unique-id="{{ t._unique_id|tojson }}" ...>
  <script>window.__TASK_META[{{ t.id | tojson }}] = {...};</script>
  ```

---

### Medium（6）

#### M1. 两个服务器均缺失安全响应头（CSP / X-Frame-Options / X-Content-Type-Options / Referrer-Policy）

- **位置**：`bridge_server.py`（全局，无任何安全头）、`preview_server.py`（全局）
- **问题描述**：`bridge_server.py` 只在静态文件/SSE 上加了 `Cache-Control`/`X-Accel-Buffering`，没有 `Content-Security-Policy`、`X-Frame-Options`（或 `frame-ancestors`）、`X-Content-Type-Options: nosniff`、`Referrer-Policy`。preview_server 同样全缺。
- **为什么是问题**：管理台页面可被第三方 iframe 嵌入 → **点击劫持**；即便 CSRF 双提交能挡住大部分状态变更 POST，GET 触发的动作/信息泄露/UI 重定向仍可被利用；无 `nosniff` 使 MIME 嗅探风险存在；无 CSP 意味着 H1 一旦被触发没有任何纵深缓解。
- **修复建议**：在 FastAPI middleware（或 nginx 反代层）为所有响应加：
  - `X-Frame-Options: DENY`（或 CSP `frame-ancestors 'none'`）
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: same-origin`（或 `strict-origin-when-cross-origin`）
  - 渐进式 `Content-Security-Policy`：因模板大量内联脚本（主题引导、`{% block js %}`、onclick），直接全站 strict CSP 会炸 UI，建议先 `default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'`，后续再收敛 `script-src` 到 nonce/hash。注意：preview 是纯前端预览，CSP 加在 bridge 即可。

#### M2. 内联 `onclick` 属性内裸插值 `{{ t.id }}`（属性内 JS 上下文注入面）

- **位置**：`templates/partials/_tasks_table.html:39,61,64`、`templates/task_detail.html:81,82`、`templates/base.html:110/117/122/129/143/159/178`（onclick 调 `window.__*`）
- **问题描述**：大量 `onclick="window.__retryTask({{ t.id }}, this)"` 把模板变量拼进内联事件处理器。属性上下文里 Jinja autoescape 会转义引号，但值本身仍作为 JS 源码执行；id 为数字时安全，但这是 H1 同源的不安全模式，且内联 handler 与未来 CSP 策略冲突。
- **为什么是问题**：同上，靠"id 恰好是 int"维持安全；属性内 JS 拼接是 XSS 经典重灾区；同时内联事件处理器是 a11y（键盘可达性）与 CSP 的阻碍。
- **修复建议**：改用 `data-task-id` + `addEventListener` 事件委托（`app.js` 中 `__retryTask/__cancelTask` 改为读 `e.currentTarget.dataset`）；或至少 `onclick="window.__retryTask({{ t.id|int }}, this)"` 并给按钮加 `type="button"`。

#### M3. `tg_login.html` 步骤条 `:class` 重复绑定，"done" 状态失效

- **位置**：`templates/tg_login.html:20`
- **问题描述**：同一元素写了两条 `:class` 指令：
  ```html
  <div class="step" :class="step>=1?'active':''" :class="step>1?'done':''">
  ```
  HTML 规范下**重复属性只保留第一个**（`:class="step>=1?'active':''"`），第二条 `:class="step>1?'done':''"` 被浏览器丢弃 → 步骤 2/3 永远不会显示"已完成 ✓"状态，步骤条 UI 逻辑失效。
- **为什么是问题**：向导步骤指示与真实进度不一致，用户无法判断步骤已完成；属功能性 bug。
- **修复建议**：合并为单条绑定：` :class="{'active': step>=1, 'done': step>1}"`（第 24 行同理核对）。

#### M4. preview_server.py 无鉴权/无 CSRF（含写接口与 SSE），若被暴露即为开放管理台

- **位置**：`preview_server.py:245-317`（全部路由）、`:390 post_task`、`:323 partial_tasks`、`:347/372 SSE`
- **问题描述**：preview_server 是"纯前端预览"服务器，**没有任何 portal 门禁与 CSRF**：`POST /tasks` 直接接受并返回任务表；`/sse/*` 无鉴权流式输出模拟数据；`/partials/tasks` 无鉴权。启动绑定 `127.0.0.1:8000`，README 也声明仅预览。
- **为什么是问题**：本身是设计如此（mock 数据无真实业务），但**一旦有人把它绑到公网/反代**，就得到一个无需登录、可提交任务、可读 SSE 的"半开放控制台"；且它渲染的模板与生产共用，容易让人误以为是生产可依赖的地址。属于部署/运维面风险。
- **修复建议**：① 在 README/启动日志显著标注"仅本机预览、禁止公网暴露"；② 给 preview_server 也加一层最简单的共享口令或干脆仅 `127.0.0.1`（已满足）；③ 若需公网演示，至少给 `/tasks` POST 加 CSRF 校验（可复用 `_csrf_token`/cookie 逻辑）。

#### M5. `_stable_task_id` 48 位哈希截断 → 任务 id 可能碰撞，`__TASK_META` 以 id 为键会互相覆盖

- **位置**：`bridge_server.py:725-729`（`_stable_task_id`，SHA1→48 位 int）↔ `templates/partials/_tasks_table.html:106`、`templates/task_detail.html:96`、`static/js/app.js:148/170/208`（以 `t.id` 为键读写 `__TASK_META`）
- **问题描述**：前端用 `id` 作为 `window.__TASK_META` 的唯一键，但 `id` 是 `sha1(unique_id)` 截断为 **48 位**（约 2.8e14 空间）。任务量级小概率碰撞；一旦两个不同任务的 id 碰撞，`__TASK_META[id]` 会被后者覆盖，批量/单条重试取消会**对错误任务发送 uniqueId**（后端 `_find_task_by_uid` 按 `_unique_id` 解析，导致操作对象错误）。
- **为什么是问题**：重试/取消是副作用操作（重新下载/取消下载），键碰撞 = 操作错对象，属数据一致性/功能 bug；前端无任何重复键检测。
- **修复建议**：前端脚本内同时校验唯一性（渲染时若发现重复 id 打日志）；更稳妥的是 `__TASK_META` 改用 `_unique_id` 字符串为键、表格行 `data-unique-id` 关联，彻底解耦显示 id 与身份 id。

#### M6. 管理台展示敏感信息面较宽（TG 手机号 / 本地云端路径 / 哈希），且 login 表单无客户端限速

- **位置**：`templates/account.html:60`（`{{ a.phone }}` 完整手机号）、`templates/task_detail.html:38-49`、`templates/partials/_tasks_table.html:74-76`（本地/云端路径、md5/qx 全量）、`templates/login.html`（无前端限速/无 CSRF 字段）
- **问题描述**：账号健康页展示**完整 TG 手机号**；任务详情/表格向任意已登录管理员展示本地磁盘路径、云端路径、完整哈希（路径含 `/data/...`、频道前缀，可辅助针对性攻击）；登录表单直接 form POST，无 CSRF 字段（登录/初始化是凭据首次建立，后端已做限流 `_login_blocked` 与 CSRF 豁免，此处仅提示纵深）。
- **为什么是问题**：手机号属 PII，路径/哈希属运维敏感信息；虽在鉴权后，但"任何管理员可见"面偏大，若 H1 类漏洞或账号被盗，泄露面即被放大。
- **修复建议**：手机号默认掩码（`138****0000`，可加"显示"开关）；路径/哈希在详情页保留但对非必要页面（任务表格展开行）考虑省略；登录页无需额外处理（后端已限流），但可在前端提示"多次失败将临时锁定"。

---

### Low（10）

#### L1. `dashboard.html:23` 硬索引 `stats.kpis[1].value`
假设 `kpis` 恒有 ≥2 项；preview/bridge 目前都返回 4 项，但若后端 `_dashboard_stats` 收缩列表即抛 `IndexError` → 500。建议 `stats.kpis[1] if stats.kpis|length>1 else ''`。

#### L2. OTP 输入组件健壮性
`tg_login.html:211-243`：`otpInput` 中 `v.length>1` 分支调用 `otpPaste(e)`，但 `@input` 事件没有 `clipboardData`（仅 `@paste` 有），粘贴路径实际由 `@paste` 处理，输入框内多字符（如中文输入法上屏）会静默丢弃；`otpPaste` 里 `((e.clipboardData || window.clipboardData) || {}).getData` 取值后若 `clipboardData` 为 undefined 会 `e.clipboardData.getData` 抛错（未 try）。建议统一用 `@paste` 处理 + `otpInput` 内做数字清洗与 try/catch。

#### L3. `logs.html:111-119` `visibleLines` getter 内副作用
getter 里 `l.ok = ok` 直接改行对象属性（每次渲染都写），虽能跑但违背"getter 应纯"；`l.msg.toLowerCase()` 假设 msg 恒为字符串（SSE 种子含，但后端若发非字符串会崩）。建议用 map 生成副本或把过滤逻辑移到渲染表达式。

#### L4. 可访问性（a11y）
- 可点击 `<tr class="clickable-row" onclick>`（`_tasks_table.html:39`）无 `tabindex/role`，键盘不可达；
- 图标按钮大多有 `aria-label`（做得不错），但 `library_local.html:61/83` 的禁用按钮用 `title` 而非 `aria-describedby`；
- 模态（`_submit_modal.html`）无焦点陷阱/初始焦点/焦点还原；图表 `<canvas>` 无 `role="img"` + 可读替代文本；
- `main.css` 有 `:focus-visible` 与 `x-cloak`（好评）。建议逐步补齐。

#### L5. vendor 脚本无 SRI / 版本固定但无完整性校验
已自托管（低供应链风险），但 `base.html:197-200` 的 `<script src>` 未加 `integrity`；本地文件被篡改（如 VPS 被写）浏览器不会告警。建议生成 SRI 注入（或在构建时校验 vendor 哈希）。

#### L6. preview_server 部分 mock 逻辑粗糙
`preview_server.py:323-331 partial_tasks` 只按 status/source 过滤，忽略 `date_from/date_to`（与 bridge 行为不一致，易误导前端联调）；`:267-271 task_detail` 找不到任务时静默回退 `TASKS[0]`，不返回 404（bridge 返回"任务不存在"占位，语义不同）。建议 mock 行为与 bridge 对齐。

#### L7. `settings.html:174-178` 改密与保存设置链式耦合
`save()` 的 `.then` 链中，即使设置保存请求失败（`.catch` 已 toast 错误），只要 `pwd.new` 非空仍会继续执行 `__changePassword()`，可能导致"设置失败但密码被改了"的困惑状态。建议仅在设置保存成功（`d.ok===true`）后才触发改密。

#### L8. CSRF token 为进程级静态值 + logout 豁免
`bridge_server.py:124-126` `_csrf_token()` 由固定 secret 派生，**所有会话共用同一 token**（未随会话轮换），`SameSite=Lax` 缓解了大部分跨站场景，但会话间 token 复用减弱了双提交的会话绑定强度；`bridge_server.py:1742` 把 `/auth/logout` 排除在 CSRF 校验外（登出 CSRF 影响低，但既然有 token 顺手校验更稳）。建议 token 绑定 portal 会话随机值。

#### L9. `tg_login.html:116` `x-for="i in 5"` 依赖 Alpine 数字迭代能力
Alpine 3.14.1 支持 `x-for` 数字范围，但该写法版本敏感（老 Alpine 需 `x-for="i in [1,2,3,4,5]"`），建议改成显式数组或 `Array.from({length:5})`，避免升级 vendor 时静默失效。

#### L10. 登录页无前端 CSRF 字段 / 错误回显面
`login.html:52-85` 表单无 CSRF token（凭据首建豁免，可接受）；`preview_server.py:246` 与 `bridge_server.py:1640` 把 query 参数 `error` 直接渲染进 `{{ error }}`（已被 autoescape，**无 XSS**，仅提示保持 autoescape 不关闭即可）。

---

## 3. 总体结论

前端工程在**防御正确性**上做得相当扎实：全面 autoescape、`tojson` 覆盖所有脚本数据、`textContent`/`x-text` 注入、写操作统一 CSRF、vendor 本地化、鉴权门禁 + 限流齐备，**当前版本未发现可直接利用的高危漏洞**。

需要优先处理的真实风险是"**靠巧合防御**"的两处：

1. **H1/M2 — 任务 id 在脚本/事件上下文裸插值**：现在因 id 被强制 int 而安全，但这是存储型 XSS 的潜伏注入点，且 htmx 会执行返回片段内联脚本。修复成本极低（`|tojson` / `data-*` 委托），强烈建议先做。
2. **M1 — 缺失安全响应头**：管理台可被 iframe（点击劫持），无 nosniff/CSP 纵深，建议在 bridge middleware 一次性补齐。

其次是 M4（preview 服务器不可外露）、M5（id 碰撞导致误操作）、M3（步骤条 UI bug）等中等级问题，以及若干 low 级健壮性/可访问性优化。整体评估：**当前可上线（内网/VPS 反代 + HTTPS），但在上线前至少完成 H1 + M1 + M4 的加固。**

### 统计

| 严重度 | 数量 | 列表 |
| --- | --- | --- |
| Blocker | 0 | — |
| High | 1 | H1 |
| Medium | 6 | M1–M6 |
| Low | 10 | L1–L10 |
| **合计** | **17** | — |
