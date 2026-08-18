from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_agent_mail_verification_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentmailconfig",
            name="verification_next_attempt_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="agentmailconfig",
            name="verification_retry_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
