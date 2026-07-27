from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .audit import audit_event
from .models import Content, Delivery, DeliveryMessage, Operation


def _expired_delivery_q(now):
    return Q(deletable_until__lte=now) | (
        Q(deletable_until__isnull=True)
        & Q(sent_at__lte=now - timedelta(hours=48))
    )


def _delivery_is_expired(delivery: Delivery, now) -> bool:
    deadline = delivery.deletable_until
    if not deadline and delivery.sent_at:
        deadline = delivery.sent_at + timedelta(hours=48)
    return bool(deadline and deadline <= now)


def audit_retained_deliveries(
    *,
    fix: bool = False,
    content_id: int | None = None,
) -> dict:
    """检查历史超期 PARTIAL Delivery；fix=True 时收敛为 RETAINED。"""
    now = timezone.now()
    queryset = (
        Delivery.objects.filter(status=Delivery.Status.PARTIAL)
        .filter(_expired_delivery_q(now))
        .select_related("content__owner_user", "channel")
        .order_by("id")
    )
    if content_id:
        queryset = queryset.filter(content_id=content_id)

    report = {
        "issues": [],
        "fixed_deliveries": 0,
        "retained_messages": 0,
        "updated_contents": 0,
    }
    delivery_ids = list(queryset.values_list("id", flat=True))
    for delivery_id in delivery_ids:
        delivery = (
            Delivery.objects.select_related("content__owner_user", "channel")
            .get(pk=delivery_id)
        )
        message_count = delivery.messages.exclude(
            status=DeliveryMessage.Status.DELETED
        ).count()
        report["issues"].append(
            {
                "delivery_id": delivery.pk,
                "content_id": delivery.content_id,
                "content_title": delivery.content.title,
                "channel_title": delivery.channel.title,
                "message_count": message_count,
                "deletable_until": (
                    delivery.deletable_until.isoformat()
                    if delivery.deletable_until
                    else None
                ),
            }
        )
        if not fix:
            continue

        with transaction.atomic():
            locked = (
                Delivery.objects.select_for_update()
                .select_related("content__owner_user", "channel")
                .get(pk=delivery_id)
            )
            if (
                locked.status != Delivery.Status.PARTIAL
                or not _delivery_is_expired(locked, now)
            ):
                continue

            retained_count = locked.messages.exclude(
                status=DeliveryMessage.Status.DELETED
            ).update(status=DeliveryMessage.Status.RETAINED)
            locked.status = Delivery.Status.RETAINED
            locked.save(update_fields=("status", "updated_at"))
            report["fixed_deliveries"] += 1
            report["retained_messages"] += retained_count

            audit_event(
                request=None,
                actor="retained-delivery-audit",
                owner_user=locked.content.owner_user,
                action="delivery.mark_retained",
                object_type="delivery",
                object_id=str(locked.pk),
                before={"status": Delivery.Status.PARTIAL},
                after={
                    "status": Delivery.Status.RETAINED,
                    "retained_messages": retained_count,
                    "reason": "DELETE_WINDOW_EXPIRED",
                },
                source=Operation.Source.SYSTEM,
                actor_type=Operation.ActorType.SYSTEM,
            )

            content = Content.objects.select_for_update().get(pk=locked.content_id)
            retryable_exists = content.deliveries.filter(
                status__in=(Delivery.Status.ACTIVE, Delivery.Status.PARTIAL)
            ).exists()
            retained_exists = content.deliveries.filter(
                status=Delivery.Status.RETAINED
            ).exists()
            if (
                content.status == Content.Status.PARTIAL
                and retained_exists
                and not retryable_exists
            ):
                content.status = Content.Status.UNPUBLISHED_RETAINED
                content.save(update_fields=("status", "updated_at"))
                report["updated_contents"] += 1
                audit_event(
                    request=None,
                    actor="retained-delivery-audit",
                    owner_user=content.owner_user,
                    action="content.mark_unpublished_retained",
                    object_type="content",
                    object_id=str(content.pk),
                    before={"status": Content.Status.PARTIAL},
                    after={
                        "status": Content.Status.UNPUBLISHED_RETAINED,
                        "reason": "DELETE_WINDOW_EXPIRED",
                    },
                    source=Operation.Source.SYSTEM,
                    actor_type=Operation.ActorType.SYSTEM,
                )
    return report
