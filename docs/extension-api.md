# 浏览器插件接入 —— M3U8 下载 API

给 Chrome 扩展（或任何客户端）对接的 m3u8(HLS) 下载接口。插件在网页上嗅探到
`.m3u8` 播放列表后，调用本 API 提交下载；服务端并发拉取分片、按需 AES-128 解密、
合并成单文件，**并自动创建归档任务**（复用现有 OpenList 上传管线，与 Telegram
文件同等对待，可在「云端归档」页看到、可重试、可被全局搜索命中）。

---

## 1. 鉴权

插件接口使用**独立 Token**，不使用门户 Cookie（插件没有 Cookie，也不该有）。

- 请求头：`X-Ext-Token: <token>`
- Token 获取：管理台 **设置 → 浏览器插件接入**，点「复制 Token」
- Token 存储：服务端 `.ext_token`（权限 0600），首次访问时自动生成
- 重置：设置页「重置 Token」——**旧 Token 立即失效**，需回插件重新填写

校验失败统一返回 `401`（不区分「未携带」与「错误」，避免探测 Token 长度）。
插件端点另有每 IP 限流（60 秒 / 120 次），超限返回 `429`。

部署在反向代理后面时，请确保把真实客户端 IP 传给 bridge（`X-Forwarded-For`
配合 `TRUSTED_PROXIES`），否则限流会以代理 IP 为单位计算。

---

## 2. 接口

所有插件接口以 `/api/ext/m3u8/` 开头，请求体为 JSON。

### 2.1 探测 `POST /api/ext/m3u8/resolve`

先探测再提交：master playlist 需要先选清晰度。

```json
{
  "url": "https://example.com/hls/master.m3u8",
  "headers": { "referer": "https://example.com/watch/1", "user_agent": "..." }
}
```

- `headers` 可选。`referer` / `user_agent` 会透传给所有分片请求（**防盗链站点必填**）。
- 其他请求头会被忽略（白名单透传）。

返回（master：多清晰度）：

```json
{ "ok": true, "kind": "master",
  "variants": [ { "url": "https://.../720p/index.m3u8", "bandwidth": 2400000,
                  "resolution": "1280x720", "name": "高清" } ] }
```

返回（media：可直接下载）：

```json
{ "ok": true, "kind": "media", "segments": 1240, "duration_secs": 7440.0,
  "encrypted": true, "init_segment": false, "message": "AES-128 加密流，将自动取密钥解密" }
```

拿到 `master` 时，插件应让用户选一路 `variants[].url`，再提交那个 URL。

### 2.2 提交下载 `POST /api/ext/m3u8/submit`

```json
{ "url": "https://example.com/hls/720p/index.m3u8",
  "title": "示例影片 1080p",
  "headers": { "referer": "https://example.com/watch/1" },
  "remote_dir": "/onedrive/剧集" }
```

- `title` 可选，作为成品文件名（会做归档同款广告尾巴清洗）；缺省则从 URL 末段推导。
- `remote_dir` 可选（别名 `archive_dir`），插件指定的归档目标目录（OpenList 绝对路径，
  可先用 2.7 的目录浏览接口取候选值）。必须是 `/` 开头的**具体子目录**：非 `/` 开头、
  含 `..`、或直接给根目录 `/` 都会返回 `400`。留空则用设置页的默认归档目录。

返回：

```json
{ "ok": true, "task_id": "a1b2c3d4e5f6", "duplicate": false, "state": "queued",
  "remote_dir": "/onedrive/剧集" }
```

- `duplicate: true` 表示**同 URL 已在队列中**，只返回已有任务，不会重复下载。
- `remote_dir` 回显归一化后的归档目标目录（未指定时为空串）。

### 2.3 查询进度 `GET /api/ext/m3u8/tasks?limit=50`

进行中的任务排在最前，其余按提交时间倒序。

```json
{ "ok": true, "active": 1,
  "tasks": [ { "id": "a1b2c3d4e5f6", "state": "running",
               "total_segments": 1240, "done_segments": 300,
               "speed_bps": 2411724.8, "downloaded_bytes": 157286400,
               "encrypted": true, "duration_secs": 7440.0,
               "filename": "", "output_size": 0, "archived_job_id": "",
               "remote_dir": "/onedrive/剧集", "error": "" } ] }
```

`state` 取值：`queued`（排队）→ `running`（下载中）→ `done`（完成）/
`failed`（失败，见 `error`）/ `cancelled`（已取消）。

`remote_dir` 为该任务的归档目标目录（空串表示用设置页默认目录）。

`done` 且 `archived_job_id` 非空时，成品已进入归档队列。

### 2.4 取消 / 重试

```
POST /api/ext/m3u8/cancel   { "task_id": "a1b2c3d4e5f6" }
POST /api/ext/m3u8/retry    { "task_id": "a1b2c3d4e5f6" }
```

- `retry` 仅对 `failed` / `cancelled` 有效；**已下载的分片会复用**，只补缺失部分
  （bridge 重启后同理，中断任务标记为可重试）。
- `retry` 在活动任务数达上限时返回 `429`。

### 2.5 错误响应

```json
{ "ok": false, "message": "拒绝访问内网/保留地址（SSRF 防护）: 127.0.0.1" }
```

| 状态码 | 含义 |
|---|---|
| 400 | 参数错误 / 目标 URL 非法（含私网 SSRF 拦截）/ 播放列表不被支持 / `remote_dir` 非法 |
| 401 | Token 无效或缺失 |
| 404 | 任务不存在或状态不允许该操作 |
| 429 | 限流，或活动任务数达上限 |
| 502 | 探测上游失败 |

### 2.6 删除任务 `POST /api/ext/m3u8/delete`

```json
{ "task_id": "a1b2c3d4e5f6" }
```

删除一个**已结束**的任务（`done` / `failed` / `cancelled`）：回收任务目录下的分片、
删除**位于本任务目录内**的成品、移除任务记录。云端已归档的文件不受影响。

```json
{ "ok": true, "message": "已删除", "freed": 314572800 }
```

- `freed` 为本次实际释放的字节数（分片 + 本地成品）。
- 进行中的任务必须先 `cancel`（返回 404）：

```json
{ "ok": false, "message": "任务仍在进行中，请先取消再删除", "freed": 0 }
```

- 取消后协程可能仍在收尾，此时删除会与飞行中的分片写入竞争 —— 返回 404：

```json
{ "ok": false, "message": "任务协程仍在收尾，请稍后再试", "freed": 0 }
```

- 若该任务的归档任务仍在 `queued`/`uploading`，删除会让本地成品变成云端
  未完成、本地又无记录的孤儿文件 —— 同样拒绝，等上传核验完成后再删：

```json
{ "ok": false, "message": "该任务正在归档上传中，请等上传完成后再删除（也可在归档页取消该上传）", "freed": 0 }
```

网页端同款端点：`POST /api/m3u8/delete`（走门户 Cookie + CSRF，请求体相同）。

### 2.7 归档目录浏览 `GET /api/ext/m3u8/dirs?path=/`

给插件选择一个归档目标目录（把返回值填进 `submit` 的 `remote_dir`）。
实现与网页端 `/openlist/dirs` 完全一致，只是改走 `X-Ext-Token`。

```json
{ "ok": true, "path": "/",
  "dirs": [ { "name": "onedrive", "path": "/onedrive" },
            { "name": "阿里云盘", "path": "/阿里云盘" } ] }
```

- `path` 缺省为 `/`（根，逐级下钻）。
- 只返回**子目录**（文件不返回）。
- 非法 `path` 或 OpenList 不可用时返回 `{"ok": false, "message": "..."}`。
---

## 3. 支持范围与限制

**支持**
- 点播 media playlist（含 `#EXT-X-ENDLIST`）
- master playlist（经 `resolve` 选路后提交）
- AES-128 加密（`#EXT-X-KEY`，自动取密钥、按 RFC 8216 推导 IV）
- fMP4 / `#EXT-X-MAP`（init 段自动前置拼接，产出 `.mp4`）
- `#EXT-X-BYTERANGE`（含 `@offset` 缺省时的续算）
- TS 明文流（服务端探测到 ffmpeg 时无损 remux 为 MP4，否则保留 `.ts`）

**不支持**
- 直播流（无 `#EXT-X-ENDLIST`）——直接报错，不做无限拉取
- SAMPLE-AES / 其他加密方式
- `#EXT-X-MAP` 带 `BYTERANGE`

**护栏**
- 仅 http/https；DNS 解析到私网、环回、链路本地、保留地址一律拒绝（SSRF）
- 重定向逐跳复检 SSRF，最多 5 跳
- 分片与播放列表响应均**流式限长读取**（分片 256MB、播放列表 32MB），超限即中止，不会先整包进内存
- 带 `Range` 的请求必须是 206 且长度与声明一致，否则判失败（防源站不支持 Range 时静默拼错）
- `title` 会被净化：剥离路径分隔符与 `..`，成品一律落在本任务目录内（防路径穿越）
- `remote_dir` 会被归一化：非 `/` 开头、含 `..`、或指定根目录 `/` 一律拒绝（防写到别的网盘目录）
- 分片回收有目录红线：真实路径必须落在 `m3u8-downloads/` 内且目录名等于 task id，否则不删任何文件
- 非法 `IV`（非 16 字节/非十六进制）直接报错，不静默回落（防解密出垃圾却标成完成）
- 单任务：分片数 ≤ 20000、体积 ≤ 20GB、分片并发 6
- 同时进行的下载任务 ≤ 5
- 不走系统代理（`trust_env=False`），避免代理绕过 SSRF 检查

**归档开关**
- 成品自动入队归档受设置页「自动归档」总开关约束；关闭时只留在本地，等你在归档页手动归档
- 文件名清洗同样遵循设置页的「清洗归档文件名」开关

---

## 4. 最小对接示例

```js
const BASE = 'http://your-bridge:8000';
const TOKEN = '粘贴设置页的 Token';

async function api(path, body) {
  const r = await fetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Ext-Token': TOKEN },
    body: JSON.stringify(body || {})
  });
  return r.json();               // 非 2xx 也会带回 { ok:false, message }
}

// 1) 探测；master 时让用户选清晰度
const info = await api('/api/ext/m3u8/resolve', { url, headers: { referer: location.href } });
let target = url;
if (info.kind === 'master') {
  target = await pickVariant(info.variants);   // 由插件 UI 决定
}

// 2) 可选：让用户选归档目录（不选就留空，走设置页默认目录）
const { dirs } = await api('/api/ext/m3u8/dirs?path=/');
const remoteDir = await pickDir(dirs);        // 例如 "/onedrive/剧集"

// 3) 提交
const sub = await api('/api/ext/m3u8/submit', {
  url: target, title: document.title,
  headers: { referer: location.href },
  remote_dir: remoteDir                     // 可选
});

// 4) 轮询进度（或改为后台任务，不必阻塞页面）
setInterval(async () => {
  const r = await fetch(BASE + '/api/ext/m3u8/tasks', { headers: { 'X-Ext-Token': TOKEN } });
  const { tasks } = await r.json();
  render(tasks.find(t => t.id === sub.task_id));
}, 2000);

// 5) 清理：任务结束后删除（回收本地分片与成品）
await api('/api/ext/m3u8/delete', { task_id: sub.task_id });
```

`manifest.json` 需要目标主机的访问权限：

```json
{
  "host_permissions": ["http://your-bridge:8000/*"],
  "permissions": ["storage"]
}
```

> 若管理台使用 https，插件请求也必须是 https，否则浏览器会拦截混合内容。

---

## 5. 数据落地与归档

```
{APP_ROOT_DIR}/m3u8-downloads/<task_id>/   分片暂存（seg_00000.bin 等）
{APP_ROOT_DIR}/.m3u8_tasks.json            任务表（权限 0600）
{APP_ROOT_DIR}/.ext_token                  插件 Token（权限 0600）
```

下载完成后成品落到 `<task_id>/` 目录下，并自动创建一个归档任务：

- `unique_id` = `m3u8-<sha1(url) 前 16 位>`（稳定身份，重复提交不会重复归档）
- 目标目录优先级：**提交时指定的 `remote_dir` > 设置页「归档与自动转存」默认目录
  > `/m3u8`**；提交时的值经 `_archive_norm_dir` 归一化，必须是 `/` 开头的具体子目录
- 归档成功后的本地删除策略沿用设置页的「归档成功后自动删除本地文件」

**分片回收**

- 任务成功（`done`）后**自动回收分片**：只删 `seg_*.bin` / `init.bin` 这类暂存文件，
  成品与其他文件一律不动（成品仍按上面的 `deleteLocal` 开关决定去留）
- **失败 / 取消的任务不回收分片**（保留下来供 `retry` 断点续传复用）
- 要连成品一起清掉用「删除任务」（见 2.6）：仅限终态任务，删除后分片、位于本任务
  目录内的成品与任务记录一并移除；云端已归档的文件不受影响
- 回收有目录红线：分片目录的真实路径必须落在 `m3u8-downloads/` 内且目录名等于
  task id，不满足则整体放弃，绝不越界删除

**注意**：m3u8 任务不登记到 Java 后端的任务库（bridge 无法写该库），因此
「任务队列」页的 KPI 计数不含网页下载任务。网页端展示在 **任务队列 → 网页下载**
分区，归档后在 **云端归档** 页可见。

---

## 6. 管理台侧

- **设置 → 浏览器插件接入**：查看 / 复制 / 重置 Token，附接口速查
- **任务队列 → 网页下载**：进度条、分片计数、速率、取消、重试、删除（删除会回收
  分片与本地成品）；有进行中任务时每 4 秒自动刷新，空闲自动停表
