from django.db import IntegrityError, transaction
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from datetime import time, timedelta
import tempfile
from io import BytesIO, StringIO
from PIL import Image

from accounts.models import User

from .models import (
    AuditEvent,
    BotChannelBinding,
    Content,
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
from .forms import ExternalMessageActionForm
from .security import decrypt_secret, encrypt_secret
from .services import attach_files, enqueue_content_operation, enqueue_due_content_operations, render_content_text, telegram_media_source_path
from .webhook_views import _handle_command
from .operation_processor import _send_to_telegram, process_operation, resolve_bindings
from .services import enqueue_operation
from .telegram_api import _parse_channel_reference, resolve_channel_reference


def image_upload(name: str = "photo.jpg", size: tuple[int, int] = (8, 8), image_format: str = "JPEG") -> SimpleUploadedFile:
    image_buffer = BytesIO()
    Image.new("RGB", size, color=(10, 20, 30)).save(image_buffer, format=image_format)
    content_type = "image/png" if image_format.upper() == "PNG" else "image/jpeg"
    return SimpleUploadedFile(name, image_buffer.getvalue(), content_type=content_type)


class ManagerFoundationTests(TestCase):
    def setUp(self):
        self.temp_media = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_media.cleanup)
        media_override = override_settings(MEDIA_ROOT=self.temp_media.name)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.master = User.objects.create_user(username="master", password="StrongPass123!", role=User.Role.MASTER, display_name="总账号")
        self.sub1 = User.objects.create_user(username="sub1", password="StrongPass123!", role=User.Role.SUB, parent=self.master, display_name="甲")
        self.sub2 = User.objects.create_user(username="sub2", password="StrongPass123!", role=User.Role.SUB, parent=self.master, display_name="乙")
        self.channel = TelegramChannel.objects.create(title="测试频道", telegram_chat_id=-1001234567890, created_by=self.master)
        UserChannelAccess.objects.create(user=self.sub1, channel=self.channel)

    def test_master_can_add_encrypted_bot_from_web(self):
        self.client.force_login(self.master)
        token = "123456:example-secret-token"
        response = self.client.post(reverse("master-bot-add"), {"display_name": "主机器人", "token": token, "webhook_secret": "hook-secret"})
        self.assertRedirects(response, reverse("master-telegram"), fetch_redirect_response=False)
        bot = TelegramBot.objects.get()
        self.assertNotEqual(bot.token_ciphertext, token)
        self.assertEqual(decrypt_secret(bot.token_ciphertext), token)
        self.assertTrue(AuditEvent.objects.filter(action="telegram.bot.create").exists())

    def test_master_channel_resolver_returns_popup_json_without_redirect(self):
        self.client.force_login(self.master)
        bot = TelegramBot.objects.create(
            display_name="resolver",
            token_ciphertext=encrypt_secret("1:token"),
            status=TelegramBot.Status.ACTIVE,
            created_by=self.master,
        )
        with patch(
            "channel_manager.telegram_api.resolve_channel_reference",
            return_value={"chat_id": -1004418203649, "source": "telegram_api"},
        ):
            response = self.client.post(
                reverse("master-channel-resolve-id"),
                {"bot": bot.pk, "channel_link": "https://t.me/mol_test_channel/72"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "chat_id": "-1004418203649"})
        self.assertTrue(
            AuditEvent.objects.filter(
                action="telegram.channel.resolve_id",
                object_id="-1004418203649",
            ).exists()
        )

    def test_master_telegram_page_contains_channel_id_dialog(self):
        self.client.force_login(self.master)
        response = self.client.get(reverse("master-telegram"))
        self.assertContains(response, "data-channel-resolver")
        self.assertContains(response, "data-channel-result-dialog")
        self.assertContains(response, "data-copy-channel-id")

    def test_upload_page_uses_checkbox_channel_picker(self):
        self.client.force_login(self.sub1)
        response = self.client.get(reverse("user-content-upload"))
        self.assertContains(response, 'class="channel-picker"')
        self.assertContains(response, 'type="checkbox"')
        self.assertNotContains(response, '<select name="channels"')

    def test_master_channel_assignment_uses_checkboxes_and_accepts_multiple_channels(self):
        second_channel = TelegramChannel.objects.create(
            title="第二频道", telegram_chat_id=-1001234567891, created_by=self.master
        )
        self.client.force_login(self.master)
        response = self.client.get(reverse("master-subaccount-detail", args=[self.sub1.pk]))
        self.assertContains(response, 'class="channel-picker"')
        self.assertContains(response, 'type="checkbox"', count=2)
        self.assertNotContains(response, '<select name="channels"')

        response = self.client.post(
            reverse("master-subaccount-detail", args=[self.sub1.pk]),
            {"action": "channels", "channels": [self.channel.pk, second_channel.pk]},
        )
        self.assertRedirects(
            response, reverse("master-subaccount-detail", args=[self.sub1.pk]), fetch_redirect_response=False
        )
        self.assertEqual(
            set(self.sub1.channel_accesses.values_list("channel_id", flat=True)),
            {self.channel.pk, second_channel.pk},
        )

    def test_subaccount_profile_lists_only_its_assigned_channels(self):
        other_channel = TelegramChannel.objects.create(
            title="乙账号频道", telegram_chat_id=-1001234567892, username="sub2_only", created_by=self.master
        )
        UserChannelAccess.objects.create(user=self.sub2, channel=other_channel)
        self.client.force_login(self.sub1)
        response = self.client.get(reverse("user-profile"))
        self.assertContains(response, "我的可用频道")
        self.assertContains(response, self.channel.title)
        self.assertContains(response, str(self.channel.telegram_chat_id))
        self.assertNotContains(response, other_channel.title)

    def test_upload_media_inputs_append_and_have_one_click_clear(self):
        self.client.force_login(self.sub1)
        response = self.client.get(reverse("user-content-upload"))
        self.assertContains(response, 'data-append-files="true"', count=2)
        self.assertContains(response, 'data-max-files="10"', count=2)
        self.assertContains(response, 'data-clear-file-input="id_files_primary"')
        self.assertContains(response, 'data-clear-file-input="id_files_secondary"')
        self.assertContains(response, "一键清空本组媒体", count=2)

    def test_channel_supports_multiple_bots_with_distinct_priority(self):
        bot1 = TelegramBot.objects.create(display_name="A", token_ciphertext="encrypted-a", created_by=self.master)
        bot2 = TelegramBot.objects.create(display_name="B", token_ciphertext="encrypted-b", created_by=self.master)
        BotChannelBinding.objects.create(bot=bot1, channel=self.channel, priority=1)
        BotChannelBinding.objects.create(bot=bot2, channel=self.channel, priority=2)
        self.assertEqual(self.channel.bot_bindings.count(), 2)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BotChannelBinding.objects.create(bot=bot2, channel=self.channel, priority=1)

    def test_subaccount_content_query_is_isolated(self):
        Content.objects.create(owner_user=self.sub1, code="1", title="甲.1", created_by=self.sub1, updated_by=self.sub1)
        Content.objects.create(owner_user=self.sub2, code="1", title="乙.1", created_by=self.sub2, updated_by=self.sub2)
        self.client.force_login(self.sub1)
        response = self.client.get(reverse("user-contents"), {"status": "unpublished"})
        self.assertContains(response, "甲.1")
        self.assertNotContains(response, "乙.1")

    def test_subaccount_can_create_atomic_two_stage_content(self):
        self.client.force_login(self.sub1)
        response = self.client.post(
            reverse("user-content-upload"),
            {
                "text": "测试内容",
                "channels": [self.channel.pk],
                "cycle_days": "",
                "cycle_time": "",
                "publish_at": "",
                "files_secondary": SimpleUploadedFile("second.mp4", b"video", content_type="video/mp4"),
            },
        )
        content = Content.objects.get(owner_user=self.sub1)
        self.assertEqual(content.title, "甲.1")
        self.assertEqual(list(content.channels.values_list("pk", flat=True)), [self.channel.pk])
        self.assertRedirects(response, reverse("user-content-view", args=[content.pk]), fetch_redirect_response=False)

    def test_new_content_rejects_missing_second_stage_media(self):
        self.client.force_login(self.sub1)
        response = self.client.post(
            reverse("user-content-upload"),
            {"text": "测试内容", "channels": [self.channel.pk]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "第二次发送必须至少保留或上传一个媒体文件")
        self.assertFalse(Content.objects.filter(owner_user=self.sub1).exists())

    def test_due_content_is_enqueued_once(self):
        content = Content.objects.create(
            owner_user=self.sub1,
            code="2",
            title="scheduled",
            next_run_at=timezone.now(),
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        self.assertEqual(enqueue_due_content_operations(), 1)
        self.assertEqual(enqueue_due_content_operations(), 0)
        operation = Operation.objects.get(target_id=str(content.pk))
        self.assertEqual(operation.source, Operation.Source.SCHEDULER)
        self.assertEqual(operation.action, Operation.Action.SEND)

    def test_scheduled_task_pages_are_scoped(self):
        run_at = timezone.now() + timedelta(days=1)
        own = Content.objects.create(
            owner_user=self.sub1,
            code="task-1",
            title="甲定时",
            publish_at=run_at,
            next_run_at=run_at,
            cycle_days=1,
            cycle_time=time(4, 0),
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        own.channels.add(self.channel)
        other = Content.objects.create(
            owner_user=self.sub2,
            code="task-2",
            title="乙定时",
            publish_at=run_at,
            next_run_at=run_at,
            created_by=self.sub2,
            updated_by=self.sub2,
        )

        self.client.force_login(self.sub1)
        response = self.client.get(reverse("user-tasks"))
        self.assertContains(response, "甲定时")
        self.assertContains(response, "每 1 天 04:00")
        self.assertNotContains(response, "乙定时")

        self.client.force_login(self.master)
        response = self.client.get(reverse("master-tasks"))
        self.assertContains(response, "甲定时")
        self.assertContains(response, "乙定时")
        self.assertContains(response, self.sub1.username)
        self.assertContains(response, self.sub2.username)

    def test_subaccount_can_stop_scheduled_task_and_cancel_scheduler_queue(self):
        run_at = timezone.now() + timedelta(days=1)
        content = Content.objects.create(
            owner_user=self.sub1,
            code="task-stop",
            title="待中止任务",
            publish_at=run_at,
            next_run_at=run_at,
            cycle_days=2,
            cycle_time=time(6, 30),
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        operation = Operation.objects.create(
            owner_user=self.sub1,
            actor_type=Operation.ActorType.SYSTEM,
            actor_id="scheduler",
            source=Operation.Source.SCHEDULER,
            action=Operation.Action.REPLACE,
            target_type="content",
            target_id=str(content.pk),
            request_json={"reason": "SCHEDULED_UPDATE"},
            idempotency_key="task-stop-queue",
            state=Operation.State.QUEUED,
            available_at=timezone.now(),
        )

        self.client.force_login(self.sub1)
        response = self.client.post(reverse("user-task-stop", args=[content.pk]))
        self.assertRedirects(response, reverse("user-tasks"), fetch_redirect_response=False)
        content.refresh_from_db()
        operation.refresh_from_db()
        self.assertIsNone(content.publish_at)
        self.assertIsNone(content.next_run_at)
        self.assertIsNone(content.cycle_days)
        self.assertIsNone(content.cycle_time)
        self.assertEqual(operation.state, Operation.State.CANCELLED)
        self.assertTrue(AuditEvent.objects.filter(action="scheduled_task.stop", object_id=str(content.pk)).exists())

    def test_master_can_stop_subaccount_scheduled_task(self):
        run_at = timezone.now() + timedelta(days=2)
        content = Content.objects.create(
            owner_user=self.sub1,
            code="master-stop",
            title="主账号中止",
            publish_at=run_at,
            next_run_at=run_at,
            cycle_days=3,
            cycle_time=time(8, 0),
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        self.client.force_login(self.master)
        response = self.client.post(reverse("master-task-stop", args=[content.pk]))
        self.assertRedirects(response, reverse("master-tasks"), fetch_redirect_response=False)
        content.refresh_from_db()
        self.assertIsNone(content.next_run_at)
        self.assertIsNone(content.cycle_days)

    def test_published_content_must_be_unpublished_before_archive(self):
        content = Content.objects.create(
            owner_user=self.sub1,
            code="3",
            title="published",
            status=Content.Status.PUBLISHED,
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        self.client.force_login(self.sub1)
        response = self.client.post(reverse("user-content-delete", args=[content.pk]))
        self.assertRedirects(response, reverse("user-content-view", args=[content.pk]), fetch_redirect_response=False)
        content.refresh_from_db()
        self.assertIsNone(content.deleted_at)

    def test_unpublish_stops_cycle_and_cancels_queued_scheduler_operation(self):
        content = Content.objects.create(
            owner_user=self.sub1,
            code="stop-on-unpublish",
            title="下架停止循环",
            status=Content.Status.PUBLISHED,
            publish_at=timezone.now() - timedelta(days=2),
            cycle_days=2,
            cycle_time=time(14, 26),
            next_run_at=timezone.now() + timedelta(days=2),
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        content.channels.add(self.channel)
        scheduled_operation = Operation.objects.create(
            owner_user=self.sub1,
            actor_type=Operation.ActorType.SYSTEM,
            actor_id="scheduler",
            source=Operation.Source.SCHEDULER,
            action=Operation.Action.REPLACE,
            target_type="content",
            target_id=str(content.pk),
            request_json={"reason": "SCHEDULED_UPDATE"},
            idempotency_key="unpublish-cancels-cycle",
            state=Operation.State.QUEUED,
            available_at=timezone.now() + timedelta(days=2),
        )

        self.client.force_login(self.sub1)
        response = self.client.post(reverse("user-content-unpublish", args=[content.pk]))
        self.assertRedirects(response, reverse("user-content-logs", args=[content.pk]), fetch_redirect_response=False)
        content.refresh_from_db()
        scheduled_operation.refresh_from_db()
        self.assertEqual(content.status, Content.Status.UNPUBLISHING)
        self.assertIsNone(content.publish_at)
        self.assertIsNone(content.cycle_days)
        self.assertIsNone(content.cycle_time)
        self.assertIsNone(content.next_run_at)
        self.assertEqual(scheduled_operation.state, Operation.State.CANCELLED)
        self.assertTrue(
            Operation.objects.filter(target_id=str(content.pk), action=Operation.Action.DELETE, state=Operation.State.QUEUED).exists()
        )
        task_response = self.client.get(reverse("user-tasks"))
        self.assertNotContains(task_response, content.title)

    def test_stale_scheduler_operation_is_cancelled_before_sending(self):
        content = Content.objects.create(
            owner_user=self.sub1,
            code="stale-scheduler",
            title="已停止自动任务",
            status=Content.Status.UNPUBLISHED,
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        content.channels.add(self.channel)
        operation = Operation.objects.create(
            owner_user=self.sub1,
            actor_type=Operation.ActorType.SYSTEM,
            actor_id="scheduler",
            source=Operation.Source.SCHEDULER,
            action=Operation.Action.SEND,
            target_type="content",
            target_id=str(content.pk),
            idempotency_key="stale-scheduler-operation",
            state=Operation.State.QUEUED,
            available_at=timezone.now(),
        )
        process_operation(operation)
        operation.refresh_from_db()
        self.assertEqual(operation.state, Operation.State.CANCELLED)
        self.assertEqual(operation.telegram_error_code, "TASK_STOPPED")

    def test_scheduled_task_audit_command_supports_dry_run_and_fix(self):
        content = Content.objects.create(
            owner_user=self.sub1,
            code="history-audit",
            title="历史循环脏数据",
            status=Content.Status.PARTIAL,
            cycle_days=3,
            cycle_time=time(9, 30),
            next_run_at=timezone.now() + timedelta(days=3),
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        scheduled_operation = Operation.objects.create(
            owner_user=self.sub1,
            actor_type=Operation.ActorType.SYSTEM,
            actor_id="scheduler",
            source=Operation.Source.SCHEDULER,
            action=Operation.Action.REPLACE,
            target_type="content",
            target_id=str(content.pk),
            idempotency_key="history-audit-scheduler",
            state=Operation.State.QUEUED,
            available_at=timezone.now() + timedelta(days=3),
        )
        Operation.objects.create(
            owner_user=self.sub1,
            actor_type=Operation.ActorType.WEB_USER,
            actor_id=str(self.sub1.pk),
            source=Operation.Source.WEB,
            action=Operation.Action.DELETE,
            target_type="content",
            target_id=str(content.pk),
            idempotency_key="history-audit-delete",
            state=Operation.State.PARTIAL,
            available_at=timezone.now(),
            finished_at=timezone.now(),
        )

        dry_output = StringIO()
        call_command("audit_scheduled_tasks", stdout=dry_output)
        content.refresh_from_db()
        scheduled_operation.refresh_from_db()
        self.assertIn("内容配置异常 1", dry_output.getvalue())
        self.assertIsNotNone(content.cycle_days)
        self.assertEqual(scheduled_operation.state, Operation.State.QUEUED)

        fix_output = StringIO()
        call_command("audit_scheduled_tasks", "--fix", stdout=fix_output)
        content.refresh_from_db()
        scheduled_operation.refresh_from_db()
        self.assertIn("已修复内容 1", fix_output.getvalue())
        self.assertIsNone(content.cycle_days)
        self.assertIsNone(content.cycle_time)
        self.assertIsNone(content.next_run_at)
        self.assertEqual(scheduled_operation.state, Operation.State.CANCELLED)
        self.assertTrue(AuditEvent.objects.filter(action="scheduled_task.reconcile").exists())

    def test_editing_published_media_enqueues_replace(self):
        content = Content.objects.create(
            owner_user=self.sub1,
            code="4",
            title="media",
            text="old",
            status=Content.Status.PUBLISHED,
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        content.channels.add(self.channel)
        self.client.force_login(self.sub1)
        response = self.client.post(
            reverse("user-content-edit", args=[content.pk]),
            {
                "text": "new",
                "channels": [self.channel.pk],
                "cycle_days": "",
                "cycle_time": "",
                "publish_at": "",
                "files_primary": image_upload("new.jpg"),
                "files_secondary": SimpleUploadedFile("second.mp4", b"video", content_type="video/mp4"),
            },
        )
        self.assertRedirects(response, reverse("user-content-view", args=[content.pk]), fetch_redirect_response=False)
        self.assertTrue(Operation.objects.filter(target_id=str(content.pk), action=Operation.Action.REPLACE).exists())

    def test_file_delete_is_committed_only_with_whole_edit(self):
        content = Content.objects.create(
            owner_user=self.sub1,
            code="stage-edit",
            title="stage-edit",
            text="old",
            status=Content.Status.PUBLISHED,
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        content.channels.add(self.channel)
        attach_files(content, [image_upload("first.jpg")], group_no=1)
        attach_files(content, [SimpleUploadedFile("second.mp4", b"video", content_type="video/mp4")], group_no=2)
        first = content.files.get(group_no=1)
        second = content.files.get(group_no=2)
        self.client.force_login(self.sub1)
        response = self.client.post(
            reverse("user-content-edit", args=[content.pk]),
            {
                "text": "new",
                "channels": [self.channel.pk],
                "delete_file_ids": str(first.pk),
                "file_order": f"{first.pk},{second.pk}",
            },
        )
        self.assertRedirects(response, reverse("user-content-view", args=[content.pk]), fetch_redirect_response=False)
        self.assertFalse(ContentFile.objects.filter(pk=first.pk).exists())
        self.assertTrue(ContentFile.objects.filter(pk=second.pk).exists())
        operation = Operation.objects.get(target_id=str(content.pk), action=Operation.Action.REPLACE)
        self.assertTrue(operation.request_json["force_resend"])

    def test_external_message_form_accepts_telegram_link(self):
        form = ExternalMessageActionForm(
            {
                "channel": self.channel.pk,
                "telegram_message_id": "https://t.me/example/321",
                "action": ExternalMessageActionForm.Action.EDIT_TEXT,
                "text": "changed",
            },
            user=self.sub1,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["telegram_message_id"], 321)

    def test_telegram_edit_command_updates_and_enqueues_replacement(self):
        bot = TelegramBot.objects.create(
            display_name="command-bot",
            token_ciphertext=encrypt_secret("123456:token"),
            webhook_secret_ciphertext=encrypt_secret("secret"),
            created_by=self.master,
        )
        TelegramUserLink.objects.create(user=self.sub1, bot=bot, telegram_user_id=10001)
        content = Content.objects.create(
            owner_user=self.sub1,
            code="5",
            title="command",
            text="old",
            status=Content.Status.PUBLISHED,
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        content.channels.add(self.channel)
        reply = _handle_command(
            bot_record=bot,
            update_id=100,
            chat_id=10001,
            telegram_user_id=10001,
            text="/编辑 5 新文字",
        )
        content.refresh_from_db()
        self.assertEqual(content.text, "新文字")
        self.assertIn("替换发布队列", reply)
        operation = Operation.objects.get(target_id=str(content.pk), action=Operation.Action.REPLACE)
        self.assertTrue(operation.request_json["force_resend"])

    def test_manual_resend_is_removed_from_web_and_bot(self):
        bot = TelegramBot.objects.create(
            display_name="no-resend-bot",
            token_ciphertext=encrypt_secret("123456:token"),
            webhook_secret_ciphertext=encrypt_secret("secret"),
            created_by=self.master,
        )
        TelegramUserLink.objects.create(user=self.sub1, bot=bot, telegram_user_id=10003)
        content = Content.objects.create(
            owner_user=self.sub1,
            code="no-resend",
            title="no-resend",
            status=Content.Status.PUBLISHED,
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        content.channels.add(self.channel)

        self.client.force_login(self.sub1)
        detail_response = self.client.get(reverse("user-content-view", args=[content.pk]))
        self.assertNotContains(detail_response, ">重发<")
        removed_route_response = self.client.post(f"/user/content/{content.pk}/resend")
        self.assertEqual(removed_route_response.status_code, 404)

        reply = _handle_command(
            bot_record=bot,
            update_id=103,
            chat_id=10003,
            telegram_user_id=10003,
            text="/重发 no-resend",
        )
        self.assertNotIn("/重发", reply)
        self.assertFalse(Operation.objects.filter(target_id=str(content.pk), action=Operation.Action.COPY).exists())

    def test_telegram_delete_requires_confirmation(self):
        bot = TelegramBot.objects.create(
            display_name="confirm-bot",
            token_ciphertext=encrypt_secret("123456:token"),
            webhook_secret_ciphertext=encrypt_secret("secret"),
            created_by=self.master,
        )
        TelegramUserLink.objects.create(user=self.sub1, bot=bot, telegram_user_id=10002)
        content = Content.objects.create(
            owner_user=self.sub1,
            code="6",
            title="confirm",
            status=Content.Status.PUBLISHED,
            cycle_days=1,
            cycle_time=time(4, 0),
            next_run_at=timezone.now() + timedelta(days=1),
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        content.channels.add(self.channel)
        first_reply = _handle_command(
            bot_record=bot,
            update_id=101,
            chat_id=10002,
            telegram_user_id=10002,
            text="/下架 6",
        )
        self.assertIn("/确认", first_reply)
        self.assertFalse(Operation.objects.filter(target_id=str(content.pk), action=Operation.Action.DELETE).exists())
        pending = TelegramPendingAction.objects.get(content=content)
        second_reply = _handle_command(
            bot_record=bot,
            update_id=102,
            chat_id=10002,
            telegram_user_id=10002,
            text=f"/确认 {pending.token}",
        )
        self.assertIn("任务已加入队列", second_reply)
        self.assertTrue(Operation.objects.filter(target_id=str(content.pk), action=Operation.Action.DELETE).exists())
        content.refresh_from_db()
        self.assertIsNone(content.cycle_days)
        self.assertIsNone(content.cycle_time)
        self.assertIsNone(content.next_run_at)

    def test_master_channel_edit_enqueues_remote_update(self):
        self.client.force_login(self.master)
        response = self.client.post(
            reverse("master-channel-edit", args=[self.channel.pk]),
            {
                "title": "新频道名称",
                "username": "",
                "description": "新简介",
                "route_policy": TelegramChannel.RoutePolicy.PRIMARY_ONLY,
            },
        )
        self.assertRedirects(response, reverse("master-operations"), fetch_redirect_response=False)
        operation = Operation.objects.get(target_type="channel", target_id=str(self.channel.pk))
        self.assertEqual(operation.action, Operation.Action.UPDATE_CHANNEL)

    def test_subaccount_navigation_hides_external_message_tool(self):
        self.client.force_login(self.sub1)
        response = self.client.get(reverse("user-contents"))
        self.assertNotContains(response, "指定消息操作")

    def test_traditional_variant_converts_simplified_input(self):
        content = Content.objects.create(
            owner_user=self.sub1,
            code="traditional",
            title="traditional",
            text="后台发送简体消息",
            text_variant=Content.TextVariant.TRADITIONAL,
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        self.assertEqual(render_content_text(content), "後臺發送簡體消息")

    def test_scheduled_update_replaces_and_deletes_old_delivery(self):
        content = Content.objects.create(
            owner_user=self.sub1,
            code="cycle",
            title="cycle",
            status=Content.Status.PUBLISHED,
            next_run_at=timezone.now(),
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        self.assertEqual(enqueue_due_content_operations(), 1)
        operation = Operation.objects.get(target_id=str(content.pk))
        self.assertEqual(operation.action, Operation.Action.REPLACE)
        self.assertTrue(operation.request_json["force_resend"])

    def test_bulk_select_can_enqueue_multiple_unpublishes(self):
        first = Content.objects.create(owner_user=self.sub1, code="b1", title="b1", status=Content.Status.PUBLISHED)
        second = Content.objects.create(owner_user=self.sub1, code="b2", title="b2", status=Content.Status.PUBLISHED)
        first.channels.add(self.channel)
        second.channels.add(self.channel)
        self.client.force_login(self.sub1)
        response = self.client.post(
            reverse("user-contents-bulk"),
            {
                "content_ids": [first.pk, second.pk],
                "bulk_action": "unpublish",
                "status": "active",
                "page_nonce": "bulk-test",
            },
        )
        self.assertRedirects(response, f"{reverse('user-contents')}?status=active", fetch_redirect_response=False)
        self.assertEqual(Operation.objects.filter(action=Operation.Action.DELETE).count(), 2)

    def test_failover_binding_order_is_deterministic(self):
        bot1 = TelegramBot.objects.create(display_name="primary", token_ciphertext=encrypt_secret("1:token"), created_by=self.master)
        bot2 = TelegramBot.objects.create(display_name="backup", token_ciphertext=encrypt_secret("2:token"), created_by=self.master)
        BotChannelBinding.objects.create(bot=bot2, channel=self.channel, priority=2, can_post_messages=True)
        BotChannelBinding.objects.create(bot=bot1, channel=self.channel, priority=1, can_post_messages=True)
        self.channel.route_policy = TelegramChannel.RoutePolicy.FAILOVER
        self.channel.save(update_fields=("route_policy", "updated_at"))
        self.assertEqual([item.bot.display_name for item in resolve_bindings(self.channel)], ["primary", "backup"])

    def test_worker_processes_channel_update_operation(self):
        bot = TelegramBot.objects.create(display_name="channel-admin", token_ciphertext=encrypt_secret("1:token"), created_by=self.master)
        BotChannelBinding.objects.create(bot=bot, channel=self.channel, priority=1, can_change_info=True)
        operation, _ = enqueue_operation(
            owner_user=self.master,
            actor=self.master,
            source=Operation.Source.WEB,
            action=Operation.Action.UPDATE_CHANNEL,
            target_type="channel",
            target_id=str(self.channel.pk),
            request_json={"title": "new", "description": "description", "photo_path": "", "remove_photo": False},
            idempotency_key="channel-update-test",
        )
        with patch("channel_manager.operation_processor._update_channel_info", new=AsyncMock(return_value=(2, []))):
            process_operation(operation)
        operation.refresh_from_db()
        self.assertEqual(operation.state, Operation.State.SUCCEEDED)

    def test_worker_processes_external_message_edit(self):
        bot = TelegramBot.objects.create(display_name="message-admin", token_ciphertext=encrypt_secret("1:token"), created_by=self.master)
        BotChannelBinding.objects.create(bot=bot, channel=self.channel, priority=1, can_edit_messages=True)
        operation, _ = enqueue_operation(
            owner_user=self.sub1,
            actor=self.sub1,
            source=Operation.Source.WEB,
            action=Operation.Action.EDIT_EXTERNAL,
            target_type="external_message",
            target_id=f"{self.channel.pk}:99",
            request_json={"channel_id": self.channel.pk, "telegram_message_id": 99, "mode": "EDIT_TEXT", "text": "new"},
            idempotency_key="external-edit-worker-test",
        )
        with patch("channel_manager.operation_processor._operate_external_message", new=AsyncMock(return_value=None)):
            process_operation(operation)
        operation.refresh_from_db()
        self.assertEqual(operation.state, Operation.State.SUCCEEDED)

    def test_anti_scan_image_creates_processed_copy(self):
        content = Content.objects.create(
            owner_user=self.sub1,
            code="7",
            title="anti-scan",
            anti_scan_enabled=True,
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        attach_files(content, [image_upload("photo.jpg")], group_no=1)
        item = ContentFile.objects.get(content=content)
        self.assertTrue(bool(item.processed_file))

    def test_extreme_photo_dimensions_are_normalized_for_telegram(self):
        content = Content.objects.create(
            owner_user=self.sub1,
            code="wide-photo",
            title="wide-photo",
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        attach_files(content, [image_upload("wide.png", size=(5000, 100), image_format="PNG")], group_no=1)
        item = ContentFile.objects.get(content=content)
        source_path = telegram_media_source_path(item, anti_scan_enabled=False)
        with Image.open(source_path) as image:
            width, height = image.size
        self.assertLessEqual(width + height, 10000)
        self.assertLessEqual(max(width, height) / min(width, height), 20)
        self.assertTrue(bool(item.processed_file))

    def test_worker_replaces_compatible_media_in_place(self):
        bot = TelegramBot.objects.create(display_name="media-editor", token_ciphertext=encrypt_secret("1:token"), created_by=self.master)
        binding = BotChannelBinding.objects.create(
            bot=bot,
            channel=self.channel,
            priority=1,
            can_post_messages=True,
            can_edit_messages=True,
            can_delete_messages=True,
        )
        content = Content.objects.create(
            owner_user=self.sub1,
            code="8",
            title="in-place",
            text="caption",
            status=Content.Status.PUBLISHED,
            version=2,
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        content.channels.add(self.channel)
        attach_files(content, [image_upload("new.jpg")], group_no=1)
        delivery = Delivery.objects.create(
            content=content,
            channel=self.channel,
            bot_channel_binding=binding,
            status=Delivery.Status.ACTIVE,
            content_version=1,
        )
        DeliveryMessage.objects.create(
            delivery=delivery,
            group_no=1,
            sort_index=0,
            telegram_message_id=10,
            payload_snapshot_json={"kind": "photo", "content_version": 1},
        )
        operation, _ = enqueue_content_operation(content=content, action=Operation.Action.REPLACE, actor=self.sub1)
        with patch("channel_manager.operation_processor._edit_delivery_media", new=AsyncMock(return_value=[])):
            process_operation(operation)
        operation.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(operation.state, Operation.State.SUCCEEDED)
        self.assertEqual(delivery.content_version, content.version)

    def test_private_message_link_converts_to_bot_api_chat_id(self):
        kind, chat_id = _parse_channel_reference("https://t.me/c/4418203649/2")
        self.assertEqual(kind, "id")
        self.assertEqual(chat_id, -1004418203649)

    def test_private_invite_uses_observed_channel_update(self):
        bot = TelegramBot.objects.create(display_name="resolver", token_ciphertext=encrypt_secret("1:token"), created_by=self.master)
        TelegramUpdate.objects.create(
            bot=bot,
            update_id=1,
            body_json={"update_id": 1, "channel_post": {"chat": {"id": -10099887766, "type": "channel", "title": "私有频道"}}},
        )
        result = resolve_channel_reference(bot, "https://t.me/+abcdef")
        self.assertEqual(result["chat_id"], -10099887766)

    def test_two_stage_send_rolls_back_first_message_when_second_fails(self):
        bot_record = TelegramBot.objects.create(display_name="atomic", token_ciphertext=encrypt_secret("1:token"), created_by=self.master)
        binding = BotChannelBinding.objects.create(
            bot=bot_record,
            channel=self.channel,
            priority=1,
            can_post_messages=True,
            can_delete_messages=True,
        )
        content = Content.objects.create(owner_user=self.sub1, code="atomic", title="atomic", text="caption")
        attach_files(content, [image_upload("first.jpg")], group_no=1)
        attach_files(content, [SimpleUploadedFile("second.mp4", b"video", content_type="video/mp4")], group_no=2)

        class FakeBot:
            def __init__(self):
                self.deleted = []

            async def send_photo(self, *args, **kwargs):
                return SimpleNamespace(message_id=101)

            async def send_video(self, *args, **kwargs):
                raise RuntimeError("second stage failed")

            async def delete_message(self, chat_id, message_id):
                self.deleted.append((chat_id, message_id))

        fake_bot = FakeBot()

        class FakeContext:
            async def __aenter__(self):
                return fake_bot

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with patch("channel_manager.operation_processor.create_bot", return_value=FakeContext()):
            with self.assertRaisesRegex(RuntimeError, "second stage failed"):
                async_to_sync(_send_to_telegram)(binding, content, list(content.files.all()))
        self.assertEqual(fake_bot.deleted, [(self.channel.telegram_chat_id, 101)])
