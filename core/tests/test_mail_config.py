import pytest
from django.urls import reverse

from core.models import AgentMailConfig
from core.services.agent_mail import AgentMailAuthError

SENDER = "mijiatong@agent.qq.com"
RECIPIENT = "850634546@qq.com"


@pytest.mark.django_db
def test_agent_mail_config_is_a_singleton_with_fixed_sender_and_recipient():
    first = AgentMailConfig.get_solo()
    second = AgentMailConfig.get_solo()

    assert first.pk == second.pk == 1
    assert first.sender_email == SENDER
    assert first.recipient_email == RECIPIENT


@pytest.mark.django_db
def test_mail_settings_show_agent_sender_without_smtp_credentials(client):
    response = client.get(reverse("core:mail-edit"))
    body = response.content.decode()

    assert response.status_code == 200
    assert SENDER in body
    assert RECIPIENT in body
    assert "SMTP 主机" not in body
    assert 'name="authorization_code"' not in body


@pytest.mark.django_db
def test_sending_fixed_recipient_test_mail_verifies_identity(client, mocker):
    test_mail = mocker.patch("core.views.test_agent_mail_config")

    response = client.post(reverse("core:mail-test"))

    assert response.status_code == 302
    config = AgentMailConfig.get_solo()
    assert config.recipient_email == RECIPIENT
    assert config.is_verified is True
    assert config.verified_at is not None
    assert config.last_error == ""
    test_mail.assert_called_once_with(config)


@pytest.mark.django_db
@pytest.mark.django_db
def test_agent_mail_auth_failure_leaves_configuration_unverified(client, mocker):
    mocker.patch(
        "core.views.test_agent_mail_config",
        side_effect=AgentMailAuthError("agent-mail-auth-required"),
    )

    response = client.post(reverse("core:mail-test"))

    config = AgentMailConfig.get_solo()
    assert response.status_code == 302
    assert config.is_verified is False
    assert config.verified_at is None
    assert config.last_error == "agent-mail-auth-required"
    assert response.url == reverse("core:mail-edit")


def test_legacy_smtp_page_redirects_to_mail_settings(client):
    response = client.get("/smtp/")

    assert response.status_code == 302
    assert response.url == reverse("core:mail-edit")
