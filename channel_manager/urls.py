from django.urls import path

from . import master_views, user_views, webhook_views


urlpatterns = [
    path("telegram/webhook/<uuid:public_id>", webhook_views.telegram_webhook, name="telegram-webhook"),
    path("master/dashboard", master_views.dashboard, name="master-dashboard"),
    path("master/subaccounts", master_views.subaccount_list, name="master-subaccounts"),
    path("master/subaccounts/create", master_views.subaccount_create, name="master-subaccount-create"),
    path("master/subaccounts/<int:pk>", master_views.subaccount_detail, name="master-subaccount-detail"),
    path("master/subaccounts/<int:pk>/toggle", master_views.subaccount_toggle, name="master-subaccount-toggle"),
    path("master/subaccounts/<int:pk>/password", master_views.subaccount_password, name="master-subaccount-password"),
    path("master/telegram", master_views.telegram_settings, name="master-telegram"),
    path("master/telegram/bots/add", master_views.bot_add, name="master-bot-add"),
    path("master/telegram/bots/<int:pk>/edit", master_views.bot_edit, name="master-bot-edit"),
    path("master/telegram/bots/<int:pk>/verify", master_views.bot_verify, name="master-bot-verify"),
    path("master/telegram/bots/<int:pk>/webhook/enable", master_views.bot_webhook_enable, name="master-bot-webhook-enable"),
    path("master/telegram/bots/<int:pk>/webhook/disable", master_views.bot_webhook_disable, name="master-bot-webhook-disable"),
    path("master/telegram/channels/add", master_views.channel_add, name="master-channel-add"),
    path("master/telegram/channels/resolve-id", master_views.channel_resolve_id, name="master-channel-resolve-id"),
    path("master/telegram/channels/<int:pk>/edit", master_views.channel_edit, name="master-channel-edit"),
    path("master/telegram/bindings/add", master_views.binding_add, name="master-binding-add"),
    path("master/telegram/bindings/<int:pk>/edit", master_views.binding_edit, name="master-binding-edit"),
    path("master/telegram/bindings/<int:pk>/verify", master_views.binding_verify, name="master-binding-verify"),
    path("master/operations", master_views.operation_list, name="master-operations"),
    path("master/audits", master_views.audit_list, name="master-audits"),
    path("user/profile", user_views.profile, name="user-profile"),
    path("user/contents", user_views.contents, name="user-contents"),
    path("user/contents/bulk", user_views.contents_bulk, name="user-contents-bulk"),
    path("user/content/upload", user_views.content_upload, name="user-content-upload"),
    path("user/content/view/<int:pk>", user_views.content_view, name="user-content-view"),
    path("user/content/edit/<int:pk>", user_views.content_edit, name="user-content-edit"),
    path("user/content/<int:pk>/publish", user_views.content_publish, name="user-content-publish"),
    path("user/content/<int:pk>/unpublish", user_views.content_unpublish, name="user-content-unpublish"),
    path("user/content/<int:pk>/delete", user_views.content_delete, name="user-content-delete"),
    path("user/content/logs/<int:pk>", user_views.content_logs, name="user-content-logs"),
]
