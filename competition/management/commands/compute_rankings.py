"""计算并保存主观排名（Bradley–Terry），两个任务分别拟合。"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from competition.config import load_config
from competition.services import leaderboard


class Command(BaseCommand):
    help = "拟合 Bradley–Terry 模型，输出各任务的主观排名并写入数据库快照"

    def add_arguments(self, parser):
        parser.add_argument("--task", default=None, help="只计算指定任务（默认全部）")
        parser.add_argument("--top", type=int, default=20)

    def handle(self, *args, **options):
        config = load_config()
        if options["task"]:
            task = config.task(options["task"])
            if not task or not config.peer_review.for_task(task.slug).enabled:
                raise CommandError(f"{options['task']} 不是已开放互评的任务。")
            targets = [task]
        else:
            targets = config.review_tasks()
        if not targets:
            raise CommandError("配置中没有开放互评的任务。")

        for task in targets:
            rows = leaderboard.subjective_rows(task)
            if not rows:
                self.stdout.write(self.style.WARNING(f"[{task.name}] 还没有互评记录，跳过。"))
                continue

            count = leaderboard.refresh_subjective(task)
            self.stdout.write(self.style.SUCCESS(f"[{task.name}] 已更新 {count} 条主观排名。"))
            self.stdout.write(f"{'名次':<6}{'编号':<8}{'主观分':<10}{'比较次数':<10}分图")
            for row in rows[: options["top"]]:
                per = " ".join(f"{k}={v:.1f}" for k, v in row.per_image.items())
                self.stdout.write(
                    f"{row.rank:<6}{row.code:<8}{row.score:<10.2f}{row.comparisons:<10}{per}"
                )
