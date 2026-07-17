from __future__ import annotations

import secrets

from django import forms
from PIL import Image, UnidentifiedImageError

from accounts.models import User

from .models import BotChannelBinding, Content, ContentFile, TelegramBot, TelegramChannel, UserChannelAccess
from .security import encrypt_secret
from .services import available_channels_for


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_clean = super().clean
        if isinstance(data, (list, tuple)):
            return [single_clean(item, initial) for item in data]
        return [single_clean(data, initial)] if data else []


class TelegramBotCreateForm(forms.Form):
    display_name = forms.CharField(label="Bot 名称", max_length=80)
    token = forms.CharField(label="Bot Token", widget=forms.PasswordInput(render_value=False))
    webhook_secret = forms.CharField(label="Webhook Secret", required=False, help_text="留空则自动生成")

    def save(self, *, user):
        token = self.cleaned_data["token"].strip()
        webhook_secret = self.cleaned_data["webhook_secret"].strip() or secrets.token_urlsafe(32)
        return TelegramBot.objects.create(
            display_name=self.cleaned_data["display_name"],
            token_ciphertext=encrypt_secret(token),
            webhook_secret_ciphertext=encrypt_secret(webhook_secret),
            created_by=user,
        )


class TelegramBotEditForm(forms.ModelForm):
    new_token = forms.CharField(label="新 Bot Token", required=False, widget=forms.PasswordInput(render_value=False), help_text="不修改则留空")
    new_webhook_secret = forms.CharField(label="新 Webhook Secret", required=False, widget=forms.PasswordInput(render_value=False), help_text="不修改则留空")

    class Meta:
        model = TelegramBot
        fields = ("display_name", "status")

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.cleaned_data.get("new_token"):
            instance.token_ciphertext = encrypt_secret(self.cleaned_data["new_token"].strip())
            instance.telegram_bot_id = None
            instance.username = ""
            instance.last_verified_at = None
        if self.cleaned_data.get("new_webhook_secret"):
            instance.webhook_secret_ciphertext = encrypt_secret(self.cleaned_data["new_webhook_secret"].strip())
        if commit:
            instance.save()
        return instance


class TelegramChannelForm(forms.ModelForm):
    class Meta:
        model = TelegramChannel
        fields = ("title", "telegram_chat_id", "username", "description", "photo", "route_policy")
        labels = {"route_policy": "路由策略"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["route_policy"].choices = (
            (TelegramChannel.RoutePolicy.PRIMARY_ONLY, "只使用主 Bot"),
            (TelegramChannel.RoutePolicy.FAILOVER, "主 Bot 失败时切换"),
        )

    def save(self, commit=True, *, user=None):
        instance = super().save(commit=False)
        if user:
            instance.created_by = user
        if commit:
            instance.save()
        return instance

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if photo and photo.size > 5 * 1024 * 1024:
            raise forms.ValidationError("频道头像不能超过 5 MB。")
        return photo


class TelegramChannelEditForm(forms.ModelForm):
    remove_photo = forms.BooleanField(label="删除当前频道头像", required=False)

    class Meta:
        model = TelegramChannel
        fields = ("title", "description", "photo", "route_policy")
        labels = {"route_policy": "路由策略"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["route_policy"].choices = (
            (TelegramChannel.RoutePolicy.PRIMARY_ONLY, "只使用主 Bot"),
            (TelegramChannel.RoutePolicy.FAILOVER, "主 Bot 失败时切换"),
        )

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if photo and photo.size > 5 * 1024 * 1024:
            raise forms.ValidationError("频道头像不能超过 5 MB。")
        return photo


class ChannelIdResolveForm(forms.Form):
    bot = forms.ModelChoiceField(label="用于识别频道的 Bot", queryset=TelegramBot.objects.none())
    channel_link = forms.CharField(
        label="频道链接、消息链接或 @username",
        max_length=300,
        widget=forms.TextInput(attrs={"placeholder": "https://t.me/channel、https://t.me/c/... 或 https://t.me/+..."}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["bot"].queryset = TelegramBot.objects.filter(status=TelegramBot.Status.ACTIVE).order_by("display_name")


class ExternalMessageActionForm(forms.Form):
    class Action:
        EDIT_TEXT = "EDIT_TEXT"
        EDIT_CAPTION = "EDIT_CAPTION"
        DELETE = "DELETE"
        choices = (
            (EDIT_TEXT, "修改文字消息"),
            (EDIT_CAPTION, "修改媒体说明"),
            (DELETE, "删除消息"),
        )

    channel = forms.ModelChoiceField(label="频道", queryset=TelegramChannel.objects.none())
    telegram_message_id = forms.CharField(label="Telegram Message ID 或消息链接", max_length=300)
    action = forms.ChoiceField(label="操作", choices=Action.choices)
    text = forms.CharField(
        label="新文字",
        required=False,
        widget=forms.Textarea(attrs={"rows": 5, "placeholder": "删除操作时留空"}),
    )

    def __init__(self, *args, user: User, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.fields["channel"].queryset = available_channels_for(user)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("action") != self.Action.DELETE and not (cleaned.get("text") or "").strip():
            self.add_error("text", "编辑消息时必须填写新文字。")
        channel = cleaned.get("channel")
        action = cleaned.get("action")
        if channel:
            permission = "can_delete" if action == self.Action.DELETE else "can_edit"
            if not UserChannelAccess.objects.filter(user=self.user, channel=channel, **{permission: True}).exists():
                self.add_error("channel", "当前账号没有执行该操作的频道权限。")
        return cleaned

    def clean_telegram_message_id(self):
        value = self.cleaned_data["telegram_message_id"].strip().rstrip("/")
        candidate = value.rsplit("/", 1)[-1]
        try:
            message_id = int(candidate)
        except ValueError as exc:
            raise forms.ValidationError("请输入数字 Message ID，或以数字 Message ID 结尾的 Telegram 消息链接。") from exc
        if message_id <= 0:
            raise forms.ValidationError("Message ID 必须大于 0。")
        return message_id


class BotChannelBindingForm(forms.ModelForm):
    class Meta:
        model = BotChannelBinding
        fields = ("bot", "channel", "priority")


class BotChannelBindingEditForm(forms.ModelForm):
    class Meta:
        model = BotChannelBinding
        fields = ("priority", "status")


class ContentForm(forms.ModelForm):
    channels = forms.ModelMultipleChoiceField(
        label="发送频道",
        queryset=TelegramChannel.objects.none(),
        required=True,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "channel-check-input"}),
    )
    files_primary = MultipleFileField(
        label="第一次发送：图片/视频",
        required=False,
        widget=MultipleFileInput(
            attrs={"accept": "image/*,video/*", "data-append-files": "true", "data-max-files": "10"}
        ),
    )
    files_secondary = MultipleFileField(
        label="第二次发送：媒体文件（必填）",
        required=False,
        widget=MultipleFileInput(
            attrs={"accept": "image/*,video/*", "data-append-files": "true", "data-max-files": "10"}
        ),
    )
    delete_file_ids = forms.CharField(required=False, widget=forms.HiddenInput())
    file_order = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = Content
        fields = ("text", "text_variant", "cycle_days", "cycle_time", "publish_at", "anti_scan_enabled")
        widgets = {
            "text": forms.Textarea(attrs={"rows": 7, "placeholder": "输入发送到频道的文字说明…"}),
            "text_variant": forms.RadioSelect,
            "cycle_time": forms.TimeInput(attrs={"type": "time"}),
            "publish_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }

    def __init__(self, *args, user: User, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.fields["text_variant"].required = False
        self.fields["channels"].queryset = available_channels_for(user)
        if self.instance.pk:
            self.fields["channels"].initial = self.instance.channels.all()

    def clean_text_variant(self):
        return self.cleaned_data.get("text_variant") or Content.TextVariant.SIMPLIFIED

    def clean(self):
        cleaned = super().clean()
        if bool(cleaned.get("cycle_days")) != bool(cleaned.get("cycle_time")):
            raise forms.ValidationError("循环间隔天数和每天循环时间需要同时填写。")
        files = list(cleaned.get("files_primary") or []) + list(cleaned.get("files_secondary") or [])
        if len(cleaned.get("files_primary") or []) > 10 or len(cleaned.get("files_secondary") or []) > 10:
            raise forms.ValidationError("每次发送最多包含 10 个媒体文件。")
        if any(item.size > 50 * 1024 * 1024 for item in files):
            raise forms.ValidationError("单个文件不能超过 50 MB。")
        if sum(item.size for item in files) > 200 * 1024 * 1024:
            raise forms.ValidationError("单次上传文件总大小不能超过 200 MB。")
        for item in files:
            content_type = (getattr(item, "content_type", "") or "").lower()
            if not (content_type.startswith("image/") or content_type.startswith("video/")):
                raise forms.ValidationError(f"{item.name} 不是支持的图片或视频文件。")
            if content_type.startswith("image/"):
                self._validate_image_upload(item)

        delete_ids = self._parse_id_list(cleaned.get("delete_file_ids"), "待删除文件")
        order_ids = self._parse_id_list(cleaned.get("file_order"), "媒体顺序")
        if self.instance.pk:
            owned_ids = set(self.instance.files.values_list("id", flat=True))
            unknown_ids = (set(delete_ids) | set(order_ids)) - owned_ids
            if unknown_ids:
                raise forms.ValidationError("媒体编辑数据已过期，请刷新页面后重试。")

        primary_uploads = list(cleaned.get("files_primary") or [])
        secondary_uploads = list(cleaned.get("files_secondary") or [])
        remaining_primary = []
        remaining_secondary = []
        if self.instance.pk:
            remaining_primary = list(self.instance.files.filter(group_no=1).exclude(pk__in=delete_ids))
            remaining_secondary = list(self.instance.files.filter(group_no=2).exclude(pk__in=delete_ids))
        planned_primary_count = len(primary_uploads) if primary_uploads else len(remaining_primary)
        planned_secondary = secondary_uploads if secondary_uploads else remaining_secondary
        if not planned_secondary:
            self.add_error("files_secondary", "第二次发送必须至少保留或上传一个媒体文件。")
        elif any(getattr(item, "media_type", None) == ContentFile.MediaType.DOCUMENT for item in planned_secondary):
            self.add_error("files_secondary", "第二次发送只支持图片或视频。")

        text_limit = 1024 if planned_primary_count else 4096
        if len(cleaned.get("text") or "") > text_limit:
            self.add_error("text", f"当前第一次发送结构下，文字最多 {text_limit} 个字符。")
        cleaned["delete_file_ids"] = delete_ids
        cleaned["file_order"] = order_ids
        return cleaned

    @staticmethod
    def _parse_id_list(value, label: str) -> list[int]:
        if not value:
            return []
        result = []
        try:
            for raw in str(value).split(","):
                if raw.strip():
                    result.append(int(raw.strip()))
        except ValueError as exc:
            raise forms.ValidationError(f"{label}格式错误。") from exc
        return list(dict.fromkeys(result))

    @staticmethod
    def _validate_image_upload(item) -> None:
        try:
            position = item.tell()
        except (AttributeError, OSError):
            position = None
        try:
            with Image.open(item) as image:
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise forms.ValidationError(f"{item.name} 图片文件读取失败，请换成 JPG、PNG 或 WebP。") from exc
        finally:
            try:
                item.seek(position or 0)
            except (AttributeError, OSError):
                pass


class TelegramAdminIdsForm(forms.Form):
    bot = forms.ModelChoiceField(label="用于接收指令的 Bot", queryset=TelegramBot.objects.none())
    telegram_ids = forms.CharField(
        label="Telegram 管理员数字 ID",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "每行一个 Telegram 数字 ID"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["bot"].queryset = TelegramBot.objects.filter(status=TelegramBot.Status.ACTIVE)

    def clean_telegram_ids(self):
        result = []
        for line in self.cleaned_data["telegram_ids"].splitlines():
            value = line.strip()
            if not value:
                continue
            try:
                result.append(int(value))
            except ValueError as exc:
                raise forms.ValidationError(f"{value} 不是有效数字 ID。") from exc
        return list(dict.fromkeys(result))


class SubaccountChannelAccessForm(forms.Form):
    channels = forms.ModelMultipleChoiceField(label="允许使用的频道", queryset=TelegramChannel.objects.all(), required=False)

    def save(self, user: User):
        selected = self.cleaned_data["channels"]
        UserChannelAccess.objects.filter(user=user).exclude(channel__in=selected).delete()
        existing = set(UserChannelAccess.objects.filter(user=user).values_list("channel_id", flat=True))
        UserChannelAccess.objects.bulk_create(
            [UserChannelAccess(user=user, channel=channel) for channel in selected if channel.pk not in existing],
            ignore_conflicts=True,
        )
