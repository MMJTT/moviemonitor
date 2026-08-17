from __future__ import annotations

import configparser
import os
import shlex
import stat
import subprocess
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = PROJECT_ROOT / "deploy"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _fake_commands(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command_log = tmp_path / "commands.log"
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >>"$COMMAND_LOG"
case " $* " in
  *" pg_dump "*)
    printf 'partial-or-complete-dump'
    exit "${PG_DUMP_EXIT:-0}"
    ;;
  *" mark_backup_success "*)
    test -s "$EXPECTED_FINAL"
    test ! -e "$EXPECTED_OLD"
    exit "${MARK_EXIT:-0}"
    ;;
  *" createdb "*) exit 0 ;;
  *" pg_restore "*)
    cat >"$RESTORED_PAYLOAD"
    exit "${RESTORE_EXIT:-0}"
    ;;
  *" psql "*) printf ' 1\n 2\n 3\n 4\n 5\n 6\n' ;;
  *" dropdb "*) exit 0 ;;
  *) exit 97 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "gzip",
        """#!/usr/bin/env bash
set -eu
case "${1:-}" in
  -c) cat ;;
  -dc) cat "$2" ;;
  *) exit 98 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "date",
        """#!/usr/bin/env bash
set -eu
case "${2:-}" in
  +%Y%m%dT%H%M%SZ) printf '20260817T032000Z\n' ;;
  *) exit 99 ;;
esac
""",
    )
    return fake_bin, command_log


def _sandbox_script(script_name: str, tmp_path: Path) -> tuple[Path, Path]:
    source = DEPLOY_DIR / script_name
    sandbox_root = tmp_path / "opt" / "ticketwatch"
    sandbox_root.mkdir(parents=True)
    rendered = source.read_text().replace("/opt/ticketwatch", str(sandbox_root))
    script = tmp_path / script_name
    _write_executable(script, rendered)
    return script, sandbox_root


def _base_env(fake_bin: Path, command_log: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "COMMAND_LOG": str(command_log),
        "RESTORED_PAYLOAD": str(command_log.parent / "restored.dump"),
    }


def _prepare_install_root(root: Path) -> None:
    (root / "app").mkdir()
    (root / "data" / "backups").mkdir(parents=True)
    (root / ".env").write_text(
        "POSTGRES_DB=ticketwatch\n"
        "POSTGRES_USER=ticketwatch\n"
        "POSTGRES_PASSWORD=do-not-print-this-secret\n"
    )


def _run(script: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=script.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_backup_publishes_atomically_then_rotates_and_records_success(tmp_path):
    script, root = _sandbox_script("backup-postgres.sh", tmp_path)
    _prepare_install_root(root)
    backup_dir = root / "data" / "backups"
    old_backup = backup_dir / "ticketwatch-20260801T032000Z.sql.gz"
    unrelated = backup_dir / "notes.sql.gz"
    nested = backup_dir / "nested" / "ticketwatch-20260801T032000Z.sql.gz"
    old_backup.write_bytes(b"old")
    unrelated.write_bytes(b"unrelated")
    nested.parent.mkdir()
    nested.write_bytes(b"nested")
    old_time = time.time() - 9 * 24 * 60 * 60
    os.utime(old_backup, (old_time, old_time))
    os.utime(unrelated, (old_time, old_time))
    os.utime(nested, (old_time, old_time))
    fake_bin, command_log = _fake_commands(tmp_path)
    final_path = backup_dir / "ticketwatch-20260817T032000Z.sql.gz"
    env = {
        **_base_env(fake_bin, command_log),
        "EXPECTED_FINAL": str(final_path),
        "EXPECTED_OLD": str(old_backup),
    }

    result = _run(script, env)

    assert result.returncode == 0, result.stderr
    assert final_path.read_bytes() == b"partial-or-complete-dump"
    assert stat.S_IMODE(final_path.stat().st_mode) == 0o600
    assert not old_backup.exists()
    assert unrelated.exists()
    assert nested.exists()
    assert list(backup_dir.glob(".ticketwatch-backup.*")) == []
    commands = command_log.read_text().splitlines()
    assert len(commands) == 2
    assert commands[0].endswith(
        "exec -T postgres pg_dump --format=custom --no-owner --no-acl "
        "--username ticketwatch ticketwatch"
    )
    assert commands[1].endswith(
        "exec -T web python manage.py mark_backup_success "
        "--name ticketwatch-20260817T032000Z.sql.gz"
    )
    assert "do-not-print-this-secret" not in result.stdout + result.stderr
    assert "do-not-print-this-secret" not in command_log.read_text()


def test_failed_dump_removes_only_temp_and_does_not_rotate(tmp_path):
    script, root = _sandbox_script("backup-postgres.sh", tmp_path)
    _prepare_install_root(root)
    backup_dir = root / "data" / "backups"
    old_backup = backup_dir / "ticketwatch-20260801T032000Z.sql.gz"
    old_backup.write_bytes(b"old")
    old_time = time.time() - 9 * 24 * 60 * 60
    os.utime(old_backup, (old_time, old_time))
    fake_bin, command_log = _fake_commands(tmp_path)
    final_path = backup_dir / "ticketwatch-20260817T032000Z.sql.gz"
    env = {
        **_base_env(fake_bin, command_log),
        "EXPECTED_FINAL": str(final_path),
        "EXPECTED_OLD": str(old_backup),
        "PG_DUMP_EXIT": "17",
    }

    result = _run(script, env)

    assert result.returncode == 17
    assert not final_path.exists()
    assert old_backup.read_bytes() == b"old"
    assert list(backup_dir.glob(".ticketwatch-backup.*")) == []
    assert len(command_log.read_text().splitlines()) == 1
    assert "do-not-print-this-secret" not in result.stdout + result.stderr


def test_failed_status_record_keeps_completed_backup(tmp_path):
    script, root = _sandbox_script("backup-postgres.sh", tmp_path)
    _prepare_install_root(root)
    backup_dir = root / "data" / "backups"
    old_backup = backup_dir / "ticketwatch-20260801T032000Z.sql.gz"
    old_backup.write_bytes(b"old")
    old_time = time.time() - 9 * 24 * 60 * 60
    os.utime(old_backup, (old_time, old_time))
    fake_bin, command_log = _fake_commands(tmp_path)
    final_path = backup_dir / "ticketwatch-20260817T032000Z.sql.gz"
    env = {
        **_base_env(fake_bin, command_log),
        "EXPECTED_FINAL": str(final_path),
        "EXPECTED_OLD": str(old_backup),
        "MARK_EXIT": "23",
    }

    result = _run(script, env)

    assert result.returncode == 23
    assert final_path.read_bytes() == b"partial-or-complete-dump"
    assert list(backup_dir.glob(".ticketwatch-backup.*")) == []


@pytest.mark.parametrize(
    "candidate_kind",
    ["missing", "relative", "outside", "symlink-outside", "nested", "wrong-name"],
)
def test_restore_rejects_anything_except_an_explicit_direct_backup(candidate_kind, tmp_path):
    script, root = _sandbox_script("verify-backup.sh", tmp_path)
    _prepare_install_root(root)
    backup_dir = root / "data" / "backups"
    valid_name = "ticketwatch-20260817T032000Z.sql.gz"
    if candidate_kind == "missing":
        args: tuple[str, ...] = ()
    elif candidate_kind == "relative":
        relative = tmp_path / valid_name
        relative.write_bytes(b"dump")
        args = (relative.name,)
    elif candidate_kind == "outside":
        outside = tmp_path / valid_name
        outside.write_bytes(b"dump")
        args = (str(outside),)
    elif candidate_kind == "symlink-outside":
        outside = tmp_path / "outside.sql.gz"
        outside.write_bytes(b"dump")
        symlink = backup_dir / valid_name
        symlink.symlink_to(outside)
        args = (str(symlink),)
    elif candidate_kind == "nested":
        nested = backup_dir / "nested" / valid_name
        nested.parent.mkdir()
        nested.write_bytes(b"dump")
        args = (str(nested),)
    else:
        wrong_name = backup_dir / "latest.sql.gz"
        wrong_name.write_bytes(b"dump")
        args = (str(wrong_name),)
    fake_bin, command_log = _fake_commands(tmp_path)
    env = _base_env(fake_bin, command_log)

    result = _run(script, env, *args)

    assert result.returncode != 0
    assert not command_log.exists()
    assert "do-not-print-this-secret" not in result.stdout + result.stderr


def test_restore_uses_fresh_isolated_database_and_always_drops_it(tmp_path):
    script, root = _sandbox_script("verify-backup.sh", tmp_path)
    _prepare_install_root(root)
    backup = root / "data" / "backups" / "ticketwatch-20260817T032000Z.sql.gz"
    backup.write_bytes(b"verified-dump")
    fake_bin, command_log = _fake_commands(tmp_path)
    env = _base_env(fake_bin, command_log)

    first = _run(script, env, str(backup))
    second = _run(script, env, str(backup))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    commands = command_log.read_text().splitlines()
    assert len(commands) == 8
    created_names: list[str] = []
    for offset in (0, 4):
        created = shlex.split(commands[offset])[-1]
        restored = shlex.split(commands[offset + 1])[-1]
        queried = shlex.split(commands[offset + 2])
        dropped = shlex.split(commands[offset + 3])[-1]
        assert created.startswith("ticketwatch_restore_")
        assert created != "ticketwatch"
        assert restored == created
        assert queried[queried.index("--dbname") + 1] == created
        assert dropped == created
        created_names.append(created)
    assert created_names[0] != created_names[1]
    dropped_names = [shlex.split(command)[-1] for command in commands if " dropdb " in command]
    assert dropped_names == created_names
    query = commands[2]
    for table in (
        "django_migrations",
        "core_monitortask",
        "core_checkrun",
        "core_notification",
        "core_appsetting",
        "core_agentmailconfig",
    ):
        assert table in query
    assert (tmp_path / "restored.dump").read_bytes() == b"verified-dump"
    assert "do-not-print-this-secret" not in first.stdout + first.stderr


def test_restore_failure_drops_only_the_database_created_by_that_run(tmp_path):
    script, root = _sandbox_script("verify-backup.sh", tmp_path)
    _prepare_install_root(root)
    backup = root / "data" / "backups" / "ticketwatch-20260817T032000Z.sql.gz"
    backup.write_bytes(b"broken-dump")
    fake_bin, command_log = _fake_commands(tmp_path)
    env = {**_base_env(fake_bin, command_log), "RESTORE_EXIT": "31"}

    result = _run(script, env, str(backup))

    assert result.returncode == 31
    commands = command_log.read_text().splitlines()
    assert len(commands) == 3
    created = shlex.split(commands[0])[-1]
    assert " createdb " in f" {commands[0]} "
    assert " pg_restore " in f" {commands[1]} "
    assert " dropdb " in f" {commands[2]} "
    assert shlex.split(commands[2])[-1] == created
    assert created != "ticketwatch"


def test_scripts_pin_install_paths_and_pass_bash_syntax_check():
    backup = DEPLOY_DIR / "backup-postgres.sh"
    restore = DEPLOY_DIR / "verify-backup.sh"
    for script in (backup, restore):
        result = subprocess.run(["bash", "-n", script], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        source = script.read_text()
        assert source.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\numask 077\n")
        assert "WORK_DIR=/opt/ticketwatch/app" in source
        assert "BACKUP_DIR=/opt/ticketwatch/data/backups" in source
        assert "ENV_FILE=/opt/ticketwatch/.env" in source


def test_systemd_units_schedule_the_backup_as_admin():
    service = configparser.ConfigParser(interpolation=None)
    service.optionxform = str
    service.read(DEPLOY_DIR / "ticketwatch-backup.service")
    assert dict(service["Service"]) == {
        "Type": "oneshot",
        "User": "admin",
        "ExecStart": "/opt/ticketwatch/app/deploy/backup-postgres.sh",
    }

    timer = configparser.ConfigParser(interpolation=None)
    timer.optionxform = str
    timer.read(DEPLOY_DIR / "ticketwatch-backup.timer")
    assert dict(timer["Timer"]) == {
        "OnCalendar": "*-*-* 03:20:00 Asia/Shanghai",
        "Persistent": "true",
        "RandomizedDelaySec": "300",
        "Unit": "ticketwatch-backup.service",
    }
    assert timer["Install"]["WantedBy"] == "timers.target"
