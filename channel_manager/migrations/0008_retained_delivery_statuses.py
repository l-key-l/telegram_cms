from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("channel_manager", "0007_reconcile_stale_scheduler_operations"),
    ]

    operations = [
        migrations.AlterField(
            model_name="content",
            name="status",
            field=models.CharField(
                choices=[
                    ("DRAFT", "\u8349\u7a3f"),
                    ("PUBLISHED", "\u5df2\u4e0a\u67b6"),
                    ("UNPUBLISHING", "\u4e0b\u67b6\u5904\u7406\u4e2d"),
                    ("UNPUBLISHED", "\u5df2\u4e0b\u67b6"),
                    (
                        "UNPUBLISHED_RETAINED",
                        "\u5df2\u4e0b\u67b6\uff08\u8fdc\u7aef\u6709\u4fdd\u7559\uff09",
                    ),
                    ("ARCHIVED", "\u5df2\u5f52\u6863"),
                    ("PARTIAL", "\u90e8\u5206\u4e0b\u67b6"),
                ],
                db_index=True,
                default="DRAFT",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="delivery",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDING", "\u5f85\u53d1\u9001"),
                    ("PARTIAL", "\u90e8\u5206\u6210\u529f"),
                    ("ACTIVE", "\u6d3b\u52a8"),
                    ("DELETE_PENDING", "\u5f85\u5220\u9664"),
                    ("DELETED", "\u5df2\u5220\u9664"),
                    ("RETAINED", "\u8fdc\u7aef\u8d85\u671f\u4fdd\u7559"),
                    ("FAILED", "\u5931\u8d25"),
                    ("UNKNOWN", "\u672a\u77e5"),
                ],
                db_index=True,
                default="PENDING",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="deliverymessage",
            name="status",
            field=models.CharField(
                choices=[
                    ("SENT", "\u5df2\u53d1\u9001"),
                    ("EDITED", "\u5df2\u7f16\u8f91"),
                    ("DELETED", "\u5df2\u5220\u9664"),
                    ("RETAINED", "\u8fdc\u7aef\u8d85\u671f\u4fdd\u7559"),
                    ("UNKNOWN", "\u672a\u77e5"),
                ],
                default="SENT",
                max_length=16,
            ),
        ),
    ]
