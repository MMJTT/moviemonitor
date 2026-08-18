import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import AgentMailConfig
from core.services.agent_mail import (
    AgentMailConfigError,
    safe_agent_mail_error_code,
    test_agent_mail_config,
    verify_agent_mail,
)
from core.services.coordination import notify_worker


@dataclass(frozen=True)
class MailVerificationClaim:
    token: uuid.UUID
    requested_at: datetime


def request_mail_verification(now: datetime | None = None) -> None:
    now = now or timezone.now()
    with transaction.atomic():
        config, _ = AgentMailConfig.objects.select_for_update().get_or_create(pk=1)
        AgentMailConfig.objects.filter(pk=config.pk).update(
            verification_status=AgentMailConfig.VerificationStatus.PENDING,
            verification_requested_at=now,
            verification_claim_token=None,
            verification_claim_expires_at=None,
            updated_at=now,
        )
        transaction.on_commit(notify_worker)


def _claim_pending_verification(
    now: datetime | None = None,
) -> MailVerificationClaim | None:
    now = now or timezone.now()
    with transaction.atomic():
        config = AgentMailConfig.objects.select_for_update().filter(pk=1).first()
        if (
            config is None
            or config.verification_status
            != AgentMailConfig.VerificationStatus.PENDING
            or config.verification_requested_at is None
        ):
            return None
        if (
            config.verification_claim_token is not None
            and config.verification_claim_expires_at is not None
            and config.verification_claim_expires_at > now
        ):
            return None

        token = uuid.uuid4()
        AgentMailConfig.objects.filter(pk=config.pk).update(
            verification_claim_token=token,
            verification_claim_expires_at=now
            + timedelta(seconds=settings.AGENT_MAIL_CLAIM_SECONDS),
            updated_at=now,
        )
        return MailVerificationClaim(
            token=token,
            requested_at=config.verification_requested_at,
        )


def _complete_claim(
    claim: MailVerificationClaim,
    *,
    now: datetime,
    verified: bool,
    error_code: str,
) -> bool:
    updated = AgentMailConfig.objects.filter(
        pk=1,
        verification_status=AgentMailConfig.VerificationStatus.PENDING,
        verification_requested_at=claim.requested_at,
        verification_claim_token=claim.token,
    ).update(
        is_verified=verified,
        verified_at=now if verified else None,
        verification_status=(
            AgentMailConfig.VerificationStatus.VERIFIED
            if verified
            else AgentMailConfig.VerificationStatus.FAILED
        ),
        verification_completed_at=now,
        verification_claim_token=None,
        verification_claim_expires_at=None,
        last_error=error_code,
        updated_at=now,
    )
    return updated == 1


def _verification_values(*, now: datetime, verified: bool, error_code: str) -> dict:
    return {
        "is_verified": verified,
        "verified_at": now if verified else None,
        "verification_status": (
            AgentMailConfig.VerificationStatus.VERIFIED
            if verified
            else AgentMailConfig.VerificationStatus.FAILED
        ),
        "verification_completed_at": now,
        "verification_claim_token": None,
        "verification_claim_expires_at": None,
        "last_error": error_code,
        "updated_at": now,
    }


def _run_verification(call, *, require_queued: bool = False) -> tuple[bool, str]:
    try:
        result = call()
        if require_queued and result != "queued":
            raise AgentMailConfigError("agent-mail-message-not-queued")
    except Exception as error:
        return False, safe_agent_mail_error_code(error)
    return True, ""


def process_mail_verification(
    now: datetime | None = None, force_identity: bool = False
) -> bool:
    now = now or timezone.now()
    claim = _claim_pending_verification(now=now)
    if claim is not None:
        config = AgentMailConfig.get_solo()
        verified, error_code = _run_verification(
            lambda: test_agent_mail_config(config),
            require_queued=True,
        )
        _complete_claim(
            claim,
            now=now,
            verified=verified,
            error_code=error_code,
        )
        return True

    config = AgentMailConfig.get_solo()
    if config.verification_status == AgentMailConfig.VerificationStatus.PENDING:
        if config.verification_requested_at is None:
            AgentMailConfig.objects.filter(
                pk=config.pk,
                updated_at=config.updated_at,
                verification_status=AgentMailConfig.VerificationStatus.PENDING,
                verification_requested_at__isnull=True,
            ).update(
                **_verification_values(
                    now=now,
                    verified=False,
                    error_code="agent-mail-config-error",
                )
            )
        return False
    if not force_identity and config.verification_completed_at is not None:
        age = now - config.verification_completed_at
        if timedelta(0) <= age < timedelta(
            seconds=settings.AGENT_MAIL_REVERIFY_SECONDS
        ):
            return False

    verified, error_code = _run_verification(verify_agent_mail)
    (
        AgentMailConfig.objects.filter(pk=config.pk, updated_at=config.updated_at)
        .exclude(verification_status=AgentMailConfig.VerificationStatus.PENDING)
        .update(**_verification_values(now=now, verified=verified, error_code=error_code))
    )
    return True
