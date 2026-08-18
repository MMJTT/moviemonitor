from pathlib import Path

DOCUMENT = (Path(__file__).resolve().parents[1] / "docs" / "server-deployment.md").read_text(
    encoding="utf-8"
)


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
    assert "--volume /opt/ticketwatch/data/agently:/home/ticketwatch" not in DOCUMENT


def test_database_startup_waits_before_no_deps_migration():
    assert DOCUMENT.count(
        "docker compose --env-file /opt/ticketwatch/.env up -d --wait postgres redis"
    ) == 2
