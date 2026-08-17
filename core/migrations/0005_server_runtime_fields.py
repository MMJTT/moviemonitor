import django.core.validators
from django.db import migrations, models

FIXED_RECIPIENT = "850634546@qq.com"


def copy_poll_interval_and_reset_mail(apps, schema_editor):
    AppSetting = apps.get_model("core", "AppSetting")
    AgentMailConfig = apps.get_model("core", "AgentMailConfig")
    AppSetting.objects.all().update(
        urgent_interval_seconds=models.F("poll_interval_seconds")
    )
    AgentMailConfig.objects.all().update(
        recipient_email=FIXED_RECIPIENT,
        is_verified=False,
        verified_at=None,
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0004_switch_notifications_to_agent_mail")]

    operations = [
        migrations.AddField(
            model_name="appsetting",
            name="far_interval_seconds",
            field=models.PositiveIntegerField(
                default=900, validators=[django.core.validators.MinValueValidator(60)]
            ),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="near_interval_seconds",
            field=models.PositiveIntegerField(
                default=300, validators=[django.core.validators.MinValueValidator(60)]
            ),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="near_window_days",
            field=models.PositiveIntegerField(
                default=7, validators=[django.core.validators.MinValueValidator(1)]
            ),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="urgent_interval_seconds",
            field=models.PositiveIntegerField(
                default=60, validators=[django.core.validators.MinValueValidator(60)]
            ),
        ),
        migrations.AddField(
            model_name="appsetting",
            name="urgent_window_hours",
            field=models.PositiveIntegerField(
                default=48, validators=[django.core.validators.MinValueValidator(1)]
            ),
        ),
        migrations.AlterField(
            model_name="agentmailconfig",
            name="recipient_email",
            field=models.EmailField(default=FIXED_RECIPIENT, editable=False, max_length=254),
        ),
        migrations.AddField(
            model_name="monitortask",
            name="claim_expires_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="monitortask",
            name="claim_token",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="notification",
            name="notification_type",
            field=models.CharField(
                choices=[
                    ("OPENING", "开票"),
                    ("EXPIRY", "到期"),
                    ("SYSTEM_ALERT", "系统提醒"),
                ],
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="notification",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDING", "待发送"),
                    ("SENDING", "发送中"),
                    ("SENT", "成功"),
                    ("FAILED", "失败"),
                    ("PERMANENT_FAILED", "永久失败"),
                    ("NEEDS_REVIEW", "需要人工处理"),
                ],
                default="PENDING",
                max_length=24,
            ),
        ),
        migrations.RunPython(copy_poll_interval_and_reset_mail, migrations.RunPython.noop),
    ]
