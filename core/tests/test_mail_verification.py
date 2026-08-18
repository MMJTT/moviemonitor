import threading
import uuid
from datetime import timedelta

import pytest
from django.db import close_old_connections, connection, connections, transaction
from django.utils import timezone

from core.models import AgentMailConfig
from core.services.agent_mail import (
    AgentMailAuthError,
    AgentMailConfigError,
    AgentMailTemporaryError,
)
from core.services.mail_verification import (
    MailVerificationClaim,
    _claim_pending_verification,
    _complete_claim,
    process_mail_verification,
    request_mail_verification,
)


@pytest.mark.django_db(transaction=True)
def test_request_persists_pending_state_and_wakes_only_after_commit(mocker):
    """Moving the wake before commit could let the Worker miss the request."""
    now = timezone.now()
    stale_token = uuid.uuid4()
    config = AgentMailConfig.get_solo()
    AgentMailConfig.objects.filter(pk=config.pk).update(
        verification_status=AgentMailConfig.VerificationStatus.VERIFIED,
        verification_claim_token=stale_token,
        verification_claim_expires_at=now + timedelta(minutes=1),
    )
    wake = mocker.patch("core.services.mail_verification.notify_worker", return_value=True)

    with transaction.atomic():
        request_mail_verification(now=now)

        config.refresh_from_db()
        assert config.verification_status == AgentMailConfig.VerificationStatus.PENDING
        assert config.verification_requested_at == now
        assert config.verification_claim_token is None
        assert config.verification_claim_expires_at is None
        wake.assert_not_called()

    wake.assert_called_once_with()


@pytest.mark.django_db(transaction=True)
def test_redis_wake_failure_leaves_postgres_request_pending(mocker):
    """Treating Redis as authoritative would discard a durable request on failure."""
    now = timezone.now()
    wake = mocker.patch("core.services.mail_verification.notify_worker", return_value=False)

    request_mail_verification(now=now)

    config = AgentMailConfig.get_solo()
    assert config.verification_status == AgentMailConfig.VerificationStatus.PENDING
    assert config.verification_requested_at == now
    wake.assert_called_once_with()


@pytest.mark.django_db
def test_active_verification_claim_cannot_be_claimed_twice(settings):
    """Ignoring a live claim would send duplicate test messages."""
    settings.AGENT_MAIL_CLAIM_SECONDS = 120
    now = timezone.now()
    request_mail_verification(now=now)

    first = _claim_pending_verification(now=now)
    second = _claim_pending_verification(now=now + timedelta(seconds=119))

    assert first is not None
    assert first.requested_at == now
    assert second is None
    config = AgentMailConfig.get_solo()
    assert config.verification_claim_token == first.token
    assert config.verification_claim_expires_at == now + timedelta(seconds=120)


@pytest.mark.django_db
def test_claim_is_reclaimed_at_its_expiry_boundary(settings):
    """Treating an expired claim as live would strand a pending request."""
    settings.AGENT_MAIL_CLAIM_SECONDS = 120
    now = timezone.now()
    request_mail_verification(now=now)
    stale = _claim_pending_verification(now=now)

    replacement = _claim_pending_verification(now=now + timedelta(seconds=120))

    assert stale is not None
    assert replacement is not None
    assert replacement.token != stale.token
    assert replacement.requested_at == now


@pytest.mark.django_db
def test_successful_claim_completion_clears_lease_and_records_completion(settings):
    """Leaving a completed lease behind would make request state internally inconsistent."""
    settings.AGENT_MAIL_CLAIM_SECONDS = 120
    requested_at = timezone.now()
    completed_at = requested_at + timedelta(seconds=3)
    request_mail_verification(now=requested_at)
    claim = _claim_pending_verification(now=requested_at)

    assert claim is not None
    assert _complete_claim(
        claim,
        now=completed_at,
        verified=True,
        error_code="",
    )

    config = AgentMailConfig.get_solo()
    assert config.is_verified is True
    assert config.verified_at == completed_at
    assert config.verification_status == AgentMailConfig.VerificationStatus.VERIFIED
    assert config.verification_completed_at == completed_at
    assert config.verification_claim_token is None
    assert config.verification_claim_expires_at is None
    assert config.last_error == ""


@pytest.mark.django_db
def test_old_claim_token_cannot_complete_a_newer_request(settings):
    """Dropping the token guard would let stale work overwrite a repeated request."""
    settings.AGENT_MAIL_CLAIM_SECONDS = 120
    first_requested_at = timezone.now()
    second_requested_at = first_requested_at + timedelta(seconds=1)
    request_mail_verification(now=first_requested_at)
    stale = _claim_pending_verification(now=first_requested_at)
    request_mail_verification(now=second_requested_at)
    current = _claim_pending_verification(now=second_requested_at)

    assert stale is not None
    assert current is not None
    assert not _complete_claim(
        stale,
        now=second_requested_at + timedelta(seconds=1),
        verified=True,
        error_code="",
    )

    config = AgentMailConfig.get_solo()
    assert config.verification_status == AgentMailConfig.VerificationStatus.PENDING
    assert config.verification_requested_at == second_requested_at
    assert config.verification_claim_token == current.token
    assert config.verification_completed_at is None


@pytest.mark.django_db
def test_older_request_timestamp_cannot_complete_newer_request_with_reused_token(settings):
    """Dropping the request-time guard would make the token-only update unsafe."""
    settings.AGENT_MAIL_CLAIM_SECONDS = 120
    first_requested_at = timezone.now()
    second_requested_at = first_requested_at + timedelta(seconds=1)
    request_mail_verification(now=second_requested_at)
    current = _claim_pending_verification(now=second_requested_at)
    assert current is not None
    stale = MailVerificationClaim(token=current.token, requested_at=first_requested_at)

    assert not _complete_claim(
        stale,
        now=second_requested_at + timedelta(seconds=1),
        verified=False,
        error_code="agent-mail-network-error",
    )

    config = AgentMailConfig.get_solo()
    assert config.verification_status == AgentMailConfig.VerificationStatus.PENDING
    assert config.verification_claim_token == current.token
    assert config.last_error == ""


@pytest.mark.postgres
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL")
@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_claimers_receive_one_request(settings):
    """Removing row locking would allow two Workers to own one verification request."""
    settings.AGENT_MAIL_CLAIM_SECONDS = 120
    settings.REDIS_URL = ""
    now = timezone.now()
    request_mail_verification(now=now)
    barrier = threading.Barrier(2)
    claims = []
    errors = []

    def claim():
        close_old_connections()
        try:
            barrier.wait(timeout=2)
            claims.append(_claim_pending_verification(now=now))
        except BaseException as error:
            errors.append(error)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(thread.is_alive() is False for thread in threads)
    assert errors == []
    assert len([claim for claim in claims if claim is not None]) == 1


@pytest.mark.django_db
def test_pending_request_sends_test_mail_and_completes_verified(mocker):
    """Using identity-only verification for a manual request would skip its test message."""
    requested_at = timezone.now()
    completed_at = requested_at + timedelta(seconds=2)
    request_mail_verification(now=requested_at)
    send_test = mocker.patch(
        "core.services.mail_verification.test_agent_mail_config",
        return_value="queued",
    )

    assert process_mail_verification(now=completed_at) is True

    config = AgentMailConfig.get_solo()
    assert config.is_verified is True
    assert config.verified_at == completed_at
    assert config.verification_status == AgentMailConfig.VerificationStatus.VERIFIED
    assert config.verification_requested_at == requested_at
    assert config.verification_completed_at == completed_at
    assert config.verification_claim_token is None
    assert config.verification_claim_expires_at is None
    assert config.last_error == ""
    called_config = send_test.call_args.args[0]
    assert called_config.pk == config.pk
    assert called_config.recipient_email == "850634546@qq.com"


@pytest.mark.parametrize(
    ("error", "safe_code"),
    [
        (AgentMailConfigError("private identity detail"), "agent-mail-config-error"),
        (AgentMailAuthError("private OAuth token"), "agent-mail-auth-required"),
        (AgentMailTemporaryError("private endpoint"), "agent-mail-network-error"),
        (RuntimeError("unexpected private detail"), "agent-mail-preflight-failed"),
    ],
)
@pytest.mark.django_db
def test_pending_request_failure_records_only_safe_failed_state(
    mocker, error, safe_code
):
    """Persisting raw transport details would leak credentials and preserve stale proof."""
    requested_at = timezone.now()
    completed_at = requested_at + timedelta(seconds=2)
    config = AgentMailConfig.get_solo()
    AgentMailConfig.objects.filter(pk=config.pk).update(
        is_verified=True,
        verified_at=requested_at - timedelta(hours=1),
        verification_status=AgentMailConfig.VerificationStatus.VERIFIED,
    )
    request_mail_verification(now=requested_at)
    mocker.patch(
        "core.services.mail_verification.test_agent_mail_config",
        side_effect=error,
    )

    assert process_mail_verification(now=completed_at) is True

    config.refresh_from_db()
    assert config.is_verified is False
    assert config.verified_at is None
    assert config.verification_status == AgentMailConfig.VerificationStatus.FAILED
    assert config.verification_completed_at == completed_at
    assert config.verification_claim_token is None
    assert config.verification_claim_expires_at is None
    assert config.last_error == safe_code
    assert "private" not in config.last_error


@pytest.mark.django_db
def test_manual_completion_cannot_overwrite_request_created_during_test_mail(mocker):
    """Completing without request guards would lose a repeat click made during I/O."""
    first_requested_at = timezone.now()
    second_requested_at = first_requested_at + timedelta(seconds=1)
    completed_at = first_requested_at + timedelta(seconds=2)
    request_mail_verification(now=first_requested_at)

    def repeat_request(config):
        request_mail_verification(now=second_requested_at)
        return "queued"

    mocker.patch(
        "core.services.mail_verification.test_agent_mail_config",
        side_effect=repeat_request,
    )

    assert process_mail_verification(now=completed_at) is True

    config = AgentMailConfig.get_solo()
    assert config.verification_status == AgentMailConfig.VerificationStatus.PENDING
    assert config.verification_requested_at == second_requested_at
    assert config.verification_completed_at is None


@pytest.mark.django_db
def test_forced_verification_checks_identity_even_when_attestation_is_fresh(mocker):
    """Skipping a fresh forced check would omit the required Worker startup verification."""
    now = timezone.now()
    config = AgentMailConfig.get_solo()
    AgentMailConfig.objects.filter(pk=config.pk).update(
        is_verified=True,
        verified_at=now,
        verification_status=AgentMailConfig.VerificationStatus.VERIFIED,
        verification_completed_at=now,
        updated_at=now,
    )
    verify = mocker.patch(
        "core.services.mail_verification.verify_agent_mail",
        return_value=config.sender_email,
    )

    assert process_mail_verification(now=now, force_identity=True) is True

    verify.assert_called_once_with()
    config.refresh_from_db()
    assert config.verification_status == AgentMailConfig.VerificationStatus.VERIFIED
    assert config.verification_completed_at == now


@pytest.mark.django_db
def test_periodic_verification_refreshes_at_exactly_six_hours(settings, mocker):
    """Using a strict age comparison would miss the configured boundary."""
    settings.AGENT_MAIL_REVERIFY_SECONDS = 21600
    now = timezone.now()
    previous_completion = now - timedelta(seconds=21600)
    config = AgentMailConfig.get_solo()
    AgentMailConfig.objects.filter(pk=config.pk).update(
        is_verified=True,
        verified_at=previous_completion,
        verification_status=AgentMailConfig.VerificationStatus.VERIFIED,
        verification_completed_at=previous_completion,
        updated_at=previous_completion,
    )
    verify = mocker.patch(
        "core.services.mail_verification.verify_agent_mail",
        return_value=config.sender_email,
    )

    assert process_mail_verification(now=now) is True

    verify.assert_called_once_with()
    config.refresh_from_db()
    assert config.is_verified is True
    assert config.verified_at == now
    assert config.verification_status == AgentMailConfig.VerificationStatus.VERIFIED
    assert config.verification_completed_at == now
    assert config.last_error == ""


@pytest.mark.django_db
def test_periodic_verification_does_not_run_before_six_hours(settings, mocker):
    """Ignoring the interval would call Agent Mail on every Worker scan."""
    settings.AGENT_MAIL_REVERIFY_SECONDS = 21600
    now = timezone.now()
    config = AgentMailConfig.get_solo()
    AgentMailConfig.objects.filter(pk=config.pk).update(
        verification_status=AgentMailConfig.VerificationStatus.VERIFIED,
        verification_completed_at=now - timedelta(seconds=21599),
        updated_at=now - timedelta(seconds=21599),
    )
    verify = mocker.patch("core.services.mail_verification.verify_agent_mail")

    assert process_mail_verification(now=now) is False

    verify.assert_not_called()


@pytest.mark.django_db
def test_recent_failed_verification_is_throttled_by_completion_time(settings, mocker):
    """Keying retries off success time would hammer Agent Mail after a failure."""
    settings.AGENT_MAIL_REVERIFY_SECONDS = 21600
    now = timezone.now()
    config = AgentMailConfig.get_solo()
    AgentMailConfig.objects.filter(pk=config.pk).update(
        is_verified=False,
        verified_at=None,
        verification_status=AgentMailConfig.VerificationStatus.FAILED,
        verification_completed_at=now - timedelta(minutes=5),
        last_error="agent-mail-network-error",
        updated_at=now - timedelta(minutes=5),
    )
    verify = mocker.patch("core.services.mail_verification.verify_agent_mail")

    assert process_mail_verification(now=now) is False

    verify.assert_not_called()


@pytest.mark.django_db
def test_forced_identity_error_is_handled_and_persisted_safely(mocker):
    """Letting an Agent Mail failure escape would stop Worker startup and task checks."""
    now = timezone.now()
    config = AgentMailConfig.get_solo()
    AgentMailConfig.objects.filter(pk=config.pk).update(
        is_verified=True,
        verified_at=now - timedelta(hours=1),
        verification_status=AgentMailConfig.VerificationStatus.VERIFIED,
        verification_completed_at=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
    )
    mocker.patch(
        "core.services.mail_verification.verify_agent_mail",
        side_effect=RuntimeError("private provider detail"),
    )

    assert process_mail_verification(now=now, force_identity=True) is True

    config.refresh_from_db()
    assert config.is_verified is False
    assert config.verified_at is None
    assert config.verification_status == AgentMailConfig.VerificationStatus.FAILED
    assert config.verification_completed_at == now
    assert config.last_error == "agent-mail-preflight-failed"
    assert "private" not in config.last_error


@pytest.mark.django_db
def test_periodic_completion_cannot_overwrite_concurrent_manual_request(settings, mocker):
    """Dropping the updated-at/status guard would erase a request made during CLI I/O."""
    settings.AGENT_MAIL_REVERIFY_SECONDS = 21600
    now = timezone.now()
    requested_at = now + timedelta(seconds=1)
    config = AgentMailConfig.get_solo()
    AgentMailConfig.objects.filter(pk=config.pk).update(
        is_verified=True,
        verified_at=now - timedelta(hours=6),
        verification_status=AgentMailConfig.VerificationStatus.VERIFIED,
        verification_completed_at=now - timedelta(hours=6),
        updated_at=now - timedelta(hours=6),
    )

    def request_during_identity_check():
        request_mail_verification(now=requested_at)
        return config.sender_email

    mocker.patch(
        "core.services.mail_verification.verify_agent_mail",
        side_effect=request_during_identity_check,
    )

    assert process_mail_verification(now=now) is True

    config.refresh_from_db()
    assert config.verification_status == AgentMailConfig.VerificationStatus.PENDING
    assert config.verification_requested_at == requested_at
    assert config.verification_completed_at == now - timedelta(hours=6)


@pytest.mark.django_db
def test_live_pending_claim_does_not_fall_through_to_identity_verification(
    settings, mocker
):
    """Falling through after a lost claim would busy-loop and duplicate Agent Mail I/O."""
    settings.AGENT_MAIL_CLAIM_SECONDS = 120
    now = timezone.now()
    request_mail_verification(now=now)
    owner = _claim_pending_verification(now=now)
    send_test = mocker.patch("core.services.mail_verification.test_agent_mail_config")
    verify = mocker.patch("core.services.mail_verification.verify_agent_mail")

    assert owner is not None
    assert process_mail_verification(now=now + timedelta(seconds=1)) is False

    send_test.assert_not_called()
    verify.assert_not_called()
    config = AgentMailConfig.get_solo()
    assert config.verification_status == AgentMailConfig.VerificationStatus.PENDING
    assert config.verification_claim_token == owner.token


@pytest.mark.django_db
def test_pending_without_request_is_repaired_once_without_cli_loop(mocker):
    """Leaving malformed PENDING state intact would keep every Worker scan busy."""
    now = timezone.now()
    config = AgentMailConfig.get_solo()
    AgentMailConfig.objects.filter(pk=config.pk).update(
        is_verified=True,
        verified_at=now - timedelta(hours=1),
        verification_status=AgentMailConfig.VerificationStatus.PENDING,
        verification_requested_at=None,
        verification_completed_at=None,
        verification_claim_token=uuid.uuid4(),
        verification_claim_expires_at=now + timedelta(minutes=2),
        last_error="old-error",
        updated_at=now - timedelta(minutes=1),
    )
    send_test = mocker.patch("core.services.mail_verification.test_agent_mail_config")
    verify = mocker.patch("core.services.mail_verification.verify_agent_mail")

    assert process_mail_verification(now=now) is False

    config.refresh_from_db()
    assert config.is_verified is False
    assert config.verified_at is None
    assert config.verification_status == AgentMailConfig.VerificationStatus.FAILED
    assert config.verification_completed_at == now
    assert config.verification_claim_token is None
    assert config.verification_claim_expires_at is None
    assert config.last_error == "agent-mail-config-error"
    assert process_mail_verification(now=now + timedelta(seconds=1)) is False
    send_test.assert_not_called()
    verify.assert_not_called()


@pytest.mark.parametrize("future_offset", [timedelta(seconds=1), timedelta(days=365)])
@pytest.mark.django_db
def test_future_completion_timestamp_is_due_for_identity_check(
    settings, mocker, future_offset
):
    """Throttling negative ages would let clock skew suppress verification indefinitely."""
    settings.AGENT_MAIL_REVERIFY_SECONDS = 21600
    now = timezone.now()
    config = AgentMailConfig.get_solo()
    AgentMailConfig.objects.filter(pk=config.pk).update(
        is_verified=True,
        verified_at=now - timedelta(hours=1),
        verification_status=AgentMailConfig.VerificationStatus.VERIFIED,
        verification_completed_at=now + future_offset,
        updated_at=now - timedelta(minutes=1),
    )
    verify = mocker.patch(
        "core.services.mail_verification.verify_agent_mail",
        return_value=config.sender_email,
    )

    assert process_mail_verification(now=now) is True

    verify.assert_called_once_with()
    config.refresh_from_db()
    assert config.verification_completed_at == now
