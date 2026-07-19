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


def _schedule_snapshot(content: Content) -> dict:
    return {
        "publish_at": content.publish_at.isoformat() if content.publish_at else None,
        "cycle_days": content.cycle_days,
        "cycle_time": content.cycle_time.isoformat() if content.cycle_time else None,
        "next_run_at": content.next_run_at.isoformat() if content.next_run_at else None,
    }


def _has_schedule(content: Content) -> bool:
    return bool(content.publish_at or content.cycle_days or content.cycle_time or content.next_run_at)


def audit_scheduled_task_consistency(*, fix: bool = False) -> dict:
    """扫描历史自动任务；fix=True 时只修复可确定的矛盾记录。"""
    report = {
        "content_issues": [],
        "operation_issues": [],
        "running_reviews": [],
        "fixed_contents": 0,
        "cancelled_operations": 0,
    }
    scheduled_contents = Content.objects.filter(scheduled_content_q()).select_related("owner_user")
    for content in scheduled_contents.iterator():
        latest_delete = (
            Operation.objects.filter(
                owner_user=content.owner_user,
                target_type="content",
                target_id=str(content.pk),
                action=Operation.Action.DELETE,
            )
            .only("created_at", "updated_at", "finished_at")
            .order_by("-created_at")
            .first()
        )
        latest_delete_at = None
        if latest_delete:
            latest_delete_at = latest_delete.finished_at or latest_delete.updated_at or latest_delete.created_at

        reason = ""
        if content.deleted_at or content.status == Content.Status.ARCHIVED:
            reason = "内容已归档或删除"
        elif content.status == Content.Status.UNPUBLISHING:
            reason = "内容正在下架"
        elif (
            content.status in {Content.Status.UNPUBLISHED, Content.Status.PARTIAL}
            and latest_delete_at
            and content.updated_at <= latest_delete_at
        ):
            reason = "最近一次内容变更来自下架任务"

        if not reason:
            continue
        report["content_issues"].append(
            {"content_id": content.pk, "title": content.title, "reason": reason, "schedule": _schedule_snapshot(content)}
        )
        if fix:
            cancelled_count = stop_scheduled_task(
                request=None,
                actor="scheduled-task-audit",
                content=content,
                reason="CONSISTENCY_REPAIR",
                source=Operation.Source.SYSTEM,
                actor_type=Operation.ActorType.SYSTEM,
            )
            report["fixed_contents"] += 1
            report["cancelled_operations"] += cancelled_count

    active_operations = Operation.objects.filter(
        source=Operation.Source.SCHEDULER,
        target_type="content",
        action__in=TASK_OPERATION_ACTIONS,
        state__in=(*TASK_QUEUE_STATES, Operation.State.RUNNING),
    ).select_related("owner_user")
    for operation in active_operations.iterator():
        content = None
        if operation.target_id.isdigit():
            content = Content.objects.filter(pk=int(operation.target_id), owner_user=operation.owner_user).first()

        reason = ""
        if not content:
            reason = "任务目标内容不存在"
        elif content.deleted_at or content.status == Content.Status.ARCHIVED:
            reason = "任务目标已经归档或删除"
        elif content.status == Content.Status.UNPUBLISHING:
            reason = "任务目标正在下架"
        elif not _has_schedule(content):
            reason = "任务目标已清空定时和循环配置"
        elif Operation.objects.filter(
            owner_user=operation.owner_user,
            target_type="content",
            target_id=operation.target_id,
            action=Operation.Action.DELETE,
            created_at__gt=operation.created_at,
        ).exists():
            reason = "自动任务创建后又提交了下架任务"

        if not reason:
            continue
        item = {
            "operation_id": str(operation.pk),
            "content_id": content.pk if content else operation.target_id,
            "reason": reason,
            "state": operation.state,
        }
        if operation.state == Operation.State.RUNNING:
            report["running_reviews"].append(item)
            continue
        report["operation_issues"].append(item)
        if fix:
            now = timezone.now()
            updated = Operation.objects.filter(pk=operation.pk, state__in=TASK_QUEUE_STATES).update(
                state=Operation.State.CANCELLED,
                finished_at=now,
                locked_by="",
                locked_until=None,
                telegram_error_code="TASK_RECONCILED",
                telegram_error_text=f"历史任务一致性巡检已取消：{reason}",
            )
            if updated:
                report["cancelled_operations"] += 1
                audit_event(
                    request=None,
                    actor="scheduled-task-audit",
                    owner_user=operation.owner_user,
                    action="scheduled_task.reconcile",
                    object_type="operation",
                    object_id=str(operation.pk),
                    before={"state": operation.state},
                    after={"state": Operation.State.CANCELLED, "reason": reason},
                    operation=operation,
                    source=Operation.Source.SYSTEM,
                    actor_type=Operation.ActorType.SYSTEM,
                )
    return report


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


def stop_scheduled_task(
    *,
    request,
    actor,
    content: Content,
    reason: str = "MANUAL_STOP",
    source: str = Operation.Source.WEB,
    actor_type: str = "WEB_USER",
) -> int:
    with transaction.atomic():
        locked = Content.objects.select_for_update().get(pk=content.pk)
        before = _schedule_snapshot(locked)
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
            telegram_error_text=(
                "内容下架，自动任务已中止"
                if reason == "CONTENT_UNPUBLISH"
                else "历史任务一致性巡检已清理"
                if reason == "CONSISTENCY_REPAIR"
                else "任务已由页面中止"
            ),
        )
        audit_event(
            request=request,
            actor=actor,
            owner_user=locked.owner_user,
            action="scheduled_task.reconcile" if reason == "CONSISTENCY_REPAIR" else "scheduled_task.stop",
            object_type="content",
            object_id=str(locked.pk),
            before=before,
            after={
                "publish_at": None,
                "cycle_days": None,
                "cycle_time": None,
                "next_run_at": None,
                "cancelled_operations": cancelled_count,
                "reason": reason,
            },
            source=source,
            actor_type=actor_type,
        )
    return cancelled_count
