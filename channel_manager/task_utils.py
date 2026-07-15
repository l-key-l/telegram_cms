from __future__ import annotations

from collections import defaultdict

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .audit import audit_event
from .models import Content, Operation


TASK_OPERATION_ACTIONS = (Operation.Action.SEND, Operation.Action.REPLACE)
TASK_FINISHED_STATES = (Operation.State.SUCCEEDED, Operation.State.PARTIAL)
TASK_QUEUE_STATES = (Operation.State.QUEUED, Operation.State.PENDING_CONFIRM)


def scheduled_content_q() -> Q:
    return (
        Q(next_run_at__isnull=False)
        | Q(cycle_days__isnull=False)
        | Q(cycle_time__isnull=False)
        | Q(publish_at__isnull=False)
    )


def build_scheduled_task_rows(contents):
    content_list = list(contents)
    content_ids = [str(item.pk) for item in content_list]
    stats = defaultdict(
        lambda: {
            "execution_count": 0,
            "last_run_at": None,
            "queued_count": 0,
            "running_count": 0,
            "failed_count": 0,
            "uncertain_count": 0,
        }
    )

    if content_ids:
        operations = Operation.objects.filter(
            target_type="content",
            target_id__in=content_ids,
            action__in=TASK_OPERATION_ACTIONS,
        ).only("target_id", "state", "finished_at", "created_at", "updated_at")
        for operation in operations:
            item_stats = stats[operation.target_id]
            if operation.state in TASK_FINISHED_STATES:
                item_stats["execution_count"] += 1
                run_at = operation.finished_at or operation.updated_at or operation.created_at
                if not item_stats["last_run_at"] or run_at > item_stats["last_run_at"]:
                    item_stats["last_run_at"] = run_at
            elif operation.state in TASK_QUEUE_STATES:
                item_stats["queued_count"] += 1
            elif operation.state == Operation.State.RUNNING:
                item_stats["running_count"] += 1
            elif operation.state == Operation.State.FAILED:
                item_stats["failed_count"] += 1
            elif operation.state == Operation.State.UNCERTAIN:
                item_stats["uncertain_count"] += 1

    rows = []
    for content in content_list:
        key = str(content.pk)
        item_stats = stats[key]
        channels = "、".join(channel.title for channel in content.channels.all()) or "-"
        rule_parts = []
        if content.publish_at:
            rule_parts.append(f"首次 {timezone.localtime(content.publish_at).strftime('%Y-%m-%d %H:%M')}")
        if content.cycle_days and content.cycle_time:
            rule_parts.append(f"每 {content.cycle_days} 天 {content.cycle_time.strftime('%H:%M')}")

        if item_stats["running_count"]:
            status_label = "执行中"
            status_class = "status-pending"
        elif item_stats["queued_count"]:
            status_label = "队列中"
            status_class = "status-pending"
        elif content.next_run_at:
            status_label = "等待执行"
            status_class = "status-pending"
        elif content.cycle_days and content.cycle_time:
            status_label = "循环待排队"
            status_class = "status-success"
        elif item_stats["failed_count"] or item_stats["uncertain_count"]:
            status_label = "需查看日志"
            status_class = "status-danger"
        else:
            status_label = "已完成"
            status_class = "status-success"

        rows.append(
            {
                "content": content,
                "channel_names": channels,
                "rule_label": "；".join(rule_parts) or "-",
                "execution_count": item_stats["execution_count"],
                "last_run_at": item_stats["last_run_at"],
                "queued_count": item_stats["queued_count"],
                "running_count": item_stats["running_count"],
                "failed_count": item_stats["failed_count"],
                "uncertain_count": item_stats["uncertain_count"],
                "status_label": status_label,
                "status_class": status_class,
                "can_stop": bool(
                    content.next_run_at
                    or content.cycle_days
                    or content.cycle_time
                    or item_stats["queued_count"]
                    or item_stats["running_count"]
                ),
            }
        )
    return rows


def stop_scheduled_task(*, request, actor, content: Content) -> int:
    with transaction.atomic():
        locked = Content.objects.select_for_update().get(pk=content.pk)
        before = {
            "publish_at": locked.publish_at.isoformat() if locked.publish_at else None,
            "cycle_days": locked.cycle_days,
            "cycle_time": locked.cycle_time.isoformat() if locked.cycle_time else None,
            "next_run_at": locked.next_run_at.isoformat() if locked.next_run_at else None,
        }
        locked.publish_at = None
        locked.cycle_days = None
        locked.cycle_time = None
        locked.next_run_at = None
        locked.save(update_fields=("publish_at", "cycle_days", "cycle_time", "next_run_at", "updated_at"))

        now = timezone.now()
        cancelled_count = Operation.objects.filter(
            owner_user=locked.owner_user,
            source=Operation.Source.SCHEDULER,
            target_type="content",
            target_id=str(locked.pk),
            action__in=TASK_OPERATION_ACTIONS,
            state__in=TASK_QUEUE_STATES,
        ).update(
            state=Operation.State.CANCELLED,
            finished_at=now,
            locked_by="",
            locked_until=None,
            telegram_error_code="TASK_STOPPED",
            telegram_error_text="任务已由网页中止",
        )
        audit_event(
            request=request,
            actor=actor,
            owner_user=locked.owner_user,
            action="scheduled_task.stop",
            object_type="content",
            object_id=str(locked.pk),
            before=before,
            after={
                "publish_at": None,
                "cycle_days": None,
                "cycle_time": None,
                "next_run_at": None,
                "cancelled_operations": cancelled_count,
            },
        )
    return cancelled_count
