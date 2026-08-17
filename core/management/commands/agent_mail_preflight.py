from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import AgentMailConfig
from core.services.agent_mail import (
    AgentMailAuthError,
    AgentMailConfigError,
    AgentMailError,
    AgentMailPermanentError,
    AgentMailTemporaryError,
    AgentMailUncertainError,
    test_agent_mail_config,
    verify_agent_mail,
)

ERROR_CODES = (
    (AgentMailAuthError, "agent-mail-auth-required"),
    (AgentMailTemporaryError, "agent-mail-network-error"),
    (AgentMailUncertainError, "agent-mail-result-unknown"),
    (AgentMailPermanentError, "agent-mail-recipient-rejected"),
    (AgentMailConfigError, "agent-mail-config-error"),
)
DEFAULT_ERROR_CODE = "agent-mail-preflight-failed"
SUCCESS_MESSAGE = "Agent Mail preflight passed."


def safe_error_code(error):
    for error_type, error_code in ERROR_CODES:
        if isinstance(error, error_type):
            return error_code
    return DEFAULT_ERROR_CODE


class Command(BaseCommand):
    help = "Verify the configured Agent Mail identity and optionally send a test message."

    def add_arguments(self, parser):
        parser.add_argument(
            "--send-test",
            action="store_true",
            help="Queue a test email to the fixed configured recipient.",
        )

    def _raise_safe_error(self, error):
        raise CommandError(f"Agent Mail preflight failed: {safe_error_code(error)}") from None

    @staticmethod
    def _reset_verification(config):
        now = timezone.now()
        AgentMailConfig.objects.filter(pk=config.pk).update(
            is_verified=False,
            verified_at=None,
            last_error="",
            updated_at=now,
        )
        config.refresh_from_db()

    @staticmethod
    def _mark_verified(config):
        now = timezone.now()
        AgentMailConfig.objects.filter(pk=config.pk).update(
            is_verified=True,
            verified_at=now,
            last_error="",
            updated_at=now,
        )

    @staticmethod
    def _mark_failed(config, error):
        now = timezone.now()
        AgentMailConfig.objects.filter(pk=config.pk).update(
            is_verified=False,
            verified_at=None,
            last_error=safe_error_code(error),
            updated_at=now,
        )

    def handle(self, *args, **options):
        if not options["send_test"]:
            try:
                verify_agent_mail()
            except AgentMailError as error:
                self._raise_safe_error(error)
            self.stdout.write(SUCCESS_MESSAGE)
            return

        config = AgentMailConfig.get_solo()
        self._reset_verification(config)
        try:
            queued = test_agent_mail_config(config)
            if queued != "queued":
                raise AgentMailConfigError("agent-mail-message-not-queued")
        except AgentMailError as error:
            self._mark_failed(config, error)
            self._raise_safe_error(error)

        self._mark_verified(config)
        self.stdout.write(SUCCESS_MESSAGE)
