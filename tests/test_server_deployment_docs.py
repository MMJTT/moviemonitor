import re
import shlex
from pathlib import Path, PurePosixPath

import pytest

DOCUMENT = (Path(__file__).resolve().parents[1] / "docs" / "server-deployment.md").read_text(
    encoding="utf-8"
)
AGENT_MAIL_HOST_ROOT = "/opt/ticketwatch/data/agently"
AGENT_MAIL_CONTAINER_ROOT = "/home/ticketwatch"


def _is_equal_or_descendant(candidate, root):
    candidate_path = PurePosixPath(candidate)
    root_path = PurePosixPath(root)
    return candidate_path == root_path or root_path in candidate_path.parents


def _bash_commands(document):
    for block in re.findall(r"```bash\s*\n(.*?)```", document, flags=re.DOTALL):
        pending = ""
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            pending += stripped
            if pending.endswith("\\"):
                pending = pending[:-1] + " "
                continue
            yield pending
            pending = ""
        if pending:
            yield pending


def _option_values(tokens, short_name, long_name):
    for index, token in enumerate(tokens):
        if token in {short_name, long_name} and index + 1 < len(tokens):
            yield tokens[index + 1]
        elif token.startswith(f"{long_name}="):
            yield token.partition("=")[2]
        elif short_name and token.startswith(short_name) and token != short_name:
            yield token[len(short_name) :].removeprefix("=")


def _volume_paths(tokens):
    for specification in _option_values(tokens, "-v", "--volume"):
        parts = specification.split(":", 2)
        if len(parts) >= 2:
            yield parts[0], parts[1]
    for specification in _option_values(tokens, "", "--mount"):
        fields = dict(
            field.split("=", 1)
            for field in specification.split(",")
            if "=" in field
        )
        source = fields.get("source", fields.get("src"))
        target = fields.get("target", fields.get("dst", fields.get("destination")))
        if source is not None and target is not None:
            yield source, target


def _assert_document_has_no_web_agent_mail_mount(document):
    offenders = []
    for command in _bash_commands(document):
        tokens = shlex.split(command)
        compose_indexes = [
            index
            for index, token in enumerate(tokens[:-1])
            if token == "docker" and tokens[index + 1] == "compose"
        ]
        for compose_index in compose_indexes:
            compose_tokens = tokens[compose_index:]
            if "web" not in compose_tokens:
                continue
            for source, target in _volume_paths(compose_tokens):
                if _is_equal_or_descendant(
                    source, AGENT_MAIL_HOST_ROOT
                ) or _is_equal_or_descendant(target, AGENT_MAIL_CONTAINER_ROOT):
                    offenders.append((source, target))
    assert offenders == []


def test_macos_checksum_and_contexts_are_copyable():
    assert "(cd migration-data && shasum -a 256" in DOCUMENT
    assert "在 **Mac** 执行：" in DOCUMENT
    assert "在 **服务器** 执行" in DOCUMENT
    assert "sha256sum -c -" in DOCUMENT


def test_import_permissions_and_manifest_gate_fail_closed():
    assert (
        "sudo install -d -o admin -g admin -m 0751 /opt/ticketwatch/migration-data"
        in DOCUMENT
    )
    assert "sudo chown 10001:admin core-data.json" in DOCUMENT
    assert "sudo chmod 0640 core-data.json" in DOCUMENT
    assert "sudo chmod 0600 local.manifest.json SHA256SUMS" in DOCUMENT
    assert (
        "web sh -ec 'python manage.py loaddata --format=json - "
        "< /migration/core-data.json'" in DOCUMENT
    )
    assert "web python manage.py loaddata /migration/core-data.json" not in DOCUMENT
    assert "if ! cmp -s" in DOCUMENT
    assert "exit 1" in DOCUMENT


def test_private_deployment_and_environment_invariants_are_documented():
    assert "https://github.com/MMJTT/moviemonitor.git" in DOCUMENT
    assert "RELEASE_REF='refs/heads/feature/ticket-monitor'" in DOCUMENT
    assert 'git ls-remote --exit-code "$REPOSITORY_URL" "$RELEASE_REF"' in DOCUMENT
    assert 'merge-base --is-ancestor "$RELEASE_SHA" FETCH_HEAD' in DOCUMENT
    reviewed_ref = DOCUMENT[DOCUMENT.index("RELEASE_REF=") : DOCUMENT.index("创建 `.env`")]
    assert "set -Eeuo pipefail" in reviewed_ref
    assert "admin:admin" in DOCUMENT
    assert "sudo test -f /opt/ticketwatch/.env && sudo test ! -L" in DOCUMENT
    assert "TICKETWATCH_ENV_FILE=/opt/ticketwatch/.env" in DOCUMENT
    assert "AGENTLY_DATA_DIR=/opt/ticketwatch/data/agently" in DOCUMENT


def test_upgrade_rollback_and_incident_evidence_are_persistent():
    assert "/opt/ticketwatch/data/upgrade-records" in DOCUMENT
    assert (
        "RECORD='/opt/ticketwatch/data/upgrade-records/REPLACE_WITH_SELECTED_RECORD.env'"
        in DOCUMENT
    )
    assert "BACKUP_SHA256" in DOCUMENT
    assert "/opt/ticketwatch/data/incidents/$INCIDENT_ID" in DOCUMENT
    assert "failure-scene.sql.gz" in DOCUMENT
    assert "set -Eeuo pipefail" in DOCUMENT


def test_rollback_record_is_strict_and_validated_before_service_stop():
    assert "PREVIOUS_SHA NEW_SHA WEB_IMAGE_ID" in DOCUMENT
    assert "grep -c \"^$key=\" \"$RECORD\"" in DOCUMENT
    assert "test \"$(grep -Ec" in DOCUMENT
    assert 'for sha in "$PREVIOUS_SHA" "$NEW_SHA"' in DOCUMENT
    assert "'^[0-9a-f]{40}$'" in DOCUMENT
    assert 'test "$(git rev-parse HEAD)" = "$NEW_SHA"' in DOCUMENT
    rollback = DOCUMENT[DOCUMENT.index("## 回滚") :]
    assert rollback.index('test "$(git rev-parse HEAD)" = "$NEW_SHA"') < rollback.index(
        "docker compose --env-file /opt/ticketwatch/.env stop worker web"
    )


def test_incident_snapshot_uses_atomic_no_replace_publication():
    assert "REPLACE_WITH_TICKET_OR_TIMESTAMP" not in DOCUMENT
    assert "od -An -N4 -tx1 /dev/urandom" in DOCUMENT
    assert "/opt/ticketwatch/data/incidents" in DOCUMENT
    assert 'mkdir --mode=0700 -- "$INCIDENT_DIR"' in DOCUMENT
    assert "install -d -o admin -g admin -m 0700 \"$INCIDENT_DIR\"" not in DOCUMENT
    assert "ln --no-target-directory -- \"$COPY_TMP\" \"$FAILURE_SCENE_COPY\"" in DOCUMENT
    assert "ln --no-target-directory -- \"$RECORD_TMP\" \"$FAILURE_SCENE_RECORD\"" in DOCUMENT
    assert "cleanup_incident_temps" in DOCUMENT
    incident = DOCUMENT[DOCUMENT.index("INCIDENT_ID=") :]
    assert incident.index("COPY_TMP=\nRECORD_TMP=\ncleanup_incident_temps") < incident.index(
        'COPY_TMP=$(mktemp "$INCIDENT_DIR/.failure-scene.XXXXXX")'
    )
    assert incident.index("trap cleanup_incident_temps EXIT") < incident.index(
        'COPY_TMP=$(mktemp "$INCIDENT_DIR/.failure-scene.XXXXXX")'
    )


def test_weekly_mac_copy_checks_source_and_destination_hashes():
    assert 'scp "admin@47.116.69.108:$REMOTE_BACKUP"' in DOCUMENT
    assert "REMOTE_SHA=" in DOCUMENT
    assert "LOCAL_SHA=" in DOCUMENT
    assert 'test "$REMOTE_SHA" = "$LOCAL_SHA"' in DOCUMENT


def test_worker_owned_mail_attestation_rollout_is_documented():
    assert "core.0008_agent_mail_verification_state" in DOCUMENT
    assert "Worker 启动时先执行一次身份验证并写入有时效的验证证明" in DOCUMENT
    assert "Web 容器绝不运行 `agently-cli`" in DOCUMENT
    assert "验证按钮只把 Worker 请求持久化到 PostgreSQL" in DOCUMENT
    assert "邮件验证失败不会停止电影检查" in DOCUMENT
    assert "必须通过生产 Web 页面和服务路径创建最终验收任务" in DOCUMENT


def test_web_deployment_never_receives_agent_mail_credentials():
    assert "Web 不挂载 `/opt/ticketwatch/data/agently` 或 `/home/ticketwatch`" in DOCUMENT
    assert "web agently-cli" not in DOCUMENT
    _assert_document_has_no_web_agent_mail_mount(DOCUMENT)


@pytest.mark.parametrize(
    "volume_arguments",
    [
        "--volume /opt/ticketwatch/data/agently:/tmp/web-agent-mail",
        "-v /opt/ticketwatch/data/agently/.local/share/keyrings:/tmp/web-keyrings",
        "-v /tmp/web-agent-mail:/home/ticketwatch",
        "--volume=/tmp/web-keyrings:/home/ticketwatch/.local/share/keyrings",
        (
            "--mount type=bind,source=/opt/ticketwatch/data/agently/.agently-cli,"
            "target=/tmp/web-cli"
        ),
    ],
)
def test_docs_reject_web_agent_mail_child_bind_with_alternate_syntax(
    volume_arguments,
):
    mutated_document = DOCUMENT + f"""
```bash
docker compose run --rm {volume_arguments} web true
```
"""
    with pytest.raises(AssertionError):
        _assert_document_has_no_web_agent_mail_mount(mutated_document)


def test_docs_allow_similarly_prefixed_non_agent_mail_bind():
    mutated_document = DOCUMENT + """
```bash
docker compose run --rm \
  --volume=/opt/ticketwatch/data/agently-backup:/home/ticketwatch-cache \
  web true
```
"""

    _assert_document_has_no_web_agent_mail_mount(mutated_document)


def test_database_startup_waits_before_no_deps_migration():
    assert DOCUMENT.count(
        "docker compose --env-file /opt/ticketwatch/.env up -d --wait postgres redis"
    ) == 2
