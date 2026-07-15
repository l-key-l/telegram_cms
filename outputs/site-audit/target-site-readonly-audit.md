# 目标站只读审计摘要

审计范围：只查看页面、DOM、表单动作、响应头和已加载前端资源。未执行上传、保存、编辑、删除、发送、上架、下架、绑定或退出登录。

## 1. 信息架构

### 个人中心

- 账号信息：用户名、昵称、注册时间。
- 账号管理：修改用户名、密码、昵称。
- 内容入口：我的内容、已下架内容、上传内容。
- Bot 上传设置：一个多行文本框绑定多个 Telegram 数字用户 ID。
- 页面公开的 Bot 操作说明：
  - 发送图片媒体组并附文字，再发送视频，形成一次上传；
  - `/上架 编号`；
  - `/下架 编号`；
  - `/cancel`；
  - 向 Bot 发送“查询ID”获取 Telegram 数字 ID。

### 内容列表

- 已上架、已下架两个视图。
- 搜索条件：编号、地区、关键词。
- 桌面端每页 15 条；前端脚本会在移动端默认改成 12 条。
- 卡片字段：内容类型、状态、标题、创建时间、文件数。
- 操作：上架/下架、查看、编辑、删除、发送记录。
- 标题采用“昵称 + 递增序号”自动生成。

### 上传与编辑

- 文本说明。
- 循环间隔天数，HTML 限制为 1–365。
- 可选每天循环发送时间，精确到分钟。
- 可选定时上架时间，精确到分钟。
- 多选 Telegram 频道。
- 第一次发送：图片与视频混排，支持拖拽排序。
- 第二次发送：一个或多个视频，作为第二个媒体组。
- 单条内容可开启“防扫图处理”；服务器同时保留原文件与带 `_processed` 后缀的处理图片。
- 编辑可增加/删除文件、调整顺序、修改频道和循环设置；标题不可改。

### 发送记录

- 记录列：时间、动作、发起者（用户/系统）、状态、详情。
- 已观察到的动作：首次上架、手动上架、手动下架、循环发送、加入队列、`delete`、`text_sync`。
- 状态至少包括 `pending`、成功，以及逐频道的失败详情。
- 每次发送按频道记录第一组/第二组结果。
- 循环发送会发送新批次，再删除旧的已上架消息。
- 下架会把已有频道消息放入后台删除队列。
- 修改文本会通过 `text_sync` 同步编辑已发送到多个频道的 Telegram 消息。

## 2. 当前子账号权限线索

- 可完整管理自己的内容：查看、上传、编辑、删除、上架/下架、查看日志。
- 可修改自己的账号资料。
- 可绑定多个 Telegram 管理员数字 ID，让这些 TG 账号通过 Bot 操作该账号内容。
- 可在内容级选择系统已配置好的频道。
- 页面没有频道管理、Bot Token、队列管理、全局设置、用户/子账号管理入口。
- 未看到总后台或创建子账号入口；无法只凭该角色页面确认站点内部是否另有管理员后台。

## 3. 路由与表单动作

| 方法 | 路由模式 | 用途 |
|---|---|---|
| GET | `/user/profile` | 个人中心 |
| POST | `/user/bot-admin/save` | 保存绑定的 TG 管理员 ID |
| GET | `/user/contents` | 内容列表、搜索、状态筛选、分页 |
| GET/POST | `/user/content/upload` | 上传页/提交上传 |
| GET | `/user/content/view/<content_id>` | 内容详情 |
| GET/POST | `/user/content/edit/<content_id>` | 编辑页/保存编辑 |
| POST | `/user/content/delete_file/<file_id>` | 删除单个文件 |
| POST | `/user/content/unpublish/<content_id>` | 下架 |
| GET | `/user/content/publish/<content_id>` | 上架 |
| POST | `/user/content/delete/<content_id>` | 删除内容 |
| GET | `/user/content/logs/<content_id>` | 发送记录 |

未观察到 SPA 或 JSON CRUD API；它主要是服务端 HTML 路由。上传页用 `XMLHttpRequest` 把 `multipart/form-data` 提交到同一路由，成功后跟随服务端重定向。

## 4. 技术栈证据

- **反向代理/系统**：响应头为 `nginx/1.18.0 (Ubuntu)`。
- **异步任务**：发送日志明确出现“已提交到 Celery 异步队列”，可确认使用 Celery。
- **后端语言**：Celery 明确指向 Python 生态。
- **Web 框架**：服务端渲染、路由风格、CSRF 隐藏字段 `_csrf_token` 与页面结构更像 Flask/Jinja；这是高概率推断，不是响应头直接证明。
- **前端**：原生 HTML + 大段内联 CSS + 原生 JavaScript，没有 React/Vue 打包资源。
- **拖拽库**：同域静态文件 `SortableJS 1.15.2`。
- **上传**：`multipart/form-data` + `XMLHttpRequest.upload` 进度条。
- **媒体存储**：同域 `/uploads/...` 路径；图片有原图和 `_processed` 处理图。
- **应用版本**：页面显示 `v5.7.0`。

响应头还包含 HSTS、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、严格来源策略和 CSP；但 CSP 对脚本、样式允许 `unsafe-inline`，与页面大量内联代码一致。

## 5. 可借鉴与应改进

### 借鉴

- 内容、媒体文件、目标频道、Telegram 消息映射、发送任务/日志分层。
- “第一次发送 + 第二次发送”的媒体组模型。
- 每个频道分别保存结果，允许部分成功、部分失败。
- 发送、编辑同步、旧消息清理全部异步化，并先写 `pending` 日志。
- 日志同时记录动作、操作者、状态和可读详情。
- 移动端友好的文件选择、预览、拖拽排序和上传进度。

### 应改进

- 目标站把“上架”实现为带确认框的 GET 链接；状态变更应改为 POST/PUT，并校验 CSRF 和幂等键。
- 发送详情目前是一段长文本；新系统应把每个频道、每个媒体组、每条 Telegram `message_id` 结构化保存。
- Celery 对小规模系统可用，但部署面较大；若追求最少组件，可使用 Redis + RQ/Dramatiq/ARQ 之一，不需要 Kafka。
- 所有资源查询必须强制带当前租户/父账号条件，不能只按递增 ID 读取。
- 删除、重发、文本同步需使用任务幂等键，避免用户重复点击或队列重试导致重复发送。

## 6. 截图证据

![内容列表](C:/Users/17630/Documents/Codex/2026-07-10/telegram-github-https-doudou-8baoyang-com-4/outputs/site-audit/contents-list.png)

![个人中心与 Bot 设置](C:/Users/17630/Documents/Codex/2026-07-10/telegram-github-https-doudou-8baoyang-com-4/outputs/site-audit/profile.png)

![上传设置](C:/Users/17630/Documents/Codex/2026-07-10/telegram-github-https-doudou-8baoyang-com-4/outputs/site-audit/upload.png)

![发送记录](C:/Users/17630/Documents/Codex/2026-07-10/telegram-github-https-doudou-8baoyang-com-4/outputs/site-audit/send_logs.png)
