"""客观排名快照（按任务、按参赛者）。"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("competition", "0004_simple_registration"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ObjectiveRanking",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("task", models.CharField(max_length=32, verbose_name="任务")),
                (
                    "metric",
                    models.CharField(
                        blank=True, default="", max_length=16, verbose_name="排名指标"
                    ),
                ),
                (
                    "score",
                    models.FloatField(blank=True, null=True, verbose_name="排名指标均值"),
                ),
                (
                    "tiebreak",
                    models.FloatField(blank=True, null=True, verbose_name="并列指标均值"),
                ),
                ("per_image", models.JSONField(blank=True, default=dict, verbose_name="分图指标")),
                ("rank", models.PositiveIntegerField(default=0, verbose_name="名次")),
                ("computed_at", models.DateTimeField(auto_now=True, verbose_name="计算时间")),
                (
                    "student",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="objective_rankings",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="学生",
                    ),
                ),
            ],
            options={
                "verbose_name": "客观排名",
                "verbose_name_plural": "客观排名",
                "ordering": ["task", "rank"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("student", "task"), name="uniq_objective_per_student"
                    )
                ],
            },
        ),
    ]
