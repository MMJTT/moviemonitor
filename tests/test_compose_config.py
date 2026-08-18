import copy
import json
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENT_MAIL_HOST_ROOT = "/tmp/ticketwatch-contract-agently"
AGENT_MAIL_CONTAINER_ROOT = "/home/ticketwatch"
WEB_REQUIRED_ENVIRONMENT = {
    "TICKETWATCH_ENV",
    "DJANGO_SECRET_KEY",
    "DJANGO_DEBUG",
    "DJANGO_ALLOWED_HOSTS",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "REDIS_URL",
    "WORKER_SCAN_SECONDS",
    "WORKER_LEASE_SECONDS",
    "AGENT_MAIL_REVERIFY_SECONDS",
    "AGENT_MAIL_ATTESTATION_TTL_SECONDS",
    "AGENT_MAIL_CLAIM_SECONDS",
}


def _is_equal_or_descendant(candidate, root):
    if not isinstance(candidate, str):
        return False
    candidate_path = PurePosixPath(candidate)
    root_path = PurePosixPath(root)
    return candidate_path == root_path or root_path in candidate_path.parents


def _assert_web_has_no_agent_mail_mounts(web_volumes):
    for volume in web_volumes:
        assert not _is_equal_or_descendant(
            volume.get("source"), AGENT_MAIL_HOST_ROOT
        )
        assert not _is_equal_or_descendant(
            volume.get("target"), AGENT_MAIL_CONTAINER_ROOT
        )


def _assert_web_has_no_agent_mail_environment(environment):
    assert [name for name in environment if name.startswith("AGENTLY_")] == []


@pytest.fixture(scope="module")
def compose_config():
    if shutil.which("docker") is None:
        pytest.skip("requires Docker Compose CLI")

    env = os.environ.copy()
    env.update(
        {
            "TICKETWATCH_ENV_FILE": ".env.example",
            "POSTGRES_DATA_DIR": "/tmp/ticketwatch-contract-postgres",
            "REDIS_DATA_DIR": "/tmp/ticketwatch-contract-redis",
            "AGENTLY_DATA_DIR": "/tmp/ticketwatch-contract-agently",
        }
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env.example",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_compose_has_exactly_four_production_services(compose_config):
    assert set(compose_config["services"]) == {"web", "worker", "postgres", "redis"}


def test_web_has_no_agent_mail_home_or_credentials_mount(compose_config):
    web_volumes = compose_config["services"]["web"].get("volumes", [])

    _assert_web_has_no_agent_mail_mounts(web_volumes)


def test_web_receives_only_the_explicit_non_agent_mail_environment(compose_config):
    web_environment = compose_config["services"]["web"]["environment"]

    _assert_web_has_no_agent_mail_environment(web_environment)
    assert set(web_environment) == WEB_REQUIRED_ENVIRONMENT


def test_web_agent_mail_environment_mutation_is_rejected(compose_config):
    mutated_environment = copy.deepcopy(
        compose_config["services"]["web"]["environment"]
    )
    mutated_environment["AGENTLY_KEYRING_PASSWORD"] = "contract-secret"

    with pytest.raises(AssertionError):
        _assert_web_has_no_agent_mail_environment(mutated_environment)


def test_worker_retains_agent_mail_runtime_environment(compose_config):
    worker_environment = compose_config["services"]["worker"]["environment"]

    assert set(worker_environment) == WEB_REQUIRED_ENVIRONMENT | {
        "AGENTLY_KEYRING_PASSWORD",
        "AGENTLY_WORKSPACE",
    }


@pytest.mark.parametrize(
    "agent_mail_bind",
    [
        {
            "type": "bind",
            "source": "/tmp/ticketwatch-contract-agently",
            "target": "/tmp/web-agent-mail",
            "bind": {"create_host_path": False},
        },
        {
            "type": "bind",
            "source": "/tmp/ticketwatch-contract-agently/.local/share/keyrings",
            "target": "/tmp/web-keyrings",
            "bind": {"create_host_path": False},
        },
        {
            "type": "bind",
            "source": "/tmp/web-agent-mail",
            "target": "/home/ticketwatch",
            "bind": {"create_host_path": False},
        },
        {
            "type": "bind",
            "source": "/tmp/web-keyrings",
            "target": "/home/ticketwatch/.local/share/keyrings",
            "bind": {"create_host_path": False},
        },
    ],
)
def test_web_agent_mail_bind_is_rejected(compose_config, agent_mail_bind):
    mutated_config = copy.deepcopy(compose_config)
    mutated_config["services"]["web"]["volumes"] = [agent_mail_bind]

    with pytest.raises(AssertionError):
        _assert_web_has_no_agent_mail_mounts(
            mutated_config["services"]["web"]["volumes"]
        )


def test_agent_mail_path_containment_does_not_match_same_prefix_siblings():
    web_volumes = [
        {
            "type": "bind",
            "source": "/tmp/ticketwatch-contract-agently-backup",
            "target": "/home/ticketwatch-cache",
            "bind": {"create_host_path": False},
        }
    ]

    _assert_web_has_no_agent_mail_mounts(web_volumes)


def test_worker_agent_mail_home_requires_preprovisioned_bind(compose_config):
    worker_volumes = compose_config["services"]["worker"]["volumes"]
    agent_mail_homes = [
        volume for volume in worker_volumes if volume["target"] == "/home/ticketwatch"
    ]

    assert len(agent_mail_homes) == 1
    agent_mail_home = agent_mail_homes[0]
    assert agent_mail_home["type"] == "bind"
    assert agent_mail_home["source"] == "/tmp/ticketwatch-contract-agently"
    assert agent_mail_home["bind"]["create_host_path"] is False


def test_postgres_healthcheck_uses_configured_database(compose_config):
    healthcheck_command = compose_config["services"]["postgres"]["healthcheck"]["test"][1]

    assert "POSTGRES_USER" in healthcheck_command
    assert "POSTGRES_DB" in healthcheck_command
