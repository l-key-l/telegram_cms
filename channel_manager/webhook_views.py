from __future__ import annotations

import hmac
import json
import secrets
from datetime import timedelta

from asgiref.sync import async_to_sync
from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .audit import audit_event
from .models import (
    Content,
    Operation,
    TelegramBot,
    TelegramPendingAction,
    TelegramUpdate,
    TelegramUserLink,
    UserChannelAccess,
)
from .security import decrypt_secret
from .services import enqueue_content_operation
from .telegram_client import create_bot


async def _reply(bot_record, chat_id, text):
    # 懒加载 aiogram，避免 create_master/migrate 等普通管理命令被 Telegram SDK 拖慢。
    async with create_bot(decrypt_secret(bot_record.token_ciphertext)) as bot:
        await bot.send_message(chat_id, text)


def _link_for(bot_record, telegram_user_id):
    return TelegramUserLink.objects.select_related("user").get(
        bot=bot_record,
        telegram_user_id=telegram_user_id,
        status=TelegramUserLink.Status.ACTIVE,
        user__is_active=True,
    )


def _content_for(link, code: str):
    return Content.objects.get(owner_user=link.user, code=code.strip(), deleted_at__isnull=True)


def _assert_content_permission(content, permission: str):
    channel_ids = set(content.channels.values_list("id", flat=True))
    allowed_ids = set(
        UserChannelAccess.objects.filter(
            user=content.owner_user,
            channel_id__in=channel_ids,
            **{permission: True},
        ).values_list("channel_id", flat=True)
    )
    if not channel_ids or channel_ids != allowed_ids:
        raise PermissionError("当前账号没有该内容全部频道的操作权限")


def _enqueue_from_telegram(*, content, action, telegram_user_id, bot_record, update_id, marker="", request_json=None):
    required_permission = {
        Operation.Action.SEND: "can_publish",
        Operation.Action.DELETE: "can_delete",
        Operation.Action.SYNC_TEXT: "can_edit",
    }.get(action, "can_edit")
    _assert_content_permission(content, required_permission)
    operation, created = enqueue_content_operation(
        content=content,
        action=action,
        actor=telegram_user_id,
        source=Operation.Source.TELEGRAM,
        request_json={"telegram_update_id": update_id, **(request_json or {})},
        idempotency_key=marker or f"telegram:{bot_record.pk}:{update_id}",
    )
    if created:
        audit_event(
            request=None,
            actor=telegram_user_id,
            owner_user=content.owner_user,
            action="operation.enqueue",
            object_type="operation",
            object_id=str(operation.pk),
            outcome="ACCEPTED",
            operation=operation,
            source="TELEGRAM",
            actor_type="TELEGRAM_USER",
        )
    return operation


def _create_pending(*, bot_record, link, content, action, telegram_user_id, update_id):
    TelegramPendingAction.objects.filter(
        bot=bot_record,
        telegram_user_id=telegram_user_id,
        status=TelegramPendingAction.Status.PENDING,
        expires_at__lte=timezone.now(),
    ).update(status=TelegramPendingAction.Status.EXPIRED)
    token = secrets.token_hex(3).upper()
    pending = TelegramPendingAction.objects.create(
        bot=bot_record,
        owner_user=link.user,
        telegram_user_id=telegram_user_id,
        content=content,
        action=action,
        token=token,
        payload_json={"telegram_update_id": update_id},
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    audit_event(
        request=None,
        actor=telegram_user_id,
        owner_user=link.user,
        action="telegram.operation.pending_confirmation",
        object_type="telegram_pending_action",
        object_id=str(pending.pk),
        outcome="ACCEPTED",
        after={"action": action, "content_id": content.pk, "expires_at": pending.expires_at.isoformat()},
        source="TELEGRAM",
        actor_type="TELEGRAM_USER",
    )
    return pending


def _handle_command(*, bot_record, update_id: int, chat_id: int, telegram_user_id: int, text: str) -> str:
    if text == "查询ID":
        return f"你的 Telegram 数字 ID：{telegram_user_id}"

    if text in {"/取消", "/cancel"}:
        pending = TelegramPendingAction.objects.filter(
            bot=bot_record,
            telegram_user_id=telegram_user_id,
            status=TelegramPendingAction.Status.PENDING,
        ).first()
        if not pending:
            return "没有等待确认的操作。"
        pending.status = TelegramPendingAction.Status.CANCELLED
        pending.save(update_fields=("status", "updated_at"))
        audit_event(
            request=None,
            actor=telegram_user_id,
            owner_user=pending.owner_user,
            action="telegram.operation.cancel",
            object_type="telegram_pending_action",
            object_id=str(pending.pk),
            source="TELEGRAM",
            actor_type="TELEGRAM_USER",
        )
        return "操作已取消。"

    if text.startswith("/确认"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            return "格式：/确认 确认码"
        link = _link_for(bot_record, telegram_user_id)
        with transaction.atomic():
            pending = TelegramPendingAction.objects.select_for_update().get(
                bot=bot_record,
                owner_user=link.user,
                telegram_user_id=telegram_user_id,
                token=parts[1].strip().upper(),
                status=TelegramPendingAction.Status.PENDING,
            )
            if pending.expires_at <= timezone.now():
                pending.status = TelegramPendingAction.Status.EXPIRED
                pending.save(update_fields=("status", "updated_at"))
                return "确认码已过期，请重新发送操作命令。"
            if pending.action == Operation.Action.COPY:
                pending.status = TelegramPendingAction.Status.CANCELLED
                pending.save(update_fields=("status", "updated_at"))
                return "该操作已下线。"
            operation = _enqueue_from_telegram(
                content=pending.content,
                action=pending.action,
                telegram_user_id=telegram_user_id,
                bot_record=bot_record,
                update_id=update_id,
                marker=f"confirm:{pending.token}",
            )
            if pending.action == Operation.Action.DELETE:
                pending.content.status = Content.Status.UNPUBLISHING
                pending.content.save(update_fields=("status", "updated_at"))
            pending.status = TelegramPendingAction.Status.CONFIRMED
            pending.save(update_fields=("status", "updated_at"))
            audit_event(
                request=None,
                actor=telegram_user_id,
                owner_user=link.user,
                action="telegram.operation.confirm",
                object_type="telegram_pending_action",
                object_id=str(pending.pk),
                operation=operation,
                source="TELEGRAM",
                actor_type="TELEGRAM_USER",
            )
        return f"已确认，任务已加入队列：{operation.id}"

    if text.startswith("/编辑"):
        parts = text.split(maxsplit=2)
        if len(parts) != 3:
            return "格式：/编辑 编号 新文字"
        link = _link_for(bot_record, telegram_user_id)
        content = _content_for(link, parts[1])
        _assert_content_permission(content, "can_edit")
        before = {"text": content.text, "version": content.version}
        content.text = parts[2]
        content.version += 1
        content.updated_by = link.user
        content.save(update_fields=("text", "version", "updated_by", "updated_at"))
        operation = None
        if content.status in {Content.Status.PUBLISHED, Content.Status.PARTIAL}:
            operation = _enqueue_from_telegram(
                content=content,
                action=Operation.Action.REPLACE,
                telegram_user_id=telegram_user_id,
                bot_record=bot_record,
                update_id=update_id,
                request_json={"force_resend": True, "reason": "TELEGRAM_CONTENT_EDIT"},
            )
        audit_event(
            request=None,
            actor=telegram_user_id,
            owner_user=link.user,
            action="content.text.update",
            object_type="content",
            object_id=str(content.pk),
            before=before,
            after={"text": content.text, "version": content.version},
            operation=operation,
            source="TELEGRAM",
            actor_type="TELEGRAM_USER",
        )
        return "文字已更新并加入替换发布队列；新消息成功后会删除旧消息。" if operation else "文字已更新；内容尚未上架。"

    if text.startswith("/状态"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            return "格式：/状态 编号"
        link = _link_for(bot_record, telegram_user_id)
        content = _content_for(link, parts[1])
        latest = content.owner_user.operations.filter(target_type="content", target_id=str(content.pk)).first()
        suffix = f"，最近任务：{latest.get_state_display()}" if latest else ""
        return f"{content.title}：{content.get_status_display()}{suffix}"

    if text.startswith(("/上架", "/下架")):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            return "格式：/上架 编号 或 /下架 编号"
        link = _link_for(bot_record, telegram_user_id)
        content = _content_for(link, parts[1])
        command_to_action = {
            "/上架": Operation.Action.SEND,
            "/下架": Operation.Action.DELETE,
        }
        action = command_to_action[parts[0]]
        if action == Operation.Action.SEND and content.status not in {Content.Status.DRAFT, Content.Status.UNPUBLISHED}:
            return "只有草稿或已下架内容可以上架。"
        if action == Operation.Action.DELETE and content.status not in {Content.Status.PUBLISHED, Content.Status.PARTIAL}:
            return "该操作要求内容已经上架。"
        if action == Operation.Action.DELETE:
            _assert_content_permission(content, "can_delete")
            pending = _create_pending(
                bot_record=bot_record,
                link=link,
                content=content,
                action=action,
                telegram_user_id=telegram_user_id,
                update_id=update_id,
            )
            return f"即将{pending.get_action_display()} {content.title}。5 分钟内发送：/确认 {pending.token}"
        operation = _enqueue_from_telegram(
            content=content,
            action=action,
            telegram_user_id=telegram_user_id,
            bot_record=bot_record,
            update_id=update_id,
        )
        return f"任务已加入队列：{operation.id}"

    return "可用命令：查询ID、/上架、/下架、/编辑、/状态、/确认、/取消"


@csrf_exempt
@require_POST
def telegram_webhook(request, public_id):
    bot_record = get_object_or_404(TelegramBot, public_id=public_id, status=TelegramBot.Status.ACTIVE)
    expected = decrypt_secret(bot_record.webhook_secret_ciphertext)
    provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if expected and not hmac.compare_digest(expected, provided):
        return HttpResponseForbidden("invalid webhook secret")
    try:
        body = json.loads(request.body.decode("utf-8"))
        update_id = int(body["update_id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "invalid update"}, status=400)

    update, created = TelegramUpdate.objects.get_or_create(bot=bot_record, update_id=update_id, defaults={"body_json": body})
    if not created:
        return JsonResponse({"ok": True, "duplicate": True})

    message = body.get("message") or {}
    text = (message.get("text") or "").strip()
    chat_id = message.get("chat", {}).get("id")
    telegram_user_id = message.get("from", {}).get("id")
    chat_type = message.get("chat", {}).get("type")
    try:
        if chat_id and telegram_user_id and text and chat_type == "private":
            reply_text = _handle_command(
                bot_record=bot_record,
                update_id=update_id,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                text=text,
            )
            async_to_sync(_reply)(bot_record, chat_id, reply_text)
        update.state = TelegramUpdate.State.PROCESSED
    except Exception as exc:
        update.state = TelegramUpdate.State.FAILED
        audit_event(
            request=None,
            actor=telegram_user_id or "unknown",
            owner_user=None,
            action="telegram.command.failed",
            object_type="telegram_update",
            object_id=str(update_id),
            outcome="FAILED",
            source="TELEGRAM",
            actor_type="TELEGRAM_USER",
            error_code=exc.__class__.__name__,
        )
        if chat_id:
            async_to_sync(_reply)(bot_record, chat_id, f"操作失败：{exc.__class__.__name__}")
    update.processed_at = timezone.now()
    update.save(update_fields=("state", "processed_at"))
    return JsonResponse({"ok": True})
