from __future__ import annotations

import json
import time

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max
from django.test import RequestFactory

from channel_manager.models import TelegramBot, TelegramUpdate
from channel_manager.security import decrypt_secret
from channel_manager.webhook_views import telegram_webhook
from channel_manager.telegram_client import create_bot


async def _delete_webhook(bot_record):
    async with create_bot(decrypt_secret(bot_record.token_ciphertext)) as bot:
        await bot.delete_webhook(drop_pending_updates=False)


async def _poll(bot_record, offset, timeout):
    async with create_bot(decrypt_secret(bot_record.token_ciphertext)) as bot:
        return await bot.get_updates(
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "channel_post", "edited_channel_post", "my_chat_member"],
        )


class Command(BaseCommand):
    help = "本地开发时使用 long polling 接收 Telegram 指令；执行任务仍需 run_operation_worker"

    def add_arguments(self, parser):
        parser.add_argument("--bot-id", type=int, help="只轮询指定的 TelegramBot 数据库 ID")
        parser.add_argument("--once", action="store_true", help="每个 Bot 只请求一次后退出")
        parser.add_argument("--timeout", type=int, default=2, help="单次 long polling 秒数")

    def handle(self, *args, **options):
        bots = TelegramBot.objects.filter(status=TelegramBot.Status.ACTIVE, telegram_bot_id__isnull=False)
        if options["bot_id"]:
            bots = bots.filter(pk=options["bot_id"])
        bots = list(bots)
        if not bots:
            raise CommandError("没有可轮询的活动 Bot。")

        factory = RequestFactory()
        offsets = {
            bot.pk: (TelegramUpdate.objects.filter(bot=bot).aggregate(value=Max("update_id"))["value"] or 0) + 1
            for bot in bots
        }
        self.stdout.write(self.style.SUCCESS(f"Polling started for {len(bots)} bot(s)."))
        for bot in bots:
            async_to_sync(_delete_webhook)(bot)
        while True:
            for bot in bots:
                try:
                    updates = async_to_sync(_poll)(bot, offsets[bot.pk], max(0, options["timeout"]))
                except Exception as exc:
                    self.stderr.write(f"Bot {bot.pk} polling error: {exc.__class__.__name__}")
                    continue
                for update in updates:
                    body = update.model_dump(mode="json", exclude_none=True)
                    request = factory.post(
                        f"/telegram/webhook/{bot.public_id}",
                        data=json.dumps(body, ensure_ascii=False),
                        content_type="application/json",
                        HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=decrypt_secret(bot.webhook_secret_ciphertext),
                    )
                    telegram_webhook(request, bot.public_id)
                    offsets[bot.pk] = max(offsets[bot.pk], update.update_id + 1)
                    self.stdout.write(f"Bot {bot.pk}: update {update.update_id}")
            if options["once"]:
                return
            time.sleep(0.2)
