from django.conf import settings


def product_context(request):
    return {
        "product_name": settings.PRODUCT_NAME,
        "public_base_url": settings.PUBLIC_BASE_URL,
        "telegram_proxy_enabled": bool(settings.TELEGRAM_PROXY_URL),
    }
