import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

MIGRATION_0004 = ("core", "0004_switch_notifications_to_agent_mail")
MIGRATION_0005 = ("core", "0005_server_runtime_fields")
MIGRATION_0006 = ("core", "0006_terminal_failure_streak")
MIGRATION_0007 = ("core", "0007_runtime_state")
MIGRATION_0008 = ("core", "0008_agent_mail_verification_state")
MIGRATION_0009 = ("core", "0009_agent_mail_verification_retry")
MIGRATION_0010 = ("core", "0010_invite_users_and_task_owners")


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


@pytest.mark.django_db(transaction=True)
def test_0006_initializes_existing_task_terminal_failure_streak():
    executor = MigrationExecutor(connection)
    assert MIGRATION_0006 in executor.loader.graph.nodes
    executor.migrate([MIGRATION_0005])
    old_apps = executor.loader.project_state([MIGRATION_0005]).apps
    OldMonitorTask = old_apps.get_model("core", "MonitorTask")
    OldMonitorTask.objects.all().delete()
    task = OldMonitorTask.objects.create(
        source_url="https://www.maoyan.com/cinemas?movieId=1545360",
        normalized_url="https://www.maoyan.com/cinemas?movieId=1545360",
        query_key="maoyan:10:migration-terminal-streak",
        city_id=10,
        city_name="上海",
        movie_id="1545360",
        movie_name="奥德赛",
        show_date=timezone.localdate(),
        cinema_name="测试影院",
        normalized_cinema_name="测试影院",
        consecutive_failures=4,
    )

    executor = MigrationExecutor(connection)
    executor.migrate([MIGRATION_0006])
    new_apps = executor.loader.project_state([MIGRATION_0006]).apps
    NewMonitorTask = new_apps.get_model("core", "MonitorTask")

    migrated = NewMonitorTask.objects.get(pk=task.pk)
    assert migrated.consecutive_failures == 4
    assert migrated.consecutive_terminal_failures == 0

    restore_executor = MigrationExecutor(connection)
    restore_executor.migrate(restore_executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("is_verified", "expected_status"),
    [(True, "VERIFIED"), (False, "FAILED")],
)
def test_0008_initializes_verification_status_from_existing_mail_verification(
    is_verified, expected_status
):
    executor = MigrationExecutor(connection)
    assert MIGRATION_0008 in executor.loader.graph.nodes
    executor.migrate([MIGRATION_0007])
    old_apps = executor.loader.project_state([MIGRATION_0007]).apps
    OldAgentMailConfig = old_apps.get_model("core", "AgentMailConfig")
    OldAgentMailConfig.objects.all().delete()
    OldAgentMailConfig.objects.create(id=1, is_verified=is_verified)

    executor = MigrationExecutor(connection)
    executor.migrate([MIGRATION_0008])
    new_apps = executor.loader.project_state([MIGRATION_0008]).apps
    NewAgentMailConfig = new_apps.get_model("core", "AgentMailConfig")

    config = NewAgentMailConfig.objects.get(pk=1)
    assert config.verification_status == expected_status
    assert config.verification_requested_at is None
    assert config.verification_completed_at is None
    assert config.verification_claim_token is None
    assert config.verification_claim_expires_at is None

    restore_executor = MigrationExecutor(connection)
    restore_executor.migrate(restore_executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_0009_initializes_persisted_identity_retry_state():
    executor = MigrationExecutor(connection)
    assert MIGRATION_0009 in executor.loader.graph.nodes
    executor.migrate([MIGRATION_0008])
    old_apps = executor.loader.project_state([MIGRATION_0008]).apps
    OldAgentMailConfig = old_apps.get_model("core", "AgentMailConfig")
    OldAgentMailConfig.objects.all().delete()
    OldAgentMailConfig.objects.create(id=1, is_verified=False)

    executor = MigrationExecutor(connection)
    executor.migrate([MIGRATION_0009])
    new_apps = executor.loader.project_state([MIGRATION_0009]).apps
    NewAgentMailConfig = new_apps.get_model("core", "AgentMailConfig")

    config = NewAgentMailConfig.objects.get(pk=1)
    assert config.verification_retry_count == 0
    assert config.verification_next_attempt_at is None

    restore_executor = MigrationExecutor(connection)
    restore_executor.migrate(restore_executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_0010_assigns_existing_tasks_to_the_invited_admin_owner():
    executor = MigrationExecutor(connection)
    executor.migrate([MIGRATION_0009])
    auth_leaf = executor.loader.graph.leaf_nodes("auth")[0]
    old_apps = executor.loader.project_state([MIGRATION_0009, auth_leaf]).apps
    OldMonitorTask = old_apps.get_model("core", "MonitorTask")
    OldUser = old_apps.get_model("auth", "User")
    OldMonitorTask.objects.all().delete()
    OldUser.objects.filter(username="850634546@qq.com").delete()
    old_task = OldMonitorTask.objects.create(
        source_url="https://www.maoyan.com/cinemas?movieId=1545360",
        normalized_url="https://www.maoyan.com/cinemas?movieId=1545360",
        query_key="maoyan:10:legacy-owner",
        city_id=10,
        city_name="上海",
        movie_id="1545360",
        movie_name="奥德赛",
        show_date=timezone.localdate(),
        cinema_name="测试影院",
        normalized_cinema_name="测试影院",
    )

    executor = MigrationExecutor(connection)
    executor.migrate([MIGRATION_0010])
    new_apps = executor.loader.project_state([MIGRATION_0010, auth_leaf]).apps
    NewMonitorTask = new_apps.get_model("core", "MonitorTask")
    NewUser = new_apps.get_model("auth", "User")

    owner = NewUser.objects.get(username="850634546@qq.com")
    migrated = NewMonitorTask.objects.get(pk=old_task.pk)
    assert migrated.owner_id == owner.pk
    assert owner.email == "850634546@qq.com"
    assert owner.is_staff is True
    assert owner.is_superuser is True
    assert owner.password.startswith("!")

    restore_executor = MigrationExecutor(connection)
    restore_executor.migrate(restore_executor.loader.graph.leaf_nodes())
