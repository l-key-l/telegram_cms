from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "创建业务主账号"

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--password", required=True)
        parser.add_argument("--display-name", default="总管理员")

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"]
        if User.objects.filter(username=username).exists():
            raise CommandError("用户名已存在。")
        user = User.objects.create_user(
            username=username,
            password=options["password"],
            display_name=options["display_name"],
            role=User.Role.MASTER,
        )
        self.stdout.write(self.style.SUCCESS(f"已创建主账号：{user.username}"))
