from __future__ import annotations

import uuid

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from accounts.forms import ProfileForm

from .audit import audit_event
from .forms import ContentForm, ExternalMessageActionForm, TelegramAdminIdsForm
from .models import Content, ContentFile, Operation, TelegramUserLink, UserChannelAccess
from .permissions import sub_required
from .services import attach_files, enqueue_content_operation, enqueue_operation, next_content_identity, sync_content_channels


def _owned_content(request, pk: int):
    return get_object_or_404(Content, pk=pk, owner_user=request.user, deleted_at__isnull=True)


def _has_content_permission(user, content: Content, permission: str) -> bool:
    channel_ids = set(content.channels.values_list("id", flat=True))
    allowed_ids = set(
        UserChannelAccess.objects.filter(user=user, channel_id__in=channel_ids, **{permission: True})
        .values_list("channel_id", flat=True)
    )
    return bool(channel_ids) and channel_ids == allowed_ids


@sub_required
def contents(request):
    status = request.GET.get("status", "active")
    query = request.GET.get("q", "").strip()
    items = Content.objects.filter(owner_user=request.user, deleted_at__isnull=True).prefetch_related("files", "channels")
    if status == "unpublished":
        items = items.filter(status__in=(Content.Status.DRAFT, Content.Status.UNPUBLISHED, Content.Status.ARCHIVED))
    else:
        items = items.filter(status__in=(Content.Status.PUBLISHED, Content.Status.UNPUBLISHING, Content.Status.PARTIAL))
    if query:
        items = items.filter(Q(code__icontains=query) | Q(title__icontains=query) | Q(text__icontains=query))
    page_obj = Paginator(items, 15).get_page(request.GET.get("page"))
    return render(
        request,
        "user/contents.html",
        {"items": page_obj, "page_obj": page_obj, "status_filter": status, "q": query, "page_nonce": uuid.uuid4().hex},
    )


@sub_required
@require_POST
def contents_bulk(request):
    selected_ids = list(dict.fromkeys(request.POST.getlist("content_ids")))[:100]
    action = request.POST.get("bulk_action", "")
    status_filter = request.POST.get("status", "active")
    return_url = f"{reverse('user-contents')}?status={'unpublished' if status_filter == 'unpublished' else 'active'}"
    if not selected_ids:
        messages.error(request, "请先选择至少一条内容。")
        return redirect(return_url)

    contents_by_id = {
        str(item.pk): item
        for item in Content.objects.filter(
            owner_user=request.user,
            deleted_at__isnull=True,
            pk__in=selected_ids,
        ).prefetch_related("channels")
    }
    accepted = skipped = 0
    for selected_id in selected_ids:
        content = contents_by_id.get(selected_id)
        if not content:
            skipped += 1
            continue
        if action == "publish":
            operation_action = Operation.Action.SEND
            allowed_statuses = {Content.Status.DRAFT, Content.Status.UNPUBLISHED}
            permission = "can_publish"
        elif action == "unpublish":
            operation_action = Operation.Action.DELETE
            allowed_statuses = {Content.Status.PUBLISHED, Content.Status.PARTIAL}
            permission = "can_delete"
        elif action == "archive":
            if content.status not in {Content.Status.DRAFT, Content.Status.UNPUBLISHED, Content.Status.ARCHIVED}:
                skipped += 1
                continue
            content.deleted_at = timezone.now()
            content.status = Content.Status.ARCHIVED
            content.save(update_fields=("deleted_at", "status", "updated_at"))
            audit_event(
                request=request,
                actor=request.user,
                owner_user=request.user,
                action="content.archive",
                object_type="content",
                object_id=str(content.pk),
                after={"bulk": True, "deleted_at": content.deleted_at.isoformat()},
            )
            accepted += 1
            continue
        else:
            messages.error(request, "不支持的批量操作。")
            return redirect(return_url)

        if content.status not in allowed_statuses or not _has_content_permission(request.user, content, permission):
            skipped += 1
            continue
        operation, created = enqueue_content_operation(
            content=content,
            action=operation_action,
            actor=request.user,
            idempotency_key=f"bulk:{request.POST.get('page_nonce', '')}:{operation_action}:{content.pk}",
        )
        if operation_action == Operation.Action.DELETE:
            content.status = Content.Status.UNPUBLISHING
            content.save(update_fields=("status", "updated_at"))
        if created:
            audit_event(
                request=request,
                actor=request.user,
                owner_user=request.user,
                action="operation.enqueue",
                object_type="operation",
                object_id=str(operation.pk),
                outcome="ACCEPTED",
                after={"bulk": True, "action": operation_action, "content_id": content.pk},
                operation=operation,
            )
            accepted += 1
        else:
            skipped += 1
    messages.success(request, f"批量操作已受理 {accepted} 条，跳过 {skipped} 条。")
    return redirect(return_url)


@sub_required
@require_http_methods(["GET", "POST"])
def content_upload(request):
    form = ContentForm(request.POST or None, request.FILES or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            code, title = next_content_identity(request.user)
            content = form.save(commit=False)
            content.owner_user = request.user
            content.code = code
            content.title = title
            content.created_by = request.user
            content.updated_by = request.user
            content.next_run_at = form.cleaned_data["publish_at"]
            content.save()
            sync_content_channels(content, form.cleaned_data["channels"])
            attach_files(content, form.cleaned_data["files_primary"], group_no=1)
            attach_files(content, form.cleaned_data["files_secondary"], group_no=2)
        audit_event(
            request=request,
            actor=request.user,
            owner_user=request.user,
            action="content.create",
            object_type="content",
            object_id=str(content.pk),
            after={"code": content.code, "title": content.title, "channel_ids": list(content.channels.values_list("id", flat=True))},
        )
        messages.success(request, f"内容 {content.title} 已创建。")
        return redirect("user-content-view", pk=content.pk)
    return render(request, "user/content_form.html", {"form": form, "page_title": "上传内容", "is_edit": False})


@sub_required
def content_view(request, pk: int):
    content = _owned_content(request, pk)
    return render(request, "user/content_view.html", {"content": content, "page_nonce": uuid.uuid4().hex})


@sub_required
@require_http_methods(["GET", "POST"])
def content_edit(request, pk: int):
    content = _owned_content(request, pk)
    if request.method == "POST" and not _has_content_permission(request.user, content, "can_edit"):
        messages.error(request, "当前账号没有该内容全部频道的编辑权限。")
        return redirect("user-content-view", pk=content.pk)
    before = {
        "text": content.text,
        "text_variant": content.text_variant,
        "version": content.version,
        "channel_ids": list(content.channels.values_list("id", flat=True)),
        "file_ids": list(content.files.values_list("id", flat=True)),
    }
    form = ContentForm(request.POST or None, request.FILES or None, instance=content, user=request.user)
    if request.method == "POST" and form.is_valid():
        primary_uploads = form.cleaned_data["files_primary"]
        secondary_uploads = form.cleaned_data["files_secondary"]
        delete_file_ids = set(form.cleaned_data["delete_file_ids"])
        requested_order = form.cleaned_data["file_order"]
        with transaction.atomic():
            content = form.save(commit=False)
            content.updated_by = request.user
            content.version += 1
            if "publish_at" in form.changed_data:
                content.next_run_at = form.cleaned_data["publish_at"]
            content.save()
            sync_content_channels(content, form.cleaned_data["channels"])
            if primary_uploads:
                content.files.filter(group_no=1).delete()
                attach_files(content, primary_uploads, group_no=1)
            else:
                content.files.filter(group_no=1, pk__in=delete_file_ids).delete()
            if secondary_uploads:
                content.files.filter(group_no=2).delete()
                attach_files(content, secondary_uploads, group_no=2)
            else:
                content.files.filter(group_no=2, pk__in=delete_file_ids).delete()

            order_rank = {file_id: index for index, file_id in enumerate(requested_order)}
            reordered = []
            for group_no in (1, 2):
                group_files = list(content.files.filter(group_no=group_no).order_by("sort_index", "id"))
                group_files.sort(key=lambda item: (order_rank.get(item.pk, len(order_rank) + item.sort_index), item.pk))
                for index, item in enumerate(group_files):
                    if item.sort_index != index:
                        item.sort_index = index
                        reordered.append(item)
            if reordered:
                ContentFile.objects.bulk_update(reordered, ["sort_index"])
        audit_event(
            request=request,
            actor=request.user,
            owner_user=request.user,
            action="content.update",
            object_type="content",
            object_id=str(content.pk),
            before=before,
            after={
                "text": content.text,
                "text_variant": content.text_variant,
                "version": content.version,
                "channel_ids": list(content.channels.values_list("id", flat=True)),
                "file_ids": list(content.files.values_list("id", flat=True)),
            },
        )
        if content.status in {Content.Status.PUBLISHED, Content.Status.PARTIAL}:
            operation, _ = enqueue_content_operation(
                content=content,
                action=Operation.Action.REPLACE,
                actor=request.user,
                request_json={"force_resend": True, "reason": "CONTENT_EDIT"},
            )
            audit_event(request=request, actor=request.user, owner_user=request.user, action="operation.enqueue", object_type="operation", object_id=str(operation.pk), outcome="ACCEPTED", operation=operation)
        messages.success(request, "内容已整批更新；如已上架，将作为一个完整批次替换旧消息。")
        return redirect("user-content-view", pk=content.pk)
    return render(request, "user/content_form.html", {"form": form, "content": content, "page_title": "编辑内容", "is_edit": True})


def _queue_action(request, content: Content, action: str, new_status: str | None, success_message: str):
    required_permission = {
        Operation.Action.SEND: "can_publish",
        Operation.Action.DELETE: "can_delete",
    }.get(action, "can_edit")
    if not _has_content_permission(request.user, content, required_permission):
        messages.error(request, "当前账号没有该内容全部频道的操作权限。")
        return redirect("user-content-view", pk=content.pk)
    if Operation.objects.filter(
        owner_user=request.user,
        target_type="content",
        target_id=str(content.pk),
        action=action,
        state__in=(Operation.State.QUEUED, Operation.State.RUNNING),
    ).exists():
        messages.info(request, "相同操作已经在队列中或正在执行。")
        return redirect("user-content-logs", pk=content.pk)
    operation, created = enqueue_content_operation(
        content=content,
        action=action,
        actor=request.user,
        idempotency_key=request.POST.get("idempotency_key"),
    )
    if new_status:
        content.status = new_status
        content.save(update_fields=("status", "updated_at"))
    audit_event(
        request=request,
        actor=request.user,
        owner_user=request.user,
        action="operation.enqueue",
        object_type="operation",
        object_id=str(operation.pk),
        outcome="ACCEPTED",
        after={"action": action, "content_id": content.pk},
        operation=operation,
    )
    messages.success(request, success_message if created else "相同任务已经在队列中，无需重复提交。")
    return redirect("user-content-logs", pk=content.pk)


@sub_required
@require_POST
def content_publish(request, pk: int):
    content = _owned_content(request, pk)
    if content.status not in {Content.Status.DRAFT, Content.Status.UNPUBLISHED}:
        messages.error(request, "只有草稿或已下架内容可以上架。")
        return redirect("user-content-view", pk=content.pk)
    return _queue_action(request, content, Operation.Action.SEND, None, "上架任务已加入队列。")


@sub_required
@require_POST
def content_unpublish(request, pk: int):
    content = _owned_content(request, pk)
    if content.status not in {Content.Status.PUBLISHED, Content.Status.PARTIAL}:
        messages.error(request, "只有已上架或部分下架内容可以继续下架。")
        return redirect("user-content-view", pk=content.pk)
    return _queue_action(request, content, Operation.Action.DELETE, Content.Status.UNPUBLISHING, "下架任务已加入队列。")


@sub_required
@require_POST
def content_delete(request, pk: int):
    content = _owned_content(request, pk)
    if content.status in {Content.Status.PUBLISHED, Content.Status.UNPUBLISHING, Content.Status.PARTIAL}:
        messages.error(request, "请先下架并等待删除任务完成，再归档内容。")
        return redirect("user-content-view", pk=content.pk)
    content.deleted_at = timezone.now()
    content.status = Content.Status.ARCHIVED
    content.save(update_fields=("deleted_at", "status", "updated_at"))
    audit_event(request=request, actor=request.user, owner_user=request.user, action="content.archive", object_type="content", object_id=str(content.pk), after={"deleted_at": content.deleted_at.isoformat()})
    messages.success(request, "内容已归档。")
    return redirect("user-contents")


@sub_required
def content_logs(request, pk: int):
    content = _owned_content(request, pk)
    operations = Operation.objects.filter(owner_user=request.user, target_type="content", target_id=str(content.pk))
    return render(request, "user/content_logs.html", {"content": content, "operations": operations})


@sub_required
@require_http_methods(["GET", "POST"])
def profile(request):
    profile_form = ProfileForm(request.POST or None, instance=request.user, prefix="profile")
    admin_form = TelegramAdminIdsForm(request.POST or None, prefix="telegram")
    if request.method == "POST" and request.POST.get("action") == "profile" and profile_form.is_valid():
        before = {"display_name": request.user.display_name}
        profile_form.save()
        audit_event(request=request, actor=request.user, owner_user=request.user, action="profile.update", object_type="user", object_id=str(request.user.pk), before=before, after={"display_name": request.user.display_name})
        messages.success(request, "昵称已更新。")
        return redirect("user-profile")
    if request.method == "POST" and request.POST.get("action") == "telegram" and admin_form.is_valid():
        bot = admin_form.cleaned_data["bot"]
        ids = admin_form.cleaned_data["telegram_ids"]
        TelegramUserLink.objects.filter(user=request.user, bot=bot).exclude(telegram_user_id__in=ids).delete()
        for telegram_id in ids:
            TelegramUserLink.objects.get_or_create(user=request.user, bot=bot, telegram_user_id=telegram_id)
        audit_event(request=request, actor=request.user, owner_user=request.user, action="telegram.admin_ids.update", object_type="bot", object_id=str(bot.pk), after={"telegram_user_ids": ids})
        messages.success(request, "Telegram 管理员 ID 已保存。")
        return redirect("user-profile")
    return render(
        request,
        "user/profile.html",
        {"profile_form": profile_form, "admin_form": admin_form, "links": request.user.telegram_links.select_related("bot")},
    )


@sub_required
@require_http_methods(["GET", "POST"])
def message_tools(request):
    form = ExternalMessageActionForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        channel = form.cleaned_data["channel"]
        mode = form.cleaned_data["action"]
        message_id = form.cleaned_data["telegram_message_id"]
        action = Operation.Action.DELETE_EXTERNAL if mode == ExternalMessageActionForm.Action.DELETE else Operation.Action.EDIT_EXTERNAL
        payload = {
            "channel_id": channel.pk,
            "telegram_message_id": message_id,
            "mode": mode,
            "text": form.cleaned_data["text"].strip(),
        }
        operation, created = enqueue_operation(
            owner_user=request.user,
            actor=request.user,
            source=Operation.Source.WEB,
            action=action,
            target_type="external_message",
            target_id=f"{channel.pk}:{message_id}",
            request_json=payload,
            idempotency_key=request.POST.get("idempotency_key"),
        )
        audit_event(
            request=request,
            actor=request.user,
            owner_user=request.user,
            action="external_message.operation.enqueue",
            object_type="telegram_message",
            object_id=f"{channel.telegram_chat_id}:{message_id}",
            after={"mode": mode, "text_length": len(payload["text"])},
            outcome="ACCEPTED",
            operation=operation,
        )
        messages.success(request, "指定消息操作已加入队列。" if created else "相同操作已经在队列中。")
        return redirect("user-message-tools")
    operations = Operation.objects.filter(owner_user=request.user, target_type="external_message")[:50]
    return render(
        request,
        "user/message_tools.html",
        {"form": form, "operations": operations, "page_nonce": uuid.uuid4().hex},
    )
