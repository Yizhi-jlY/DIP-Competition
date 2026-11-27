"""删除账号及其全部数据，只保留指定的账号。

    python manage.py prune_accounts --dry-run     # 先看会删谁、会动多少数据
    python manage.py prune_accounts               # 保留 teacher 与 demo001…demo140
    python manage.py prune_accounts --keep admin --keep-pattern "^s\\d+$"

默认保留两类账号：用户名 ``teacher``，以及正则 ``^demo\\d+$`` 命中的演示账号
（``demo001``…``demo140``）。

删除会级联清掉该账号的档案、提交、互评任务与投票。被删掉的提交如果正被别人的
互评任务引用，这些任务也会一并消失（外键是 CASCADE），所以删除后默认重新拟合
两个任务的主观榜，保证排行榜与剩下的投票一致；不需要时加 ``--no-refit``。
"""
from __future__ import annotations

import re
from typing import List, Pattern

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Q

from competition.config import load_config
from competition.models import Comparison, Profile, ReviewAssignment, Submission
from competition.services import leaderboard

DEFAULT_KEEP = ["teacher"]
DEFAULT_KEEP_PATTERN = [r"^demo\d+$"]


class Command(BaseCommand):
    help = "删除账号及其全部数据，默认只保留老师账号与演示账号"

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep",
            action="append",
            default=None,
            metavar="用户名",
            help=f"要保留的用户名，可重复（默认 {'、'.join(DEFAULT_KEEP)}）",
        )
        parser.add_argument(
            "--keep-pattern",
            action="append",
            default=None,
            metavar="正则",
            help=f"要保留的用户名正则，可重复（默认 {DEFAULT_KEEP_PATTERN[0]}）",
        )
        parser.add_argument("--dry-run", action="store_true", help="只统计，不删除")
        parser.add_argument("--no-refit", action="store_true", help="删除后不重新拟合主观榜")

    # ------------------------------------------------------------------
    def handle(self, *args, **options):
        keep_names: List[str] = options["keep"] or DEFAULT_KEEP
        patterns: List[str] = options["keep_pattern"] or DEFAULT_KEEP_PATTERN
        try:
            regexes: List[Pattern[str]] = [re.compile(p) for p in patterns]
        except re.error as exc:
            raise CommandError(f"用户名正则无效：{exc}") from exc

        keep_ids = set(User.objects.filter(username__in=keep_names).values_list("id", flat=True))
        for user_id, username in User.objects.values_list("id", "username"):
            if any(r.search(username) for r in regexes):
                keep_ids.add(user_id)

        missing = [n for n in keep_names if not User.objects.filter(username=n).exists()]
        if missing:
            self.stderr.write(f"提示：这些保留账号不存在 —— {'、'.join(missing)}")

        doomed = list(
            User.objects.exclude(id__in=keep_ids)
            .annotate(
                n_sub=Count("submissions", distinct=True),
                n_review=Count("review_assignments", distinct=True),
                n_vote=Count("review_assignments__comparison", distinct=True),
            )
            .order_by("id")
        )

        if not doomed:
            self.stdout.write(self.style.SUCCESS("没有需要删除的账号，数据保持原样。"))
            return

        kept = User.objects.filter(id__in=keep_ids).count()
        self.stdout.write(f"保留 {kept} 个账号，待删除 {len(doomed)} 个：\n")
        self.stdout.write(f"{'用户名':<20}{'姓名':<10}{'提交':>6}{'互评':>6}{'投票':>6}  备注")
        total_sub = total_review = total_vote = 0
        privileged = []
        for u in doomed:
            profile = Profile.objects.filter(user=u).first()
            name = profile.real_name if profile else "（无档案）"
            total_sub += u.n_sub
            total_review += u.n_review
            total_vote += u.n_vote
            note = ""
            if u.is_superuser:
                note = "超级管理员"
                privileged.append(u.username)
            elif u.is_staff:
                note = "教师权限"
                privileged.append(u.username)
            self.stdout.write(
                f"{u.username:<20}{name:<10}{u.n_sub:>6}{u.n_review:>6}{u.n_vote:>6}  {note}"
            )

        # 别人的互评任务若引用了待删提交，会跟着一起消失，先统计出来
        collateral = ReviewAssignment.objects.filter(
            Q(left__student_id__in=[u.id for u in doomed])
            | Q(right__student_id__in=[u.id for u in doomed])
        ).exclude(reviewer_id__in=[u.id for u in doomed])

        self.stdout.write(
            f"\n合计：提交 {total_sub} 条、互评任务 {total_review} 条、投票 {total_vote} 条；"
            f"另有 {collateral.count()} 条留在保留账号名下的互评任务因引用了被删作品而一并移除。"
        )
        if privileged:
            self.stdout.write(
                self.style.WARNING(f"注意：待删列表里有管理权限账号 —— {'、'.join(privileged)}")
            )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("\n--dry-run：以上仅为统计，未做任何删除。"))
            return

        with transaction.atomic():
            deleted, detail = User.objects.filter(id__in=[u.id for u in doomed]).delete()

        self.stdout.write(self.style.SUCCESS(f"\n已删除 {len(doomed)} 个账号，共清理 {deleted} 行记录。"))
        for label, count in sorted(detail.items()):
            self.stdout.write(f"  {label}: {count}")

        if not options["no_refit"]:
            self._refit()
        self._summary()

    # ------------------------------------------------------------------
    def _refit(self):
        config = load_config()
        tasks = config.review_tasks()
        if not tasks:
            self.stdout.write("配置中没有开放互评的任务，跳过主观榜拟合。")
            return
        for task in tasks:
            try:
                count = leaderboard.refresh_subjective(task)
            except Exception as exc:  # noqa: BLE001 - 剩余投票为空时排序为空属正常
                self.stdout.write(self.style.WARNING(f"{task.name} 主观榜重拟合失败：{exc}"))
            else:
                self.stdout.write(f"{task.name} 主观榜已重拟合：{count} 条")

    def _summary(self):
        self.stdout.write(
            f"\n当前数据：账号 {User.objects.count()}，提交 {Submission.objects.count()}，"
            f"互评任务 {ReviewAssignment.objects.count()}，投票 {Comparison.objects.count()}。"
        )
