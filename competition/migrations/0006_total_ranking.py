"""总排名权重与折算方式（教师面板可调，空值表示沿用 yaml）。"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("competition", "0005_objective_ranking"),
    ]

    operations = [
        migrations.AddField(
            model_name="appsetting",
            name="total_weights",
            field=models.JSONField(blank=True, default=dict, verbose_name="总排名权重"),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="total_normalize",
            field=models.CharField(
                blank=True, default="", max_length=8, verbose_name="总排名折算方式"
            ),
        ),
    ]
