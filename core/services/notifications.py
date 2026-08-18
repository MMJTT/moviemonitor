from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from core.models import AgentMailConfig, MonitorTask, Notification
from core.services.agent_mail import (
    AgentMailAuthError,
    AgentMailConfigError,
    AgentMailPermanentError,
    AgentMailTemporaryError,
    AgentMailUncertainError,
    safe_agent_mail_error_code,
    send_agent_mail,
)
from core.services.tasks import TASK_NO_LONGER_DETECTED, opening_notification_transition

RETRY_MINUTES = (1, 5, 15, 30, 60)
EXPIRED_BEFORE_DELIVERY = "expired-before-delivery"
UNCERTAIN_AFTER_RESTART = "agent-mail-result-unknown-after-restart"

EXPIRABLE_TASK_STATUSES = (
    MonitorTask.Status.MONITORING,
    MonitorTask.Status.PAUSED,
    MonitorTask.Status.DETECTED,
    MonitorTask.Status.ERROR,
)


def _expire_locked_task(task, now):
    with opening_notification_transition():
        task.status = MonitorTask.Status.EXPIRED
        task.expired_at = now
        task.next_check_at = None
        task.claim_token = None
        task.claim_expires_at = None
        task.save(
            update_fields=[
                "status",
                "expired_at",
                "next_check_at",
                "claim_token",
                "claim_expires_at",
                "updated_at",
            ]
        )
        Notification.objects.filter(
            task=task,
            notification_type=Notification.Type.OPENING,
            status=Notification.Status.PENDING,
        ).update(
            status=Notification.Status.PERMANENT_FAILED,
            next_attempt_at=None,
            last_error=EXPIRED_BEFORE_DELIVERY,
            updated_at=now,
        )
        Notification.objects.get_or_create(
            task=task,
            notification_type=Notification.Type.EXPIRY,
            defaults={"status": Notification.Status.PENDING},
        )


def expire_due_task(now=None):
    now = now or timezone.now()
    local_date = timezone.localdate(now)
    with opening_notification_transition(), transaction.atomic():
        tasks = list(
            MonitorTask.objects.select_for_update()
            .filter(status__in=EXPIRABLE_TASK_STATUSES, show_date__lt=local_date)
            .order_by("created_at")
        )
        if not tasks:
            return False
        for task in tasks:
            _expire_locked_task(task, now)
    return True


def _message_id(notification):
    label = notification.notification_type.lower()
    return f"<{label}-{notification.pk}@ticketwatch.local>"


def _local_timestamp(value):
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S %Z")


@dataclass(frozen=True)
class OutgoingMail:
    subject: str
    body: str


def _build_message(notification, now):
    task = notification.task
    if notification.notification_type == Notification.Type.OPENING:
        subject = f"[TicketWatch] {task.movie_name} 已在 {task.cinema_name} 开票"
        event_time = task.detected_at or now
        lines = [
            f"电影：{task.movie_name}",
            f"日期：{task.show_date.isoformat()}",
            f"城市：{task.city_name}",
            f"影院：{task.cinema_name}",
            f"发现时间：{_local_timestamp(event_time)}",
            f"监控链接：{task.source_url}",
            f"购票链接：{task.booking_url}",
        ]
    elif notification.notification_type == Notification.Type.EXPIRY:
        subject = f"[TicketWatch] {task.movie_name} 监控已到期"
        event_time = task.expired_at or now
        lines = [
            f"电影：{task.movie_name}",
            f"日期：{task.show_date.isoformat()}",
            f"城市：{task.city_name}",
            f"影院：{task.cinema_name}",
            f"到期时间：{_local_timestamp(event_time)}",
            f"监控链接：{task.source_url}",
        ]
    else:
        subject = "[TicketWatch] 监控任务需要处理"
        lines = [
            f"电影：{task.movie_name}",
            f"日期：{task.show_date.isoformat()}",
            f"城市：{task.city_name}",
            f"影院：{task.cinema_name}",
            f"错误类别：{task.last_error}",
            f"任务详情：{reverse('core:task-detail', args=[task.pk])}",
        ]
    return OutgoingMail(subject=subject, body="\n".join(lines))


def _mark_notification_needs_review(notification_id, error):
    with transaction.atomic():
        current = Notification.objects.select_for_update().get(pk=notification_id)
        if current.status != Notification.Status.SENDING:
            return
        current.status = Notification.Status.NEEDS_REVIEW
        current.next_attempt_at = None
        current.last_error = error
        current.save(
            update_fields=["status", "next_attempt_at", "last_error", "updated_at"]
        )


def mark_uncertain_sending_notifications() -> int:
    now = timezone.now()
    with transaction.atomic():
        return Notification.objects.filter(status=Notification.Status.SENDING).update(
            status=Notification.Status.NEEDS_REVIEW,
            next_attempt_at=None,
            last_error=UNCERTAIN_AFTER_RESTART,
            updated_at=now,
        )


def _park_for_mail_repair(notification_id, error, *, unverify_mail, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        current = Notification.objects.select_for_update().get(pk=notification_id)
        if current.status != Notification.Status.SENDING:
            return
        current.status = Notification.Status.PENDING
        current.next_attempt_at = None
        current.last_error = error
        current.save(
            update_fields=["status", "next_attempt_at", "last_error", "updated_at"]
        )
        if unverify_mail:
            AgentMailConfig.objects.filter(pk=1).update(
                is_verified=False,
                verified_at=None,
                verification_status=AgentMailConfig.VerificationStatus.FAILED,
                verification_completed_at=now,
                verification_claim_token=None,
                verification_claim_expires_at=None,
                last_error=error,
                updated_at=now,
            )


def _reschedule_notification(notification_id, error, now):
    with transaction.atomic():
        current = Notification.objects.select_for_update().get(pk=notification_id)
        if current.status != Notification.Status.SENDING:
            return
        if current.retry_count >= len(RETRY_MINUTES):
            current.status = Notification.Status.FAILED
            current.next_attempt_at = None
        else:
            delay = RETRY_MINUTES[current.retry_count]
            current.retry_count += 1
            current.status = Notification.Status.PENDING
            current.next_attempt_at = now + timedelta(minutes=delay)
        current.last_error = error
        current.save(
            update_fields=[
                "status",
                "retry_count",
                "next_attempt_at",
                "last_error",
                "updated_at",
            ]
        )


def _fail_notification_permanently(notification_id, error):
    with transaction.atomic():
        current = Notification.objects.select_for_update().get(pk=notification_id)
        if current.status != Notification.Status.SENDING:
            return
        current.status = Notification.Status.PERMANENT_FAILED
        current.next_attempt_at = None
        current.last_error = error
        current.save(
            update_fields=["status", "next_attempt_at", "last_error", "updated_at"]
        )


def deliver_notification(notification_id, now=None):
    now = now or timezone.now()
    with opening_notification_transition(), transaction.atomic():
        notification = (
            Notification.objects.select_for_update()
            .select_related("task")
            .filter(pk=notification_id, status=Notification.Status.PENDING)
            .first()
        )
        if notification is None:
            return
        task = MonitorTask.objects.select_for_update().get(pk=notification.task_id)
        if notification.notification_type == Notification.Type.OPENING:
            if task.status != MonitorTask.Status.DETECTED:
                notification.status = Notification.Status.PERMANENT_FAILED
                notification.next_attempt_at = None
                notification.last_error = TASK_NO_LONGER_DETECTED
                notification.save(
                    update_fields=[
                        "status",
                        "next_attempt_at",
                        "last_error",
                        "updated_at",
                    ]
                )
                return
            if task.show_date < timezone.localdate(now):
                _expire_locked_task(task, now)
                return
        notification.status = Notification.Status.SENDING
        notification.message_id = notification.message_id or _message_id(notification)
        notification.next_attempt_at = None
        notification.save(
            update_fields=["status", "message_id", "next_attempt_at", "updated_at"]
        )

    config = AgentMailConfig.get_solo()
    if not config.is_verified:
        _park_for_mail_repair(
            notification_id,
            "agent-mail-not-verified",
            unverify_mail=False,
            now=now,
        )
        return
    notification.task = task
    outgoing = _build_message(notification, now)
    try:
        response = send_agent_mail(
            config.recipient_email, outgoing.subject, outgoing.body
        )
    except AgentMailUncertainError as exc:
        _mark_notification_needs_review(notification_id, str(exc))
        return
    except (AgentMailAuthError, AgentMailConfigError) as exc:
        _park_for_mail_repair(
            notification_id,
            safe_agent_mail_error_code(exc),
            unverify_mail=True,
            now=now,
        )
        return
    except AgentMailTemporaryError as exc:
        _reschedule_notification(notification_id, str(exc), now)
        return
    except AgentMailPermanentError as exc:
        _fail_notification_permanently(notification_id, str(exc))
        return

    with transaction.atomic():
        current = Notification.objects.select_for_update().get(pk=notification_id)
        if current.status != Notification.Status.SENDING:
            return
        current.status = Notification.Status.SENT
        current.transport_response = response
        current.last_error = ""
        current.next_attempt_at = None
        current.sent_at = now
        current.save(
            update_fields=[
                "status",
                "transport_response",
                "last_error",
                "next_attempt_at",
                "sent_at",
                "updated_at",
            ]
        )
        if current.notification_type == Notification.Type.OPENING:
            current_task = MonitorTask.objects.select_for_update().get(pk=current.task_id)
            if current_task.status == MonitorTask.Status.DETECTED:
                current_task.status = MonitorTask.Status.COMPLETED
                current_task.notified_at = now
                current_task.completed_at = now
                current_task.next_check_at = None
                current_task.save(
                    update_fields=[
                        "status",
                        "notified_at",
                        "completed_at",
                        "next_check_at",
                        "updated_at",
                    ]
                )


def dispatch_due_notifications(now=None):
    now = now or timezone.now()
    if not AgentMailConfig.objects.filter(is_verified=True).exists():
        return 0
    ids = list(
        Notification.objects.filter(status=Notification.Status.PENDING)
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[:1]
    )
    for notification_id in ids:
        deliver_notification(notification_id, now=now)
    return len(ids)
