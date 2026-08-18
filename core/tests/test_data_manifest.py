import json
import stat

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from core.models import CheckRun, Notification, RuntimeState
from core.services.data_manifest import build_data_manifest

pytestmark = pytest.mark.django_db


def test_manifest_is_stable_for_durable_business_data(task_factory):
    first_task = task_factory()
    second_task = task_factory(
        city_id=20,
        city_name="北京",
        movie_id="1298177",
        movie_name="沙丘：第三部",
        normalized_cinema_name="北京影院",
    )
    CheckRun.objects.create(
        task=first_task,
        status=CheckRun.Status.SUCCEEDED,
        started_at=timezone.now(),
        finished_at=timezone.now(),
        http_status=200,
        cinema_count=3,
    )
    Notification.objects.create(
        task=second_task,
        notification_type=Notification.Type.OPENING,
        status=Notification.Status.PENDING,
    )

    manifest = build_data_manifest()

    assert build_data_manifest() == manifest
    assert manifest["schema_version"] == 2
    assert manifest["models"]["auth.user"]["count"] >= 1
    assert manifest["models"]["core.monitortask"]["count"] == 2
    assert len(manifest["models"]["core.monitortask"]["sha256"]) == 64
    assert "runtimestate" not in manifest["models"]


def test_manifest_digest_changes_when_durable_data_changes(task_factory):
    task = task_factory()
    before = build_data_manifest()

    task.last_error = "网络暂时不可用"
    task.save(update_fields=["last_error"])

    after = build_data_manifest()

    assert after["models"]["core.monitortask"]["sha256"] != before["models"]["core.monitortask"][
        "sha256"
    ]


def test_manifest_ignores_runtime_state_changes():
    state = RuntimeState.get_solo()
    before = build_data_manifest()

    state.worker_heartbeat_at = timezone.now()
    state.save(update_fields=["worker_heartbeat_at"])

    assert build_data_manifest() == before


def test_manifest_ignores_ephemeral_user_login_timestamp(owner_user):
    before = build_data_manifest()

    owner_user.last_login = timezone.now()
    owner_user.save(update_fields=["last_login"])

    assert build_data_manifest() == before


def test_command_writes_private_manifest_and_only_prints_output_path(tmp_path, capsys):
    output_path = tmp_path / "local.manifest.json"

    call_command("data_manifest", "--output", output_path)

    captured = capsys.readouterr()
    assert captured.out == f"{output_path}\n"
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    assert json.loads(output_path.read_text(encoding="utf-8")) == build_data_manifest()


def test_command_requires_existing_output_parent(tmp_path):
    output_path = tmp_path / "missing" / "local.manifest.json"

    with pytest.raises(CommandError, match="parent directory"):
        call_command("data_manifest", "--output", output_path)

    assert not output_path.exists()
