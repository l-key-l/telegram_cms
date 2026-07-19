from django.db import migrations
from django.utils import timezone


def reconcile_stale_scheduler_operations(apps, schema_editor):
    Content = apps.get_model("channel_manager", "Content")
    Operation = apps.get_model("channel_manager", "Operation")
    AuditEvent = apps.get_model("channel_manager", "AuditEvent")
    now = timezone.now()
    operations = Operation.objects.filter(
        source="SCHEDULER",
        target_type="content",
        action__in=("SEND", "REPLACE"),
        state__in=("QUEUED", "PENDING_CONFIRM"),
    )

    for operation in operations.iterator():
        content = None
        if operation.target_id.isdigit():
            content = Content.objects.filter(
                pk=int(operation.target_id), owner_user_id=operation.owner_user_id
            ).first()
        reason = ""
        if not content:
            reason = "任务目标内容不存在"
        elif content.deleted_at or content.status in ("ARCHIVED", "UNPUBLISHING"):
            reason = "任务目标已归档、删除或正在下架"
        elif not any((content.publish_at, content.cycle_days, content.cycle_time, content.next_run_at)):
            reason = "任务目标已清空定时和循环配置"
        elif Operation.objects.filter(
            owner_user_id=operation.owner_user_id,
            target_type="content",
            target_id=operation.target_id,
            action="DELETE",
            created_at__gt=operation.created_at,
        ).exists():
            reason = "自动任务创建后又提交了下架任务"
        if not reason:
            continue

        before_state = operation.state
        operation.state = "CANCELLED"
        operation.finished_at = now
        operation.locked_by = ""
        operation.locked_until = None
        operation.telegram_error_code = "TASK_RECONCILED"
        operation.telegram_error_text = f"部署迁移已取消历史失效任务：{reason}"
        operation.updated_at = now
        operation.save(
            update_fields=(
                "state",
                "finished_at",
                "locked_by",
                "locked_until",
                "telegram_error_code",
                "telegram_error_text",
                "updated_at",
            )
        )
        AuditEvent.objects.create(
            owner_user_id=operation.owner_user_id,
            actor_type="SYSTEM",
            actor_id="migration-0007",
            source="SYSTEM",
            action="scheduled_task.reconcile",
            object_type="operation",
            object_id=str(operation.pk),
            operation_id=operation.pk,
            before_json={"state": before_state},
            after_json={"state": "CANCELLED", "reason": reason},
            outcome="SUCCESS",
        )


class Migration(migrations.Migration):
    dependencies = [("channel_manager", "0006_stop_cycles_for_unpublished_content")]

    operations = [
        migrations.RunPython(reconcile_stale_scheduler_operations, migrations.RunPython.noop),
    ]
