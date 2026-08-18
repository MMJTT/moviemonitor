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
    if [[ -n "${RACE_FINAL_PATH:-}" ]]; then
      case "${RACE_FINAL_KIND:-}" in
        directory) mkdir "$RACE_FINAL_PATH" ;;
        symlink)
          mkdir "$RACE_FINAL_PATH.target"
          /bin/ln -s "$RACE_FINAL_PATH.target" "$RACE_FINAL_PATH"
          ;;
        *) exit 95 ;;
      esac
    fi
    if [[ -n "${TRUST_BREAK_TRIGGER:-}" ]]; then
      : >"$TRUST_BREAK_TRIGGER"
    fi
    if [[ -n "${PG_DUMP_DELAY:-}" ]]; then
      sleep "$PG_DUMP_DELAY"
    fi
    printf '%s' "${DUMP_PAYLOAD:-partial-or-complete-dump}"
    exit "${PG_DUMP_EXIT:-0}"
    ;;
  *" mark_backup_success "*)
    test -s "$EXPECTED_FINAL"
    test ! -e "$EXPECTED_OLD"
    exit "${MARK_EXIT:-0}"
    ;;
  *" createdb "*)
    if [[ -n "${SWAP_BACKUP_PATH:-}" ]]; then
      printf 'replacement-after-open' >"$SWAP_BACKUP_PATH.replacement"
      mv "$SWAP_BACKUP_PATH.replacement" "$SWAP_BACKUP_PATH"
    fi
    exit 0
    ;;
  *" pg_restore "*)
    cat >"$RESTORED_PAYLOAD"
    exit "${RESTORE_EXIT:-0}"
    ;;
  *" psql "*) printf ' 1\n 2\n 3\n 4\n 5\n 6\n' ;;
  *" dropdb "*) exit "${DROPDB_EXIT:-0}" ;;
  *) exit 97 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "ln",
        """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
no_target_directory = args[:2] == ["--no-target-directory", "--"]
if no_target_directory:
    source, destination = args[2:]
elif args[:1] == ["--"]:
    source, destination = args[1:]
else:
    raise SystemExit(94)
if not no_target_directory and os.path.isdir(destination):
    destination = os.path.join(destination, os.path.basename(source))
os.link(source, destination)
""",
    )
    _write_executable(
        fake_bin / "gzip",
        """#!/usr/bin/env bash
set -eu
case "${1:-}" in
  -c) cat ;;
  -dc)
    if [[ $# -eq 1 ]]; then cat; else cat "$2"; fi
    ;;
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
    _write_executable(
        fake_bin / "stat",
        """#!/usr/bin/env bash
set -eu
format=$2
path=
for arg in "$@"; do path=$arg; done
owner=admin
mode=700
case "$path" in
  *.env|*.lock) mode=600 ;;
esac
if [[ -n "${UNTRUSTED_PATH:-}" && "$path" == "$UNTRUSTED_PATH" ]]; then
  if [[ -z "${TRUST_BREAK_TRIGGER:-}" || -e "$TRUST_BREAK_TRIGGER" ]]; then
    owner=${UNTRUSTED_OWNER:-$owner}
    mode=${UNTRUSTED_MODE:-$mode}
  fi
fi
case "$format" in
  %U) printf '%s\n' "$owner" ;;
  %a) printf '%s\n' "$mode" ;;
  *) exit 96 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "realpath",
        """#!/usr/bin/env python3
import fcntl
import os
import sys

path = sys.argv[-1]
if path.startswith("/dev/fd/"):
    if override := os.environ.get("FD_REAL_OVERRIDE"):
        print(override)
    else:
        fd = int(path.rsplit("/", 1)[-1])
        resolved = fcntl.fcntl(fd, 50, bytes(1024))
        if isinstance(resolved, str):
            resolved = resolved.encode()
        print(resolved.rstrip(b"\\0").decode())
else:
    print(os.path.realpath(path))
""",
    )
    _write_executable(
        fake_bin / "flock",
        """#!/usr/bin/env python3
import fcntl
import sys

fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX)
""",
    )
    return fake_bin, command_log


def _sandbox_script(script_name: str, tmp_path: Path) -> tuple[Path, Path]:
    source = DEPLOY_DIR / script_name
    sandbox_root = tmp_path / "opt" / "ticketwatch"
    sandbox_root.mkdir(parents=True)
    rendered = source.read_text().replace("/proc/$$/fd", "/dev/fd")
    rendered = rendered.replace("/opt", str(tmp_path / "opt"))
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
    (root / ".env").chmod(0o600)


def _run(script: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=script.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _popen(script: Path, env: dict[str, str], *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        ["bash", str(script), *args],
        cwd=script.parent,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
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
    assert list(backup_dir.glob(".ticketwatch-backup.??????")) == []
    commands = command_log.read_text().splitlines()
    assert len(commands) == 2
    assert commands[0].endswith(
        "exec -T postgres pg_dump --format=custom --no-owner --no-acl "
        "--no-password --username ticketwatch ticketwatch"
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
    assert list(backup_dir.glob(".ticketwatch-backup.??????")) == []
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
    assert list(backup_dir.glob(".ticketwatch-backup.??????")) == []


@pytest.mark.parametrize("script_name", ["backup-postgres.sh", "verify-backup.sh"])
@pytest.mark.parametrize(
    "env_text",
    [
        "POSTGRES_USER=ticketwatch\n",
        "POSTGRES_USER=ticketwatch\nPOSTGRES_USER=other\nPOSTGRES_DB=ticketwatch\n",
        'POSTGRES_USER="ticketwatch"\nPOSTGRES_DB=ticketwatch\n',
        "POSTGRES_USER=ticket-watch\nPOSTGRES_DB=ticketwatch\n",
    ],
)
def test_scripts_reject_missing_duplicate_quoted_or_invalid_identifiers(
    script_name, env_text, tmp_path
):
    script, root = _sandbox_script(script_name, tmp_path)
    _prepare_install_root(root)
    (root / ".env").write_text(env_text)
    fake_bin, command_log = _fake_commands(tmp_path)
    env = _base_env(fake_bin, command_log)
    args: tuple[str, ...] = ()
    if script_name == "verify-backup.sh":
        backup = root / "data" / "backups" / "ticketwatch-20260817T032000Z.sql.gz"
        backup.write_bytes(b"dump")
        args = (str(backup),)

    result = _run(script, env, *args)

    assert result.returncode != 0
    assert not command_log.exists()


@pytest.mark.parametrize("script_name", ["backup-postgres.sh", "verify-backup.sh"])
def test_env_command_substitution_is_rejected_without_execution(script_name, tmp_path):
    script, root = _sandbox_script(script_name, tmp_path)
    _prepare_install_root(root)
    sentinel = tmp_path / "must-not-exist"
    (root / ".env").write_text(
        f"POSTGRES_USER=$(touch {sentinel})\nPOSTGRES_DB=ticketwatch\n"
    )
    fake_bin, command_log = _fake_commands(tmp_path)
    env = _base_env(fake_bin, command_log)
    args: tuple[str, ...] = ()
    if script_name == "verify-backup.sh":
        backup = root / "data" / "backups" / "ticketwatch-20260817T032000Z.sql.gz"
        backup.write_bytes(b"dump")
        args = (str(backup),)

    result = _run(script, env, *args)

    assert result.returncode != 0
    assert not sentinel.exists()
    assert not command_log.exists()


@pytest.mark.parametrize("script_name", ["backup-postgres.sh", "verify-backup.sh"])
def test_whitespace_prefixed_command_like_duplicate_is_not_a_comment(script_name, tmp_path):
    script, root = _sandbox_script(script_name, tmp_path)
    _prepare_install_root(root)
    sentinel = tmp_path / "must-not-exist"
    (root / ".env").write_text(
        "POSTGRES_USER=ticketwatch\n"
        "POSTGRES_DB=ticketwatch\n"
        ' POSTGRES_USER="$(command)" # x\n'
        f' POSTGRES_USER="$(touch {sentinel})" # x\n'
    )
    fake_bin, command_log = _fake_commands(tmp_path)
    env = _base_env(fake_bin, command_log)
    args: tuple[str, ...] = ()
    if script_name == "verify-backup.sh":
        backup = root / "data" / "backups" / "ticketwatch-20260817T032000Z.sql.gz"
        backup.write_bytes(b"dump")
        args = (str(backup),)

    result = _run(script, env, *args)

    assert result.returncode != 0
    assert not sentinel.exists()
    assert not command_log.exists()


@pytest.mark.parametrize("script_name", ["backup-postgres.sh", "verify-backup.sh"])
def test_scripts_reject_symlinked_env_file(script_name, tmp_path):
    script, root = _sandbox_script(script_name, tmp_path)
    _prepare_install_root(root)
    env_file = root / ".env"
    real_env = tmp_path / "real.env"
    env_file.replace(real_env)
    env_file.symlink_to(real_env)
    fake_bin, command_log = _fake_commands(tmp_path)
    env = _base_env(fake_bin, command_log)
    args: tuple[str, ...] = ()
    if script_name == "verify-backup.sh":
        backup = root / "data" / "backups" / "ticketwatch-20260817T032000Z.sql.gz"
        backup.write_bytes(b"dump")
        args = (str(backup),)

    result = _run(script, env, *args)

    assert result.returncode != 0
    assert not command_log.exists()


@pytest.mark.parametrize("script_name", ["backup-postgres.sh", "verify-backup.sh"])
@pytest.mark.parametrize("trusted_target", ["env-file", "backup-directory"])
@pytest.mark.parametrize(
    ("metadata_env", "unsafe_value"),
    [("UNTRUSTED_OWNER", "nobody"), ("UNTRUSTED_MODE", "722")],
)
def test_scripts_reject_untrusted_owner_or_writable_metadata(
    script_name, trusted_target, metadata_env, unsafe_value, tmp_path
):
    script, root = _sandbox_script(script_name, tmp_path)
    _prepare_install_root(root)
    backup_dir = root / "data" / "backups"
    fake_bin, command_log = _fake_commands(tmp_path)
    env = {
        **_base_env(fake_bin, command_log),
        "UNTRUSTED_PATH": str(root / ".env" if trusted_target == "env-file" else backup_dir),
        metadata_env: unsafe_value,
    }
    args: tuple[str, ...] = ()
    if script_name == "verify-backup.sh":
        backup = backup_dir / "ticketwatch-20260817T032000Z.sql.gz"
        backup.write_bytes(b"dump")
        args = (str(backup),)

    result = _run(script, env, *args)

    assert result.returncode != 0
    assert not command_log.exists()


def test_backup_revalidates_trusted_directory_immediately_before_publish(tmp_path):
    script, root = _sandbox_script("backup-postgres.sh", tmp_path)
    _prepare_install_root(root)
    backup_dir = root / "data" / "backups"
    fake_bin, command_log = _fake_commands(tmp_path)
    trigger = tmp_path / "trust-broken"
    final_path = backup_dir / "ticketwatch-20260817T032000Z.sql.gz"
    env = {
        **_base_env(fake_bin, command_log),
        "UNTRUSTED_PATH": str(backup_dir),
        "UNTRUSTED_MODE": "722",
        "TRUST_BREAK_TRIGGER": str(trigger),
        "EXPECTED_FINAL": str(final_path),
        "EXPECTED_OLD": str(backup_dir / "old.sql.gz"),
    }

    result = _run(script, env)

    assert result.returncode != 0
    assert trigger.exists()
    assert not final_path.exists()
    assert list(backup_dir.glob(".ticketwatch-backup.??????")) == []
    assert len(command_log.read_text().splitlines()) == 1


def test_concurrent_same_second_backup_never_replaces_first_publication(tmp_path):
    script, root = _sandbox_script("backup-postgres.sh", tmp_path)
    _prepare_install_root(root)
    backup_dir = root / "data" / "backups"
    old_backup = backup_dir / "ticketwatch-20260801T032000Z.sql.gz"
    old_backup.write_bytes(b"old")
    old_time = time.time() - 9 * 24 * 60 * 60
    os.utime(old_backup, (old_time, old_time))
    fake_bin, command_log = _fake_commands(tmp_path)
    final_path = backup_dir / "ticketwatch-20260817T032000Z.sql.gz"
    common_env = {
        **_base_env(fake_bin, command_log),
        "EXPECTED_FINAL": str(final_path),
        "EXPECTED_OLD": str(old_backup),
    }
    first = _popen(
        script,
        {**common_env, "DUMP_PAYLOAD": "first-publication", "PG_DUMP_DELAY": "0.5"},
    )
    deadline = time.monotonic() + 10
    while not command_log.exists() and time.monotonic() < deadline:
        if first.poll() is not None:
            first_stdout, first_stderr = first.communicate()
            pytest.fail(
                "first backup exited before reaching pg_dump: "
                + first_stdout
                + first_stderr
            )
        time.sleep(0.01)
    if not command_log.exists():
        first.kill()
        first_stdout, first_stderr = first.communicate()
        pytest.fail(
            "first backup did not reach pg_dump within 10 seconds: "
            + first_stdout
            + first_stderr
        )
    second = _popen(script, {**common_env, "DUMP_PAYLOAD": "second-publication"})

    first_stdout, first_stderr = first.communicate(timeout=5)
    second_stdout, second_stderr = second.communicate(timeout=5)

    assert first.returncode == 0, first_stdout + first_stderr
    assert second.returncode != 0, second_stdout + second_stderr
    assert final_path.read_bytes() == b"first-publication"
    commands = command_log.read_text().splitlines()
    assert len(commands) == 2
    assert " pg_dump " in f" {commands[0]} "
    assert " mark_backup_success " in f" {commands[1]} "


@pytest.mark.parametrize("raced_target_kind", ["directory", "symlink"])
def test_backup_publish_never_treats_raced_target_as_directory(raced_target_kind, tmp_path):
    script, root = _sandbox_script("backup-postgres.sh", tmp_path)
    _prepare_install_root(root)
    backup_dir = root / "data" / "backups"
    fake_bin, command_log = _fake_commands(tmp_path)
    final_path = backup_dir / "ticketwatch-20260817T032000Z.sql.gz"
    env = {
        **_base_env(fake_bin, command_log),
        "RACE_FINAL_KIND": raced_target_kind,
        "RACE_FINAL_PATH": str(final_path),
        "EXPECTED_FINAL": str(final_path),
        "EXPECTED_OLD": str(backup_dir / "old.sql.gz"),
    }

    result = _run(script, env)

    assert result.returncode != 0
    assert len(command_log.read_text().splitlines()) == 1
    assert list(backup_dir.glob(".ticketwatch-backup.??????")) == []
    target_dir = final_path if raced_target_kind == "directory" else Path(f"{final_path}.target")
    assert target_dir.is_dir()
    assert list(target_dir.iterdir()) == []


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
    assert all(" --no-password " in f" {command} " for command in commands)
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


def test_restore_reads_open_descriptor_when_approved_path_is_replaced(tmp_path):
    script, root = _sandbox_script("verify-backup.sh", tmp_path)
    _prepare_install_root(root)
    backup = root / "data" / "backups" / "ticketwatch-20260817T032000Z.sql.gz"
    backup.write_bytes(b"approved-before-open")
    fake_bin, command_log = _fake_commands(tmp_path)
    env = {**_base_env(fake_bin, command_log), "SWAP_BACKUP_PATH": str(backup)}

    result = _run(script, env, str(backup))

    assert result.returncode == 0, result.stderr
    assert backup.read_bytes() == b"replacement-after-open"
    assert (tmp_path / "restored.dump").read_bytes() == b"approved-before-open"


def test_restore_rejects_descriptor_that_does_not_resolve_to_approved_file(tmp_path):
    script, root = _sandbox_script("verify-backup.sh", tmp_path)
    _prepare_install_root(root)
    backup = root / "data" / "backups" / "ticketwatch-20260817T032000Z.sql.gz"
    backup.write_bytes(b"dump")
    fake_bin, command_log = _fake_commands(tmp_path)
    env = {
        **_base_env(fake_bin, command_log),
        "FD_REAL_OVERRIDE": str(tmp_path / "different.sql.gz"),
    }

    result = _run(script, env, str(backup))

    assert result.returncode != 0
    assert not command_log.exists()


def test_successful_restore_returns_failure_when_dropdb_cleanup_fails(tmp_path):
    script, root = _sandbox_script("verify-backup.sh", tmp_path)
    _prepare_install_root(root)
    backup = root / "data" / "backups" / "ticketwatch-20260817T032000Z.sql.gz"
    backup.write_bytes(b"dump")
    fake_bin, command_log = _fake_commands(tmp_path)
    env = {**_base_env(fake_bin, command_log), "DROPDB_EXIT": "47"}

    result = _run(script, env, str(backup))

    assert result.returncode != 0
    commands = command_log.read_text().splitlines()
    assert len(commands) == 4
    assert " dropdb " in f" {commands[-1]} "


def test_failed_restore_preserves_original_status_when_dropdb_also_fails(tmp_path):
    script, root = _sandbox_script("verify-backup.sh", tmp_path)
    _prepare_install_root(root)
    backup = root / "data" / "backups" / "ticketwatch-20260817T032000Z.sql.gz"
    backup.write_bytes(b"dump")
    fake_bin, command_log = _fake_commands(tmp_path)
    env = {
        **_base_env(fake_bin, command_log),
        "RESTORE_EXIT": "31",
        "DROPDB_EXIT": "47",
    }

    result = _run(script, env, str(backup))

    assert result.returncode == 31
    assert " dropdb " in f" {command_log.read_text().splitlines()[-1]} "


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
