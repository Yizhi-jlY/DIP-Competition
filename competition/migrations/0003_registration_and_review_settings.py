# Generated for 自助注册 + 双任务互评设置

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("competition", "0002_submission_finalized_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="status",
            field=models.CharField(
                choices=[("active", "正常"), ("pending", "待审核"), ("rejected", "已拒绝")],
                default="active",
                max_length=12,
                verbose_name="账号状态",
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="imported_at",
            field=models.DateTimeField(auto_now_add=True, verbose_name="导入/注册时间"),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="registration_open",
            field=models.BooleanField(default=False, verbose_name="开放自助注册"),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="registration_code",
            field=models.CharField(blank=True, max_length=64, verbose_name="注册邀请码"),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="registration_prefix",
            field=models.CharField(
                blank=True,
                help_text="留空表示不限制；填写后学号必须以此开头",
                max_length=16,
                verbose_name="学号前缀限制",
            ),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="registration_require_approval",
            field=models.BooleanField(default=False, verbose_name="注册后需教师审核"),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="registration_force_password_change",
            field=models.BooleanField(default=False, verbose_name="注册后强制改密"),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="review_counts",
            field=models.JSONField(blank=True, default=dict, verbose_name="各任务评价份数"),
        ),
    ]
