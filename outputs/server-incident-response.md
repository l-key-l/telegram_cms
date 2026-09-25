# 2026-09-25 频道资料被修改：服务器处理

## 已确认与待确认

- 当前可读数据库是 Windows 本机 `127.0.0.1/telegram`，最近一条审计为 2026-07-19，今天无审计。它不是线上服务器。
- 尚未读取服务器当天的日志，也未修改服务器账号。具体入侵路径待证据确认。
- 项目在网站修改频道资料时记录 `telegram.channel.update.enqueue`（修改前后值），并创建 `UPDATE_CHANNEL` Operation。
- 登录审计 `auth.login` 可与前述记录的 actor、operation、时间关联。审计 IP 取自 `X-Forwarded-For`，需和可信 Nginx 上游日志交叉核对，不据此直接归因。
- 通过泄露 Bot Token 或 Telegram 客户端实施的动作，可能没有本网站审计。仍需检查频道“最近操作”、管理员名单和 Telegram 登录设备。

## 先遏制继续修改，保留现场

1. 在宝塔暂停网站进程；如果有独立 Worker，一并暂停。保留数据库、媒体和日志，不删除重装。
2. 在 Telegram 官方客户端核对“最近操作”，保留 2026-09-25 06:00 北京时间前后的操作者、动作与时间截图。再处理陌生管理员/设备。
3. 网站密码、Bot Token、数据库密码、Django 签名密钥和 Bot 加密密钥是不同凭据；网站改密不会使已泄露 Bot Token 失效。涉及 Bot Token 时在 BotFather 撤销旧 Token，并把新 Token 更新到系统。加密密钥先有计划地迁移密文，避免直接替换导致已有 Bot 记录解密失败。

## 脚本用法

将 `scripts/server_incident_response.py` 单独上传到服务器 `/www/wwwroot/telegram_cms/scripts/`，不要求推送本地其他未完成改动。
使用宝塔该项目的 Python 环境，在项目根目录执行。脚本不连接 Telegram、不迁移表、不触发 Worker、不覆盖 `.env`。

只读导出（默认收集指定日期整天，时区固定为北京时间 UTC+8）：

```bash
python3 scripts/server_incident_response.py --day 2026-09-25 --output /root/telegram-incident-20260925-inspect
```

导出证据后原地修改主账号（保留用户 ID 和全部子账号归属）：

```bash
python3 scripts/server_incident_response.py --day 2026-09-25 --output /root/telegram-incident-20260925-reset --apply --new-username ops_20260925
```

- 在私有终端交互输入两次至少 16 位的随机密码；输入不回显，不放进命令行、代码、报告或聊天。
- 有多个管理员时，脚本停止改密并输出管理员列表，再用一个新的证据目录并加 `--user-id 实际ID` 重跑，避免重置错人。
- 证据目录须位于网站外，且每次新建；Linux 目录权限 `0700`、文件 `0600`。
- 先导出并核验证据清单，再改密码、撤销该账号的数据库会话、写系统审计；不清除其他账号会话。原密码认证哈希失效。并发已开始的请求仍可能完成，所以先暂停服务。
- 改密后 `reset-result.json` 是额外结果，改密前 `manifest.json` 不被覆盖。数据库的 `security.master.credentials.rotate` 为事务内重置记录。

要保全实际 Nginx/应用日志，追加 `--log-file /实际路径/访问.log`（可重复）。先在宝塔找到实际日志路径；脚本不猜测日志名。
原始日志私有保全副本可能含敏感信息，仅在受控环境分析；不要公开上传。

先返回 `summary.json` 中的计数与管理员列表（用户名可打码），并提供相关 `auth.login` / `telegram.channel.update.enqueue` 条目与请求时间。不要发送 `.env`、密码、Token、cookie 或完整数据库。

## 回滚与验证

- 只读命令：数据库零写入。
- 参数、密码校验失败：不改账号；事务内审计失败：改名、改密码、会话撤销一起回滚。
- 已完成的紧急改密不恢复旧泄露密码，也不恢复旧会话。如新用户名不合适，重跑脚本指定原用户 ID、另一个新用户名及新的强密码。
- 本次代码都是新增脚本/测试/说明；停用脚本不会改变现有服务流程。不要用全仓库 `git reset` 丢弃其他修改。
- 服务器实测应确认：原账号密码登录失效，新账号密码登录成功，子账号归属仍在，频道资料和排队任务逐项核对后再启动 Worker。
- 如果现网仍使用 HTTP，应先部署 HTTPS。当前代码把安全 cookie 与 DEBUG 绑定，直接设 `DJANGO_DEBUG=0` 会同时打开 Secure cookie；HTTP 登录会受影响，应和 TLS 一起修正，别只关闭 DEBUG 就结束。
