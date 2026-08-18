from datetime import timedelta

import pytest
from django.utils import timezone
from freezegun import freeze_time

from core.models import AgentMailConfig, RuntimeState
from core.services.mail_readiness import MailReadiness, mail_readiness


def _verified_config(now):
    config = AgentMailConfig.get_solo()
    config.is_verified = True
    config.verification_status = AgentMailConfig.VerificationStatus.VERIFIED
    config.verified_at = now - timedelta(hours=1)
    config.save()
    return config


@pytest.mark.django_db
def test_mail_readiness_accepts_fresh_attestation_and_worker(settings):
    now = timezone.now()
    _verified_config(now)
    RuntimeState.objects.create(worker_heartbeat_at=now - timedelta(seconds=5))

    assert mail_readiness(now=now) == MailReadiness(True, "ready")


@pytest.mark.django_db
def test_mail_readiness_rejects_unverified_mail():
    now = timezone.now()
    RuntimeState.objects.create(worker_heartbeat_at=now)

    assert mail_readiness(now=now) == MailReadiness(False, "mail-unverified")


@pytest.mark.django_db
@freeze_time("2026-08-18 10:00:00+00:00")
def test_mail_readiness_accepts_attestation_exactly_at_ttl(settings):
    now = timezone.now()
    config = _verified_config(now)
    config.verified_at = now - timedelta(
        seconds=settings.AGENT_MAIL_ATTESTATION_TTL_SECONDS
    )
    config.save()
    RuntimeState.objects.create(worker_heartbeat_at=now)

    assert mail_readiness(now=now) == MailReadiness(True, "ready")


@pytest.mark.django_db
@freeze_time("2026-08-18 10:00:00+00:00")
def test_mail_readiness_rejects_stale_attestation(settings):
    now = timezone.now()
    config = _verified_config(now)
    config.verified_at = now - timedelta(
        seconds=settings.AGENT_MAIL_ATTESTATION_TTL_SECONDS,
        microseconds=1,
    )
    config.save()
    RuntimeState.objects.create(worker_heartbeat_at=now)

    assert mail_readiness(now=now) == MailReadiness(False, "mail-attestation-stale")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sender_email", "wrong@example.com"),
        ("recipient_email", "attacker@example.com"),
    ],
)
@pytest.mark.django_db
def test_mail_readiness_rejects_fixed_address_mismatch(field, value):
    now = timezone.now()
    config = _verified_config(now)
    setattr(config, field, value)
    config.save()
    RuntimeState.objects.create(worker_heartbeat_at=now)

    assert mail_readiness(now=now) == MailReadiness(False, "mail-address-mismatch")


@pytest.mark.django_db
def test_mail_readiness_rejects_stale_worker():
    now = timezone.now()
    _verified_config(now)
    RuntimeState.objects.create(worker_heartbeat_at=now - timedelta(seconds=31))

    assert mail_readiness(now=now) == MailReadiness(False, "worker-stale")
