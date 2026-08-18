from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import AgentMailConfig, RuntimeState

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


@pytest.mark.django_db(transaction=True)
def test_mail_test_only_enqueues_worker_request(client, mocker):
    request_verification = mocker.patch(
        "core.views.request_mail_verification",
        create=True,
    )
    mocker.patch(
        "core.services.agent_mail._run",
        side_effect=AssertionError("Web must not execute Agent Mail CLI"),
    )

    response = client.post(reverse("core:mail-test"))

    assert response.status_code == 302
    assert response.url == reverse("core:mail-edit")
    request_verification.assert_called_once_with()


@pytest.mark.django_db
def test_pending_mail_verification_refreshes_in_document_head(client):
    config = AgentMailConfig.get_solo()
    config.verification_status = AgentMailConfig.VerificationStatus.PENDING
    config.verification_requested_at = timezone.now()
    config.save()

    body = client.get(reverse("core:mail-edit")).content.decode()

    refresh = '<meta http-equiv="refresh" content="2">'
    assert "等待后台 Worker 验证" in body
    assert refresh in body
    assert body.index(refresh) < body.index("</head>")
    assert '<button class="button-primary" type="submit" disabled>' in body


@pytest.mark.django_db
def test_verified_mail_page_shows_worker_attestation_timestamp(client):
    verified_at = timezone.now() - timedelta(minutes=3)
    config = AgentMailConfig.get_solo()
    config.is_verified = True
    config.verified_at = verified_at
    config.verification_status = AgentMailConfig.VerificationStatus.VERIFIED
    config.save()
    RuntimeState.objects.create(worker_heartbeat_at=timezone.now())

    body = client.get(reverse("core:mail-edit")).content.decode()

    assert "Worker 已确认固定邮箱身份" in body
    assert timezone.localtime(verified_at).strftime("%Y年%m月%d日 %H:%M") in body


@pytest.mark.django_db
def test_failed_mail_page_shows_safe_error_and_server_device_login_guidance(client):
    config = AgentMailConfig.get_solo()
    config.verification_status = AgentMailConfig.VerificationStatus.FAILED
    config.last_error = "agent-mail-auth-required"
    config.save()

    body = client.get(reverse("core:mail-edit")).content.decode()

    assert "agent-mail-auth-required" in body
    assert "服务器" in body
    assert "设备登录" in body
    assert "macOS 钥匙串" not in body


@pytest.mark.django_db
def test_mail_page_distinguishes_stale_worker_from_mail_authorization(client):
    config = AgentMailConfig.get_solo()
    config.is_verified = True
    config.verified_at = timezone.now()
    config.verification_status = AgentMailConfig.VerificationStatus.VERIFIED
    config.save()
    RuntimeState.objects.create(
        worker_heartbeat_at=timezone.now() - timedelta(minutes=5),
    )

    body = client.get(reverse("core:mail-edit")).content.decode()

    assert "后台 Worker 暂不可用" in body
    assert "Worker 已确认固定邮箱身份" in body


def test_legacy_smtp_page_redirects_to_mail_settings(client):
    response = client.get("/smtp/")

    assert response.status_code == 302
    assert response.url == reverse("core:mail-edit")
