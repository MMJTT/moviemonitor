from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0003_add_agent_mail_config")]

    operations = [
        migrations.RenameField(
            model_name="notification",
            old_name="smtp_response",
            new_name="transport_response",
        ),
        migrations.DeleteModel(name="SMTPConfig"),
    ]
