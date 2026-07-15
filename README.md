# Telegram 频道内容管理系统

全新开发的 Django 单体项目，包含：

- 同一网站内的 `MASTER` 后台和 `SUB` 内容前台；
- 目标站风格的内容列表、上传、编辑、个人中心和发送记录；
- 网页添加 Telegram Bot、频道以及 Bot ↔ 频道多对多绑定；
- 主 Bot、备用 Bot、权限快照和路由策略；
- 内容、媒体、投递消息 ID、数据库任务队列和操作审计；
- Telegram Webhook、`查询ID`、`/上架`、`/下架`、`/编辑`、`/状态`、`/确认`、`/取消`；
- MySQL Worker 发送、编辑/循环更新后的替换发布、下架删除和频道资料修改；
- 简繁转换、列表全选与批量操作、危险指令二次确认、严格任务顺序、Bot 安全故障切换、频道权限复核和完整审计。
- 两阶段媒体投递：第二次发送必填，单频道失败自动回滚已发消息，编辑保存时整批替换并记录全部 Telegram Message ID。

## PyCharm 公共 Python 环境

项目不要求创建虚拟环境。当前开发环境使用系统 Python 3.12。

在 PyCharm 中将 Interpreter 指向：

```text
C:\Users\17630\AppData\Local\Programs\Python\Python312\python.exe
```

公共环境安装依赖：

```powershell
python -m pip install -r requirements.txt
```

## 本地启动

项目固定使用 MySQL。项目根目录的 `.env` 保存本机配置且已加入 `.gitignore`，PyCharm 直接运行时会自动加载。

```powershell
python manage.py migrate
python manage.py create_master admin --password "请替换为强密码" --display-name "总管理员"
python manage.py runserver 127.0.0.1:8000
```

浏览器访问：

```text
http://127.0.0.1:8000/login/
```

本地 `.env` 使用 `EMBEDDED_WORKER=1` 时，网页进程会自动启动内置 Worker，通常不需要第二个 Terminal。内置 Worker 会被任务事件立即唤醒，同时处理定时任务和本地 Telegram Polling。

只有将 `EMBEDDED_WORKER=0`、单独调试 Worker 或正式拆分部署时才运行：

```powershell
python manage.py run_operation_worker
```

仅处理一条任务后退出：

```powershell
python manage.py run_operation_worker --once
```

本地没有配置 `PUBLIC_BASE_URL` 时，内置 Worker 会在网站进程中自动使用 long polling 接收 Bot 指令，因此本地通常只需要启动 `runserver` 一个程序。

只有调试 Telegram 接收链路时，才单独运行：

```powershell
python manage.py run_telegram_polling
```

单独运行前请先停止 Worker 内置 polling，或使用 `python manage.py run_operation_worker --no-telegram-polling`。Polling 会停用对应 Bot 当前的 Webhook；部署公网后在主后台重新点击“启用指令”即可切回 Webhook。

## MySQL

复制 `.env.example` 为 `.env`，或在 PyCharm Run Configuration 中配置：

```text
DB_NAME=telegram
DB_USER=telegram_manager
DB_PASSWORD=你的密码
DB_HOST=127.0.0.1
DB_PORT=3306
```

数据库字符集使用 `utf8mb4`。

## Bot Token 加密

生成加密主密钥：

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

将结果配置为：

```text
BOT_TOKEN_ENCRYPTION_KEY=生成的密钥
PUBLIC_BASE_URL=https://你的公网域名
```

之后在主账号后台打开：

```text
/master/telegram
```

按顺序：

1. 添加机器人；
2. 验证 Token；
3. 添加频道；
4. 创建 Bot ↔ 频道绑定；
5. 验证发送、编辑、删除权限；
6. 给子账号分配频道。

部署 HTTPS 域名并配置 `PUBLIC_BASE_URL` 后，在 Bot 卡片点击“启用指令”注册 Webhook。

已上架内容保存编辑后会先发布新版本，成功后删除旧消息；循环自动更新采用同一流程。手动“重发”入口和 Bot `/重发` 指令均已移除。

添加频道前可在 `/master/telegram` 使用“频道链接转 Chat ID”：公开频道链接和 `t.me/c/...` 消息链接可直接解析；私有邀请链接需要先把所选 Bot 设为频道管理员，并在频道发送一条新消息让系统收到 `channel_post`。

## 关键文档

- `AGENTS.md`
- `PROJECT_HANDOFF.md`
- `outputs/telegram-channel-system-architecture.md`
- `outputs/site-audit/target-site-readonly-audit.md`
