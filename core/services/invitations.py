import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from core.models import Invitation

INVITATION_TTL = timedelta(days=7)


class InvitationError(ValueError):
    pass


@dataclass(frozen=True)
class IssuedInvitation:
    invitation: Invitation
    token: str


def normalize_email(value: str) -> str:
    email = value.strip().casefold()
    try:
        validate_email(email)
    except ValidationError as exc:
        raise InvitationError("请输入有效的邮箱地址。") from exc
    if len(email) > 150:
        raise InvitationError("邮箱地址过长。")
    return email


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_invitation(email: str, created_by, now=None) -> IssuedInvitation:
    now = now or timezone.now()
    email = normalize_email(email)
    User = get_user_model()
    if User.objects.filter(email__iexact=email).exists():
        raise InvitationError("该邮箱已经注册。")
    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        Invitation.objects.select_for_update().filter(
            email__iexact=email,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
        ).update(revoked_at=now)
        invitation = Invitation.objects.create(
            email=email,
            token_digest=token_digest(token),
            created_by=created_by,
            expires_at=now + INVITATION_TTL,
        )
    return IssuedInvitation(invitation=invitation, token=token)


def valid_invitation(token: str, *, now=None, for_update=False):
    now = now or timezone.now()
    query = Invitation.objects
    if for_update:
        query = query.select_for_update()
    return query.filter(
        token_digest=token_digest(token),
        accepted_at__isnull=True,
        revoked_at__isnull=True,
        expires_at__gt=now,
    ).first()
