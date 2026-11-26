"""批量导入学生名单并生成初始密码。

CSV 格式（支持中英文表头）：
    student_id,name
    20260001,学生甲

用法：
    python manage.py import_students --csv students.csv --prefix S --out accounts.csv
"""
from __future__ import annotations

import csv
import secrets
import string
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from competition.config import load_config
from competition.models import Profile

ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "O0Il1")
ID_HEADERS = {"student_id", "学号", "id", "sid"}
NAME_HEADERS = {"name", "姓名", "名字"}


def _random_password(length: int = 10) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


class Command(BaseCommand):
    help = "从 CSV 批量导入学生账号（用户名=学号，随机初始密码）"

    def add_arguments(self, parser):
        parser.add_argument("--csv", required=True, help="学生名单 CSV 路径")
        parser.add_argument("--prefix", default=None, help="参赛编号前缀，默认取 yaml 配置")
        parser.add_argument("--out", default="accounts.csv", help="输出账号密码的 CSV")
        parser.add_argument("--dry-run", action="store_true", help="只校验，不写数据库")

    def handle(self, *args, **options):
        path = Path(options["csv"])
        if not path.exists():
            raise CommandError(f"找不到文件：{path}")

        prefix = options["prefix"] or load_config().contest_code_prefix
        rows = self._read_csv(path)
        if not rows:
            raise CommandError("名单为空。")

        existing = set(Profile.objects.values_list("student_id", flat=True))
        existing_users = set(User.objects.values_list("username", flat=True))
        used_codes = set(Profile.objects.values_list("contest_code", flat=True))
        next_no = 1
        while f"{prefix}{next_no:03d}" in used_codes:
            next_no += 1

        accounts = []
        skipped = []
        with transaction.atomic():
            for student_id, name in rows:
                student_id = student_id.strip()
                name = name.strip()
                if not student_id:
                    continue
                if student_id in existing:
                    skipped.append(student_id)
                    continue
                if student_id in existing_users:
                    skipped.append(student_id)
                    continue
                while f"{prefix}{next_no:03d}" in used_codes:
                    next_no += 1
                code = f"{prefix}{next_no:03d}"
                password = _random_password()
                user = User.objects.create_user(
                    username=student_id, password=password, first_name=name[:30]
                )
                user.is_staff = False
                user.save(update_fields=["is_staff"])
                Profile.objects.create(
                    user=user,
                    student_id=student_id,
                    real_name=name,
                    contest_code=code,
                    must_change_password=True,
                )
                used_codes.add(code)
                next_no += 1
                accounts.append((student_id, name, code, password, user.id))

            if options["dry_run"]:
                transaction.set_rollback(True)

        out_path = Path(options["out"])
        with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["学号", "姓名", "参赛编号", "初始密码", "用户名"])
            for student_id, name, code, password, uid in accounts:
                writer.writerow([student_id, name, code, password, student_id])
        try:
            out_path.chmod(0o600)
        except OSError:
            pass

        self.stdout.write(self.style.SUCCESS(
            f"创建 {len(accounts)} 个账号，跳过 {len(skipped)} 个已存在账号。"
        ))
        if skipped:
            self.stdout.write("已存在：" + ", ".join(skipped[:10]) + ("…" if len(skipped) > 10 else ""))
        self.stdout.write(f"账号密码已写入：{out_path.resolve()}")
        if accounts:
            self.stdout.write("前 5 个账号：")
            for student_id, name, code, password, _ in accounts[:5]:
                self.stdout.write(f"  {code}  {student_id}  {name}  {password}")

    def _read_csv(self, path: Path):
        for encoding in ("utf-8-sig", "gbk", "utf-8"):
            try:
                with path.open("r", encoding=encoding, newline="") as fh:
                    reader = csv.reader(fh)
                    rows = [r for r in reader if any(c.strip() for c in r)]
                break
            except UnicodeDecodeError:
                continue
        else:
            raise CommandError("无法识别 CSV 编码，请另存为 UTF-8。")

        if not rows:
            return []
        header = [h.strip().lower() for h in rows[0]]
        id_idx = next((i for i, h in enumerate(header) if h in ID_HEADERS), 0)
        name_idx = next((i for i, h in enumerate(header) if h in NAME_HEADERS), 1 if len(header) > 1 else 0)

        body = rows[1:] if any(h in ID_HEADERS or h in NAME_HEADERS for h in header) else rows
        out = []
        for row in body:
            sid = row[id_idx] if id_idx < len(row) else ""
            name = row[name_idx] if name_idx < len(row) else ""
            if sid.strip():
                out.append((sid, name))
        return out
