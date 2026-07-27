from django.core.management.base import BaseCommand

from channel_manager.delivery_utils import audit_retained_deliveries


class Command(BaseCommand):
    help = "检查历史超期 PARTIAL Delivery，并按需标记为远端保留"

    def add_arguments(self, parser):
        parser.add_argument(
            "--fix",
            action="store_true",
            help="执行状态收敛；省略时只预览",
        )
        parser.add_argument(
            "--content-id",
            type=int,
            help="只检查指定 content ID",
        )

    def handle(self, *args, **options):
        fix = options["fix"]
        report = audit_retained_deliveries(
            fix=fix,
            content_id=options.get("content_id"),
        )

        for item in report["issues"]:
            deadline = item["deletable_until"] or "按 sent_at + 48h 计算"
            self.stdout.write(
                f"[超期 Delivery] delivery #{item['delivery_id']} / "
                f"content #{item['content_id']} {item['content_title']} / "
                f"{item['channel_title']} / 消息 {item['message_count']} 条 / "
                f"删除截止 {deadline}"
            )

        summary = (
            f"检查完成：发现 {len(report['issues'])} 个超期 PARTIAL Delivery。"
        )
        if fix:
            summary += (
                f" 已收敛 Delivery {report['fixed_deliveries']} 个，"
                f"标记远端保留消息 {report['retained_messages']} 条，"
                f"更新内容状态 {report['updated_contents']} 个。"
            )
            self.stdout.write(self.style.SUCCESS(summary))
        elif report["issues"]:
            self.stdout.write(
                self.style.WARNING(summary + " 核对后追加 --fix 执行。")
            )
        else:
            self.stdout.write(self.style.SUCCESS(summary))
