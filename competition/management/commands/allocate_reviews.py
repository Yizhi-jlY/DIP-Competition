"""分配匿名互评任务。"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from competition.config import load_config
from competition.services import reviews


class Command(BaseCommand):
    help = "为去色任务分配匿名两两比较任务"

    def add_arguments(self, parser):
        parser.add_argument("--per-student", type=int, default=None, help="每人比较次数")
        parser.add_argument("--seed", type=int, default=2026)
        parser.add_argument("--keep", action="store_true", help="保留已有分配（不删除旧记录）")

    def handle(self, *args, **options):
        config = load_config()
        task = config.task(config.peer_review.task)
        if not task:
            raise CommandError("peer_review.task 未指向有效任务。")
        try:
            summary = reviews.allocate_reviews(
                task,
                per_student=options["per_student"],
                seed=options["seed"],
                replace=not options["keep"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(
            f"生成 {summary['assignments']} 条互评任务，覆盖 {summary['reviewers']} 位学生"
            f"（每人 {summary['per_student']} 次）。"
        ))
        cov = summary["coverage"]
        self.stdout.write(
            f"覆盖情况：作品 {cov['entries']} 张，平均被评价 {cov['mean']} 次，"
            f"最少 {cov['min']} 次，最多 {cov['max']} 次，未覆盖 {cov['uncovered']} 张。"
        )
        if summary["repaired"]:
            self.stdout.write(f"补充分配 {summary['repaired']} 条以消除零覆盖。")
        if summary["issues"]:
            self.stdout.write(self.style.WARNING("检查提示："))
            for issue in summary["issues"][:10]:
                self.stdout.write(f"  - {issue}")
