from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import AgentMailConfig
from core.services.agent_mail import (
    AgentMailConfigError,
    safe_agent_mail_error_code,
    test_agent_mail_config,
    verify_agent_mail,
)

SUCCESS_MESSAGE = "Agent Mail preflight passed."


class Command(BaseCommand):
    help = "Verify the configured Agent Mail identity and optionally send a test message."

    def add_arguments(self, parser):
        parser.add_argument(
            "--send-test",
            action="store_true",
            help="Queue a test email to the fixed configured recipient.",
        )

    def _raise_safe_error(self, error):
        code = safe_agent_mail_error_code(error)
        raise CommandError(f"Agent Mail preflight failed: {code}") from None

    @staticmethod
    def _reset_verification(config):
        now = timezone.now()
        updated = (
            AgentMailConfig.objects.filter(
                pk=config.pk,
                updated_at=config.updated_at,
            )
            .exclude(verification_status=AgentMailConfig.VerificationStatus.PENDING)
            .update(
                is_verified=False,
                verified_at=None,
                verification_status=AgentMailConfig.VerificationStatus.FAILED,
                verification_completed_at=now,
                verification_claim_token=None,
                verification_claim_expires_at=None,
                last_error="agent-mail-preflight-failed",
                updated_at=now,
            )
        )
        if updated != 1:
            return False
        config.refresh_from_db()
        return True

    @staticmethod
    def _mark_verified(config):
        now = timezone.now()
        (
            AgentMailConfig.objects.filter(
                pk=config.pk,
                updated_at=config.updated_at,
            )
            .exclude(verification_status=AgentMailConfig.VerificationStatus.PENDING)
            .update(
                is_verified=True,
                verified_at=now,
                verification_status=AgentMailConfig.VerificationStatus.VERIFIED,
                verification_completed_at=now,
                verification_claim_token=None,
                verification_claim_expires_at=None,
                last_error="",
                updated_at=now,
            )
        )

    @staticmethod
    def _mark_failed(config, error):
        now = timezone.now()
        (
            AgentMailConfig.objects.filter(
                pk=config.pk,
                updated_at=config.updated_at,
            )
            .exclude(verification_status=AgentMailConfig.VerificationStatus.PENDING)
            .update(
                is_verified=False,
                verified_at=None,
                verification_status=AgentMailConfig.VerificationStatus.FAILED,
                verification_completed_at=now,
                verification_claim_token=None,
                verification_claim_expires_at=None,
                last_error=safe_agent_mail_error_code(error),
                updated_at=now,
            )
        )

    def handle(self, *args, **options):
        config = AgentMailConfig.get_solo()
        if not self._reset_verification(config):
            raise CommandError(
                "Agent Mail preflight failed: agent-mail-preflight-failed"
            )
        if not options["send_test"]:
            try:
                verify_agent_mail()
            except Exception as error:
                self._mark_failed(config, error)
                self._raise_safe_error(error)
            self._mark_verified(config)
            self.stdout.write(SUCCESS_MESSAGE)
            return

        try:
            queued = test_agent_mail_config(config)
            if queued != "queued":
                raise AgentMailConfigError("agent-mail-message-not-queued")
        except Exception as error:
            self._mark_failed(config, error)
            self._raise_safe_error(error)

        self._mark_verified(config)
        self.stdout.write(SUCCESS_MESSAGE)
