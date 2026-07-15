from django.contrib import admin

from .models import (
    AuditEvent,
    BotChannelBinding,
    Content,
    ContentChannel,
    ContentFile,
    Delivery,
    DeliveryMessage,
    Operation,
    TelegramBot,
    TelegramChannel,
    TelegramPendingAction,
    TelegramUpdate,
    TelegramUserLink,
    UserChannelAccess,
)


@admin.register(TelegramBot)
class TelegramBotAdmin(admin.ModelAdmin):
    list_display = ("display_name", "username", "telegram_bot_id", "status", "last_verified_at")
    exclude = ("token_ciphertext", "webhook_secret_ciphertext")


@admin.register(TelegramChannel)
class TelegramChannelAdmin(admin.ModelAdmin):
    list_display = ("title", "telegram_chat_id", "route_policy", "status")


@admin.register(BotChannelBinding)
class BindingAdmin(admin.ModelAdmin):
    list_display = ("channel", "bot", "priority", "status", "can_post_messages", "can_edit_messages", "can_delete_messages", "can_change_info")


@admin.register(Content)
class ContentAdmin(admin.ModelAdmin):
    list_display = ("title", "owner_user", "status", "version", "created_at")
    list_filter = ("status",)
    search_fields = ("title", "code", "text")


@admin.register(Operation)
class OperationAdmin(admin.ModelAdmin):
    list_display = ("id", "owner_user", "action", "state", "available_at", "created_at")
    list_filter = ("action", "state", "source")


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "owner_user", "action", "object_type", "object_id", "outcome")
    list_filter = ("outcome", "source")
    readonly_fields = [field.name for field in AuditEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(UserChannelAccess)
admin.site.register(TelegramUserLink)
admin.site.register(ContentFile)
admin.site.register(ContentChannel)
admin.site.register(Delivery)
admin.site.register(DeliveryMessage)
admin.site.register(TelegramUpdate)
admin.site.register(TelegramPendingAction)
