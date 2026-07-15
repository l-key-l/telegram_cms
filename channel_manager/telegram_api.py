from __future__ import annotations

import re
from urllib.parse import urlparse

from aiogram.enums import ChatMemberStatus
from asgiref.sync import async_to_sync
from django.utils import timezone

from .models import BotChannelBinding, TelegramBot, TelegramChannel, TelegramUpdate
from .security import decrypt_secret
from .telegram_client import create_bot


class ChannelReferenceError(ValueError):
    pass


def _parse_channel_reference(reference: str) -> tuple[str, int | str | None]:
    value = reference.strip().rstrip("/")
    if re.fullmatch(r"-100\d+", value):
        return "id", int(value)
    if re.fullmatch(r"@[A-Za-z0-9_]{5,}", value):
        return "public", value
    if not re.match(r"^https?://", value, flags=re.I) and (value.startswith("t.me/") or value.startswith("telegram.me/")):
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.hostname and parsed.hostname.lower() in {"t.me", "telegram.me", "telegram.dog"}:
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ChannelReferenceError("频道链接缺少频道标识。")
        if parts[0] == "c" and len(parts) >= 2 and parts[1].isdigit():
            return "id", int(f"-100{parts[1]}")
        if parts[0].startswith("+") or parts[0] == "joinchat":
            return "private_invite", None
        if parts[0] == "s" and len(parts) >= 2:
            return "public", f"@{parts[1]}"
        if re.fullmatch(r"[A-Za-z0-9_]{5,}", parts[0]):
            return "public", f"@{parts[0]}"
    if re.fullmatch(r"[A-Za-z0-9_]{5,}", value):
        return "public", f"@{value}"
    raise ChannelReferenceError("链接格式不受支持，请填写公开频道链接、/c/ 消息链接、邀请链接或 -100 Chat ID。")


async def _get_chat_by_reference(bot_record: TelegramBot, chat_reference: int | str):
    async with create_bot(decrypt_secret(bot_record.token_ciphertext)) as client:
        return await client.get_chat(chat_reference)


def _observed_channel_candidates(bot_record: TelegramBot) -> list[dict]:
    candidates = {}
    for update in TelegramUpdate.objects.filter(bot=bot_record).order_by("-received_at")[:300]:
        body = update.body_json or {}
        chats = []
        for key in ("channel_post", "edited_channel_post", "my_chat_member", "chat_member"):
            chat = (body.get(key) or {}).get("chat")
            if chat:
                chats.append(chat)
        message = body.get("message") or {}
        origin = message.get("forward_origin") or {}
        if origin.get("type") == "channel" and origin.get("chat"):
            chats.append(origin["chat"])
        for chat in chats:
            chat_id = chat.get("id")
            if chat_id and str(chat_id).startswith("-100"):
                candidates.setdefault(
                    int(chat_id),
                    {
                        "chat_id": int(chat_id),
                        "title": chat.get("title") or "",
                        "username": chat.get("username") or "",
                        "source": "telegram_update",
                    },
                )
    return list(candidates.values())


def resolve_channel_reference(bot_record: TelegramBot, reference: str) -> dict:
    kind, parsed_reference = _parse_channel_reference(reference)
    if kind == "private_invite":
        candidates = _observed_channel_candidates(bot_record)
        registered_ids = set(TelegramChannel.objects.values_list("telegram_chat_id", flat=True))
        unregistered = [item for item in candidates if item["chat_id"] not in registered_ids]
        if len(unregistered) == 1:
            return unregistered[0]
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            summary = "；".join(f"{item['title'] or '未命名'}={item['chat_id']}" for item in candidates[:10])
            raise ChannelReferenceError(f"邀请链接本身不含 Chat ID；检测到多个 Bot 已见频道：{summary}")
        raise ChannelReferenceError("邀请链接本身不含 Chat ID。先把该 Bot 设为频道管理员并在频道发送一条新消息，然后重新解析。")
    if kind == "id" and isinstance(parsed_reference, int):
        try:
            chat = async_to_sync(_get_chat_by_reference)(bot_record, parsed_reference)
            return {
                "chat_id": int(chat.id),
                "title": getattr(chat, "title", "") or "",
                "username": getattr(chat, "username", "") or "",
                "source": "telegram_api",
            }
        except Exception:
            return {"chat_id": parsed_reference, "title": "", "username": "", "source": "link_formula"}
    chat = async_to_sync(_get_chat_by_reference)(bot_record, parsed_reference)
    return {
        "chat_id": int(chat.id),
        "title": getattr(chat, "title", "") or "",
        "username": getattr(chat, "username", "") or "",
        "source": "telegram_api",
    }


async def _get_me(bot_record: TelegramBot):
    async with create_bot(decrypt_secret(bot_record.token_ciphertext)) as client:
        return await client.get_me()


async def _set_webhook(bot_record: TelegramBot, url: str):
    async with create_bot(decrypt_secret(bot_record.token_ciphertext)) as client:
        return await client.set_webhook(
            url=url,
            secret_token=decrypt_secret(bot_record.webhook_secret_ciphertext),
            allowed_updates=["message", "channel_post", "edited_channel_post", "my_chat_member"],
        )


async def _delete_webhook(bot_record: TelegramBot):
    async with create_bot(decrypt_secret(bot_record.token_ciphertext)) as client:
        return await client.delete_webhook(drop_pending_updates=False)


def verify_bot(bot_record: TelegramBot):
    me = async_to_sync(_get_me)(bot_record)
    bot_record.telegram_bot_id = me.id
    bot_record.username = me.username or ""
    bot_record.status = TelegramBot.Status.ACTIVE
    bot_record.last_verified_at = timezone.now()
    bot_record.save(update_fields=("telegram_bot_id", "username", "status", "last_verified_at", "updated_at"))
    return me


def register_webhook(bot_record: TelegramBot, public_base_url: str):
    url = f"{public_base_url.rstrip('/')}/telegram/webhook/{bot_record.public_id}"
    async_to_sync(_set_webhook)(bot_record, url)
    return url


def unregister_webhook(bot_record: TelegramBot):
    return async_to_sync(_delete_webhook)(bot_record)


async def _verify_binding(binding: BotChannelBinding):
    async with create_bot(decrypt_secret(binding.bot.token_ciphertext)) as client:
        me = await client.get_me()
        chat = await client.get_chat(binding.channel.telegram_chat_id)
        member = await client.get_chat_member(binding.channel.telegram_chat_id, me.id)
        return me, chat, member


def verify_binding(binding: BotChannelBinding):
    # 先在同步上下文缓存关联对象，避免 async_to_sync 内触发同步 ORM 查询。
    _ = binding.bot
    _ = binding.channel
    me, chat, member = async_to_sync(_verify_binding)(binding)
    is_admin = member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
    binding.can_post_messages = bool(is_admin and getattr(member, "can_post_messages", True))
    binding.can_edit_messages = bool(is_admin and getattr(member, "can_edit_messages", False))
    binding.can_delete_messages = bool(is_admin and getattr(member, "can_delete_messages", False))
    binding.can_change_info = bool(is_admin and getattr(member, "can_change_info", False))
    binding.rights_json = {
        "status": str(member.status),
        "can_post_messages": binding.can_post_messages,
        "can_edit_messages": binding.can_edit_messages,
        "can_delete_messages": binding.can_delete_messages,
        "can_change_info": binding.can_change_info,
    }
    binding.status = BotChannelBinding.Status.ACTIVE if is_admin else BotChannelBinding.Status.DEGRADED
    binding.last_verified_at = timezone.now()
    binding.save()

    channel = binding.channel
    channel.title = getattr(chat, "title", None) or channel.title
    channel.username = getattr(chat, "username", None) or channel.username
    channel.description = getattr(chat, "description", None) or channel.description
    channel.status = TelegramChannel.Status.ACTIVE if is_admin else TelegramChannel.Status.DEGRADED
    channel.save(update_fields=("title", "username", "description", "status", "updated_at"))
    return me, chat, member
