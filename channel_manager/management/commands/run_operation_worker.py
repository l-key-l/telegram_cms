from __future__ import annotations

import socket
import threading
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import close_old_connections, connection, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from channel_manager.models import Operation, TelegramBot
from channel_manager.operation_processor import process_operation
from channel_manager.services import enqueue_due_content_operations
from channel_manager.worker_signal import wait_for_operation


class Command(BaseCommand):
    help = "运行 MySQL/数据库任务 Worker"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="只处理一个任务后退出")
        parser.add_argument("--no-telegram-polling", action="store_true", help="本地环境不在 Worker 内接收 Telegram 指令")

    def handle(self, *args, **options):
        worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.stdout.write(self.style.SUCCESS(f"Worker started: {worker_id}"))
        if not options["once"] and not options["no_telegram_polling"] and not settings.PUBLIC_BASE_URL:
            self.start_polling_supervisor()
        while True:
            enqueue_due_content_operations()
            operation = self.claim(worker_id)
            if operation:
                self.stdout.write(f"Processing {operation.id} / {operation.action}")
                process_operation(operation)
                if options["once"]:
                    return
            elif options["once"]:
                self.stdout.write("No queued operation.")
                return
            else:
                wait_for_operation(settings.OPERATION_WORKER_POLL_SECONDS)

    def start_polling_supervisor(self):
        def supervise():
            while True:
                close_old_connections()
                try:
                    if TelegramBot.objects.filter(status=TelegramBot.Status.ACTIVE, telegram_bot_id__isnull=False).exists():
                        self.stdout.write("Local Telegram polling is running inside the Worker.")
                        call_command("run_telegram_polling", timeout=2)
                except Exception as exc:
                    self.stderr.write(f"Telegram polling restart: {exc.__class__.__name__}")
                finally:
                    close_old_connections()
                time.sleep(5)

        threading.Thread(target=supervise, name="telegram-polling", daemon=True).start()

    def claim(self, worker_id):
        now = timezone.now()
        with transaction.atomic():
            earlier_unfinished = Operation.objects.filter(
                owner_user_id=OuterRef("owner_user_id"),
                target_type=OuterRef("target_type"),
                target_id=OuterRef("target_id"),
                created_at__lt=OuterRef("created_at"),
                state__in=(Operation.State.QUEUED, Operation.State.RUNNING),
            )
            queryset = Operation.objects.filter(
                state=Operation.State.QUEUED,
                available_at__lte=now,
            ).filter(Q(locked_until__isnull=True) | Q(locked_until__lt=now)).annotate(
                has_earlier_unfinished=Exists(earlier_unfinished)
            ).filter(has_earlier_unfinished=False).order_by("created_at", "available_at")
            if connection.features.has_select_for_update_skip_locked:
                queryset = queryset.select_for_update(skip_locked=True)
            else:
                queryset = queryset.select_for_update()
            operation = queryset.first()
            if not operation:
                return None
            operation.locked_by = worker_id
            operation.locked_until = now + timedelta(minutes=5)
            operation.save(update_fields=("locked_by", "locked_until", "updated_at"))
            return operation
