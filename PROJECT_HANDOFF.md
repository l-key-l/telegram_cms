# Project Handoff for PyCharm + CC GUI

## Objective

Build a new Telegram channel content-management website based on the observed subaccount experience of the reference site.

The system will support:

- `MASTER` backend and `SUB` frontend in the same Django website;
- subaccount lifecycle and data isolation;
- Telegram Bot/channel many-to-many bindings;
- content upload, media ordering, channel selection, publish, unpublish, edit replacement, delete, scheduling, and recurring replacement;
- per-channel/per-message delivery state;
- complete audit of actions performed through this system.

## Current status

- Target-site read-only exploration is complete.
- GitHub reference-project research is complete.
- Architecture has been revised for the same-site master/subaccount model.
- Bot ↔ Channel many-to-many expansion has been reserved from the first database migration.
- User confirmed construction and the Django application is running on MySQL.
- MASTER/SUB login, subaccount management, Bot/channel/binding pages, content workflows, Worker, deliveries, audit and Webhook commands are implemented.
- The embedded Worker inside local `runserver` supports immediate send, safe media replacement, text/caption replacement, delete/unpublish, scheduling, polling, channel title/description/photo changes and explicit `message_id` operations. A separate Worker command is optional.
- Telegram dangerous commands use expiring confirmation tokens; Bot failover never occurs after an uncertain send timeout.
- Database migrations `channel_manager.0001` through `0003` are applied; the automated suite currently contains 23 passing tests.
- Remaining work is real Bot/channel integration testing after the user enters Telegram credentials and permissions.

## Required reading

- `AGENTS.md`
- `outputs/telegram-channel-system-architecture.md`
- `outputs/site-audit/target-site-readonly-audit.md`
- Target-site screenshots in `outputs/site-audit/`

## CC GUI / Codex session

The complete Codex conversation is stored locally in the standard Codex session directory.

```text
Session ID:
019f4b03-2b05-7300-82f9-b99f60f75b70

Session file:
C:\Users\17630\.codex\sessions\2026\07\10\rollout-2026-07-10T15-52-06-019f4b03-2b05-7300-82f9-b99f60f75b70.jsonl

Recorded project CWD:
C:\Users\17630\Documents\Codex\2026-07-10\telegram-github-https-doudou-8baoyang-com-4
```

CC GUI's Codex history reader scans `~/.codex/sessions` and filters sessions by recorded `cwd`. Opening the recorded project directory in PyCharm should make this session discoverable in CC GUI history.

Do not copy or edit the JSONL session file. Use CC GUI history/resume. If history loading fails, start a new Codex conversation in this project and send:

```text
请先读取 AGENTS.md、PROJECT_HANDOFF.md、
outputs/telegram-channel-system-architecture.md 和
outputs/site-audit/target-site-readonly-audit.md。
项目已经进入开发和联调阶段。读取后先运行 `python manage.py check` 和测试，
再根据真实 Bot/频道联调结果继续修复；不要改回 SQLite、Redis 队列或微服务。
```
