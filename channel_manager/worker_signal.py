from __future__ import annotations

import threading


_operation_wakeup = threading.Event()


def notify_operation_worker() -> None:
    _operation_wakeup.set()


def wait_for_operation(timeout: float) -> None:
    _operation_wakeup.wait(timeout=max(0.05, timeout))
    _operation_wakeup.clear()
