from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from .models import AuditEvent, Operation


def _client_ip(request: HttpRequest | None):
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return forwarded.split(",", 1)[0].strip() if forwarded else request.META.get("REMOTE_ADDR")


def audit_event(
    *,
    request: HttpRequest | None,
    actor,
    owner_user,
    action: str,
    object_type: str,
    object_id: str = "",
    outcome: str = AuditEvent.Outcome.SUCCESS,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    operation: Operation | None = None,
    source: str = "WEB",
    actor_type: str = "WEB_USER",
    error_code: str = "",
) -> AuditEvent:
    return AuditEvent.objects.create(
        owner_user=owner_user,
        actor_type=actor_type,
        actor_id=str(getattr(actor, "pk", actor or "")),
        source=source,
        action=action,
        object_type=object_type,
        object_id=str(object_id),
        operation=operation,
        request_id=getattr(request, "request_id", "") if request else "",
        before_json=before or {},
        after_json=after or {},
        outcome=outcome,
        ip=_client_ip(request),
        user_agent=(request.META.get("HTTP_USER_AGENT", "")[:500] if request else ""),
        error_code=error_code,
    )
