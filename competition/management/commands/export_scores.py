"""导出成绩 CSV。"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand

from competition.services import export


class Command(BaseCommand):
    help = "导出成绩表 CSV（含两个任务的指标、名次与主观分）"

    def add_arguments(self, parser):
        parser.add_argument("--out", default="scores.csv")
        parser.add_argument("--with-images", default="", help="同时导出最终作品 zip 到该路径")

    def handle(self, *args, **options):
        text = export.scores_csv()
        out = Path(options["out"])
        out.write_text(text, encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"成绩已写入 {out.resolve()}"))

        if options["with_images"]:
            buffer = export.submissions_zip()
            target = Path(options["with_images"])
            target.write_bytes(buffer.getvalue())
            self.stdout.write(self.style.SUCCESS(f"最终作品已打包到 {target.resolve()}"))
