import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

MIGRATION_0004 = ("core", "0004_switch_notifications_to_agent_mail")
MIGRATION_0005 = ("core", "0005_server_runtime_fields")


@pytest.mark.django_db(transaction=True)
def test_0005_copies_poll_interval_and_resets_fixed_mail_recipient():
    executor = MigrationExecutor(connection)
    executor.migrate([MIGRATION_0004])
    old_apps = executor.loader.project_state([MIGRATION_0004]).apps
    OldAppSetting = old_apps.get_model("core", "AppSetting")
    OldAgentMailConfig = old_apps.get_model("core", "AgentMailConfig")
    OldAppSetting.objects.all().delete()
    OldAgentMailConfig.objects.all().delete()
    verified_at = timezone.now()
    OldAppSetting.objects.create(id=1, poll_interval_seconds=123)
    OldAgentMailConfig.objects.create(
        id=1,
        recipient_email="old@example.com",
        is_verified=True,
        verified_at=verified_at,
    )

    executor = MigrationExecutor(connection)
    executor.migrate([MIGRATION_0005])
    new_apps = executor.loader.project_state([MIGRATION_0005]).apps
    NewAppSetting = new_apps.get_model("core", "AppSetting")
    NewAgentMailConfig = new_apps.get_model("core", "AgentMailConfig")

    setting = NewAppSetting.objects.get(pk=1)
    config = NewAgentMailConfig.objects.get(pk=1)
    assert setting.urgent_interval_seconds == 123
    assert config.recipient_email == "850634546@qq.com"
    assert config.is_verified is False
    assert config.verified_at is None

    restore_executor = MigrationExecutor(connection)
    restore_executor.migrate(restore_executor.loader.graph.leaf_nodes())
