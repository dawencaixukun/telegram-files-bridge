# 计划表：本地在存「一键归档到 OpenList 云端」

> 目标交互：本地在存页点击「立即归档」→ 弹窗确认（默认=上传到 OpenList 云端归档）→ 后台流式上传 → 卡片/表格状态实时流转 → 云端归档页可见。
> 前置已具备：OpenList 账号密码已配置并验证（`.openlist_auth`，token 失效自动重登）；OpenList 服务同机 5244 端口；`openlist-review/API.md` 完整 API 契约。

## 现状缺口

| # | 缺口 | 现状证据 |
|---|------|----------|
| 1 | 「立即归档」按钮 disabled | `templates/library_local.html:61,83` title="v2 功能（需配置转存目标）" |
| 2 | 本地文件条目缺关键字段 | `_to_local_file_from_task()` 只回传 filename/size/source/time/status，没有 uniqueId/localPath/下载状态，弹窗无从定位文件 |
| 3 | 无上传通道 | bridge 只有 OpenList 登录态管理，没有调 `PUT /api/fs/put` 的上传代码 |
| 4 | 无归档任务跟踪 | 上传是长耗时操作，需要 job 日志（重启不丢）+ 进度查询接口 |
| 5 | 云端归档页数据源不准 | `/library/cloud` 取的是 Java 后端 `transferStatus=completed`，bridge 自己传的文件后端不知道，永远显示不出来 |

## 计划表

| 阶段 | 任务 | 主要改动 | 产出/验收 | 预估 |
|------|------|----------|-----------|------|
| **P0 决策确认** | 确认 4 个设计决策点（见文末） | — | 决策记录写回本文档 | 0.5h |
| **P1 数据层**<br>本地实体可归档化 | ① `_to_local_file_from_task()` 增补 `id / uniqueId / localPath / downloadStatus / 可归档标记`；② 新增归档日志模块 `_archive_jobs`（`.archive_jobs.json`，0600，模式对齐 `.openlist_auth`）：load/save/按 uniqueId 关联归档状态（未归档/排队/上传中/已归档/失败） | `bridge_server.py` | 本地在存页每个文件带可归档标记；重启后归档状态不丢 | 0.5 天 |
| **P2 上传服务** | OpenList 上传 worker：`POST /api/fs/mkdir` 建目标目录 → `PUT /api/fs/put` 流式上传（1MB 分块读盘，不经内存全量，Content-Length + URL-encoded `File-Path` 头，兼容含空格/中文/特殊字符的路径）；复用 `_openlist_status()` 取 token，401 自动重登重试一次；解析 `{code,message}` 包络；`asyncio.Semaphore(2)` 并发上限；失败记录 error 且可重试 | `bridge_server.py` | 大视频文件流式上传成功；token 过期自愈；并发受控 | 1 天 |
| **P3 bridge API** | ① `POST /archive/start`（单文件/批量 `{items:[{uniqueId, remoteDir}]}`，CSRF 校验）→ 返回 job ids；② `GET /archive/status` 进度查询；③ `POST /archive/cancel` 尽力取消；④ `/archive/` 加入门禁 401-JSON 白名单（`bridge_server.py:2161`）；⑤ remoteDir 路径安全校验（禁 `..`、绝对路径归一化） | `bridge_server.py` | curl 全链路可跑；未登录访问返回 401 JSON；路径穿越被拒 | 0.5 天 |
| **P4 前端弹窗** | ① 新增 `partials/_archive_modal.html`（模式对齐 `_submit_modal.html`）：文件信息 + **目标目录选择器**（新增 `GET /openlist/dirs` 代理 `POST /api/fs/dirs`，从根 `/` 逐级浏览全部挂载网盘，默认记忆上次所选目标，localStorage）+ 冲突策略（覆盖/跳过已存在，跳过用 `/api/fs/get` 预检）+ OpenList 登录态提示（未登录引导去设置页）；② `app.js` 增加 `__openArchive/__closeArchive/__startArchive`，fetch + CSRF + `__toast`；③ 网格/表格「立即归档」按钮启用：仅下载完成的可点，未完成置灰带原因；④ 提交后 2s 轮询 `/archive/status` 更新按钮状态（归档中 xx% → 已归档/失败重试）；⑤ 卡片加归档状态胶囊 | `templates/partials/_archive_modal.html`、`templates/library_local.html`、`static/js/app.js` | 点击一下弹窗，可跨多个网盘逐级选目标目录；进度/成功/失败反馈完整；Esc/点遮罩可关 | 1 天 |
| **P5 云端页真实化** | `/library/cloud` 改读 OpenList 真实数据：新增 `POST /api/fs/list` 代理，按归档日志里出现过的目标目录聚合列出（多网盘各自的真实路径）；合并归档日志补 归档时间 / 状态；**表格列精简为：归档时间 · 原文件名 · 大小 · 云端路径（OpenList 实际路径）· 状态 · 操作**（按要求删掉 来源链接 / MD5 / quickXorHash 三列）；加「刷新」按钮与空态 | `bridge_server.py`、`templates/library_cloud.html` | 云端路径为实际路径，多网盘各自目录都可见；列精简后无占位空列 | 0.5 天 |
| **P6 联调验收** | 真实文件全链路：本地在存 → 弹窗归档 → 进度 → 云端页可见 → 重启 bridge 恢复 job 状态 → 失败重试 → 未登录/未配置 OpenList 的降级提示 | — | 验收清单逐项过，问题回修 | 0.5 天 |

**合计 ≈ 4–4.5 人日**（P0 后 P1→P2→P3 串行，P4 可与 P2/P3 并行开工，P5 依赖 P2 联调）。

## 交互稿（弹窗）

```
┌─ 归档到云端 ───────────────────── ✕ ─┐
│ 文件    极客飞船频道_第42期.mkv        │
│ 大小    1.8 GB · 来源 极客飞船频道      │
│ 目标    OpenList · /阿里云盘/tg-archive/ ▾ │ ← 目录选择器，逐级浏览
│        （可换 /夸克云盘/… 等任意挂载盘）    │   各挂载网盘，记忆上次选择
│ 冲突    (•) 覆盖  ( ) 跳过已存在        │
│ ──────────────────────────────────  │
│ ✓ OpenList 已登录（令牌已验证）         │   ← 未登录时变红：去设置页登录
│            [ 取消 ]  [ 开始归档 ⏎ ]    │   ← 默认动作，回车即传
└─────────────────────────────────────┘
提交后 → toast「已加入归档队列」→ 按钮变「归档中 45%」→ 完成「已归档✓」
云端页列精简为：归档时间 · 原文件名 · 大小 · 云端路径（实际路径）· 状态 · 操作
（来源链接 / MD5 / quickXorHash 三列按用户要求删除）
```

## 风险与对策

| 风险 | 对策 |
|------|------|
| 大文件占内存 | 流式分块读盘上传，不落内存 |
| bridge 与 Java 后端不同机时读不到 localPath | P0 决策点①：默认同机直读；跨机则改经后端文件端点取流 |
| OpenList 上传限速（UploadRateLimiter） | 如限速可在 OpenList 后台调；bridge 不做绕过 |
| 上传中 bridge 重启 | job 落盘，重启后标记为 failed 可重试（不做断点续传，v2 再议 multipart 分片） |
| 目标重名覆盖丢数据 | 冲突策略默认让用户选，跳过模式先 `/api/fs/get` 预检 |

## P0 待确认决策点

1. **上传通道**：✅ 已确认 — A. bridge 流式 `PUT /api/fs/put`（零配置：不改 docker 卷、不改 OpenList 存储；前提仅是 OpenList 已挂载好它自己的存储，bridge 用 `mkdir` 自动建 `/tg-archive/`）。备选 B. OpenList 挂载本地目录后服务端 move（大文件省一次 I/O，但要改 docker 卷 + OpenList 存储配置）。
2. **目标目录结构**：✅ 已确认 — 不平铺到固定文件夹（OpenList 挂了多个网盘）。归档弹窗内置**目录选择器**，从 OpenList 根逐级浏览全部挂载盘选目标；云端路径列展示 OpenList 实际路径。
3. **冲突默认值**：覆盖 or 跳过已存在（推荐默认「覆盖」，弹窗可改）。
4. **云端归档页数据源**：✅ 已确认 — 改读 OpenList 真实目录 + 归档日志 join；列精简：去掉 来源链接 / MD5 / quickXorHash（用户要求）。
5. **重名冲突默认**：按「默认覆盖，弹窗可改选跳过」执行（用户未反对即按此开工）。
