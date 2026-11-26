"""计算并保存主观排名（Bradley–Terry）。"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from competition.config import load_config
from competition.services import leaderboard


class Command(BaseCommand):
    help = "拟合 Bradley–Terry 模型，输出去色主观排名并写入数据库快照"

    def add_arguments(self, parser):
        parser.add_argument("--top", type=int, default=20)

    def handle(self, *args, **options):
        config = load_config()
        task = config.task(config.peer_review.task)
        if not task:
            raise CommandError("peer_review.task 未指向有效任务。")

        rows = leaderboard.subjective_rows(task)
        if not rows:
            raise CommandError("还没有互评记录，无法计算主观排名。")

        count = leaderboard.refresh_subjective(task)
        self.stdout.write(self.style.SUCCESS(f"已更新 {count} 条主观排名。"))
        self.stdout.write(f"{'名次':<6}{'编号':<8}{'主观分':<10}{'比较次数':<10}分图")
        for row in rows[: options["top"]]:
            per = " ".join(f"{k}={v:.1f}" for k, v in row.per_image.items())
            self.stdout.write(
                f"{row.rank:<6}{row.code:<8}{row.score:<10.2f}{row.comparisons:<10}{per}"
            )
