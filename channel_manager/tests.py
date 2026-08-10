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
from functools import lru_cache
from pathlib import Path
import subprocess
import tempfile
from io import BytesIO, StringIO
import imageio_ffmpeg
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
from .operation_processor import _prepare_media_paths, _send_to_telegram, process_operation, resolve_bindings
from .services import enqueue_operation
from .telegram_api import _parse_channel_reference, resolve_channel_reference


def image_upload(name: str = "photo.jpg", size: tuple[int, int] = (8, 8), image_format: str = "JPEG") -> SimpleUploadedFile:
    image_buffer = BytesIO()
    Image.new("RGB", size, color=(10, 20, 30)).save(image_buffer, format=image_format)
    content_type = "image/png" if image_format.upper() == "PNG" else "image/jpeg"
    return SimpleUploadedFile(name, image_buffer.getvalue(), content_type=content_type)


@lru_cache(maxsize=2)
def _video_bytes(rotated: bool = False) -> bytes:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="telegram-test-video-") as temp_dir:
        base_path = Path(temp_dir) / "base.mp4"
        subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=64x32:d=0.2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(base_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        source_path = base_path
        if rotated:
            source_path = Path(temp_dir) / "rotated.mp4"
            subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-display_rotation:v:0",
                    "90",
                    "-i",
                    str(base_path),
                    "-c",
                    "copy",
                    str(source_path),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        return source_path.read_bytes()


def video_upload(name: str = "video.mp4", *, rotated: bool = False) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, _video_bytes(rotated), content_type="video/mp4")


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

    def test_upload_page_selects_all_available_channels_by_default(self):
        second_channel = TelegramChannel.objects.create(
            title="第二频道", telegram_chat_id=-1001234567891, created_by=self.master
        )
        UserChannelAccess.objects.create(user=self.sub1, channel=second_channel)

        self.client.force_login(self.sub1)
        response = self.client.get(reverse("user-content-upload"))

        selected_ids = {int(channel_id) for channel_id in response.context["form"]["channels"].value()}
        self.assertEqual(selected_ids, {self.channel.pk, second_channel.pk})

    def test_edit_page_selects_all_available_channels_by_default(self):
        second_channel = TelegramChannel.objects.create(
            title="第二频道", telegram_chat_id=-1001234567891, created_by=self.master
        )
        UserChannelAccess.objects.create(user=self.sub1, channel=second_channel)
        content = Content.objects.create(
            owner_user=self.sub1,
            code="edit-all",
            title="甲.全选",
            text="测试",
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        content.channels.add(self.channel)

        self.client.force_login(self.sub1)
        response = self.client.get(reverse("user-content-edit", args=[content.pk]))

        selected_ids = {int(channel_id) for channel_id in response.context["form"]["channels"].value()}
        self.assertEqual(selected_ids, {self.channel.pk, second_channel.pk})

    def test_invalid_bound_content_form_keeps_submitted_channel_selection(self):
        second_channel = TelegramChannel.objects.create(
            title="第二频道", telegram_chat_id=-1001234567891, created_by=self.master
        )
        UserChannelAccess.objects.create(user=self.sub1, channel=second_channel)

        self.client.force_login(self.sub1)
        response = self.client.post(
            reverse("user-content-upload"),
            {"text": "缺少第二阶段媒体", "channels": [self.channel.pk]},
        )

        self.assertEqual(response.status_code, 200)
        selected_ids = {int(channel_id) for channel_id in response.context["form"]["channels"].value()}
        self.assertEqual(selected_ids, {self.channel.pk})

    def test_master_channel_assignment_uses_checkboxes_and_accepts_multiple_channels(self):
        second_channel = TelegramChannel.objects.create(
            title="第二频道", telegram_chat_id=-1001234567891, created_by=self.master
        )
        self.client.force_login(self.master)
        response = self.client.get(reverse("master-subaccount-detail", args=[self.sub1.pk]))
        self.assertContains(response, 'class="channel-picker"')
        self.assertContains(response, 'type="checkbox"', count=2)
        self.assertNotContains(response, '<select name="channels"')
        selected_ids = {int(channel_id) for channel_id in response.context["access_form"]["channels"].value()}
        self.assertEqual(selected_ids, {self.channel.pk, second_channel.pk})

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

    def test_subaccount_create_page_selects_all_channels_by_default(self):
        second_channel = TelegramChannel.objects.create(
            title="第二频道", telegram_chat_id=-1001234567891, created_by=self.master
        )
        self.client.force_login(self.master)

        response = self.client.get(reverse("master-subaccount-create"))

        selected_ids = {int(channel_id) for channel_id in response.context["channel_form"]["channels"].value()}
        self.assertEqual(selected_ids, {self.channel.pk, second_channel.pk})

    def test_invalid_channel_assignment_keeps_master_submitted_selection(self):
        TelegramChannel.objects.create(
            title="第二频道", telegram_chat_id=-1001234567891, created_by=self.master
        )
        self.client.force_login(self.master)

        response = self.client.post(
            reverse("master-subaccount-detail", args=[self.sub1.pk]),
            {"action": "channels", "channels": [self.channel.pk, "invalid"]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["access_form"]["channels"].value(), [str(self.channel.pk), "invalid"])

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
                "files_secondary": video_upload("second.mp4"),
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
                "files_secondary": video_upload("second.mp4"),
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
        attach_files(content, [video_upload("second.mp4")], group_no=2)
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

    def test_scheduled_replace_continues_when_old_messages_are_retained(self):
        content = Content.objects.create(
            owner_user=self.sub1,
            code="cycle-retained",
            title="cycle-retained",
            status=Content.Status.PUBLISHED,
            cycle_days=2,
            cycle_time=time(13, 13),
            next_run_at=timezone.now(),
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        content.channels.add(self.channel)
        old_delivery = Delivery.objects.create(
            content=content,
            channel=self.channel,
            status=Delivery.Status.ACTIVE,
            sent_at=timezone.now() - timedelta(days=3),
            deletable_until=timezone.now() - timedelta(days=1),
        )
        old_message = DeliveryMessage.objects.create(
            delivery=old_delivery,
            telegram_message_id=8100,
        )
        operation, _ = enqueue_content_operation(
            content=content,
            action=Operation.Action.REPLACE,
            actor="scheduler",
            source=Operation.Source.SCHEDULER,
            request_json={"force_resend": True, "reason": "SCHEDULED_UPDATE"},
        )
        expected_next_run = timezone.now() + timedelta(days=2)

        def fake_send(current_operation, current_content):
            Delivery.objects.create(
                content=current_content,
                channel=self.channel,
                operation=current_operation,
                status=Delivery.Status.ACTIVE,
                sent_at=timezone.now(),
                deletable_until=timezone.now() + timedelta(hours=48),
            )
            current_content.status = Content.Status.PUBLISHED
            current_content.next_run_at = expected_next_run
            current_content.save(update_fields=("status", "next_run_at", "updated_at"))
            current_operation.state = Operation.State.SUCCEEDED

        with patch(
            "channel_manager.operation_processor._process_send",
            side_effect=fake_send,
        ):
            process_operation(operation)

        operation.refresh_from_db()
        content.refresh_from_db()
        old_delivery.refresh_from_db()
        old_message.refresh_from_db()
        self.assertEqual(operation.state, Operation.State.SUCCEEDED)
        self.assertEqual(operation.telegram_error_code, "REPLACE_SENT_OLD_RETAINED")
        self.assertIn("新内容已发送，历史消息需要手动删除，循环任务继续", operation.telegram_error_text)
        self.assertEqual(content.status, Content.Status.PUBLISHED)
        self.assertEqual(content.next_run_at, expected_next_run)
        self.assertEqual(old_delivery.status, Delivery.Status.RETAINED)
        self.assertEqual(old_message.status, DeliveryMessage.Status.RETAINED)
        self.assertTrue(
            content.deliveries.filter(
                operation=operation,
                status=Delivery.Status.ACTIVE,
            ).exists()
        )

        self.client.force_login(self.sub1)
        response = self.client.get(reverse("user-content-logs", args=[content.pk]))
        self.assertContains(response, "新内容已发送，历史消息需要手动删除，循环任务继续")

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

    def test_unverified_binding_is_verified_before_worker_resolves_it(self):
        bot = TelegramBot.objects.create(
            display_name="lazy-verify",
            token_ciphertext=encrypt_secret("1:token"),
            created_by=self.master,
        )
        binding = BotChannelBinding.objects.create(bot=bot, channel=self.channel, priority=1)

        def fake_verify(current_binding):
            current_binding.can_post_messages = True
            current_binding.can_delete_messages = True
            current_binding.last_verified_at = timezone.now()
            current_binding.save(
                update_fields=("can_post_messages", "can_delete_messages", "last_verified_at", "updated_at")
            )

        with patch("channel_manager.operation_processor.verify_binding", side_effect=fake_verify) as verify_mock:
            resolved = resolve_bindings(self.channel)

        verify_mock.assert_called_once()
        self.assertEqual([item.pk for item in resolved], [binding.pk])

    def test_edit_added_channel_is_included_in_next_scheduled_replace(self):
        second_channel = TelegramChannel.objects.create(
            title="循环新增频道",
            telegram_chat_id=-1001234567891,
            created_by=self.master,
        )
        UserChannelAccess.objects.create(user=self.sub1, channel=second_channel)
        bot = TelegramBot.objects.create(
            display_name="cycle-bot",
            telegram_bot_id=998877,
            token_ciphertext=encrypt_secret("1:token"),
            created_by=self.master,
        )
        first_binding = BotChannelBinding.objects.create(
            bot=bot,
            channel=self.channel,
            priority=1,
            can_post_messages=True,
            can_delete_messages=True,
        )
        second_binding = BotChannelBinding.objects.create(
            bot=bot,
            channel=second_channel,
            priority=1,
            can_post_messages=True,
            can_delete_messages=True,
        )
        bindings = {
            self.channel.pk: first_binding,
            second_channel.pk: second_binding,
        }
        content = Content.objects.create(
            owner_user=self.sub1,
            code="cycle-add-channel",
            title="甲.新增频道循环",
            text="原内容",
            status=Content.Status.PUBLISHED,
            cycle_days=1,
            cycle_time=time(4, 0),
            next_run_at=timezone.now() + timedelta(days=1),
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        content.channels.add(self.channel)
        attach_files(content, [image_upload("first.jpg")], group_no=1)
        attach_files(
            content,
            [video_upload("second.mp4")],
            group_no=2,
        )

        self.client.force_login(self.sub1)
        response = self.client.post(
            reverse("user-content-edit", args=[content.pk]),
            {
                "text": "编辑后的内容",
                "text_variant": Content.TextVariant.SIMPLIFIED,
                "channels": [self.channel.pk, second_channel.pk],
                "cycle_days": "1",
                "cycle_time": "04:00",
                "publish_at": "",
                "delete_file_ids": "",
                "file_order": "",
            },
        )
        self.assertRedirects(response, reverse("user-content-view", args=[content.pk]), fetch_redirect_response=False)
        content.refresh_from_db()
        expected_channel_ids = {self.channel.pk, second_channel.pk}
        self.assertEqual(set(content.channels.values_list("pk", flat=True)), expected_channel_ids)

        edit_operation = Operation.objects.get(
            target_id=str(content.pk),
            action=Operation.Action.REPLACE,
            source=Operation.Source.WEB,
        )
        self.assertEqual(set(edit_operation.request_json["channel_ids"]), expected_channel_ids)

        sent_channel_ids = []
        message_id = 5000

        def fake_send(channel, current_content, files, media_paths):
            nonlocal message_id
            sent_channel_ids.append(channel.pk)
            message_id += 2
            return bindings[channel.pk], [
                (SimpleNamespace(message_id=message_id - 1), 1, 0, "photo"),
                (SimpleNamespace(message_id=message_id), 2, 0, "video"),
            ]

        def fake_delete(delivery):
            delivery.messages.update(status=DeliveryMessage.Status.DELETED, deleted_at=timezone.now())
            delivery.status = Delivery.Status.DELETED
            delivery.save(update_fields=("status", "updated_at"))
            return [], ""

        with patch("channel_manager.operation_processor._send_channel_with_failover", side_effect=fake_send), patch(
            "channel_manager.operation_processor._delete_delivery_record", side_effect=fake_delete
        ):
            process_operation(edit_operation)
            self.assertEqual(set(sent_channel_ids), expected_channel_ids)

            sent_channel_ids.clear()
            content.refresh_from_db()
            content.next_run_at = timezone.now() - timedelta(seconds=1)
            content.save(update_fields=("next_run_at", "updated_at"))
            self.assertEqual(enqueue_due_content_operations(), 1)
            scheduled_operation = Operation.objects.get(
                target_id=str(content.pk),
                action=Operation.Action.REPLACE,
                source=Operation.Source.SCHEDULER,
            )
            self.assertEqual(set(scheduled_operation.request_json["channel_ids"]), expected_channel_ids)
            process_operation(scheduled_operation)

        scheduled_operation.refresh_from_db()
        self.assertEqual(scheduled_operation.state, Operation.State.SUCCEEDED)
        self.assertEqual(set(sent_channel_ids), expected_channel_ids)
        self.assertEqual(
            set(scheduled_operation.deliveries.values_list("channel_id", flat=True)),
            expected_channel_ids,
        )

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

    def test_rotated_video_is_written_upright_for_telegram(self):
        content = Content.objects.create(
            owner_user=self.sub1,
            code="rotated-video",
            title="rotated-video",
            created_by=self.sub1,
            updated_by=self.sub1,
        )
        original_bytes = _video_bytes(rotated=True)
        attach_files(content, [video_upload("phone-portrait.mp4", rotated=True)], group_no=2)
        item = ContentFile.objects.get(content=content)

        original_reader = imageio_ffmpeg.read_frames(item.file.path)
        original_meta = next(original_reader)
        original_reader.close()
        self.assertEqual(original_meta["source_size"], (64, 32))
        self.assertEqual(original_meta["size"], (32, 64))

        source_path = telegram_media_source_path(item)
        item.refresh_from_db()
        normalized_reader = imageio_ffmpeg.read_frames(source_path)
        normalized_meta = next(normalized_reader)
        normalized_reader.close()

        self.assertTrue(bool(item.processed_file))
        self.assertNotEqual(Path(source_path), Path(item.file.path))
        self.assertEqual(normalized_meta["source_size"], (32, 64))
        self.assertEqual(normalized_meta["size"], (32, 64))
        self.assertEqual(normalized_meta.get("rotate", 0), 0)
        self.assertEqual(Path(item.file.path).read_bytes(), original_bytes)
        self.assertEqual(telegram_media_source_path(item), source_path)

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
        content = Content.objects.create(
            owner_user=self.sub1,
            code="atomic",
            title="atomic",
            text="caption",
            anti_scan_enabled=True,
        )
        attach_files(content, [image_upload("first.jpg")], group_no=1)
        attach_files(content, [video_upload("second.mp4")], group_no=2)
        files = list(content.files.all())
        media_paths = _prepare_media_paths(content, files)

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
                async_to_sync(_send_to_telegram)(binding, content, files, media_paths)
        self.assertEqual(fake_bot.deleted, [(self.channel.telegram_chat_id, 101)])

    def test_expired_delivery_is_marked_retained_on_unpublish(self):
        bot_record = TelegramBot.objects.create(
            display_name="delete-window",
            token_ciphertext=encrypt_secret("1:token"),
            created_by=self.master,
        )
        binding = BotChannelBinding.objects.create(
            bot=bot_record,
            channel=self.channel,
            priority=1,
            can_post_messages=True,
            can_delete_messages=True,
        )
        content = Content.objects.create(
            owner_user=self.sub1,
            code="expired-delete",
            title="expired-delete",
            status=Content.Status.PUBLISHED,
        )
        content.channels.add(self.channel)
        delivery = Delivery.objects.create(
            content=content,
            channel=self.channel,
            bot_channel_binding=binding,
            status=Delivery.Status.ACTIVE,
            sent_at=timezone.now() - timedelta(days=3),
            deletable_until=timezone.now() - timedelta(days=1),
        )
        message = DeliveryMessage.objects.create(
            delivery=delivery,
            telegram_message_id=9001,
        )
        operation, _ = enqueue_content_operation(
            content=content,
            action=Operation.Action.DELETE,
            actor=self.sub1,
        )

        with patch("channel_manager.operation_processor._delete_delivery") as delete_mock:
            process_operation(operation)

        delete_mock.assert_not_called()
        operation.refresh_from_db()
        content.refresh_from_db()
        delivery.refresh_from_db()
        message.refresh_from_db()
        self.assertEqual(operation.state, Operation.State.SUCCEEDED)
        self.assertEqual(operation.telegram_error_code, "UNPUBLISH_MANUAL_DELETE")
        self.assertIn("频道消息需要手动删除，循环任务已停止", operation.telegram_error_text)
        self.assertEqual(content.status, Content.Status.UNPUBLISHED_RETAINED)
        self.assertEqual(delivery.status, Delivery.Status.RETAINED)
        self.assertEqual(message.status, DeliveryMessage.Status.RETAINED)

    def test_unpublish_deletes_recent_messages_and_stops_cycle(self):
        bot_record = TelegramBot.objects.create(
            display_name="recent-delete",
            token_ciphertext=encrypt_secret("1:token"),
            created_by=self.master,
        )
        binding = BotChannelBinding.objects.create(
            bot=bot_record,
            channel=self.channel,
            priority=1,
            can_post_messages=True,
            can_delete_messages=True,
        )
        content = Content.objects.create(
            owner_user=self.sub1,
            code="recent-delete",
            title="recent-delete",
            status=Content.Status.PUBLISHED,
            cycle_days=1,
            cycle_time=time(4, 0),
            next_run_at=timezone.now() + timedelta(days=1),
        )
        content.channels.add(self.channel)
        delivery = Delivery.objects.create(
            content=content,
            channel=self.channel,
            bot_channel_binding=binding,
            status=Delivery.Status.ACTIVE,
            sent_at=timezone.now(),
            deletable_until=timezone.now() + timedelta(days=1),
        )
        message = DeliveryMessage.objects.create(
            delivery=delivery,
            telegram_message_id=9000,
        )
        scheduled_operation = Operation.objects.create(
            owner_user=self.sub1,
            actor_type=Operation.ActorType.SYSTEM,
            actor_id="scheduler",
            source=Operation.Source.SCHEDULER,
            action=Operation.Action.REPLACE,
            target_type="content",
            target_id=str(content.pk),
            idempotency_key="recent-delete-scheduled",
            state=Operation.State.QUEUED,
            available_at=timezone.now() + timedelta(hours=1),
        )
        operation, _ = enqueue_content_operation(
            content=content,
            action=Operation.Action.DELETE,
            actor=self.sub1,
        )

        deleted_result = ([message.pk], [], [])
        with patch(
            "channel_manager.operation_processor._delete_delivery",
            new=AsyncMock(return_value=deleted_result),
        ):
            process_operation(operation)

        operation.refresh_from_db()
        scheduled_operation.refresh_from_db()
        content.refresh_from_db()
        delivery.refresh_from_db()
        message.refresh_from_db()
        self.assertEqual(operation.state, Operation.State.SUCCEEDED)
        self.assertEqual(operation.telegram_error_code, "UNPUBLISH_DELETED")
        self.assertEqual(operation.telegram_error_text, "下架成功，频道消息已删除，循环任务已停止。")
        self.assertEqual(content.status, Content.Status.UNPUBLISHED)
        self.assertIsNone(content.cycle_days)
        self.assertIsNone(content.cycle_time)
        self.assertIsNone(content.next_run_at)
        self.assertEqual(scheduled_operation.state, Operation.State.CANCELLED)
        self.assertEqual(delivery.status, Delivery.Status.DELETED)
        self.assertEqual(message.status, DeliveryMessage.Status.DELETED)

        self.client.force_login(self.sub1)
        response = self.client.get(reverse("user-content-logs", args=[content.pk]))
        self.assertContains(response, "下架成功，频道消息已删除，循环任务已停止")

    def test_telegram_permanent_delete_rejection_is_not_retried(self):
        bot_record = TelegramBot.objects.create(
            display_name="delete-rejected",
            token_ciphertext=encrypt_secret("1:token"),
            created_by=self.master,
        )
        binding = BotChannelBinding.objects.create(
            bot=bot_record,
            channel=self.channel,
            priority=1,
            can_post_messages=True,
            can_delete_messages=True,
        )
        content = Content.objects.create(
            owner_user=self.sub1,
            code="rejected-delete",
            title="rejected-delete",
            status=Content.Status.PUBLISHED,
        )
        content.channels.add(self.channel)
        delivery = Delivery.objects.create(
            content=content,
            channel=self.channel,
            bot_channel_binding=binding,
            status=Delivery.Status.ACTIVE,
            sent_at=timezone.now(),
            deletable_until=timezone.now() + timedelta(days=1),
        )
        message = DeliveryMessage.objects.create(
            delivery=delivery,
            telegram_message_id=9002,
        )
        operation, _ = enqueue_content_operation(
            content=content,
            action=Operation.Action.DELETE,
            actor=self.sub1,
        )

        rejected = ([], [(message.pk, "Bad Request: message can't be deleted")], [])
        with patch(
            "channel_manager.operation_processor._delete_delivery",
            new=AsyncMock(return_value=rejected),
        ) as delete_mock:
            process_operation(operation)

        self.assertEqual(delete_mock.await_count, 1)
        operation.refresh_from_db()
        content.refresh_from_db()
        delivery.refresh_from_db()
        message.refresh_from_db()
        self.assertEqual(operation.state, Operation.State.SUCCEEDED)
        self.assertEqual(operation.telegram_error_code, "UNPUBLISH_MANUAL_DELETE")
        self.assertIn("频道消息需要手动删除，循环任务已停止", operation.telegram_error_text)
        self.assertEqual(content.status, Content.Status.UNPUBLISHED_RETAINED)
        self.assertEqual(delivery.status, Delivery.Status.RETAINED)
        self.assertEqual(message.status, DeliveryMessage.Status.RETAINED)

        second_operation, _ = enqueue_content_operation(
            content=content,
            action=Operation.Action.DELETE,
            actor=self.sub1,
            idempotency_key="second-attempt",
        )
        with patch("channel_manager.operation_processor._delete_delivery") as second_delete_mock:
            process_operation(second_operation)
        second_delete_mock.assert_not_called()
        second_operation.refresh_from_db()
        self.assertEqual(second_operation.state, Operation.State.SUCCEEDED)

    def test_retained_delivery_audit_previews_then_fixes_server_data(self):
        content = Content.objects.create(
            owner_user=self.sub1,
            code="retained-audit",
            title="retained-audit",
            status=Content.Status.PARTIAL,
        )
        delivery = Delivery.objects.create(
            content=content,
            channel=self.channel,
            status=Delivery.Status.PARTIAL,
            sent_at=timezone.now() - timedelta(days=3),
            deletable_until=timezone.now() - timedelta(days=1),
        )
        message = DeliveryMessage.objects.create(
            delivery=delivery,
            telegram_message_id=9100,
        )

        preview_output = StringIO()
        call_command("audit_retained_deliveries", stdout=preview_output)
        delivery.refresh_from_db()
        message.refresh_from_db()
        self.assertEqual(delivery.status, Delivery.Status.PARTIAL)
        self.assertEqual(message.status, DeliveryMessage.Status.SENT)
        self.assertIn("发现 1 个", preview_output.getvalue())

        fix_output = StringIO()
        call_command("audit_retained_deliveries", "--fix", stdout=fix_output)
        content.refresh_from_db()
        delivery.refresh_from_db()
        message.refresh_from_db()
        self.assertEqual(content.status, Content.Status.UNPUBLISHED_RETAINED)
        self.assertEqual(delivery.status, Delivery.Status.RETAINED)
        self.assertEqual(message.status, DeliveryMessage.Status.RETAINED)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="delivery.mark_retained",
                object_id=str(delivery.pk),
            ).exists()
        )
        self.assertIn("已收敛 Delivery 1 个", fix_output.getvalue())
