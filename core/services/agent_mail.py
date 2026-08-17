import json
import os
import shutil
import subprocess

from django.conf import settings

AGENT_MAIL_SENDER = "mijiatong@agent.qq.com"


class AgentMailError(RuntimeError):
    """Base class for safe Agent Mail transport failures."""


class AgentMailTemporaryError(AgentMailError):
    """A retryable Agent Mail transport failure."""


class AgentMailUncertainError(AgentMailError):
    """The provider may have accepted the message before transport failed."""


class AgentMailAuthError(AgentMailError):
    """The local Agent Mail OAuth authorization is unavailable."""


class AgentMailPermanentError(AgentMailError):
    """The provider permanently rejected this message."""


class AgentMailConfigError(AgentMailError):
    """The local Agent Mail installation or identity is invalid."""


def _cli_path():
    path = shutil.which("agently-cli")
    if path is None:
        raise AgentMailConfigError("agent-mail-cli-unavailable")
    return path


def _run(arguments):
    environment = os.environ.copy()
    environment["AGENTLY_WORKSPACE"] = settings.AGENTLY_WORKSPACE
    try:
        result = subprocess.run(
            [_cli_path(), *arguments],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            shell=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise AgentMailUncertainError("agent-mail-result-unknown") from exc
    except OSError as exc:
        raise AgentMailTemporaryError("agent-mail-network-error") from exc

    if result.returncode:
        error_map = {
            1: (AgentMailConfigError, "agent-mail-cli-error"),
            2: (AgentMailConfigError, "agent-mail-invalid-request"),
            3: (AgentMailAuthError, "agent-mail-auth-required"),
            4: (AgentMailTemporaryError, "agent-mail-network-error"),
            6: (AgentMailPermanentError, "agent-mail-recipient-rejected"),
            7: (AgentMailTemporaryError, "agent-mail-rate-limited"),
            8: (AgentMailConfigError, "agent-mail-confirmation-required"),
        }
        error_type, summary = error_map.get(
            result.returncode, (AgentMailConfigError, "agent-mail-cli-error")
        )
        raise error_type(summary)

    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AgentMailConfigError("agent-mail-invalid-response") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise AgentMailConfigError("agent-mail-invalid-response")
    return payload


def verify_agent_mail():
    payload = _run(["+me"])
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AgentMailConfigError("agent-mail-invalid-response")
    aliases = data.get("aliases")
    if not isinstance(aliases, list) or not all(
        isinstance(item, dict) for item in aliases
    ):
        raise AgentMailConfigError("agent-mail-invalid-response")
    primary = next(
        (item.get("email") for item in aliases if item.get("is_primary") is True),
        None,
    )
    if primary != AGENT_MAIL_SENDER:
        raise AgentMailConfigError("agent-mail-sender-mismatch")
    return primary


def send_agent_mail(recipient, subject, body):
    verify_agent_mail()
    payload = _run(
        [
            "message",
            "+send",
            "--to",
            recipient,
            "--subject",
            subject,
            "--body",
            body,
            "--body-format",
            "plain",
            "--confirmed",
        ]
    )
    data = payload.get("data")
    queued = payload.get("queued") is True or (
        isinstance(data, dict) and data.get("queued") is True
    )
    if not queued:
        raise AgentMailConfigError("agent-mail-message-not-queued")
    return "queued"


def test_agent_mail_config(config):
    if config.sender_email != AGENT_MAIL_SENDER:
        raise AgentMailConfigError("agent-mail-sender-mismatch")
    return send_agent_mail(
        config.recipient_email,
        "TicketWatch Agent Mail 测试邮件",
        "这是一封 TicketWatch Agent Mail 测试邮件。",
    )
