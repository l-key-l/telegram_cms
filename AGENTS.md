# Telegram Channel Manager — Agent Instructions

## Language

- Reply in Simplified Chinese.
- Keep code, commands, paths, API methods, database identifiers, and library names in their original form.

## Current stage

- The project is still in architecture confirmation.
- Do not start scaffolding, install dependencies, create migrations, or write business code until the user explicitly confirms construction can begin.

## Sources of truth

Read these before proposing or changing implementation:

1. `outputs/telegram-channel-system-architecture.md`
2. `outputs/site-audit/target-site-readonly-audit.md`

The target site is a visual and workflow reference only. This project must be developed from scratch and must not depend on the target site's source code or static assets.

## Confirmed product model

- One website, one Django application, one login system, and one MySQL database.
- `SUB` accounts use `/user/*` as the content-operation frontend.
- `MASTER` accounts use `/master/*` as the control backend.
- The subaccount UI should closely reproduce the observed target-site layout, field order, operation placement, status colors, responsive behavior, and workflows.
- The master UI uses the same design system and manages subaccounts, Bot/channel bindings, global operations, and audit events.
- Master accounts do not impersonate or operate as subaccounts in the MVP.

## Technical decisions

- Django monolith.
- MySQL.
- aiogram + Telegram Bot API.
- Django Templates + Bootstrap/HTMX + small amounts of native JavaScript + SortableJS.
- MySQL-backed operation queue. Local development uses one embedded Worker thread inside `runserver`; a separate Worker process remains optional for production or debugging.
- Caddy or Nginx, not both.
- Docker Compose deployment.
- No Redis, MongoDB, Kafka, microservices, Kubernetes, React, or Vue in the MVP.

## Telegram model

- Model Bot ↔ Channel as many-to-many from the first migration.
- A Bot can serve multiple channels, and a channel can have multiple Bots.
- Each channel supports a primary Bot, backup Bots, priority, permissions snapshot, and routing policy.
- Content selects channels, not a hard-coded Bot.
- Every Delivery stores the actual binding and Bot used.
- Every Telegram message stores `channel_id`, `telegram_message_id`, and `sent_by_bot_id`.
- Store every message ID in a media group.
- Store Telegram media `file_id` per Bot because it cannot be reused across Bots.
- Do not fail over after an uncertain send timeout; mark the operation `UNCERTAIN` to avoid duplicates.

## Data and audit rules

- Enforce account scope in backend queries; never trust a user/account ID supplied by the frontend.
- Use POST/PATCH/DELETE for state changes; never mutate state with GET.
- Use CSRF and idempotency keys.
- Store Bot tokens encrypted from the first version; environment variables hold only the master encryption key.
- Audit every login result and every business state change with actor, source, target, before/after, outcome, request ID, and operation ID.
- System audit covers actions performed through this system; do not claim complete capture of manual Telegram-client history.
- Never write passwords, full Bot tokens, session cookies, webhook secrets, or private credentials to code, fixtures, audit logs, or documentation.

## Change discipline

- Keep `outputs/telegram-channel-system-architecture.md` synchronized with confirmed product decisions.
- Prefer the smallest implementation that preserves the confirmed multi-channel/multi-Bot data model.
- Do not replace confirmed architecture with a framework or infrastructure expansion without explicit user approval.
