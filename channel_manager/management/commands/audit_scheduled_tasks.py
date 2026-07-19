from django.core.management.base import BaseCommand

from channel_manager.task_utils import audit_scheduled_task_consistency


class Command(BaseCommand):
    help = "检查历史定时/循环配置与 Scheduler 队列的一致性"

    def add_arguments(self, parser):
        parser.add_argument(
            "--fix",
            action="store_true",
            help="清理可确定的遗留配置并取消失效的排队任务；省略时只检查",
        )

    def handle(self, *args, **options):
        fix = options["fix"]
        report = audit_scheduled_task_consistency(fix=fix)

        for item in report["content_issues"]:
            self.stdout.write(
                f"[内容配置] content #{item['content_id']} {item['title']}：{item['reason']}"
            )
        for item in report["operation_issues"]:
            self.stdout.write(
                f"[排队任务] operation {item['operation_id']} / content #{item['content_id']}：{item['reason']}"
            )
        for item in report["running_reviews"]:
            self.stdout.write(
                self.style.WARNING(
                    f"[执行中复核] operation {item['operation_id']} / content #{item['content_id']}：{item['reason']}"
                )
            )

        issue_count = len(report["content_issues"]) + len(report["operation_issues"])
        summary = (
            f"检查完成：内容配置异常 {len(report['content_issues'])}，"
            f"失效排队任务 {len(report['operation_issues'])}，"
            f"执行中待复核 {len(report['running_reviews'])}。"
        )
        if fix:
            summary += (
                f" 已修复内容 {report['fixed_contents']}，"
                f"已取消任务 {report['cancelled_operations']}。"
            )
            self.stdout.write(self.style.SUCCESS(summary))
        elif issue_count:
            self.stdout.write(self.style.WARNING(summary + " 确认结果后使用 --fix 执行清理。"))
        else:
            self.stdout.write(self.style.SUCCESS(summary + " 当前数据一致。"))
