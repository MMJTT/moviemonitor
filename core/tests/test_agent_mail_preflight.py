import uuid
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from core.models import AgentMailConfig
from core.services.agent_mail import (
    AgentMailConfigError,
    AgentMailTemporaryError,
)
from core.services.mail_verification import (
    _claim_pending_verification,
    request_mail_verification,
)


@pytest.mark.django_db
def test_preflight_rejects_sender_mismatch_without_cli_details(mocker):
    cli_detail = "provider stderr: oauth token=private-token"
    config = AgentMailConfig.get_solo()
    config.is_verified = True
    config.verified_at = timezone.now()
    config.verification_status = AgentMailConfig.VerificationStatus.VERIFIED
    config.verification_claim_token = uuid.uuid4()
    config.verification_claim_expires_at = timezone.now()
    config.save()
    mocker.patch(
        "core.management.commands.agent_mail_preflight.verify_agent_mail",
        side_effect=AgentMailConfigError(cli_detail),
    )
    output = StringIO()

    with pytest.raises(CommandError) as caught:
        call_command("agent_mail_preflight", stdout=output)

    assert "agent-mail-config-error" in str(caught.value)
    assert cli_detail not in str(caught.value)
    assert cli_detail not in output.getvalue()
    assert caught.value.__cause__ is None
    config.refresh_from_db()
    assert config.is_verified is False
    assert config.verified_at is None
    assert config.verification_status == AgentMailConfig.VerificationStatus.FAILED
    assert config.verification_completed_at is not None
    assert config.verification_claim_token is None
    assert config.verification_claim_expires_at is None
    assert config.last_error == "agent-mail-config-error"


@pytest.mark.django_db
def test_preflight_sanitizes_unexpected_identity_error(mocker):
    cli_detail = "provider stderr: oauth token=private-token"
    mocker.patch(
        "core.management.commands.agent_mail_preflight.verify_agent_mail",
        side_effect=RuntimeError(cli_detail),
    )
    output = StringIO()

    with pytest.raises(CommandError) as caught:
        call_command("agent_mail_preflight", stdout=output)

    assert str(caught.value) == "Agent Mail preflight failed: agent-mail-preflight-failed"
    assert cli_detail not in str(caught.value)
    assert cli_detail not in output.getvalue()
    assert caught.value.__cause__ is None


@pytest.mark.django_db
def test_preflight_identity_success_records_fresh_verification(mocker):
    now = timezone.now()
    config = AgentMailConfig.get_solo()
    AgentMailConfig.objects.filter(pk=config.pk).update(
        verification_status=AgentMailConfig.VerificationStatus.FAILED,
        verification_completed_at=now - timedelta(days=1),
        last_error="old-safe-error",
    )
    mocker.patch(
        "core.management.commands.agent_mail_preflight.verify_agent_mail",
        return_value=config.sender_email,
    )
    output = StringIO()

    call_command("agent_mail_preflight", stdout=output)

    config.refresh_from_db()
    assert config.is_verified is True
    assert config.verified_at is not None
    assert config.verification_status == AgentMailConfig.VerificationStatus.VERIFIED
    assert config.verification_completed_at is not None
    assert config.last_error == ""
    assert "Agent Mail preflight passed." in output.getvalue()


@pytest.mark.django_db
def test_preflight_send_test_marks_config_verified_only_after_queued_success(mocker):
    config = AgentMailConfig.get_solo()
    config.is_verified = True
    config.verified_at = timezone.now()
    config.last_error = "old-safe-error"
    config.save()

    def queued_test(current):
        current.refresh_from_db()
        assert current.is_verified is False
        assert current.verified_at is None
        assert current.verification_status == AgentMailConfig.VerificationStatus.FAILED
        assert current.verification_completed_at is not None
        assert current.last_error == "agent-mail-preflight-failed"
        return "queued"

    send_test = mocker.patch(
        "core.management.commands.agent_mail_preflight.test_agent_mail_config",
        side_effect=queued_test,
    )
    output = StringIO()

    call_command("agent_mail_preflight", "--send-test", stdout=output)

    config.refresh_from_db()
    assert config.is_verified is True
    assert config.verified_at is not None
    assert config.verification_status == AgentMailConfig.VerificationStatus.VERIFIED
    assert config.verification_completed_at is not None
    assert config.last_error == ""
    assert "Agent Mail preflight passed." in output.getvalue()
    send_test.assert_called_once()


@pytest.mark.django_db
def test_preflight_send_test_persists_only_mapped_safe_error_code(mocker):
    config = AgentMailConfig.get_solo()
    config.is_verified = True
    config.verified_at = timezone.now()
    config.save()
    cli_detail = "provider stderr: oauth token=private-token"
    mocker.patch(
        "core.management.commands.agent_mail_preflight.test_agent_mail_config",
        side_effect=AgentMailTemporaryError(cli_detail),
    )
    output = StringIO()

    with pytest.raises(CommandError) as caught:
        call_command("agent_mail_preflight", "--send-test", stdout=output)

    config.refresh_from_db()
    assert config.is_verified is False
    assert config.verified_at is None
    assert config.verification_status == AgentMailConfig.VerificationStatus.FAILED
    assert config.verification_completed_at is not None
    assert config.last_error == "agent-mail-network-error"
    assert cli_detail not in str(caught.value)
    assert cli_detail not in output.getvalue()


@pytest.mark.django_db
def test_preflight_send_test_sanitizes_unexpected_error_and_marks_failed(mocker):
    config = AgentMailConfig.get_solo()
    config.is_verified = True
    config.verified_at = timezone.now()
    config.save()
    cli_detail = "provider stderr: oauth token=private-token"
    mocker.patch(
        "core.management.commands.agent_mail_preflight.test_agent_mail_config",
        side_effect=RuntimeError(cli_detail),
    )
    output = StringIO()

    with pytest.raises(CommandError) as caught:
        call_command("agent_mail_preflight", "--send-test", stdout=output)

    config.refresh_from_db()
    assert config.is_verified is False
    assert config.verified_at is None
    assert config.verification_status == AgentMailConfig.VerificationStatus.FAILED
    assert config.verification_completed_at is not None
    assert config.last_error == "agent-mail-preflight-failed"
    assert str(caught.value) == "Agent Mail preflight failed: agent-mail-preflight-failed"
    assert cli_detail not in str(caught.value)
    assert cli_detail not in output.getvalue()
    assert caught.value.__cause__ is None


@pytest.mark.django_db
def test_interrupted_send_test_leaves_recoverable_failed_state(mocker):
    """Using PENDING during synchronous I/O would leave an interruption unclaimable."""
    config = AgentMailConfig.get_solo()
    config.is_verified = True
    config.verified_at = timezone.now()
    config.verification_status = AgentMailConfig.VerificationStatus.VERIFIED
    config.save()
    mocker.patch(
        "core.management.commands.agent_mail_preflight.test_agent_mail_config",
        side_effect=KeyboardInterrupt,
    )

    with pytest.raises(KeyboardInterrupt):
        call_command("agent_mail_preflight", "--send-test", stdout=StringIO())

    config.refresh_from_db()
    assert config.is_verified is False
    assert config.verified_at is None
    assert config.verification_status == AgentMailConfig.VerificationStatus.FAILED
    assert config.verification_completed_at is not None
    assert config.verification_claim_token is None
    assert config.verification_claim_expires_at is None
    assert config.last_error == "agent-mail-preflight-failed"


@pytest.mark.django_db
def test_interrupted_identity_preflight_leaves_recoverable_failed_state(mocker):
    """An identity-only interruption must revoke a previously verified attestation."""
    config = AgentMailConfig.get_solo()
    config.is_verified = True
    config.verified_at = timezone.now()
    config.verification_status = AgentMailConfig.VerificationStatus.VERIFIED
    config.save()
    mocker.patch(
        "core.management.commands.agent_mail_preflight.verify_agent_mail",
        side_effect=KeyboardInterrupt,
    )

    with pytest.raises(KeyboardInterrupt):
        call_command("agent_mail_preflight", stdout=StringIO())

    config.refresh_from_db()
    assert config.is_verified is False
    assert config.verified_at is None
    assert config.verification_status == AgentMailConfig.VerificationStatus.FAILED
    assert config.verification_completed_at is not None
    assert config.last_error == "agent-mail-preflight-failed"


@pytest.mark.django_db
def test_identity_preflight_success_preserves_request_created_during_cli(mocker):
    """An unguarded success write would erase a newer Worker-owned request."""
    config = AgentMailConfig.get_solo()
    requested_at = timezone.now() + timedelta(seconds=1)

    def request_during_identity():
        request_mail_verification(now=requested_at)
        claim = _claim_pending_verification(now=requested_at)
        assert claim is not None
        return config.sender_email

    mocker.patch(
        "core.management.commands.agent_mail_preflight.verify_agent_mail",
        side_effect=request_during_identity,
    )

    call_command("agent_mail_preflight", stdout=StringIO())

    config.refresh_from_db()
    assert config.verification_status == AgentMailConfig.VerificationStatus.PENDING
    assert config.verification_requested_at == requested_at
    assert config.verification_claim_token is not None
    assert config.verification_claim_expires_at is not None


@pytest.mark.django_db
def test_send_test_failure_preserves_request_created_during_cli(mocker):
    """An unguarded failure write would erase a newer Worker-owned claim."""
    config = AgentMailConfig.get_solo()
    requested_at = timezone.now() + timedelta(seconds=1)

    def request_then_fail(current):
        request_mail_verification(now=requested_at)
        claim = _claim_pending_verification(now=requested_at)
        assert claim is not None
        raise AgentMailTemporaryError("private endpoint")

    mocker.patch(
        "core.management.commands.agent_mail_preflight.test_agent_mail_config",
        side_effect=request_then_fail,
    )

    with pytest.raises(CommandError):
        call_command("agent_mail_preflight", "--send-test", stdout=StringIO())

    config.refresh_from_db()
    assert config.verification_status == AgentMailConfig.VerificationStatus.PENDING
    assert config.verification_requested_at == requested_at
    assert config.verification_claim_token is not None
    assert config.verification_claim_expires_at is not None
