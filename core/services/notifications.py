import smtplib
from datetime import timedelta
from email.message import EmailMessage

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.crypto import CredentialKeyError
from core.models import MonitorTask, Notification, SMTPConfig
from core.services.smtp import sanitize_smtp_error, send_message
from core.services.tasks import TASK_NO_LONGER_DETECTED, opening_notification_transition

RETRY_MINUTES = (1, 5, 15, 30, 60)
EXPIRED_BEFORE_DELIVERY = "expired-before-delivery"

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
        task.save(update_fields=["status", "expired_at", "next_check_at", "updated_at"])
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
        task = (
            MonitorTask.objects.select_for_update()
            .filter(status__in=EXPIRABLE_TASK_STATUSES, show_date__lt=local_date)
            .order_by("created_at")
            .first()
        )
        if task is None:
            return False
        _expire_locked_task(task, now)
    return True


def _message_id(notification):
    label = notification.notification_type.lower()
    return f"<{label}-{notification.pk}@ticketwatch.local>"


def _local_timestamp(value):
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S %Z")


def _build_message(notification, config, now):
    task = notification.task
    message = EmailMessage()
    message["From"] = config.from_email
    message["To"] = config.recipient_email
    message["Message-ID"] = notification.message_id
    if notification.notification_type == Notification.Type.OPENING:
        message["Subject"] = f"[TicketWatch] {task.movie_name} 已在 {task.cinema_name} 开票"
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
    else:
        message["Subject"] = f"[TicketWatch] {task.movie_name} 监控已到期"
        event_time = task.expired_at or now
        lines = [
            f"电影：{task.movie_name}",
            f"日期：{task.show_date.isoformat()}",
            f"城市：{task.city_name}",
            f"影院：{task.cinema_name}",
            f"到期时间：{_local_timestamp(event_time)}",
            f"监控链接：{task.source_url}",
        ]
    message.set_content("\n".join(lines))
    return message


def _park_for_smtp_repair(notification_id, error, *, unverify_smtp):
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
        if unverify_smtp:
            SMTPConfig.objects.filter(pk=1).update(
                is_verified=False,
                verified_at=None,
                last_error=error,
                updated_at=timezone.now(),
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

    config = SMTPConfig.get_solo()
    if not config.is_verified:
        _park_for_smtp_repair(notification_id, "smtp-not-verified", unverify_smtp=False)
        return
    notification.task = task
    try:
        send_message(config, _build_message(notification, config, now))
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused) as exc:
        _park_for_smtp_repair(
            notification_id, sanitize_smtp_error(exc), unverify_smtp=True
        )
        return
    except CredentialKeyError:
        _park_for_smtp_repair(
            notification_id, "smtp-credential-unavailable", unverify_smtp=True
        )
        return
    except (OSError, smtplib.SMTPException) as exc:
        _reschedule_notification(notification_id, sanitize_smtp_error(exc), now)
        return

    with transaction.atomic():
        current = Notification.objects.select_for_update().get(pk=notification_id)
        if current.status != Notification.Status.SENDING:
            return
        current.status = Notification.Status.SENT
        current.smtp_response = "accepted"
        current.last_error = ""
        current.next_attempt_at = None
        current.sent_at = now
        current.save(
            update_fields=[
                "status",
                "smtp_response",
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
    if not SMTPConfig.objects.filter(is_verified=True).exists():
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
