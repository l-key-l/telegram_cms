from django.db import migrations
from django.db.models import Q
from django.utils import timezone


def stop_legacy_unpublished_cycles(apps, schema_editor):
    Content = apps.get_model("channel_manager", "Content")
    Operation = apps.get_model("channel_manager", "Operation")
    AuditEvent = apps.get_model("channel_manager", "AuditEvent")
    now = timezone.now()
    affected_contents = Content.objects.filter(
        status__in=("UNPUBLISHING", "UNPUBLISHED", "ARCHIVED")
    ).filter(
        Q(publish_at__isnull=False)
        | Q(cycle_days__isnull=False)
        | Q(cycle_time__isnull=False)
        | Q(next_run_at__isnull=False)
    )

    for content in affected_contents.iterator():
        before = {
            "publish_at": content.publish_at.isoformat() if content.publish_at else None,
            "cycle_days": content.cycle_days,
            "cycle_time": content.cycle_time.isoformat() if content.cycle_time else None,
            "next_run_at": content.next_run_at.isoformat() if content.next_run_at else None,
        }
        cancelled_count = Operation.objects.filter(
            owner_user_id=content.owner_user_id,
            source="SCHEDULER",
            target_type="content",
            target_id=str(content.pk),
            action__in=("SEND", "REPLACE"),
            state__in=("QUEUED", "PENDING_CONFIRM"),
        ).update(
            state="CANCELLED",
            finished_at=now,
            locked_by="",
            locked_until=None,
            telegram_error_code="TASK_STOPPED",
            telegram_error_text="历史下架内容的自动任务已清理",
        )
        content.publish_at = None
        content.cycle_days = None
        content.cycle_time = None
        content.next_run_at = None
        content.updated_at = now
        content.save(
            update_fields=("publish_at", "cycle_days", "cycle_time", "next_run_at", "updated_at")
        )
        AuditEvent.objects.create(
            owner_user_id=content.owner_user_id,
            actor_type="SYSTEM",
            actor_id="migration-0006",
            source="SYSTEM",
            action="scheduled_task.stop",
            object_type="content",
            object_id=str(content.pk),
            before_json=before,
            after_json={
                "publish_at": None,
                "cycle_days": None,
                "cycle_time": None,
                "next_run_at": None,
                "cancelled_operations": cancelled_count,
                "reason": "LEGACY_UNPUBLISHED_CLEANUP",
            },
            outcome="SUCCESS",
        )


class Migration(migrations.Migration):
    dependencies = [("channel_manager", "0005_remove_userchannelaccess_can_resend_and_more")]

    operations = [
        migrations.RunPython(stop_legacy_unpublished_cycles, migrations.RunPython.noop),
    ]
