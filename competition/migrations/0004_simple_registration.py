"""简化自助注册：去掉邀请码、学号前缀、注册审核与强制改密。"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("competition", "0003_registration_and_review_settings"),
    ]

    operations = [
        migrations.RemoveField(model_name="profile", name="status"),
        migrations.RemoveField(model_name="appsetting", name="registration_code"),
        migrations.RemoveField(model_name="appsetting", name="registration_prefix"),
        migrations.RemoveField(model_name="appsetting", name="registration_require_approval"),
        migrations.RemoveField(
            model_name="appsetting", name="registration_force_password_change"
        ),
    ]
