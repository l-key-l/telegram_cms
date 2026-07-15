from __future__ import annotations

import uuid
from pathlib import Path

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


def channel_photo_upload_path(instance: "TelegramChannel", filename: str) -> str:
    suffix = Path(filename).suffix.lower()[:10]
    return f"channels/{instance.telegram_chat_id}/{uuid.uuid4().hex}{suffix}"


class TelegramBot(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "正常"
        INVALID = "INVALID", "Token 失效"
        DISABLED = "DISABLED", "已停用"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    telegram_bot_id = models.BigIntegerField(null=True, blank=True, unique=True)
    display_name = models.CharField("名称", max_length=80)
    username = models.CharField("Bot 用户名", max_length=80, blank=True)
    token_ciphertext = models.TextField()
    webhook_secret_ciphertext = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="created_bots")

    class Meta:
        ordering = ("display_name", "id")

    def __str__(self):
        return self.display_name


class TelegramChannel(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "正常"
        DEGRADED = "DEGRADED", "权限异常"
        DISCONNECTED = "DISCONNECTED", "未连接"

    class RoutePolicy(models.TextChoices):
        PRIMARY_ONLY = "PRIMARY_ONLY", "只使用主 Bot"
        FAILOVER = "FAILOVER", "主 Bot 失败时切换"
        EXPLICIT = "EXPLICIT", "操作时指定 Bot"

    telegram_chat_id = models.BigIntegerField("Telegram 频道 ID", unique=True)
    username = models.CharField("频道用户名", max_length=80, blank=True)
    title = models.CharField("频道名称", max_length=128)
    description = models.CharField("频道简介", max_length=255, blank=True)
    photo = models.ImageField("频道头像", upload_to=channel_photo_upload_path, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DISCONNECTED, db_index=True)
    route_policy = models.CharField(max_length=20, choices=RoutePolicy.choices, default=RoutePolicy.PRIMARY_ONLY)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="created_channels")

    class Meta:
        ordering = ("title", "id")

    def __str__(self):
        return self.title


class BotChannelBinding(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "正常"
        DEGRADED = "DEGRADED", "权限异常"
        DISABLED = "DISABLED", "已停用"

    bot = models.ForeignKey(TelegramBot, on_delete=models.PROTECT, related_name="channel_bindings")
    channel = models.ForeignKey(TelegramChannel, on_delete=models.PROTECT, related_name="bot_bindings")
    priority = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    can_post_messages = models.BooleanField(default=False)
    can_edit_messages = models.BooleanField(default=False)
    can_delete_messages = models.BooleanField(default=False)
    can_change_info = models.BooleanField(default=False)
    rights_json = models.JSONField(default=dict, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("channel", "priority")
        constraints = [
            models.UniqueConstraint(fields=("bot", "channel"), name="uniq_bot_channel"),
            models.UniqueConstraint(fields=("channel", "priority"), name="uniq_channel_priority"),
        ]

    def __str__(self):
        return f"{self.channel} ← {self.bot} (P{self.priority})"


class UserChannelAccess(TimeStampedModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="channel_accesses")
    channel = models.ForeignKey(TelegramChannel, on_delete=models.CASCADE, related_name="user_accesses")
    can_publish = models.BooleanField(default=True)
    can_edit = models.BooleanField(default=True)
    can_delete = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("user", "channel"), name="uniq_user_channel")]

    def __str__(self):
        return f"{self.user} / {self.channel}"


class TelegramUserLink(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "正常"
        DISABLED = "DISABLED", "已停用"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="telegram_links")
    bot = models.ForeignKey(TelegramBot, on_delete=models.CASCADE, related_name="user_links")
    telegram_user_id = models.BigIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    linked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("bot", "telegram_user_id"), name="uniq_bot_telegram_user")]


class ContentSequence(models.Model):
    owner_user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="content_sequence")
    next_value = models.PositiveIntegerField(default=1)


class Content(TimeStampedModel):
    class TextVariant(models.TextChoices):
        SIMPLIFIED = "SIMPLIFIED", "简体"
        TRADITIONAL = "TRADITIONAL", "繁体"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "草稿"
        PUBLISHED = "PUBLISHED", "已上架"
        UNPUBLISHING = "UNPUBLISHING", "下架处理中"
        UNPUBLISHED = "UNPUBLISHED", "已下架"
        ARCHIVED = "ARCHIVED", "已归档"
        PARTIAL = "PARTIAL", "部分下架"

    owner_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="contents")
    code = models.CharField("编号", max_length=32)
    title = models.CharField("标题", max_length=160)
    text = models.TextField("文字说明", blank=True)
    text_variant = models.CharField(
        "发送文字",
        max_length=16,
        choices=TextVariant.choices,
        default=TextVariant.SIMPLIFIED,
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    publish_at = models.DateTimeField("定时上架", null=True, blank=True)
    cycle_days = models.PositiveSmallIntegerField(
        "循环间隔天数",
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(365)],
    )
    cycle_time = models.TimeField("每天循环时间", null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    anti_scan_enabled = models.BooleanField("防扫图处理", default=False)
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="created_contents")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="updated_contents")
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    channels = models.ManyToManyField(TelegramChannel, through="ContentChannel", related_name="contents")

    class Meta:
        ordering = ("-created_at",)
        constraints = [models.UniqueConstraint(fields=("owner_user", "code"), name="uniq_owner_content_code")]
        indexes = [models.Index(fields=("owner_user", "status", "deleted_at"))]

    def __str__(self):
        return self.title


def content_upload_path(instance: "ContentFile", filename: str) -> str:
    suffix = Path(filename).suffix.lower()[:10]
    return f"contents/{instance.content.owner_user_id}/{instance.content_id}/{uuid.uuid4().hex}{suffix}"


class ContentFile(TimeStampedModel):
    class MediaType(models.TextChoices):
        PHOTO = "PHOTO", "图片"
        VIDEO = "VIDEO", "视频"
        DOCUMENT = "DOCUMENT", "文件"

    content = models.ForeignKey(Content, on_delete=models.CASCADE, related_name="files")
    group_no = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(2)])
    sort_index = models.PositiveSmallIntegerField(default=0)
    media_type = models.CharField(max_length=16, choices=MediaType.choices)
    file = models.FileField(upload_to=content_upload_path)
    processed_file = models.FileField(upload_to=content_upload_path, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    mime = models.CharField(max_length=120, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    telegram_file_ids_json = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("group_no", "sort_index", "id")


class ContentChannel(models.Model):
    content = models.ForeignKey(Content, on_delete=models.CASCADE)
    channel = models.ForeignKey(TelegramChannel, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("content", "channel"), name="uniq_content_channel")]


class Operation(TimeStampedModel):
    class ActorType(models.TextChoices):
        WEB_USER = "WEB_USER", "网页用户"
        TELEGRAM_USER = "TELEGRAM_USER", "Telegram 用户"
        SYSTEM = "SYSTEM", "系统"

    class Source(models.TextChoices):
        WEB = "WEB", "网页"
        TELEGRAM = "TELEGRAM", "Telegram"
        SCHEDULER = "SCHEDULER", "定时器"
        SYSTEM = "SYSTEM", "系统"

    class Action(models.TextChoices):
        SEND = "SEND", "发送"
        EDIT = "EDIT", "编辑同步"
        DELETE = "DELETE", "删除/下架"
        COPY = "COPY", "历史重发（已停用）"
        FORWARD = "FORWARD", "转发"
        REPLACE = "REPLACE", "编辑/自动更新替换"
        SYNC_TEXT = "SYNC_TEXT", "文字同步"
        UPDATE_CHANNEL = "UPDATE_CHANNEL", "修改频道资料"
        EDIT_EXTERNAL = "EDIT_EXTERNAL", "编辑指定消息"
        DELETE_EXTERNAL = "DELETE_EXTERNAL", "删除指定消息"

    class State(models.TextChoices):
        PENDING_CONFIRM = "PENDING_CONFIRM", "待确认"
        QUEUED = "QUEUED", "排队中"
        RUNNING = "RUNNING", "执行中"
        SUCCEEDED = "SUCCEEDED", "成功"
        PARTIAL = "PARTIAL", "部分成功"
        FAILED = "FAILED", "失败"
        UNCERTAIN = "UNCERTAIN", "结果不确定"
        CANCELLED = "CANCELLED", "已取消"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="operations")
    actor_type = models.CharField(max_length=20, choices=ActorType.choices)
    actor_id = models.CharField(max_length=80, blank=True)
    source = models.CharField(max_length=20, choices=Source.choices)
    action = models.CharField(max_length=20, choices=Action.choices)
    target_type = models.CharField(max_length=40)
    target_id = models.CharField(max_length=80)
    request_json = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(max_length=120)
    state = models.CharField(max_length=24, choices=State.choices, default=State.QUEUED, db_index=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField(db_index=True)
    locked_by = models.CharField(max_length=80, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True, db_index=True)
    telegram_error_code = models.CharField(max_length=80, blank=True)
    telegram_error_text = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [models.UniqueConstraint(fields=("owner_user", "idempotency_key"), name="uniq_owner_idempotency")]
        indexes = [models.Index(fields=("state", "available_at"))]


class Delivery(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "待发送"
        PARTIAL = "PARTIAL", "部分成功"
        ACTIVE = "ACTIVE", "活动"
        DELETE_PENDING = "DELETE_PENDING", "待删除"
        DELETED = "DELETED", "已删除"
        FAILED = "FAILED", "失败"
        UNKNOWN = "UNKNOWN", "未知"

    content = models.ForeignKey(Content, on_delete=models.PROTECT, related_name="deliveries")
    channel = models.ForeignKey(TelegramChannel, on_delete=models.PROTECT, related_name="deliveries")
    bot_channel_binding = models.ForeignKey(BotChannelBinding, null=True, on_delete=models.PROTECT, related_name="deliveries")
    bot_id_snapshot = models.BigIntegerField(null=True, blank=True)
    operation = models.ForeignKey(Operation, null=True, on_delete=models.SET_NULL, related_name="deliveries")
    generation = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    content_version = models.PositiveIntegerField(default=1)
    sent_at = models.DateTimeField(null=True, blank=True)
    deletable_until = models.DateTimeField(null=True, blank=True)
    replaces_delivery = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replacement_deliveries")


class DeliveryMessage(TimeStampedModel):
    class Status(models.TextChoices):
        SENT = "SENT", "已发送"
        EDITED = "EDITED", "已编辑"
        DELETED = "DELETED", "已删除"
        UNKNOWN = "UNKNOWN", "未知"

    delivery = models.ForeignKey(Delivery, on_delete=models.CASCADE, related_name="messages")
    group_no = models.PositiveSmallIntegerField(default=1)
    sort_index = models.PositiveSmallIntegerField(default=0)
    telegram_message_id = models.BigIntegerField()
    sent_by_bot_id = models.BigIntegerField(null=True, blank=True)
    media_group_id = models.CharField(max_length=100, blank=True)
    payload_snapshot_json = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SENT)
    last_edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("delivery", "telegram_message_id"), name="uniq_delivery_message")]


class TelegramUpdate(models.Model):
    class State(models.TextChoices):
        RECEIVED = "RECEIVED", "已接收"
        PROCESSED = "PROCESSED", "已处理"
        FAILED = "FAILED", "失败"

    bot = models.ForeignKey(TelegramBot, on_delete=models.CASCADE, related_name="updates")
    update_id = models.BigIntegerField()
    body_json = models.JSONField(default=dict)
    state = models.CharField(max_length=16, choices=State.choices, default=State.RECEIVED)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("bot", "update_id"), name="uniq_bot_update")]


class TelegramPendingAction(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "等待确认"
        CONFIRMED = "CONFIRMED", "已确认"
        CANCELLED = "CANCELLED", "已取消"
        EXPIRED = "EXPIRED", "已过期"

    bot = models.ForeignKey(TelegramBot, on_delete=models.CASCADE, related_name="pending_actions")
    owner_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="telegram_pending_actions")
    telegram_user_id = models.BigIntegerField()
    content = models.ForeignKey(Content, on_delete=models.CASCADE, related_name="telegram_pending_actions")
    action = models.CharField(max_length=20, choices=Operation.Action.choices)
    token = models.CharField(max_length=16, unique=True)
    payload_json = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("bot", "telegram_user_id", "status"))]


class AuditEvent(models.Model):
    class Outcome(models.TextChoices):
        ACCEPTED = "ACCEPTED", "已受理"
        SUCCESS = "SUCCESS", "成功"
        PARTIAL = "PARTIAL", "部分成功"
        FAILED = "FAILED", "失败"
        DENIED = "DENIED", "拒绝"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="audit_events")
    actor_type = models.CharField(max_length=30, default="WEB_USER")
    actor_id = models.CharField(max_length=80, blank=True)
    source = models.CharField(max_length=20, default="WEB")
    action = models.CharField(max_length=80, db_index=True)
    object_type = models.CharField(max_length=50)
    object_id = models.CharField(max_length=80, blank=True)
    operation = models.ForeignKey(Operation, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_events")
    request_id = models.CharField(max_length=64, blank=True, db_index=True)
    before_json = models.JSONField(default=dict, blank=True)
    after_json = models.JSONField(default=dict, blank=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("owner_user", "created_at"))]

    def __str__(self):
        return f"{self.action} / {self.outcome}"
