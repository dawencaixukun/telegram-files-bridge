# telegram-files-bridge

把 Telegram 频道/群组里的视频媒体**自动下载、归档到自己的网盘**的自托管工具。提供一个网页控制台：提交视频链接或频道订阅，后台自动下载并转存到 OpenList 网盘（OneDrive、Google Drive 等），浏览器里随时浏览、取回、观看。

## 这是做什么的

- **提交下载**：粘贴 Telegram 消息链接（或频道 + 消息 ID）即可入队，支持批量；自动解析媒体大小/时长/清晰度
- **自动归档**：下载完成后按你设定的目录规则自动上传到 OpenList 网盘（默认 `/onedrive/yello`），支持同名覆盖/跳过/重命名策略
- **浏览取回**：网页上直接浏览频道历史媒体，点选下载；支持**断点续传**（中断后从已传字节继续）
- **云端浏览**：已归档文件在网页里网格展示，一键跳转 OpenList 定位原文件

## 关键功能

**下载与传输**
- 多任务并发队列，实时进度、限速、失败重试与自动隔离
- 频道监听：订阅规则开启后新消息**实时入库**，无需手动翻历史
- 断点续传：大文件取回中断后继续，不从头来
- FloodWait 预防与自动冷却恢复，账号状态页可视化健康度

**归档与管理**
- 归档目录模板化（按频道/日期组合命名），文件名广告自动清洗（Emby/Jellyfin 友好）
- OpenList 改名自愈：网盘里改了文件名，归档链接自动重新定位
- 磁盘高低水位熔断：占用达高水位自动暂停下载并清理，回落低水位恢复
- 会话备份与恢复：本地快照 + 一键上传云端，快照可手动管理

**浏览台（Web UI）**
- 玻璃拟态深/浅双主题，响应式布局，零构建链（纯 HTML/CSS/JS + htmx + Alpine.js）
- 会话侧栏自由管理：隐藏/显示即时生效，收藏置顶不可隐藏
- 任务队列、本地库存、云端归档、日志中心（SSE 实时推送）、账号健康一页全览

## 技术栈

Python 3.11 · FastAPI · Telethon（MTProto 直连） · Jinja2 服务端渲染 · htmx + Alpine.js · SSE 实时推送 · SQLite

## 部署

```bash
pip install -r requirements.txt
uvicorn bridge_server:app --host 0.0.0.0 --port 8000
```

浏览器打开 `http://127.0.0.1:8000/`，首次访问完成初始化（管理员账号 + Telegram 登录）。

> 📖 安装教程详见 [install.md](install.md)

## 环境要求

- 可直连 Telegram API 的网络环境
- 一个 OpenList 实例（用于网盘转存）
- Telegram API ID / Hash（[my.telegram.org](https://my.telegram.org) 免费申请）
