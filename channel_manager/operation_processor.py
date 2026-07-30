from __future__ import annotations

from datetime import datetime, timedelta

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo
from asgiref.sync import async_to_sync
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .audit import audit_event
from .models import (
    BotChannelBinding,
    Content,
    ContentFile,
    Delivery,
    DeliveryMessage,
    Operation,
    TelegramChannel,
    UserChannelAccess,
)
from .security import decrypt_secret
from .services import render_content_text, telegram_media_source_path
from .task_utils import stop_scheduled_task
from .telegram_api import verify_binding
from .telegram_client import create_bot


class AtomicSendRollbackError(RuntimeError):
    """两阶段发送失败且补偿删除未完整完成，远端状态需要人工核对。"""


def _available_bindings(channel, capability: str) -> list[BotChannelBinding]:
    return list(
        channel.bot_bindings.filter(
            status=BotChannelBinding.Status.ACTIVE,
            bot__status="ACTIVE",
            **{capability: True},
        )
        .select_related("bot", "channel")
        .order_by("priority")
    )


def resolve_bindings(channel, capability: str = "can_post_messages") -> list[BotChannelBinding]:
    bindings = _available_bindings(channel, capability)
    verification_errors = []
    if not bindings:
        unverified = list(
            channel.bot_bindings.filter(
                status=BotChannelBinding.Status.ACTIVE,
                bot__status="ACTIVE",
                last_verified_at__isnull=True,
            )
            .select_related("bot", "channel")
            .order_by("priority")
        )
        for binding in unverified:
            try:
                verify_binding(binding)
            except Exception as exc:
                verification_errors.append(f"{binding.bot.display_name}: {exc}")
        bindings = _available_bindings(channel, capability)
    if not bindings:
        detail = f" 自动验证失败：{'；'.join(verification_errors)}" if verification_errors else ""
        raise RuntimeError(
            f"频道 {channel.title} 没有具备 {capability} 权限的 Bot 绑定，"
            f"请在主后台完成 Bot ↔ 频道绑定并验证权限。{detail}"
        )
    if channel.route_policy == TelegramChannel.RoutePolicy.EXPLICIT:
        raise RuntimeError("该频道仍是旧的 EXPLICIT 路由策略，请在主后台改为主 Bot 或安全故障切换。")
    if channel.route_policy != TelegramChannel.RoutePolicy.FAILOVER:
        return bindings[:1]
    return bindings


def resolve_binding(channel, capability: str = "can_post_messages") -> BotChannelBinding:
    return resolve_bindings(channel, capability)[0]


def _operation_channel_ids(operation: Operation, content: Content) -> set[int]:
    raw_ids = operation.request_json.get("channel_ids")
    if isinstance(raw_ids, list):
        try:
            return {int(channel_id) for channel_id in raw_ids}
        except (TypeError, ValueError):
            pass
    return set(content.channels.values_list("id", flat=True))


def _assert_operation_permission(operation: Operation, content: Content):
    permission = {
        Operation.Action.SEND: "can_publish",
        Operation.Action.DELETE: "can_delete",
        Operation.Action.REPLACE: "can_edit",
        Operation.Action.EDIT: "can_edit",
        Operation.Action.SYNC_TEXT: "can_edit",
    }.get(operation.action)
    if not permission:
        return
    channel_ids = _operation_channel_ids(operation, content)
    allowed_ids = set(
        UserChannelAccess.objects.filter(
            user=content.owner_user,
            channel_id__in=channel_ids,
            **{permission: True},
        ).values_list("channel_id", flat=True)
    )
    if not channel_ids or channel_ids != allowed_ids:
        raise PermissionError("任务执行前权限复核失败")


def _prepare_media_paths(content: Content, files: list[ContentFile]) -> dict[int, str]:
    """在同步 Worker 上完成图片预处理和 ORM 保存，异步 Telegram 调用只读本地路径。"""
    return {
        item.pk: telegram_media_source_path(item, anti_scan_enabled=content.anti_scan_enabled)
        for item in files
    }


async def _send_to_telegram(
    binding: BotChannelBinding,
    content: Content,
    files: list[ContentFile],
    media_paths: dict[int, str],
):
    token = decrypt_secret(binding.bot.token_ciphertext)
    results = []
    async with create_bot(token) as bot:
        primary = [item for item in files if item.group_no == 1]
        secondary = [item for item in files if item.group_no == 2]
        if not secondary:
            raise RuntimeError("第二次发送缺少媒体文件。")
        if any(item.media_type not in {ContentFile.MediaType.PHOTO, ContentFile.MediaType.VIDEO} for item in files):
            raise RuntimeError("两阶段发送只支持图片或视频。")
        rendered_text = render_content_text(content)
        try:
            if not primary:
                message = await bot.send_message(binding.channel.telegram_chat_id, rendered_text or content.title)
                results.append((message, 1, 0, "text"))

            for group_no, group in ((1, primary), (2, secondary)):
                if not group:
                    continue
                if len(group) == 1:
                    item = group[0]
                    caption = rendered_text[:1024] if group_no == 1 else None
                    source_path = media_paths[item.pk]
                    file_input = FSInputFile(source_path)
                    if item.media_type == ContentFile.MediaType.PHOTO:
                        message = await bot.send_photo(binding.channel.telegram_chat_id, file_input, caption=caption)
                        kind = "photo"
                    elif item.media_type == ContentFile.MediaType.VIDEO:
                        message = await bot.send_video(binding.channel.telegram_chat_id, file_input, caption=caption)
                        kind = "video"
                    else:
                        message = await bot.send_document(binding.channel.telegram_chat_id, file_input, caption=caption)
                        kind = "document"
                    results.append((message, group_no, item.sort_index, kind))
                else:
                    media = []
                    for index, item in enumerate(group):
                        caption = rendered_text[:1024] if group_no == 1 and index == 0 else None
                        source_path = media_paths[item.pk]
                        source = FSInputFile(source_path)
                        media.append(InputMediaPhoto(media=source, caption=caption) if item.media_type == ContentFile.MediaType.PHOTO else InputMediaVideo(media=source, caption=caption))
                    messages = await bot.send_media_group(binding.channel.telegram_chat_id, media=media)
                    for item, message in zip(group, messages):
                        results.append((message, group_no, item.sort_index, "album_media"))
        except Exception as original:
            cleanup_errors = await _rollback_messages_with_bot(
                bot,
                binding.channel.telegram_chat_id,
                [message.message_id for message, *_ in results],
            )
            if cleanup_errors:
                raise AtomicSendRollbackError(
                    f"两阶段发送失败，且补偿删除未完整完成：{original}；{'；'.join(cleanup_errors)}"
                ) from original
            raise
    return results


async def _rollback_messages_with_bot(bot, channel_id: int, message_ids: list[int]) -> list[str]:
    errors = []
    for message_id in reversed(message_ids):
        try:
            await bot.delete_message(channel_id, message_id)
        except TelegramBadRequest as exc:
            if "message to delete not found" not in str(exc).lower():
                errors.append(f"message_id={message_id}: {exc}")
        except Exception as exc:
            errors.append(f"message_id={message_id}: {exc}")
    return errors


async def _rollback_sent_messages(binding: BotChannelBinding, results) -> list[str]:
    async with create_bot(decrypt_secret(binding.bot.token_ciphertext)) as bot:
        return await _rollback_messages_with_bot(
            bot,
            binding.channel.telegram_chat_id,
            [message.message_id for message, *_ in results],
        )


async def _delete_delivery(binding: BotChannelBinding, channel_id: int, message_items: list[tuple[int, int]]):
    token = decrypt_secret(binding.bot.token_ciphertext)
    deleted, retained, failed = [], [], []
    async with create_bot(token) as bot:
        for item_pk, telegram_message_id in message_items:
            try:
                await bot.delete_message(channel_id, telegram_message_id)
                deleted.append(item_pk)
            except TelegramBadRequest as exc:
                error = str(exc)
                if "message to delete not found" in error.lower():
                    deleted.append(item_pk)
                elif "message can't be deleted" in error.lower():
                    retained.append((item_pk, error))
                else:
                    failed.append((item_pk, error))
            except TelegramNetworkError as exc:
                failed.append((item_pk, f"NETWORK_UNCERTAIN: {exc}"))
    return deleted, retained, failed


async def _sync_delivery_text(binding: BotChannelBinding, channel_id: int, first: dict | None, text: str):
    token = decrypt_secret(binding.bot.token_ciphertext)
    if not first:
        return False
    kind = first["payload_snapshot_json"].get("kind", "")
    async with create_bot(token) as bot:
        try:
            if kind == "text":
                await bot.edit_message_text(text=text[:4096], chat_id=channel_id, message_id=first["telegram_message_id"])
            elif kind in {"photo", "video", "document", "album_media"}:
                await bot.edit_message_caption(caption=text[:1024], chat_id=channel_id, message_id=first["telegram_message_id"])
            else:
                return False
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
    return True


async def _update_channel_info(binding: BotChannelBinding, payload: dict):
    token = decrypt_secret(binding.bot.token_ciphertext)
    chat_id = binding.channel.telegram_chat_id
    succeeded = 0
    errors = []
    async with create_bot(token) as bot:
        actions = [
            ("title", lambda: bot.set_chat_title(chat_id=chat_id, title=payload["title"])),
            ("description", lambda: bot.set_chat_description(chat_id=chat_id, description=payload.get("description", ""))),
        ]
        if payload.get("remove_photo"):
            actions.append(("photo", lambda: bot.delete_chat_photo(chat_id=chat_id)))
        elif payload.get("photo_path"):
            actions.append(("photo", lambda: bot.set_chat_photo(chat_id=chat_id, photo=FSInputFile(payload["photo_path"]))))
        for label, action in actions:
            try:
                await action()
                succeeded += 1
            except Exception as exc:
                errors.append(f"{label}: {exc}")
    return succeeded, errors


async def _operate_external_message(binding: BotChannelBinding, payload: dict):
    token = decrypt_secret(binding.bot.token_ciphertext)
    chat_id = binding.channel.telegram_chat_id
    message_id = int(payload["telegram_message_id"])
    async with create_bot(token) as bot:
        if payload["mode"] == "DELETE":
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except TelegramBadRequest as exc:
                if "message to delete not found" not in str(exc).lower():
                    raise
        elif payload["mode"] == "EDIT_CAPTION":
            await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=payload["text"][:1024])
        else:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=payload["text"][:4096])


async def _edit_delivery_media(
    binding: BotChannelBinding,
    channel_id: int,
    edits: list[tuple[int, ContentFile, bool, str]],
    text: str,
):
    token = decrypt_secret(binding.bot.token_ciphertext)
    results = []
    async with create_bot(token) as bot:
        for message_id, item, include_caption, source_path in edits:
            source = FSInputFile(source_path)
            caption = text[:1024] if include_caption else None
            if item.media_type == ContentFile.MediaType.PHOTO:
                media = InputMediaPhoto(media=source, caption=caption)
            elif item.media_type == ContentFile.MediaType.VIDEO:
                media = InputMediaVideo(media=source, caption=caption)
            else:
                media = InputMediaDocument(media=source, caption=caption)
            message = await bot.edit_message_media(chat_id=channel_id, message_id=message_id, media=media)
            results.append((item, message))
    return results


def _record_delivery(operation: Operation, content: Content, channel, binding, results, generation: int):
    now = timezone.now()
    delivery = Delivery.objects.create(
        content=content,
        channel=channel,
        bot_channel_binding=binding,
        bot_id_snapshot=binding.bot.telegram_bot_id,
        operation=operation,
        generation=generation,
        status=Delivery.Status.ACTIVE,
        content_version=content.version,
        sent_at=now,
        deletable_until=now + timedelta(hours=48),
    )
    content_files = {(item.group_no, item.sort_index): item for item in content.files.all()}
    for message, group_no, sort_index, kind in results:
        DeliveryMessage.objects.create(
            delivery=delivery,
            group_no=group_no,
            sort_index=sort_index,
            telegram_message_id=message.message_id,
            sent_by_bot_id=binding.bot.telegram_bot_id,
            media_group_id=getattr(message, "media_group_id", None) or "",
            payload_snapshot_json={"kind": kind, "content_version": content.version},
        )
        content_file = content_files.get((group_no, sort_index))
        if content_file:
            telegram_file_id = ""
            if getattr(message, "photo", None):
                telegram_file_id = message.photo[-1].file_id
            elif getattr(message, "video", None):
                telegram_file_id = message.video.file_id
            elif getattr(message, "document", None):
                telegram_file_id = message.document.file_id
            if telegram_file_id:
                file_ids = dict(content_file.telegram_file_ids_json)
                file_ids[str(binding.bot_id)] = telegram_file_id
                content_file.telegram_file_ids_json = file_ids
                content_file.save(update_fields=("telegram_file_ids_json", "updated_at"))
    return delivery


def _send_channel_with_failover(
    channel,
    content: Content,
    files: list[ContentFile],
    media_paths: dict[int, str],
):
    errors = []
    for binding in resolve_bindings(channel):
        if not binding.can_delete_messages:
            errors.append(f"{binding.bot.display_name}: 两阶段原子发送要求 Bot 同时具备删除消息权限")
            continue
        try:
            results = async_to_sync(_send_to_telegram)(binding, content, files, media_paths)
            return binding, results
        except (AtomicSendRollbackError, TelegramNetworkError, TelegramRetryAfter):
            # 发送结果不确定或 Telegram 要求等待时绝不切换 Bot，避免重复消息。
            raise
        except (TelegramUnauthorizedError, TelegramForbiddenError, TelegramBadRequest) as exc:
            errors.append(f"{binding.bot.display_name}: {exc}")
    raise RuntimeError("；".join(errors) or f"频道 {channel.title} 没有可用 Bot。")


def _process_send(operation: Operation, content: Content):
    channel_ids = _operation_channel_ids(operation, content)
    channels = list(TelegramChannel.objects.filter(pk__in=channel_ids))
    if not channels:
        raise RuntimeError("内容没有选择发送频道。")
    files = list(content.files.all())
    if not any(item.group_no == 2 for item in files):
        raise RuntimeError("第二次发送缺少媒体文件，内容不会投递。")
    succeeded, failed = 0, []
    media_paths = _prepare_media_paths(content, files)
    generation = (content.deliveries.aggregate(value=Max("generation"))["value"] or 0) + 1
    for channel in channels:
        try:
            binding, results = _send_channel_with_failover(channel, content, files, media_paths)
            try:
                with transaction.atomic():
                    _record_delivery(operation, content, channel, binding, results, generation)
            except Exception as record_error:
                cleanup_errors = async_to_sync(_rollback_sent_messages)(binding, results)
                if cleanup_errors:
                    raise AtomicSendRollbackError(
                        f"Telegram 已发送但本地记录失败，补偿删除未完整完成：{record_error}；{'；'.join(cleanup_errors)}"
                    ) from record_error
                raise
            succeeded += 1
        except (AtomicSendRollbackError, TelegramNetworkError):
            raise
        except Exception as exc:
            failed.append(f"{channel.title}: {exc}")
    if succeeded:
        content.status = Content.Status.PUBLISHED
        if content.cycle_days and content.cycle_time:
            local_now = timezone.localtime()
            next_date = local_now.date() + timedelta(days=content.cycle_days)
            content.next_run_at = timezone.make_aware(
                datetime.combine(next_date, content.cycle_time),
                timezone.get_current_timezone(),
            )
        content.save(update_fields=("status", "next_run_at", "updated_at"))
    if failed and succeeded:
        operation.state = Operation.State.PARTIAL
        operation.telegram_error_text = "；".join(failed)
    elif failed:
        raise RuntimeError("；".join(failed))
    else:
        operation.state = Operation.State.SUCCEEDED


def _process_delete(operation: Operation, content: Content):
    # 网页和 Telegram 指令入队时会先停任务；Worker 再兜底一次，兼容历史队列和直接入队。
    stop_scheduled_task(
        request=None,
        actor="operation-worker",
        content=content,
        reason="CONTENT_UNPUBLISH",
        source=Operation.Source.SYSTEM,
        actor_type=Operation.ActorType.SYSTEM,
    )
    deliveries = content.deliveries.filter(
        status__in=(Delivery.Status.ACTIVE, Delivery.Status.PARTIAL)
    ).select_related("bot_channel_binding__bot", "channel")
    failed = []
    for delivery in deliveries:
        delivery_errors, _ = _delete_delivery_record(delivery)
        failed.extend(delivery_errors)

    # 网站下架以停止业务和循环为准。远端仍待人工清理的记录统一收敛，
    # 以后不再进入循环替换或重复删除。
    retryable_deliveries = content.deliveries.filter(
        status__in=(Delivery.Status.ACTIVE, Delivery.Status.PARTIAL)
    )
    for delivery in retryable_deliveries:
        _mark_delivery_retained(delivery)

    retained_exists = content.deliveries.filter(status=Delivery.Status.RETAINED).exists()
    if retained_exists:
        content.status = Content.Status.UNPUBLISHED_RETAINED
        operation.telegram_error_code = "UNPUBLISH_MANUAL_DELETE"
        summary = "频道消息需要手动删除，循环任务已停止。"
        operation.telegram_error_text = (
            f"{summary} 技术详情：{'；'.join(failed)}" if failed else summary
        )
    else:
        content.status = Content.Status.UNPUBLISHED
        operation.telegram_error_code = "UNPUBLISH_DELETED"
        operation.telegram_error_text = "下架成功，频道消息已删除，循环任务已停止。"
    content.save(update_fields=("status", "updated_at"))
    operation.state = Operation.State.SUCCEEDED


def _process_sync(operation: Operation, content: Content):
    deliveries = content.deliveries.filter(status=Delivery.Status.ACTIVE).select_related("bot_channel_binding__bot", "channel")
    failed = []
    updated = 0
    for delivery in deliveries:
        try:
            first = delivery.messages.order_by("group_no", "sort_index", "id").values("id", "telegram_message_id", "payload_snapshot_json").first()
            if async_to_sync(_sync_delivery_text)(delivery.bot_channel_binding, delivery.channel.telegram_chat_id, first, render_content_text(content)):
                DeliveryMessage.objects.filter(pk=first["id"]).update(status=DeliveryMessage.Status.EDITED, last_edited_at=timezone.now())
                updated += 1
        except Exception as exc:
            failed.append(f"{delivery.channel.title}: {exc}")
    if failed and updated:
        operation.state = Operation.State.PARTIAL
    elif failed:
        raise RuntimeError("；".join(failed))
    else:
        operation.state = Operation.State.SUCCEEDED
    operation.telegram_error_text = "；".join(failed)


def _delivery_delete_window_expired(delivery: Delivery) -> bool:
    deadline = delivery.deletable_until
    if not deadline and delivery.sent_at:
        deadline = delivery.sent_at + timedelta(hours=48)
    return bool(deadline and deadline <= timezone.now())


def _mark_delivery_retained(delivery: Delivery) -> int:
    retained_count = delivery.messages.exclude(status=DeliveryMessage.Status.DELETED).update(
        status=DeliveryMessage.Status.RETAINED
    )
    delivery.status = Delivery.Status.RETAINED
    delivery.save(update_fields=("status", "updated_at"))
    return retained_count


def _delete_delivery_record(delivery: Delivery) -> tuple[list[str], str]:
    if _delivery_delete_window_expired(delivery):
        retained_count = _mark_delivery_retained(delivery)
        return (
            [
                f"{delivery.channel.title}: 旧批次已超过 Telegram 48 小时删除窗口，"
                f"{retained_count} 条消息保留在频道，请手动清理"
            ],
            "DELETE_WINDOW_EXPIRED",
        )
    binding = delivery.bot_channel_binding
    if not binding:
        delivery.status = Delivery.Status.PARTIAL
        delivery.save(update_fields=("status", "updated_at"))
        return [f"{delivery.channel.title}: 缺少原始 Bot 绑定"], ""
    message_items = list(
        delivery.messages.exclude(
            status__in=(DeliveryMessage.Status.DELETED, DeliveryMessage.Status.RETAINED)
        )
        .order_by("group_no", "sort_index", "id")
        .values_list("pk", "telegram_message_id")
    )
    deleted, retained, failed = async_to_sync(_delete_delivery)(
        binding,
        delivery.channel.telegram_chat_id,
        message_items,
    )
    DeliveryMessage.objects.filter(pk__in=deleted).update(status=DeliveryMessage.Status.DELETED, deleted_at=timezone.now())
    DeliveryMessage.objects.filter(pk__in=[pk for pk, _ in retained]).update(
        status=DeliveryMessage.Status.RETAINED
    )
    retained_count = delivery.messages.filter(status=DeliveryMessage.Status.RETAINED).count()
    if failed:
        delivery.status = Delivery.Status.PARTIAL
    elif retained_count:
        delivery.status = Delivery.Status.RETAINED
    else:
        delivery.status = Delivery.Status.DELETED
    delivery.save(update_fields=("status", "updated_at"))
    errors = [f"{delivery.channel.title}/{pk}: {error}" for pk, error in failed]
    if retained_count:
        errors.append(
            f"{delivery.channel.title}: Telegram 拒绝删除 {retained_count} 条历史消息，"
            "已标记为远端保留，请手动清理"
        )
    return errors, "DELETE_REJECTED" if retained_count else ""


def _process_replace(operation: Operation, content: Content):
    if not operation.request_json.get("force_resend") and _try_in_place_media_replace(operation, content):
        return
    old_deliveries = list(
        content.deliveries.filter(status__in=(Delivery.Status.ACTIVE, Delivery.Status.PARTIAL))
        .exclude(operation=operation)
        .select_related("bot_channel_binding__bot", "channel")
    )
    _process_send(operation, content)
    successful_channel_ids = set(operation.deliveries.values_list("channel_id", flat=True))
    desired_channel_ids = _operation_channel_ids(operation, content)
    delete_errors = []
    delete_error_code = ""
    send_state = operation.state
    for old in old_deliveries:
        # 新内容发送失败的目标频道保留旧消息；已成功替换或已移除的频道才清理旧消息。
        if old.channel_id in desired_channel_ids and old.channel_id not in successful_channel_ids:
            continue
        delivery_errors, delivery_error_code = _delete_delivery_record(old)
        delete_errors.extend(delivery_errors)
        old.refresh_from_db(fields=("status",))
        if delivery_errors and old.status == Delivery.Status.PARTIAL:
            _mark_delivery_retained(old)
        if delivery_error_code == "DELETE_WINDOW_EXPIRED" or not delete_error_code:
            delete_error_code = delivery_error_code
        replacement = operation.deliveries.filter(channel_id=old.channel_id).first()
        if replacement and not replacement.replaces_delivery_id:
            replacement.replaces_delivery = old
            replacement.save(update_fields=("replaces_delivery",))
    if delete_errors:
        if send_state == Operation.State.SUCCEEDED:
            operation.state = Operation.State.SUCCEEDED
            operation.telegram_error_code = "REPLACE_SENT_OLD_RETAINED"
            summary = "新内容已发送，历史消息需要手动删除"
            if content.cycle_days and content.cycle_time and content.next_run_at:
                summary += "，循环任务继续"
            operation.telegram_error_text = f"{summary}。技术详情：{'；'.join(delete_errors)}"
        else:
            operation.state = Operation.State.PARTIAL
            current = [operation.telegram_error_text] if operation.telegram_error_text else []
            operation.telegram_error_text = "；".join(current + delete_errors)
            if delete_error_code:
                operation.telegram_error_code = delete_error_code


def _try_in_place_media_replace(operation: Operation, content: Content) -> bool:
    if len(content.text) > 1024:
        return False
    files = list(content.files.select_related("content").all())
    if not files:
        return False
    file_map = {(item.group_no, item.sort_index): item for item in files}
    media_paths = _prepare_media_paths(content, files)
    deliveries = list(
        content.deliveries.filter(status=Delivery.Status.ACTIVE)
        .select_related("bot_channel_binding__bot", "channel")
        .prefetch_related("messages")
    )
    if not deliveries:
        return False
    if set(content.channels.values_list("id", flat=True)) != {item.channel_id for item in deliveries}:
        return False
    plans = []
    for delivery in deliveries:
        binding = delivery.bot_channel_binding
        if not binding or not binding.can_edit_messages:
            return False
        media_messages = [
            message
            for message in delivery.messages.all()
            if message.payload_snapshot_json.get("kind") in {"photo", "video", "document", "album_media"}
            and message.status != DeliveryMessage.Status.DELETED
        ]
        message_map = {(message.group_no, message.sort_index): message for message in media_messages}
        if set(message_map) != set(file_map):
            return False
        first_key = min(file_map)
        edits = [
            (
                message_map[key].telegram_message_id,
                file_map[key],
                key == first_key,
                media_paths[file_map[key].pk],
            )
            for key in sorted(file_map)
        ]
        plans.append((delivery, binding, edits, media_messages))
    try:
        for delivery, binding, edits, media_messages in plans:
            edited_results = async_to_sync(_edit_delivery_media)(binding, delivery.channel.telegram_chat_id, edits, render_content_text(content))
            for content_file, message in edited_results:
                telegram_file_id = ""
                if getattr(message, "photo", None):
                    telegram_file_id = message.photo[-1].file_id
                elif getattr(message, "video", None):
                    telegram_file_id = message.video.file_id
                elif getattr(message, "document", None):
                    telegram_file_id = message.document.file_id
                if telegram_file_id:
                    file_ids = dict(content_file.telegram_file_ids_json)
                    file_ids[str(binding.bot_id)] = telegram_file_id
                    content_file.telegram_file_ids_json = file_ids
                    content_file.save(update_fields=("telegram_file_ids_json", "updated_at"))
            DeliveryMessage.objects.filter(pk__in=[item.pk for item in media_messages]).update(
                status=DeliveryMessage.Status.EDITED,
                last_edited_at=timezone.now(),
            )
            delivery.content_version = content.version
            delivery.save(update_fields=("content_version", "updated_at"))
    except (TelegramNetworkError, TelegramRetryAfter):
        raise
    except (TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError):
        return False
    operation.state = Operation.State.SUCCEEDED
    return True


def _process_channel_update(operation: Operation):
    channel = TelegramChannel.objects.get(pk=operation.target_id)
    binding = resolve_binding(channel, "can_change_info")
    succeeded, errors = async_to_sync(_update_channel_info)(binding, operation.request_json)
    channel.status = TelegramChannel.Status.DEGRADED if errors else TelegramChannel.Status.ACTIVE
    channel.save(update_fields=("status", "updated_at"))
    if errors and succeeded:
        operation.state = Operation.State.PARTIAL
        operation.telegram_error_text = "；".join(errors)
    elif errors:
        raise RuntimeError("；".join(errors))
    else:
        operation.state = Operation.State.SUCCEEDED


def _process_external_message(operation: Operation):
    payload = operation.request_json
    channel = TelegramChannel.objects.get(pk=payload["channel_id"])
    access_permission = "can_delete" if operation.action == Operation.Action.DELETE_EXTERNAL else "can_edit"
    if not UserChannelAccess.objects.filter(
        user=operation.owner_user,
        channel=channel,
        **{access_permission: True},
    ).exists():
        raise PermissionError("任务执行前频道权限复核失败")
    capability = "can_delete_messages" if operation.action == Operation.Action.DELETE_EXTERNAL else "can_edit_messages"
    binding = resolve_binding(channel, capability)
    async_to_sync(_operate_external_message)(binding, payload)
    operation.state = Operation.State.SUCCEEDED


def process_operation(operation: Operation):
    operation.state = Operation.State.RUNNING
    operation.started_at = timezone.now()
    operation.attempt_count += 1
    operation.save(update_fields=("state", "started_at", "attempt_count", "updated_at"))
    try:
        if operation.target_type == "content":
            content = Content.objects.get(pk=operation.target_id, owner_user=operation.owner_user)
            _assert_operation_permission(operation, content)
            scheduler_is_stopped = operation.source == Operation.Source.SCHEDULER and operation.action in {
                Operation.Action.SEND,
                Operation.Action.REPLACE,
            } and (
                content.status == Content.Status.UNPUBLISHING
                or not any((content.publish_at, content.cycle_days, content.cycle_time, content.next_run_at))
            )
            if scheduler_is_stopped:
                operation.state = Operation.State.CANCELLED
                operation.telegram_error_code = "TASK_STOPPED"
                operation.telegram_error_text = "定时或循环配置已清空，跳过本次自动执行"
            elif operation.action == Operation.Action.SEND:
                _process_send(operation, content)
            elif operation.action == Operation.Action.REPLACE:
                _process_replace(operation, content)
            elif operation.action == Operation.Action.DELETE:
                _process_delete(operation, content)
            elif operation.action in {Operation.Action.EDIT, Operation.Action.SYNC_TEXT}:
                _process_sync(operation, content)
            else:
                raise RuntimeError(f"暂不支持内容任务类型：{operation.action}")
        elif operation.target_type == "channel" and operation.action == Operation.Action.UPDATE_CHANNEL:
            _process_channel_update(operation)
        elif operation.target_type == "external_message" and operation.action in {
            Operation.Action.EDIT_EXTERNAL,
            Operation.Action.DELETE_EXTERNAL,
        }:
            _process_external_message(operation)
        else:
            raise RuntimeError(f"暂不支持任务目标/类型：{operation.target_type}/{operation.action}")
    except AtomicSendRollbackError as exc:
        operation.state = Operation.State.UNCERTAIN
        operation.telegram_error_code = "ATOMIC_ROLLBACK_INCOMPLETE"
        operation.telegram_error_text = str(exc)
    except TelegramRetryAfter as exc:
        operation.state = Operation.State.FAILED if operation.attempt_count >= 5 else Operation.State.QUEUED
        operation.available_at = timezone.now() + timedelta(seconds=exc.retry_after)
        operation.telegram_error_code = "RETRY_AFTER"
        operation.telegram_error_text = str(exc)
    except TelegramNetworkError as exc:
        if operation.action in {Operation.Action.SEND, Operation.Action.REPLACE, Operation.Action.DELETE_EXTERNAL}:
            operation.state = Operation.State.UNCERTAIN
        else:
            operation.state = Operation.State.FAILED if operation.attempt_count >= 5 else Operation.State.QUEUED
        operation.available_at = timezone.now() + timedelta(minutes=1)
        operation.telegram_error_code = "NETWORK_ERROR"
        operation.telegram_error_text = str(exc)
    except (TelegramUnauthorizedError, TelegramForbiddenError) as exc:
        operation.state = Operation.State.FAILED
        operation.telegram_error_code = exc.__class__.__name__
        operation.telegram_error_text = str(exc)
    except Exception as exc:
        operation.state = Operation.State.FAILED
        operation.telegram_error_code = exc.__class__.__name__
        operation.telegram_error_text = str(exc)

    if operation.state not in {Operation.State.QUEUED}:
        operation.finished_at = timezone.now()
    operation.locked_by = ""
    operation.locked_until = None
    operation.save()
    audit_event(
        request=None,
        actor="system",
        owner_user=operation.owner_user,
        action="operation.complete",
        object_type="operation",
        object_id=str(operation.pk),
        operation=operation,
        source="SYSTEM",
        actor_type="SYSTEM",
        outcome=("SUCCESS" if operation.state == Operation.State.SUCCEEDED else "PARTIAL" if operation.state == Operation.State.PARTIAL else "FAILED"),
        error_code=operation.telegram_error_code,
    )
    return operation
