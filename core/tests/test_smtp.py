import smtplib
from datetime import timedelta
from email.message import EmailMessage
from smtplib import SMTPAuthenticationError
from unittest.mock import patch

import pytest
from django.db import DatabaseError
from django.urls import reverse
from django.utils import timezone

from core.crypto import decrypt_secret, encrypt_secret
from core.forms import SMTPConfigForm
from core.models import MonitorTask, Notification, SMTPConfig
from core.services.smtp import _connect, send_message


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
def test_successful_reverification_makes_pending_mail_due_and_wakes_scheduler(
    smtp_ssl, client, task_factory, mocker, django_capture_on_commit_callbacks
):
    """Leaving repaired notification work asleep behind an old retry time must fail this test."""
    now = timezone.now()
    config = SMTPConfig.get_solo()
    config.host = "smtp.163.com"
    config.username = config.from_email = "sender@example.com"
    config.recipient_email = "receiver@example.com"
    config.encrypted_password = encrypt_secret("authorization-code")
    config.is_verified = False
    config.save()
    task = task_factory(status=MonitorTask.Status.CANCELLED)
    notification = Notification.objects.create(
        task=task,
        notification_type=Notification.Type.EXPIRY,
        next_attempt_at=now + timedelta(hours=1),
    )
    wake = mocker.patch("core.views.wake_scheduler")
    mocker.patch("core.views.timezone.now", return_value=now)

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(reverse("core:smtp-test"))

    config.refresh_from_db()
    notification.refresh_from_db()
    assert response.status_code == 302
    assert config.is_verified is True
    assert config.verified_at == now
    assert notification.status == Notification.Status.PENDING
    assert notification.next_attempt_at == now
    wake.assert_called_once_with()


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
def test_smtp_save_failure_never_escapes_or_renders_submitted_credential(client, mocker):
    """Letting credential persistence errors reach Django's debug page must fail this test."""
    SMTPConfig.get_solo()
    client.raise_request_exception = False
    mocker.patch(
        "core.forms.SMTPConfig.save",
        side_effect=DatabaseError("database rejected authorization-code-private"),
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
            "authorization_code": "authorization-code-private",
        },
    )

    body = response.content.decode()
    assert response.status_code == 200
    assert "无法安全保存 SMTP 配置" in body
    assert "authorization-code-private" not in body
    assert "database rejected" not in body


def _fail_second_smtp_config_save(mocker, message):
    original_save = SMTPConfig.save
    save_count = 0

    def save_then_fail(config, *args, **kwargs):
        nonlocal save_count
        save_count += 1
        if save_count == 2:
            raise DatabaseError(message)
        return original_save(config, *args, **kwargs)

    mocker.patch.object(SMTPConfig, "save", new=save_then_fail)


@pytest.mark.django_db
def test_smtp_failure_state_save_error_uses_unbound_secret_safe_response(
    client, mocker, caplog
):
    """A failed post-test state save reaching Django DEBUG would expose the POST secret."""
    SMTPConfig.get_solo()
    client.raise_request_exception = False
    submitted_secret = "failure-branch-authorization-code"
    _fail_second_smtp_config_save(mocker, f"database rejected {submitted_secret}")
    smtp_ssl = mocker.patch("core.services.smtp.smtplib.SMTP_SSL")
    smtp_ssl.return_value.login.side_effect = SMTPAuthenticationError(
        535, b"private SMTP response"
    )

    with caplog.at_level("ERROR"):
        response = client.post(
            reverse("core:smtp-edit"),
            {
                "host": "smtp.163.com",
                "port": 465,
                "security": "ssl",
                "username": "sender@example.com",
                "from_email": "sender@example.com",
                "recipient_email": "receiver@example.com",
                "authorization_code": submitted_secret,
            },
        )

    body = response.content.decode()
    assert response.status_code == 200
    assert response.context["form"].is_bound is False
    assert "无法安全保存 SMTP 配置" in body
    assert submitted_secret not in body
    assert submitted_secret not in caplog.text
    assert "database rejected" not in body
    assert "database rejected" not in caplog.text
    assert SMTPConfig.get_solo().is_verified is False


@pytest.mark.django_db
def test_smtp_success_state_save_error_uses_unbound_secret_safe_response(
    client, mocker, caplog
):
    """A failed verification-state save reaching Django DEBUG would expose the POST secret."""
    SMTPConfig.get_solo()
    client.raise_request_exception = False
    submitted_secret = "success-branch-authorization-code"
    _fail_second_smtp_config_save(mocker, f"database rejected {submitted_secret}")
    mocker.patch("core.services.smtp.smtplib.SMTP_SSL")

    with caplog.at_level("ERROR"):
        response = client.post(
            reverse("core:smtp-edit"),
            {
                "host": "smtp.163.com",
                "port": 465,
                "security": "ssl",
                "username": "sender@example.com",
                "from_email": "sender@example.com",
                "recipient_email": "receiver@example.com",
                "authorization_code": submitted_secret,
            },
        )

    body = response.content.decode()
    assert response.status_code == 200
    assert response.context["form"].is_bound is False
    assert "无法安全保存 SMTP 配置" in body
    assert submitted_secret not in body
    assert submitted_secret not in caplog.text
    assert "database rejected" not in body
    assert "database rejected" not in caplog.text
    assert SMTPConfig.get_solo().is_verified is False


@pytest.mark.django_db
@pytest.mark.parametrize("smtp_test_fails", [False, True])
def test_stored_smtp_retest_state_save_error_is_also_safe(
    client, mocker, caplog, smtp_test_fails
):
    """Stored-config retest persistence errors must not escape either result branch."""
    stored_secret = "stored-authorization-code"
    config = SMTPConfig.get_solo()
    config.host = "smtp.163.com"
    config.username = config.from_email = "sender@example.com"
    config.recipient_email = "receiver@example.com"
    config.encrypted_password = encrypt_secret(stored_secret)
    config.is_verified = False
    config.save()
    client.raise_request_exception = False
    mocker.patch.object(
        SMTPConfig,
        "save",
        side_effect=DatabaseError(f"database rejected {stored_secret}"),
    )
    smtp_ssl = mocker.patch("core.services.smtp.smtplib.SMTP_SSL")
    if smtp_test_fails:
        smtp_ssl.return_value.login.side_effect = SMTPAuthenticationError(
            535, b"private SMTP response"
        )

    with caplog.at_level("ERROR"):
        response = client.post(reverse("core:smtp-test"))

    body = response.content.decode()
    assert response.status_code == 200
    assert response.context["form"].is_bound is False
    assert "无法安全保存 SMTP 配置" in body
    assert stored_secret not in body
    assert stored_secret not in caplog.text
    assert "database rejected" not in body
    assert "database rejected" not in caplog.text
    assert SMTPConfig.get_solo().is_verified is False


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


@patch("core.services.smtp.smtplib.SMTP_SSL")
@patch("core.services.smtp.ssl.create_default_context")
def test_ssl_connection_uses_verified_default_context(create_default_context, smtp_ssl):
    """Dropping certificate and hostname verification from SSL connections must fail this test."""
    config = SMTPConfig(host="smtp.163.com", port=465, security=SMTPConfig.Security.SSL)
    verified_context = object()
    create_default_context.return_value = verified_context

    _connect(config)

    create_default_context.assert_called_once_with()
    smtp_ssl.assert_called_once_with(
        "smtp.163.com",
        465,
        timeout=15,
        context=verified_context,
    )


@patch("core.services.smtp.smtplib.SMTP")
def test_starttls_failure_closes_open_connection(smtp):
    """Leaving a socket open when STARTTLS negotiation fails must fail this test."""
    config = SMTPConfig(host="smtp.163.com", port=587, security=SMTPConfig.Security.STARTTLS)
    smtp.return_value.starttls.side_effect = smtplib.SMTPException("TLS handshake failed")

    with pytest.raises(smtplib.SMTPException):
        send_message(config, EmailMessage())

    smtp.return_value.close.assert_called_once_with()
