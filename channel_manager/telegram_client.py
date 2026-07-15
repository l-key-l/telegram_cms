from __future__ import annotations

from django.conf import settings


def create_bot(token: str):
    """创建统一的 aiogram Bot；本地可通过 .env 的 HTTP/SOCKS 代理访问 Telegram。"""
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession

    proxy_url = settings.TELEGRAM_PROXY_URL.strip()
    session = AiohttpSession(proxy=proxy_url) if proxy_url else AiohttpSession()
    return Bot(token=token, session=session)
