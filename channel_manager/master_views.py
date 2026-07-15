from __future__ import annotations

from django.contrib import messages
from django.conf import settings
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from accounts.forms import PasswordResetByMasterForm, SubaccountCreateForm
from accounts.models import User

from .audit import audit_event
from .forms import (
    BotChannelBindingForm,
    BotChannelBindingEditForm,
    ChannelIdResolveForm,
    SubaccountChannelAccessForm,
    TelegramBotCreateForm,
    TelegramBotEditForm,
    TelegramChannelEditForm,
    TelegramChannelForm,
)
from .models import AuditEvent, BotChannelBinding, Content, Operation, TelegramBot, TelegramChannel
from .permissions import master_required
from .security import decrypt_secret, mask_secret
from .services import enqueue_operation
from .task_utils import build_scheduled_task_rows, scheduled_content_q, stop_scheduled_task


@master_required
def dashboard(request):
    subaccounts = User.objects.filter(parent=request.user, role=User.Role.SUB)
    context = {
        "subaccount_count": subaccounts.count(),
        "active_subaccount_count": subaccounts.filter(is_active=True).count(),
        "content_count": Content.objects.filter(owner_user__parent=request.user, deleted_at__isnull=True).count(),
        "queued_count": Operation.objects.filter(Q(owner_user=request.user) | Q(owner_user__parent=request.user), state=Operation.State.QUEUED).count(),
        "bot_count": TelegramBot.objects.count(),
        "channel_count": TelegramChannel.objects.count(),
        "recent_operations": Operation.objects.filter(Q(owner_user=request.user) | Q(owner_user__parent=request.user)).select_related("owner_user")[:8],
    }
    return render(request, "master/dashboard.html", context)


@master_required
def subaccount_list(request):
    items = (
        User.objects.filter(parent=request.user, role=User.Role.SUB)
        .annotate(content_count=Count("contents"), channel_count=Count("channel_accesses", distinct=True))
        .order_by("-date_joined")
    )
    return render(request, "master/subaccount_list.html", {"items": items})


@master_required
@require_http_methods(["GET", "POST"])
def subaccount_create(request):
    form = SubaccountCreateForm(request.POST or None, master=request.user)
    channel_form = SubaccountChannelAccessForm(request.POST or None)
    if request.method == "POST" and form.is_valid() and channel_form.is_valid():
        user = form.save()
        channel_form.save(user)
        audit_event(
            request=request,
            actor=request.user,
            owner_user=user,
            action="subaccount.create",
            object_type="user",
            object_id=str(user.pk),
            after={"username": user.username, "display_name": user.display_name},
        )
        messages.success(request, f"子账号 {user.username} 已创建。")
        return redirect("master-subaccount-detail", pk=user.pk)
    return render(request, "master/subaccount_form.html", {"form": form, "channel_form": channel_form})


@master_required
@require_http_methods(["GET", "POST"])
def subaccount_detail(request, pk: int):
    user = get_object_or_404(User, pk=pk, parent=request.user, role=User.Role.SUB)
    access_form = SubaccountChannelAccessForm(
        request.POST or None,
        initial={"channels": user.channel_accesses.values_list("channel_id", flat=True)},
    )
    password_form = PasswordResetByMasterForm(prefix="password")
    if request.method == "POST" and request.POST.get("action") == "channels" and access_form.is_valid():
        before = list(user.channel_accesses.values_list("channel_id", flat=True))
        access_form.save(user)
        after = list(user.channel_accesses.values_list("channel_id", flat=True))
        audit_event(
            request=request,
            actor=request.user,
            owner_user=user,
            action="subaccount.channels.update",
            object_type="user",
            object_id=str(user.pk),
            before={"channel_ids": before},
            after={"channel_ids": after},
        )
        messages.success(request, "频道权限已更新。")
        return redirect("master-subaccount-detail", pk=user.pk)
    return render(
        request,
        "master/subaccount_detail.html",
        {"subaccount": user, "access_form": access_form, "password_form": password_form},
    )


@master_required
@require_POST
def subaccount_toggle(request, pk: int):
    user = get_object_or_404(User, pk=pk, parent=request.user, role=User.Role.SUB)
    before = user.is_active
    user.is_active = not user.is_active
    user.save(update_fields=("is_active",))
    audit_event(
        request=request,
        actor=request.user,
        owner_user=user,
        action="subaccount.status.update",
        object_type="user",
        object_id=str(user.pk),
        before={"is_active": before},
        after={"is_active": user.is_active},
    )
    messages.success(request, "账号状态已更新。")
    return redirect("master-subaccount-detail", pk=user.pk)


@master_required
@require_POST
def subaccount_password(request, pk: int):
    user = get_object_or_404(User, pk=pk, parent=request.user, role=User.Role.SUB)
    form = PasswordResetByMasterForm(request.POST, prefix="password")
    if form.is_valid():
        user.set_password(form.cleaned_data["new_password"])
        user.force_password_change = True
        user.save(update_fields=("password", "force_password_change"))
        audit_event(
            request=request,
            actor=request.user,
            owner_user=user,
            action="subaccount.password.reset",
            object_type="user",
            object_id=str(user.pk),
        )
        messages.success(request, "密码已重置，子账号下次登录需修改密码。")
    else:
        messages.error(request, "密码重置失败，请检查输入。")
    return redirect("master-subaccount-detail", pk=user.pk)


@master_required
def telegram_settings(request):
    bots = TelegramBot.objects.all()
    for bot in bots:
        try:
            bot.masked_token = mask_secret(decrypt_secret(bot.token_ciphertext))
        except ValueError:
            bot.masked_token = "解密失败"
    return render(
        request,
        "master/telegram_settings.html",
        {
            "bots": bots,
            "channels": TelegramChannel.objects.all(),
            "bindings": BotChannelBinding.objects.select_related("bot", "channel").all(),
            "channel_resolve_form": ChannelIdResolveForm(),
        },
    )


@master_required
@require_POST
def channel_resolve_id(request):
    form = ChannelIdResolveForm(request.POST)
    if not form.is_valid():
        error = next(
            (message for errors in form.errors.values() for message in errors),
            "频道链接和 Bot 都需要正确填写。",
        )
        return JsonResponse({"ok": False, "error": str(error)}, status=400)
    bot = form.cleaned_data["bot"]
    try:
        from .telegram_api import resolve_channel_reference

        result = resolve_channel_reference(bot, form.cleaned_data["channel_link"])
        audit_event(
            request=request,
            actor=request.user,
            owner_user=request.user,
            action="telegram.channel.resolve_id",
            object_type="telegram_channel_reference",
            object_id=str(result["chat_id"]),
            after={"chat_id": result["chat_id"], "source": result.get("source", "")},
        )
        return JsonResponse({"ok": True, "chat_id": str(result["chat_id"])})
    except Exception as exc:
        audit_event(
            request=request,
            actor=request.user,
            owner_user=request.user,
            action="telegram.channel.resolve_id",
            object_type="telegram_channel_reference",
            outcome="FAILED",
            error_code=exc.__class__.__name__,
        )
        return JsonResponse({"ok": False, "error": f"频道 ID 解析失败：{exc}"}, status=400)


@master_required
@require_http_methods(["GET", "POST"])
def bot_add(request):
    form = TelegramBotCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        bot = form.save(user=request.user)
        audit_event(request=request, actor=request.user, owner_user=request.user, action="telegram.bot.create", object_type="bot", object_id=str(bot.pk), after={"name": bot.display_name})
        messages.success(request, "Bot 已添加，请继续验证 Token。")
        return redirect("master-telegram")
    return render(request, "master/simple_form.html", {"title": "添加机器人", "subtitle": "Token 将加密保存", "form": form})


@master_required
@require_http_methods(["GET", "POST"])
def bot_edit(request, pk: int):
    bot = get_object_or_404(TelegramBot, pk=pk)
    before = {"display_name": bot.display_name, "status": bot.status, "telegram_bot_id": bot.telegram_bot_id}
    form = TelegramBotEditForm(request.POST or None, instance=bot)
    if request.method == "POST" and form.is_valid():
        bot = form.save()
        audit_event(
            request=request,
            actor=request.user,
            owner_user=request.user,
            action="telegram.bot.update",
            object_type="bot",
            object_id=str(bot.pk),
            before=before,
            after={"display_name": bot.display_name, "status": bot.status, "token_changed": bool(form.cleaned_data.get("new_token")), "webhook_secret_changed": bool(form.cleaned_data.get("new_webhook_secret"))},
        )
        messages.success(request, "Bot 设置已更新；更换 Token 后请重新验证并注册 Webhook。")
        return redirect("master-telegram")
    return render(request, "master/simple_form.html", {"title": "修改机器人", "subtitle": "可轮换 Token、Webhook Secret 或停用 Bot", "form": form})


@master_required
@require_POST
def bot_verify(request, pk: int):
    bot = get_object_or_404(TelegramBot, pk=pk)
    try:
        from .telegram_api import verify_bot

        me = verify_bot(bot)
        audit_event(request=request, actor=request.user, owner_user=request.user, action="telegram.bot.verify", object_type="bot", object_id=str(bot.pk), after={"telegram_bot_id": me.id, "username": me.username})
        messages.success(request, f"Bot 验证成功：@{me.username}")
    except Exception as exc:
        # 只有 Token 本身无效才标记 INVALID；代理/网络临时故障不能禁用一个已验证 Bot。
        from aiogram.exceptions import TelegramUnauthorizedError
        from aiogram.utils.token import TokenValidationError

        if isinstance(exc, (TelegramUnauthorizedError, TokenValidationError)):
            bot.status = TelegramBot.Status.INVALID
            bot.save(update_fields=("status", "updated_at"))
        audit_event(request=request, actor=request.user, owner_user=request.user, action="telegram.bot.verify", object_type="bot", object_id=str(bot.pk), outcome="FAILED", error_code=exc.__class__.__name__)
        messages.error(request, f"Bot 验证失败：{exc}")
    return redirect("master-telegram")


@master_required
@require_POST
def bot_webhook_enable(request, pk: int):
    bot = get_object_or_404(TelegramBot, pk=pk)
    if not settings.PUBLIC_BASE_URL.startswith("https://"):
        messages.error(request, "请先在 .env 配置 PUBLIC_BASE_URL=https://你的域名。")
        return redirect("master-telegram")
    try:
        from .telegram_api import register_webhook

        url = register_webhook(bot, settings.PUBLIC_BASE_URL)
        audit_event(request=request, actor=request.user, owner_user=request.user, action="telegram.webhook.enable", object_type="bot", object_id=str(bot.pk), after={"url": url})
        messages.success(request, "Webhook 已注册。")
    except Exception as exc:
        audit_event(request=request, actor=request.user, owner_user=request.user, action="telegram.webhook.enable", object_type="bot", object_id=str(bot.pk), outcome="FAILED", error_code=exc.__class__.__name__)
        messages.error(request, f"Webhook 注册失败：{exc}")
    return redirect("master-telegram")


@master_required
@require_POST
def bot_webhook_disable(request, pk: int):
    bot = get_object_or_404(TelegramBot, pk=pk)
    try:
        from .telegram_api import unregister_webhook

        unregister_webhook(bot)
        audit_event(request=request, actor=request.user, owner_user=request.user, action="telegram.webhook.disable", object_type="bot", object_id=str(bot.pk))
        messages.success(request, "Webhook 已停用。")
    except Exception as exc:
        audit_event(request=request, actor=request.user, owner_user=request.user, action="telegram.webhook.disable", object_type="bot", object_id=str(bot.pk), outcome="FAILED", error_code=exc.__class__.__name__)
        messages.error(request, f"Webhook 停用失败：{exc}")
    return redirect("master-telegram")


@master_required
@require_http_methods(["GET", "POST"])
def channel_add(request):
    form = TelegramChannelForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        channel = form.save(user=request.user)
        audit_event(request=request, actor=request.user, owner_user=request.user, action="telegram.channel.create", object_type="channel", object_id=str(channel.pk), after={"chat_id": channel.telegram_chat_id, "title": channel.title})
        messages.success(request, "频道已添加，请继续绑定机器人并验证权限。")
        return redirect("master-telegram")
    return render(request, "master/simple_form.html", {"title": "添加频道", "subtitle": "支持 -100 开头的频道 ID", "form": form})


@master_required
@require_http_methods(["GET", "POST"])
def binding_add(request):
    form = BotChannelBindingForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        binding = form.save()
        audit_event(request=request, actor=request.user, owner_user=request.user, action="telegram.binding.create", object_type="bot_channel_binding", object_id=str(binding.pk), after={"bot_id": binding.bot_id, "channel_id": binding.channel_id, "priority": binding.priority})
        messages.success(request, "Bot 与频道已绑定，请验证权限。")
        return redirect("master-telegram")
    return render(request, "master/simple_form.html", {"title": "绑定 Bot 与频道", "subtitle": "优先级 1 表示主 Bot", "form": form})


@master_required
@require_http_methods(["GET", "POST"])
def binding_edit(request, pk: int):
    binding = get_object_or_404(BotChannelBinding.objects.select_related("bot", "channel"), pk=pk)
    before = {"priority": binding.priority, "status": binding.status}
    form = BotChannelBindingEditForm(request.POST or None, instance=binding)
    if request.method == "POST" and form.is_valid():
        binding = form.save()
        audit_event(
            request=request,
            actor=request.user,
            owner_user=request.user,
            action="telegram.binding.update",
            object_type="bot_channel_binding",
            object_id=str(binding.pk),
            before=before,
            after={"priority": binding.priority, "status": binding.status},
        )
        messages.success(request, "绑定设置已更新。")
        return redirect("master-telegram")
    return render(request, "master/simple_form.html", {"title": "修改 Bot ↔ 频道绑定", "subtitle": f"{binding.channel} / {binding.bot}", "form": form})


@master_required
@require_POST
def binding_verify(request, pk: int):
    binding = get_object_or_404(BotChannelBinding.objects.select_related("bot", "channel"), pk=pk)
    try:
        from .telegram_api import verify_binding

        verify_binding(binding)
        audit_event(request=request, actor=request.user, owner_user=request.user, action="telegram.binding.verify", object_type="bot_channel_binding", object_id=str(binding.pk), after=binding.rights_json)
        messages.success(request, "频道权限验证完成。")
    except Exception as exc:
        binding.status = BotChannelBinding.Status.DEGRADED
        binding.save(update_fields=("status", "updated_at"))
        audit_event(request=request, actor=request.user, owner_user=request.user, action="telegram.binding.verify", object_type="bot_channel_binding", object_id=str(binding.pk), outcome="FAILED", error_code=exc.__class__.__name__)
        messages.error(request, f"权限验证失败：{exc}")
    return redirect("master-telegram")


@master_required
@require_http_methods(["GET", "POST"])
def channel_edit(request, pk: int):
    channel = get_object_or_404(TelegramChannel, pk=pk)
    before = {
        "title": channel.title,
        "description": channel.description,
        "route_policy": channel.route_policy,
        "has_photo": bool(channel.photo),
    }
    form = TelegramChannelEditForm(request.POST or None, request.FILES or None, instance=channel)
    if request.method == "POST" and form.is_valid():
        channel = form.save()
        remove_photo = form.cleaned_data["remove_photo"] or bool(request.POST.get("photo-clear"))
        if remove_photo and channel.photo:
            channel.photo.delete(save=False)
            channel.photo = ""
            channel.save(update_fields=("photo", "updated_at"))
        payload = {
            "title": channel.title,
            "description": channel.description,
            "photo_path": channel.photo.path if channel.photo else "",
            "remove_photo": remove_photo,
        }
        operation, _ = enqueue_operation(
            owner_user=request.user,
            actor=request.user,
            source=Operation.Source.WEB,
            action=Operation.Action.UPDATE_CHANNEL,
            target_type="channel",
            target_id=str(channel.pk),
            request_json=payload,
            idempotency_key=f"channel:{channel.pk}:{channel.updated_at.isoformat()}",
        )
        audit_event(
            request=request,
            actor=request.user,
            owner_user=request.user,
            action="telegram.channel.update.enqueue",
            object_type="channel",
            object_id=str(channel.pk),
            before=before,
            after=payload | {"photo_path": bool(payload["photo_path"])},
            outcome="ACCEPTED",
            operation=operation,
        )
        messages.success(request, "频道资料修改任务已加入队列。")
        return redirect("master-operations")
    return render(
        request,
        "master/simple_form.html",
        {"title": "修改频道资料", "subtitle": "同步修改频道名称、简介、头像和路由策略", "form": form},
    )


@master_required
def operation_list(request):
    items = Operation.objects.filter(Q(owner_user=request.user) | Q(owner_user__parent=request.user)).select_related("owner_user")[:200]
    return render(request, "master/operation_list.html", {"items": items})


@master_required
def scheduled_tasks(request):
    contents_qs = (
        Content.objects.filter(owner_user__parent=request.user, deleted_at__isnull=True)
        .filter(scheduled_content_q())
        .select_related("owner_user")
        .prefetch_related("channels")
        .order_by("next_run_at", "-updated_at")
    )
    rows = build_scheduled_task_rows(contents_qs)
    return render(
        request,
        "shared/scheduled_task_list.html",
        {
            "rows": rows,
            "is_master": True,
            "stop_url_name": "master-task-stop",
            "page_title": "定时/循环任务",
            "page_subtitle": "查看全部子账号的定时上架、循环更新、执行次数和下一次执行时间",
            "empty_text": "暂无子账号定时或循环任务。",
        },
    )


@master_required
@require_POST
def scheduled_task_stop(request, pk: int):
    content = get_object_or_404(Content, pk=pk, owner_user__parent=request.user, deleted_at__isnull=True)
    cancelled_count = stop_scheduled_task(request=request, actor=request.user, content=content)
    messages.success(request, f"任务已中止，已取消排队任务 {cancelled_count} 个。")
    return redirect("master-tasks")


@master_required
def audit_list(request):
    items = AuditEvent.objects.filter(Q(owner_user=request.user) | Q(owner_user__parent=request.user)).select_related("owner_user")[:300]
    return render(request, "master/audit_list.html", {"items": items})
