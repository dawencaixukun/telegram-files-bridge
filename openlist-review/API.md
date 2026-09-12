# OpenList HTTP API 清单

- 项目：OpenList（github.com/OpenListTeam/OpenList，Go + Gin）
- 版本：main @ 2bdf16d5967d0a403f67d809efd5a418b8f5bd30（2026-09-01）
- 依据：`openlist/server/router.go`（265 行，全量注册表）、`server/middlewares/auth.go`、`server/handles/task.go`、`server/mcp/handler.go`、`server/webdav.go`、`server/s3.go`、`server/common/common.go`
- 所有路径挂在前缀 `conf.URL.Path` 下（默认 `/`），下表省略该前缀

## 1. 全局约定

### 认证（`server/middlewares/auth.go`）
请求头 `Authorization`，三种取值：
1. **管理员令牌直传**：与后台设置 token 常量时间比较相等 → 以管理员身份执行（auth.go:20）
2. **JWT**：`ParseToken` 解析用户名，校验密码时间戳 PwdTS 与禁用状态（auth.go:49-72）
3. **空**：按游客（guest）处理；`Auth(false)` 时游客被禁用则 401（auth.go:32-48）

中间件语义：
| 中间件 | 语义 |
|---|---|
| `Auth(false)` | 登录用户或（已启用的）游客 |
| `Auth(true)` | 同上，但游客被禁用也放行 |
| `Authn` | 同 `Auth(true)`（WebAuthn 组用） |
| `AuthNotGuest` | 游客返回 403 |
| `AuthAdmin` | 非管理员返回 403 |

### 响应封装（`server/common/common.go:87-117`）
`/api` 下错误也返回 **HTTP 200**，JSON 包络 `{code, message, data}`；`code=200` 成功，401/403/404/500 等业务码在包络内。

### 限速
下载 `DownloadRateLimiter`、上传 `UploadRateLimiter`（router.go:47,214；webdav.go:27-28）。

## 2. 顶层端点（无需登录）

| 方法 | 路径 | 说明 | 源 |
|---|---|---|---|
| ANY | `/ping` | 健康检查，返回 "pong" | router.go:31 |
| GET | `/favicon.ico` `/robots.txt` `/manifest.json` | 静态元数据 | router.go:34-36 |
| GET | `/i/:link_name` | iOS Plist 分发 | router.go:37 |
| GET/HEAD | `/d/*path` | 直链下载（签名 `sign.Verify` + 下载限速） | router.go:49,51 |
| GET/HEAD | `/p/*path` | 代理下载（签名 + 限速） | router.go:50,52 |
| GET/HEAD | `/ad/*path` | 压缩包直链下载（`sign.VerifyArchive`） | router.go:54,57 |
| GET/HEAD | `/ap/*path` | 压缩包代理下载 | router.go:55,58 |
| GET/HEAD | `/ae/*path` | 压缩包内部解压提取 | router.go:56,59 |
| GET/HEAD | `/sd/:sid`、`/sd/:sid/*path` | 分享文件下载 | router.go:61-64 |
| GET/HEAD | `/sad/:sid`、`/sad/:sid/*path` | 分享压缩包提取 | router.go:65-68 |
| GET | `/` | 非 `/` 部署前缀时 302 跳转 | router.go:22 |
| * | 其余全部 | 前端静态资源（NoRoute → static） | router.go:114 |
| * | `/debug/*` | 仅 `flags.Debug/Dev` 构建注册 | router.go:111-113 |

## 3. /api 认证与会话

无需登录：

| 方法 | 路径 | 说明 | 源 |
|---|---|---|---|
| POST | `/api/auth/login` | 密码登录 | router.go:74 |
| POST | `/api/auth/login/hash` | 哈希登录 | router.go:75 |
| POST | `/api/auth/login/ldap` | LDAP 登录 | router.go:76 |
| GET | `/api/auth/sso` | SSO 跳转 | router.go:87 |
| GET | `/api/auth/sso_callback` / `get_sso_id` / `sso_get_token` | SSO 回调/取身份/取令牌 | router.go:88-90 |
| GET | `/api/authn/webauthn_begin_login` | WebAuthn 登录开始 | router.go:93 |
| POST | `/api/authn/webauthn_finish_login` | WebAuthn 登录完成 | router.go:94 |

`Auth(false)` 组（登录用户/游客）：

| 方法 | 路径 | 源 |
|---|---|---|
| GET | `/api/me` | router.go:77 |
| POST | `/api/me/update` | router.go:78 |
| GET | `/api/me/sshkey/list` | router.go:79 |
| POST | `/api/me/sshkey/add` | router.go:80 |
| POST | `/api/me/sshkey/delete` | router.go:81 |
| POST | `/api/auth/2fa/generate` | router.go:82 |
| POST | `/api/auth/2fa/verify` | router.go:83 |
| GET | `/api/auth/logout` | router.go:84 |

`Authn` 组（前缀 `/api/authn`）：

| 方法 | 路径 | 源 |
|---|---|---|
| GET | `/api/authn/webauthn_begin_registration` | router.go:95 |
| POST | `/api/authn/webauthn_finish_registration` | router.go:96 |
| POST | `/api/authn/delete_authn` | router.go:97 |
| GET | `/api/authn/getcredentials` | router.go:98 |

## 4. /api/public（无需登录，router.go:101-104）

| 方法 | 路径 | 说明 |
|---|---|---|
| ANY | `/api/public/settings` | 公开站点设置 |
| ANY | `/api/public/offline_download_tools` | 可用离线下载工具 |
| ANY | `/api/public/archive_extensions` | 支持的压缩包扩展名 |

## 5. /api/fs 文件系统

**游客放行组** `Auth(true)`（分享/旧脚本兼容，router.go:107,193-199）：

| 方法 | 路径 |
|---|---|
| ANY | `/api/fs/list` |
| ANY | `/api/fs/get` |
| ANY | `/api/fs/archive/meta` |
| ANY | `/api/fs/archive/list` |

**标准组** `Auth(false)`（router.go:106,201-237）：

| 方法 | 路径 | 说明 |
|---|---|---|
| ANY | `/api/fs/search` | 搜索（需搜索索引中间件） |
| ANY | `/api/fs/other` | 对象附加信息 |
| ANY | `/api/fs/dirs` | 目录树 |
| POST | `/api/fs/mkdir` `rename` `batch_rename` `regex_rename` | 目录/重命名 |
| POST | `/api/fs/move` `recursive_move` `copy` `remove` `remove_empty_directory` | 复制移动删除 |
| PUT | `/api/fs/put` | 流式上传（FsUp + 限速） |
| PUT | `/api/fs/form` | 表单上传 |
| POST | `/api/fs/multipart/init` | 分片上传初始化 |
| PUT | `/api/fs/multipart/chunk` | 分片上传 |
| POST | `/api/fs/multipart/complete` `abort` | 分片完成/中止 |
| GET | `/api/fs/multipart/status` | 分片状态 |
| POST | `/api/fs/link` | 取直链（额外 `AuthAdmin`） |
| POST | `/api/fs/add_offline_download` | 离线下载 |
| POST | `/api/fs/archive/decompress` | 解压 |
| POST | `/api/fs/torrent/parse` `upload_parse` `rapid_upload` `generate` | 种子解析/秒传/生成 |
| POST | `/api/fs/get_direct_upload_info` | 客户端直传信息 |

## 6. /api/task 任务管理（`Auth(false)` + `AuthNotGuest`）

7 类任务子组（handles/task.go:221-227）：`upload`、`copy`、`move`、`offline_download`、`offline_download_transfer`、`decompress`、`decompress_upload`。

每组共用模板（task.go:127-218）：

| 方法 | 路径 | 参数 |
|---|---|---|
| GET | `/api/task/{kind}/undone` `/done` | — |
| POST | `/api/task/{kind}/info` `cancel` `delete` `retry` | `?tid=` |
| POST | `/api/task/{kind}/cancel_some` `delete_some` `retry_some` | body: `["tid",...]` |
| POST | `/api/task/{kind}/clear_done` `clear_succeeded` `retry_failed` | — |

## 7. /api/share 分享管理（`Auth(false)` + `AuthNotGuest`，router.go:109,243-251）

| 方法 | 路径 |
|---|---|
| ANY | `/api/share/list` |
| GET | `/api/share/get` |
| POST | `/api/share/create` `update` `delete` `enable` `disable` |

## 8. /api/admin 管理（`Auth(false)` + `AuthAdmin`）

| 子组 | 端点 | 源 |
|---|---|---|
| `/api/admin/meta` | GET `list` `get`；POST `create` `update` `delete` | router.go:120-125 |
| `/api/admin/user` | GET `list` `get` `sshkey/list`；POST `create` `update` `cancel_2fa` `delete` `del_cache` `sshkey/delete` | router.go:127-136 |
| `/api/admin/storage` | GET `list` `get`；POST `create` `update` `delete` `enable` `disable` `load_all` | router.go:138-146 |
| `/api/admin/driver` | GET `list` `names` `info` | router.go:148-151 |
| `/api/admin/setting` | GET `get` `list`；POST `save` `delete` `default` `reset_token` `set_aria2` `set_qbit` `set_transmission` `set_115` `set_115_open` `set_123_pan` `set_123_open` `set_pikpak` `set_thunder` `set_thunderx` `set_thunder_browser` `set_guangyapan` | router.go:153-171 |
| `/api/admin/task` | 同第 6 节 7×12 全量（兼容旧脚本） | router.go:174 |
| `/api/admin/message` | POST `get` `send` | router.go:176-178 |
| `/api/admin/index` | POST `build` `update` `stop` `clear`；GET `progress`（SearchIndex 中间件） | router.go:180-185 |
| `/api/admin/scan` | POST `start` `stop`；GET `progress` | router.go:187-190 |

## 9. 协议网关

| 挂载点 | 协议 | 认证 | 说明 | 源 |
|---|---|---|---|---|
| `/dav` | WebDAV（PROPFIND/MKCOL/LOCK/UNLOCK/PROPPATCH/COPY/MOVE 等） | HTTP Basic（webdav.go:66） | 上传/下载限速 | router.go:43; webdav.go:23-45 |
| `/s3` | S3 兼容（ANY `/*path` 代理到 s3 server） | S3 签名 | 需 `conf.S3.Enable` 且未绑定独立端口；另有独立端口模式 `InitS3` | router.go:44,262-265; s3.go:14-34 |
| `/mcp` | MCP（GET/POST/DELETE，streamable HTTP） | `Auth(false)`+`AuthAdmin` | 未启用时 ANY 返回 403 | router.go:45; mcp/handler.go:80-84; mcp.go:11-15 |

## 10. 规模统计

- `/api` 下约 **170+ 个端点**（fs 30+、admin 60+、task 7×12=84、auth/me 19、share 7、public 3、MCP 3）
- 顶层下载/分享直链 14 条（含 HEAD 变体）
- 3 个协议网关（WebDAV / S3 / MCP）
