import pytest
from django.urls import reverse

from core.models import AgentMailConfig
from core.services.agent_mail import AgentMailAuthError

SENDER = "mijiatong@agent.qq.com"
RECIPIENT = "850634546@qq.com"


@pytest.mark.django_db
def test_agent_mail_config_is_a_singleton_with_the_fixed_sender():
    first = AgentMailConfig.get_solo()
    second = AgentMailConfig.get_solo()

    assert first.pk == second.pk == 1
    assert first.sender_email == SENDER


@pytest.mark.django_db
def test_mail_settings_show_agent_sender_without_smtp_credentials(client):
    config = AgentMailConfig.get_solo()
    config.recipient_email = RECIPIENT
    config.save()

    response = client.get(reverse("core:mail-edit"))
    body = response.content.decode()

    assert response.status_code == 200
    assert SENDER in body
    assert RECIPIENT in body
    assert "SMTP 主机" not in body
    assert 'name="authorization_code"' not in body


@pytest.mark.django_db
def test_saving_mail_recipient_verifies_identity_and_sends_test_mail(client, mocker):
    test_mail = mocker.patch("core.views.test_agent_mail_config")

    response = client.post(
        reverse("core:mail-edit"),
        {"recipient_email": RECIPIENT},
    )

    assert response.status_code == 302
    config = AgentMailConfig.get_solo()
    assert config.recipient_email == RECIPIENT
    assert config.is_verified is True
    assert config.verified_at is not None
    assert config.last_error == ""
    test_mail.assert_called_once_with(config)


@pytest.mark.django_db
def test_changing_recipient_disables_delivery_until_test_succeeds(client, mocker):
    config = AgentMailConfig.get_solo()
    config.recipient_email = "old@example.com"
    config.is_verified = True
    config.save()

    def assert_unverified_during_test(candidate):
        candidate.refresh_from_db()
        assert candidate.is_verified is False

    mocker.patch(
        "core.views.test_agent_mail_config", side_effect=assert_unverified_during_test
    )

    response = client.post(
        reverse("core:mail-edit"),
        {"recipient_email": RECIPIENT},
    )

    assert response.status_code == 302
    config.refresh_from_db()
    assert config.recipient_email == RECIPIENT
    assert config.is_verified is True


@pytest.mark.django_db
def test_stale_success_cannot_verify_a_concurrently_changed_recipient(client, mocker):
    first_recipient = "first@example.com"
    concurrent_recipient = "concurrent@example.com"

    def change_recipient_while_first_test_is_running(candidate):
        concurrent = AgentMailConfig.get_solo()
        concurrent.recipient_email = concurrent_recipient
        concurrent.is_verified = False
        concurrent.save()

    mocker.patch(
        "core.views.test_agent_mail_config",
        side_effect=change_recipient_while_first_test_is_running,
    )

    response = client.post(
        reverse("core:mail-edit"),
        {"recipient_email": first_recipient},
    )

    config = AgentMailConfig.get_solo()
    assert response.status_code == 302
    assert config.recipient_email == concurrent_recipient
    assert config.is_verified is False


@pytest.mark.django_db
def test_mail_recipient_is_required(client, mocker):
    test_mail = mocker.patch("core.views.test_agent_mail_config")

    response = client.post(reverse("core:mail-edit"), {"recipient_email": ""})

    assert response.status_code == 200
    assert "必填" in response.content.decode()
    test_mail.assert_not_called()


@pytest.mark.django_db
def test_agent_mail_auth_failure_leaves_configuration_unverified(client, mocker):
    mocker.patch(
        "core.views.test_agent_mail_config",
        side_effect=AgentMailAuthError("agent-mail-auth-required"),
    )

    response = client.post(
        reverse("core:mail-edit"),
        {"recipient_email": RECIPIENT},
    )

    config = AgentMailConfig.get_solo()
    assert response.status_code == 200
    assert config.is_verified is False
    assert config.verified_at is None
    assert config.last_error == "agent-mail-auth-required"
    assert "需要重新授权" in response.content.decode()


def test_legacy_smtp_page_redirects_to_mail_settings(client):
    response = client.get("/smtp/")

    assert response.status_code == 302
    assert response.url == reverse("core:mail-edit")
