"""为学生重置密码。"""
from __future__ import annotations

import secrets
import string

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from competition.models import Profile

ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "O0Il1")


class Command(BaseCommand):
    help = "为指定学号重置密码（默认随机生成并要求首次登录修改）"

    def add_arguments(self, parser):
        parser.add_argument("student_id")
        parser.add_argument("--password", default="", help="指定密码，留空则随机生成")

    def handle(self, *args, **options):
        sid = options["student_id"]
        profile = Profile.objects.select_related("user").filter(student_id=sid).first()
        if profile is None:
            raise CommandError(f"找不到学号 {sid}")

        password = options["password"] or "".join(secrets.choice(ALPHABET) for _ in range(10))
        user: User = profile.user
        user.set_password(password)
        user.save(update_fields=["password"])
        profile.must_change_password = True
        profile.save(update_fields=["must_change_password"])
        self.stdout.write(self.style.SUCCESS(
            f"{profile.contest_code} {profile.real_name}（{sid}）新密码：{password}"
        ))
