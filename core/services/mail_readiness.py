from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from core.models import AgentMailConfig, RuntimeState
from core.services.agent_mail import AGENT_MAIL_SENDER
from core.services.runtime_health import worker_heartbeat_is_fresh

AGENT_MAIL_RECIPIENT = "850634546@qq.com"


@dataclass(frozen=True)
class MailReadiness:
    ready: bool
    code: str


def mail_readiness(now: datetime | None = None) -> MailReadiness:
    now = now or timezone.now()
    config = AgentMailConfig.get_solo()
    if (
        config.sender_email != AGENT_MAIL_SENDER
        or config.recipient_email != AGENT_MAIL_RECIPIENT
    ):
        return MailReadiness(False, "mail-address-mismatch")
    if (
        not config.is_verified
        or config.verification_status
        != AgentMailConfig.VerificationStatus.VERIFIED
        or config.verified_at is None
    ):
        return MailReadiness(False, "mail-unverified")
    ttl = timedelta(seconds=settings.AGENT_MAIL_ATTESTATION_TTL_SECONDS)
    if config.verified_at < now - ttl:
        return MailReadiness(False, "mail-attestation-stale")
    state = RuntimeState.objects.filter(pk=1).first()
    if not worker_heartbeat_is_fresh(state, now):
        return MailReadiness(False, "worker-stale")
    return MailReadiness(True, "ready")
