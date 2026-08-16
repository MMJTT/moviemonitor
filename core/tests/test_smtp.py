from email.message import EmailMessage
from smtplib import SMTPAuthenticationError
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from core.crypto import decrypt_secret, encrypt_secret
from core.forms import SMTPConfigForm
from core.models import SMTPConfig
from core.services.smtp import send_message


@pytest.mark.django_db
def test_smtp_edit_page_renders_an_empty_authorization_code_field(client):
    """Removing the SMTP configuration page would make this test fail."""
    response = client.get(reverse("core:smtp-edit"))

    assert response.status_code == 200
    assert 'type="password"' in response.content.decode()


@pytest.mark.django_db
def test_new_authorization_code_is_encrypted_and_requires_reverification():
    """Saving an authorization code in plaintext or retaining verification must fail this test."""
    config = SMTPConfig.get_solo()
    config.is_verified = True
    config.verified_at = timezone.now()
    config.save()
    form = SMTPConfigForm(
        data={
            "host": "smtp.163.com",
            "port": 465,
            "security": "ssl",
            "username": "sender@example.com",
            "from_email": "sender@example.com",
            "recipient_email": "receiver@example.com",
            "authorization_code": "authorization-code",
        },
        instance=config,
    )

    assert form.is_valid(), form.errors
    saved = form.save()

    assert saved.encrypted_password != "authorization-code"
    assert decrypt_secret(saved.encrypted_password) == "authorization-code"
    assert saved.is_verified is False
    assert saved.verified_at is None


@pytest.mark.django_db
@patch("core.services.smtp.smtplib.SMTP_SSL")
def test_successful_test_mail_marks_configuration_verified(smtp_ssl, client):
    """Not testing SMTP acceptance before setting verification must fail this test."""
    response = client.post(
        reverse("core:smtp-edit"),
        {
            "host": "smtp.163.com",
            "port": 465,
            "security": "ssl",
            "username": "sender@example.com",
            "from_email": "sender@example.com",
            "recipient_email": "receiver@example.com",
            "authorization_code": "authorization-code",
        },
        follow=True,
    )

    assert response.status_code == 200
    config = SMTPConfig.get_solo()
    assert config.is_verified is True
    assert config.encrypted_password != "authorization-code"
    smtp_ssl.return_value.login.assert_called_once_with("sender@example.com", "authorization-code")


@pytest.mark.django_db
@patch("core.services.smtp.smtplib.SMTP_SSL")
def test_stored_configuration_can_be_retested_only_with_post(smtp_ssl, client):
    """Allowing GET retests or skipping the stored credential would make this test fail."""
    config = SMTPConfig.get_solo()
    config.host = "smtp.163.com"
    config.username = config.from_email = "sender@example.com"
    config.recipient_email = "receiver@example.com"
    config.encrypted_password = encrypt_secret("authorization-code")
    config.save()

    assert client.get(reverse("core:smtp-test")).status_code == 405
    response = client.post(reverse("core:smtp-test"), follow=True)

    assert response.status_code == 200
    config.refresh_from_db()
    assert config.is_verified is True
    smtp_ssl.return_value.login.assert_called_once_with("sender@example.com", "authorization-code")


@pytest.mark.django_db
@patch("core.services.smtp.smtplib.SMTP_SSL")
def test_smtp_failure_renders_only_sanitized_error(smtp_ssl, client):
    """Rendering an SMTP response or secret instead of its safe summary must fail this test."""
    smtp_ssl.return_value.login.side_effect = SMTPAuthenticationError(
        535, b"authorization-code must never be shown"
    )

    response = client.post(
        reverse("core:smtp-edit"),
        {
            "host": "smtp.163.com",
            "port": 465,
            "security": "ssl",
            "username": "sender@example.com",
            "from_email": "sender@example.com",
            "recipient_email": "receiver@example.com",
            "authorization_code": "authorization-code",
        },
    )

    body = response.content.decode()
    config = SMTPConfig.get_solo()
    assert response.status_code == 200
    assert config.is_verified is False
    assert config.last_error == "SMTPAuthenticationError (535)"
    assert config.last_error in body
    assert "authorization-code" not in body
    assert "must never be shown" not in body


@pytest.mark.django_db
def test_authorization_code_is_never_rendered(client):
    """Binding the stored ciphertext or plaintext into the password field must fail this test."""
    config = SMTPConfig.get_solo()
    config.encrypted_password = encrypt_secret("authorization-code")
    config.save()

    body = client.get(reverse("core:smtp-edit")).content.decode()

    assert "authorization-code" not in body
    assert config.encrypted_password not in body


@pytest.mark.django_db
@patch("core.services.smtp.smtplib.SMTP_SSL")
def test_send_message_closes_connection_when_quit_fails(smtp_ssl):
    """Leaking the transport when graceful SMTP shutdown fails must fail this test."""
    config = SMTPConfig.get_solo()
    config.host = "smtp.163.com"
    config.username = config.from_email = "sender@example.com"
    config.recipient_email = "receiver@example.com"
    config.encrypted_password = encrypt_secret("authorization-code")
    config.save()
    smtp_ssl.return_value.send_message.return_value = {}
    smtp_ssl.return_value.quit.side_effect = OSError("connection reset")
    message = EmailMessage()
    message["From"] = config.from_email
    message["To"] = config.recipient_email
    message.set_content("test")

    assert send_message(config, message) == "{}"
    smtp_ssl.return_value.close.assert_called_once_with()
