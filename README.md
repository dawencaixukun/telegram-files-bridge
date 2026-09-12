# TG 视频下载与归档管理系统 — 前端预览

> 📖 **安装教程请看 [install.md](install.md)**

纯 HTML/CSS/Jinja2 服务端渲染的玻璃拟态（Glassmorphism）管理台前端。
**本阶段仅做 UI 外壳**：完整模板 + 模拟数据 + 全部视觉与交互，不含任何真实业务逻辑。

## 目录结构

```
webapi/
├── preview_server.py            # 最小预览服务器（FastAPI，~100 行核心渲染）
├── requirements.txt             # fastapi / uvicorn / jinja2 锁版
├── static/
│   ├── css/
│   │   ├── tokens.css           # design tokens（深/浅两套主题，CSS 变量切换）
│   │   └── main.css             # 布局 / 玻璃卡片 / 组件 / 响应式
│   └── js/
│       └── app.js               # 主题 / toast / 抽屉 / 提交模态 / SSE 胶水
└── templates/
    ├── base.html                # 根布局：侧边栏 + 顶栏 + 图标 sprite
    ├── login.html               # /login 和 /init 共用（variant）
    ├── dashboard.html           # /  KPI + 趋势图 + 最近任务 + 告警
    ├── tasks.html               # /tasks 任务队列
    ├── task_detail.html         # /tasks/{id} 详情
    ├── library_local.html       # /library/local 本地在存（网格/表格切换）
    ├── library_cloud.html       # /library/cloud 云端归档（只读）
    ├── submit.html              # /submit 提交下载（实时校验）
    ├── tg_login.html            # /tg-login 三步登录向导
    ├── settings.html            # /settings 设置
    ├── logs.html                # /logs 日志中心（SSE 流）
    ├── account.html             # /account 账号健康（ApexCharts）
    └── partials/
        ├── _macros.html         # Jinja 宏（状态胶囊等）
        ├── _tasks_table.html    # 任务表格（htmx 局部刷新目标）
        └── _submit_modal.html   # 全局提交下载模态框
```

## 路由表（9+1）

| 路由 | 页面 |
|------|------|
| `/` | Dashboard |
| `/login` | 登录 |
| `/init` | 首启初始化 |
| `/tasks` | 任务队列 |
| `/tasks/{id}` | 任务详情 |
| `/library/local` | 本地在存 |
| `/library/cloud` | 云端归档 |
| `/submit` | 提交下载 |
| `/tg-login` | TG 登录向导 |
| `/settings` | 设置 |
| `/logs` | 日志中心 |
| `/account` | 账号健康 |

## 启动（两条命令）

```bash
pip install -r requirements.txt
uvicorn preview_server:app --host 127.0.0.1 --port 8000
```

浏览器打开 http://127.0.0.1:8000/

## 特点

- **零构建链**：无 npm / Vite / Webpack / Tailwind，所有文件原样运行
- **纯 CSS 渐变背景**：多层 radial-gradient 叠加，无远程图片
- **图标**：内联 SVG（lucide 风格 sprite），无图标字体
- **交互**：htmx（CDN）局部刷新 + Alpine.js（CDN）下拉/模态/向导；Chart.js、ApexCharts（CDN）仅 Dashboard/account 使用
- **SSE**：`/sse/logs`、`/sse/tasks` 推送模拟事件，断线自动静默降级
- **主题**：深色默认，浅色切换存 localStorage，CSS 变量整体切换无 FOUC
