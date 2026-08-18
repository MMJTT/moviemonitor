import json
import subprocess
from types import SimpleNamespace

import pytest
from django.test import override_settings

from core.services.agent_mail import (
    AgentMailAuthError,
    AgentMailConfigError,
    AgentMailPermanentError,
    AgentMailTemporaryError,
    AgentMailUncertainError,
    send_agent_mail,
    verify_agent_mail,
)
from core.services.agent_mail import (
    test_agent_mail_config as run_agent_mail_config_test,
)

SENDER = "mijiatong@agent.qq.com"
RECIPIENT = "850634546@qq.com"


def completed(stdout, returncode=0):
    return subprocess.CompletedProcess(
        args=["agently-cli"], returncode=returncode, stdout=stdout, stderr=""
    )


def identity_payload(sender=SENDER):
    return json.dumps(
        {
            "ok": True,
            "data": {
                "aliases": [
                    {"email": sender, "is_primary": True, "name": "mijiatong"}
                ]
            },
        }
    )


def test_verify_agent_mail_keeps_codex_as_the_local_default_workspace(mocker):
    mocker.patch("core.services.agent_mail.shutil.which", return_value="agently-cli")
    run = mocker.patch(
        "core.services.agent_mail.subprocess.run",
        return_value=completed(identity_payload()),
    )

    assert verify_agent_mail() == SENDER

    assert run.call_args.kwargs["env"]["AGENTLY_WORKSPACE"] == "codex"


@override_settings(AGENTLY_WORKSPACE="ticketwatch-server")
def test_verify_agent_mail_uses_the_configured_workspace_and_required_primary_alias(mocker):
    mocker.patch("core.services.agent_mail.shutil.which", return_value="/usr/local/bin/agently-cli")
    run = mocker.patch(
        "core.services.agent_mail.subprocess.run",
        return_value=completed(identity_payload()),
    )

    assert verify_agent_mail() == SENDER
    assert run.call_args.args[0] == ["/usr/local/bin/agently-cli", "+me"]
    assert run.call_args.kwargs["shell"] is False
    assert run.call_args.kwargs["env"]["AGENTLY_WORKSPACE"] == "ticketwatch-server"


def test_verify_agent_mail_rejects_a_different_primary_alias(mocker):
    mocker.patch("core.services.agent_mail.shutil.which", return_value="agently-cli")
    mocker.patch(
        "core.services.agent_mail.subprocess.run",
        return_value=completed(identity_payload("other@agent.qq.com")),
    )

    with pytest.raises(AgentMailConfigError, match="agent-mail-sender-mismatch"):
        verify_agent_mail()


def test_send_agent_mail_uses_confirmed_argument_list_without_a_shell(mocker):
    mocker.patch("core.services.agent_mail.shutil.which", return_value="agently-cli")
    run = mocker.patch(
        "core.services.agent_mail.subprocess.run",
        side_effect=[
            completed(identity_payload()),
            completed(json.dumps({"ok": True, "queued": True})),
        ],
    )
    subject = "开票提醒 $(touch unsafe)"
    body = "影院；rm -rf unsafe"

    assert send_agent_mail(RECIPIENT, subject, body) == "queued"

    assert run.call_args_list[1].args[0] == [
        "agently-cli",
        "message",
        "+send",
        "--to",
        RECIPIENT,
        "--subject",
        subject,
        "--body",
        body,
        "--body-format",
        "plain",
        "--confirmed",
    ]
    assert run.call_args_list[1].kwargs["shell"] is False


def test_send_agent_mail_rejects_recipient_drift_before_identity_cli(mocker):
    """Checking the recipient after identity I/O could still reach an unsafe send path."""
    verify = mocker.patch("core.services.agent_mail.verify_agent_mail")
    run = mocker.patch("core.services.agent_mail._run")

    with pytest.raises(AgentMailConfigError, match="agent-mail-recipient-mismatch"):
        send_agent_mail("attacker@example.com", "测试", "正文")

    verify.assert_not_called()
    run.assert_not_called()


def test_agent_mail_config_rejects_recipient_drift_before_transport(mocker):
    """A modified singleton recipient must not be passed into the transport."""
    send = mocker.patch("core.services.agent_mail.send_agent_mail")
    config = SimpleNamespace(
        sender_email=SENDER,
        recipient_email="attacker@example.com",
    )

    with pytest.raises(AgentMailConfigError, match="agent-mail-recipient-mismatch"):
        run_agent_mail_config_test(config)

    send.assert_not_called()


@pytest.mark.parametrize(
    ("exit_code", "error_type", "summary"),
    [
        (1, AgentMailConfigError, "agent-mail-cli-error"),
        (3, AgentMailAuthError, "agent-mail-auth-required"),
        (4, AgentMailTemporaryError, "agent-mail-network-error"),
        (6, AgentMailPermanentError, "agent-mail-recipient-rejected"),
        (7, AgentMailTemporaryError, "agent-mail-rate-limited"),
    ],
)
def test_send_agent_mail_maps_exit_codes_without_leaking_cli_output(
    mocker, exit_code, error_type, summary
):
    mocker.patch("core.services.agent_mail.shutil.which", return_value="agently-cli")
    mocker.patch(
        "core.services.agent_mail.subprocess.run",
        side_effect=[
            completed(identity_payload()),
            completed(
                '{"ok":false,"error":{"message":"private provider detail"}}',
                returncode=exit_code,
            ),
        ],
    )

    with pytest.raises(error_type, match=summary) as caught:
        send_agent_mail(RECIPIENT, "测试", "正文")

    assert "private provider detail" not in str(caught.value)


def test_missing_agent_mail_cli_is_a_configuration_error(mocker):
    mocker.patch("core.services.agent_mail.shutil.which", return_value=None)

    with pytest.raises(AgentMailConfigError, match="agent-mail-cli-unavailable"):
        verify_agent_mail()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"ok": "yes", "data": {}},
        {"ok": True, "data": None},
        {"ok": True, "data": {"aliases": None}},
        {"ok": True, "data": {"aliases": [None]}},
    ],
)
def test_verify_agent_mail_rejects_malformed_success_payloads(mocker, payload):
    mocker.patch("core.services.agent_mail.shutil.which", return_value="agently-cli")
    mocker.patch(
        "core.services.agent_mail.subprocess.run",
        return_value=completed(json.dumps(payload)),
    )

    with pytest.raises(AgentMailConfigError, match="agent-mail-invalid-response"):
        verify_agent_mail()


def test_cli_timeout_is_uncertain_not_retryable(mocker):
    """Treating an ambiguous CLI timeout as retryable could duplicate an email."""
    mocker.patch("core.services.agent_mail.shutil.which", return_value="agently-cli")
    mocker.patch(
        "core.services.agent_mail.subprocess.run",
        side_effect=subprocess.TimeoutExpired("agently-cli", 30),
    )

    with pytest.raises(AgentMailUncertainError, match="agent-mail-result-unknown"):
        send_agent_mail(RECIPIENT, "测试", "正文")
