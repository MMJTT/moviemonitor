import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


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


def test_worker_agent_mail_home_requires_preprovisioned_bind(compose_config):
    worker_volumes = compose_config["services"]["worker"]["volumes"]
    agent_mail_home = next(
        volume for volume in worker_volumes if volume["target"] == "/home/ticketwatch"
    )

    assert agent_mail_home["type"] == "bind"
    assert agent_mail_home["source"] == "/tmp/ticketwatch-contract-agently"
    assert agent_mail_home["bind"]["create_host_path"] is False


def test_postgres_healthcheck_uses_configured_database(compose_config):
    healthcheck_command = compose_config["services"]["postgres"]["healthcheck"]["test"][1]

    assert "POSTGRES_USER" in healthcheck_command
    assert "POSTGRES_DB" in healthcheck_command
