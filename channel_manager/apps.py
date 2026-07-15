import os
import sys
import threading

from django.apps import AppConfig
from django.conf import settings


_embedded_worker_started = False


class ChannelManagerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'channel_manager'

    def ready(self):
        from . import signals  # noqa: F401

        global _embedded_worker_started
        if _embedded_worker_started or not settings.EMBEDDED_WORKER:
            return
        if "runserver" not in sys.argv:
            return
        if "--noreload" not in sys.argv and os.environ.get("RUN_MAIN") != "true":
            return
        _embedded_worker_started = True

        def run_worker():
            from django.core.management import call_command
            from django.db import close_old_connections

            close_old_connections()
            call_command("run_operation_worker")

        threading.Thread(
            target=run_worker,
            name="embedded-operation-worker",
            daemon=True,
        ).start()
