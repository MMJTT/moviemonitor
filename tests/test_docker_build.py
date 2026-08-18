import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run_docker(*arguments, check=True):
    return subprocess.run(
        ["docker", *arguments],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def built_images():
    if shutil.which("docker") is None:
        pytest.skip("requires Docker")
    if _run_docker("info", check=False).returncode != 0:
        pytest.skip("requires a running Docker daemon")

    run_id = uuid.uuid4().hex
    images = {
        target: f"ticketwatch-contract-{target}:{run_id}" for target in ("web", "worker")
    }
    try:
        for target, image in images.items():
            _run_docker(
                "build",
                "--target",
                target,
                "--label",
                f"com.ticketwatch.contract-run={run_id}",
                "--tag",
                image,
                ".",
            )
        yield images
    finally:
        for image in images.values():
            _run_docker("image", "rm", "--force", image, check=False)


def test_built_web_image_does_not_contain_agent_mail_cli(built_images):
    result = _run_docker(
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        built_images["web"],
        "-c",
        "command -v agently-cli",
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""


def test_built_worker_runs_as_10001_with_private_runtime_paths(built_images):
    result = _run_docker(
        "run",
        "--rm",
        "--env",
        "AGENTLY_KEYRING_PASSWORD=contract-only-password",
        built_images["worker"],
        "sh",
        "-ec",
        """
        test "$(id -u)" = 10001
        test "$(id -g)" = 10001
        test "$(stat -c %a /tmp/ticketwatch-runtime)" = 700
        test "$(stat -c %a /home/ticketwatch/.local/share/keyrings)" = 700
        test "$(stat -c %a /home/ticketwatch/.agently-cli)" = 700
        command -v agently-cli >/dev/null
        """,
        check=False,
    )

    assert result.returncode == 0, result.stderr
