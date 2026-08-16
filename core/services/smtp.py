import smtplib
import ssl
from email.message import EmailMessage

from core.crypto import decrypt_secret
from core.models import SMTPConfig


def sanitize_smtp_error(exc):
    code = getattr(exc, "smtp_code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return f"{type(exc).__name__} ({code})"
    return type(exc).__name__


def _connect(config):
    if config.security == SMTPConfig.Security.SSL:
        return smtplib.SMTP_SSL(config.host, config.port, timeout=15)

    connection = smtplib.SMTP(config.host, config.port, timeout=15)
    connection.starttls(context=ssl.create_default_context())
    return connection


def send_message(config, message):
    connection = None
    try:
        connection = _connect(config)
        connection.login(config.username, decrypt_secret(config.encrypted_password))
        return str(connection.send_message(message))
    finally:
        if connection is not None:
            try:
                connection.quit()
            except (OSError, smtplib.SMTPException):
                connection.close()


def test_smtp_config(config):
    message = EmailMessage()
    message["From"] = config.from_email
    message["To"] = config.recipient_email
    message["Subject"] = "TicketWatch SMTP 测试邮件"
    message.set_content("这是一封 TicketWatch SMTP 测试邮件。")
    send_message(config, message)
