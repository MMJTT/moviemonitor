import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from core.models import AgentMailConfig, MonitorTask, Notification
from core.services import notifications as notification_service
from core.services.agent_mail import (
    AgentMailAuthError,
    AgentMailConfigError,
    AgentMailPermanentError,
    AgentMailTemporaryError,
    AgentMailUncertainError,
)
from core.services.mail_verification import (
    _claim_pending_verification,
    request_mail_verification,
)
from core.services.notifications import deliver_notification, dispatch_due_notifications
from core.worker import run_worker_due_work

SENDER = "mijiatong@agent.qq.com"
RECIPIENT = "850634546@qq.com"


@pytest.fixture
def verified_mail(db):
    config = AgentMailConfig.get_solo()
    config.recipient_email = RECIPIENT
    config.is_verified = True
    config.verified_at = timezone.now()
    config.verification_status = AgentMailConfig.VerificationStatus.VERIFIED
    config.save()
    return config


@pytest.mark.django_db
def test_successful_agent_mail_opening_notification_completes_task(
    opening_notification, verified_mail, mocker
):
    send = mocker.patch(
        "core.services.notifications.send_agent_mail",
        return_value="queued",
        create=True,
    )
    now = timezone.now()

    deliver_notification(opening_notification.pk, now=now)

    opening_notification.refresh_from_db()
    opening_notification.task.refresh_from_db()
    assert opening_notification.status == Notification.Status.SENT
    assert opening_notification.transport_response == "queued"
    assert opening_notification.task.status == MonitorTask.Status.COMPLETED
    recipient, subject, body = send.call_args.args
    assert recipient == RECIPIENT
    assert "已在" in subject
    assert opening_notification.task.movie_name in subject
    assert opening_notification.task.cinema_name in body
    assert opening_notification.task.booking_url in body
    assert SENDER not in body


@pytest.mark.django_db
def test_temporary_agent_mail_error_schedules_retry(
    opening_notification, verified_mail, mocker
):
    mocker.patch(
        "core.services.notifications.send_agent_mail",
        side_effect=AgentMailTemporaryError("private network endpoint"),
        create=True,
    )
    now = timezone.now()

    deliver_notification(opening_notification.pk, now=now)

    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.PENDING
    assert opening_notification.retry_count == 1
    assert opening_notification.next_attempt_at == now + timedelta(minutes=1)
    assert opening_notification.last_error == "agent-mail-network-error"
    verified_mail.refresh_from_db()
    assert verified_mail.is_verified is False
    assert verified_mail.verified_at is None
    assert verified_mail.verification_status == AgentMailConfig.VerificationStatus.FAILED
    assert verified_mail.verification_completed_at == now
    assert verified_mail.verification_retry_count == 1
    assert verified_mail.verification_next_attempt_at == now + timedelta(minutes=1)
    assert verified_mail.last_error == "agent-mail-network-error"


@pytest.mark.django_db
def test_temporary_delivery_recovers_identity_and_dispatches_at_one_minute(
    opening_notification, verified_mail, mocker
):
    """Throttling a revoked proof for six hours would strand the one-minute retry."""
    started_at = timezone.now()
    send = mocker.patch(
        "core.services.notifications.send_agent_mail",
        side_effect=[
            AgentMailTemporaryError("private network endpoint"),
            "queued",
        ],
    )
    verify = mocker.patch(
        "core.services.mail_verification.verify_agent_mail",
        return_value=SENDER,
    )

    deliver_notification(opening_notification.pk, now=started_at)

    before_boundary = run_worker_due_work(now=started_at + timedelta(seconds=59))
    assert before_boundary["mail"] is False
    assert before_boundary["notifications"] == 0
    verify.assert_not_called()

    at_boundary = run_worker_due_work(now=started_at + timedelta(seconds=60))

    opening_notification.refresh_from_db()
    opening_notification.task.refresh_from_db()
    verified_mail.refresh_from_db()
    assert at_boundary["mail"] is True
    assert at_boundary["notifications"] == 1
    verify.assert_called_once_with()
    assert send.call_count == 2
    assert opening_notification.status == Notification.Status.SENT
    assert opening_notification.task.status == MonitorTask.Status.COMPLETED
    assert verified_mail.is_verified is True
    assert verified_mail.verification_retry_count == 0
    assert verified_mail.verification_next_attempt_at is None


@pytest.mark.parametrize(
    ("error", "safe_code"),
    [
        (AgentMailAuthError("private OAuth token detail"), "agent-mail-auth-required"),
        (AgentMailConfigError("private keyring path"), "agent-mail-config-error"),
    ],
)
@pytest.mark.django_db
def test_agent_mail_credential_error_parks_notification_and_revokes_attestation(
    opening_notification, verified_mail, mocker, error, safe_code
):
    now = timezone.now()
    stale_token = uuid.uuid4()
    AgentMailConfig.objects.filter(pk=verified_mail.pk).update(
        verification_claim_token=stale_token,
        verification_claim_expires_at=now + timedelta(minutes=2),
    )
    mocker.patch(
        "core.services.notifications.send_agent_mail",
        side_effect=error,
        create=True,
    )

    deliver_notification(opening_notification.pk, now=now)

    opening_notification.refresh_from_db()
    verified_mail.refresh_from_db()
    assert opening_notification.status == Notification.Status.PENDING
    assert opening_notification.next_attempt_at is None
    assert opening_notification.last_error == safe_code
    assert verified_mail.is_verified is False
    assert verified_mail.verified_at is None
    assert verified_mail.verification_status == AgentMailConfig.VerificationStatus.FAILED
    assert verified_mail.verification_completed_at == now
    assert verified_mail.verification_claim_token is None
    assert verified_mail.verification_claim_expires_at is None
    assert verified_mail.last_error == safe_code
    assert "private" not in opening_notification.last_error
    assert "private" not in verified_mail.last_error


@pytest.mark.django_db
def test_permanent_agent_mail_rejection_is_not_retried(
    opening_notification, verified_mail, mocker
):
    mocker.patch(
        "core.services.notifications.send_agent_mail",
        side_effect=AgentMailPermanentError("private recipient detail"),
        create=True,
    )

    deliver_notification(opening_notification.pk)

    opening_notification.refresh_from_db()
    opening_notification.task.refresh_from_db()
    assert opening_notification.status == Notification.Status.PERMANENT_FAILED
    assert opening_notification.next_attempt_at is None
    assert opening_notification.last_error == "agent-mail-recipient-rejected"
    assert opening_notification.task.status == MonitorTask.Status.DETECTED
    verified_mail.refresh_from_db()
    assert verified_mail.is_verified is True
    assert verified_mail.verification_status == AgentMailConfig.VerificationStatus.VERIFIED


@pytest.mark.parametrize(
    ("error", "safe_code", "notification_status", "next_attempt_delta"),
    [
        (
            AgentMailAuthError("private OAuth token"),
            "agent-mail-auth-required",
            Notification.Status.PENDING,
            None,
        ),
        (
            AgentMailTemporaryError("private network endpoint"),
            "agent-mail-network-error",
            Notification.Status.PENDING,
            timedelta(minutes=1),
        ),
        (
            AgentMailUncertainError("private provider response"),
            "agent-mail-result-unknown",
            Notification.Status.NEEDS_REVIEW,
            None,
        ),
        (
            RuntimeError("private unexpected detail"),
            "agent-mail-preflight-failed",
            Notification.Status.PENDING,
            None,
        ),
    ],
)
@pytest.mark.django_db
def test_proof_invalidating_error_preserves_request_created_during_send(
    opening_notification,
    verified_mail,
    mocker,
    error,
    safe_code,
    notification_status,
    next_attempt_delta,
):
    """Revoking proof must not erase a newer request, lease, or safe error boundary."""
    now = timezone.now()
    requested_at = now + timedelta(seconds=1)

    def request_then_fail(*args):
        request_mail_verification(now=requested_at)
        claim = _claim_pending_verification(now=requested_at)
        assert claim is not None
        raise error

    mocker.patch(
        "core.services.notifications.send_agent_mail",
        side_effect=request_then_fail,
        create=True,
    )

    deliver_notification(opening_notification.pk, now=now)

    opening_notification.refresh_from_db()
    verified_mail.refresh_from_db()
    assert opening_notification.status == notification_status
    assert opening_notification.last_error == safe_code
    assert opening_notification.next_attempt_at == (
        now + next_attempt_delta if next_attempt_delta is not None else None
    )
    assert "private" not in opening_notification.last_error
    assert verified_mail.is_verified is False
    assert verified_mail.verified_at is None
    assert verified_mail.verification_status == AgentMailConfig.VerificationStatus.PENDING
    assert verified_mail.verification_requested_at == requested_at
    assert verified_mail.verification_claim_token is not None
    assert verified_mail.verification_claim_expires_at is not None


@pytest.mark.django_db
def test_notification_recipient_drift_is_rejected_before_cli(
    opening_notification, verified_mail, mocker
):
    """Trusting mutable DB recipient data would send mail outside the fixed boundary."""
    AgentMailConfig.objects.filter(pk=verified_mail.pk).update(
        recipient_email="attacker@example.com"
    )
    verify = mocker.patch("core.services.agent_mail.verify_agent_mail")
    run = mocker.patch(
        "core.services.agent_mail._run",
        return_value={"ok": True, "queued": True},
    )

    deliver_notification(opening_notification.pk, now=timezone.now())

    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.PENDING
    assert opening_notification.last_error == "agent-mail-config-error"
    verify.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        AgentMailUncertainError("private provider response"),
        AgentMailAuthError("private OAuth token"),
        AgentMailConfigError("private keyring path"),
        AgentMailTemporaryError("private network endpoint"),
        RuntimeError("private unexpected detail"),
    ],
)
@pytest.mark.django_db
def test_proof_invalidating_transition_rolls_back_when_revocation_fails(
    opening_notification, verified_mail, mocker, error
):
    """Committing either half would leave notification and proof state contradictory."""
    now = timezone.now()
    completed_at = now - timedelta(hours=1)
    AgentMailConfig.objects.filter(pk=verified_mail.pk).update(
        verification_completed_at=completed_at,
        last_error="",
        updated_at=completed_at,
    )
    real_revoke = notification_service._revoke_mail_attestation

    def revoke_then_fail(error_code, failed_at):
        real_revoke(error_code, failed_at)
        raise RuntimeError("injected revocation persistence failure")

    mocker.patch(
        "core.services.notifications.send_agent_mail",
        side_effect=error,
    )
    mocker.patch(
        "core.services.notifications._revoke_mail_attestation",
        side_effect=revoke_then_fail,
    )

    with pytest.raises(
        RuntimeError,
        match="injected revocation persistence failure",
    ):
        deliver_notification(opening_notification.pk, now=now)

    opening_notification.refresh_from_db()
    verified_mail.refresh_from_db()
    assert opening_notification.status == Notification.Status.SENDING
    assert opening_notification.next_attempt_at is None
    assert opening_notification.last_error == ""
    assert verified_mail.is_verified is True
    assert verified_mail.verified_at is not None
    assert verified_mail.verification_status == AgentMailConfig.VerificationStatus.VERIFIED
    assert verified_mail.verification_completed_at == completed_at
    assert verified_mail.last_error == ""


@pytest.mark.django_db
def test_dispatch_requires_verified_agent_mail(opening_notification, mocker):
    send = mocker.patch(
        "core.services.notifications.send_agent_mail", return_value="queued", create=True
    )

    assert dispatch_due_notifications() == 0
    send.assert_not_called()
