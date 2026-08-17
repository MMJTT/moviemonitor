import uuid

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class SingletonModel(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.id = 1
        self.full_clean()
        return super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        value, _ = cls.objects.get_or_create(pk=1)
        return value


class AppSetting(SingletonModel):
    poll_interval_seconds = models.PositiveIntegerField(
        default=60, validators=[MinValueValidator(60)]
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "应用设置"


class AgentMailConfig(SingletonModel):
    sender_email = models.EmailField(default="mijiatong@agent.qq.com", editable=False)
    recipient_email = models.EmailField(blank=True)
    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "Agent Mail 配置"


class MonitorTask(models.Model):
    class Status(models.TextChoices):
        MONITORING = "MONITORING", "监控中"
        PAUSED = "PAUSED", "已暂停"
        DETECTED = "DETECTED", "已检测开票"
        COMPLETED = "COMPLETED", "已完成"
        EXPIRED = "EXPIRED", "已过期"
        CANCELLED = "CANCELLED", "已取消"
        ERROR = "ERROR", "异常"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_url = models.URLField(max_length=1000)
    normalized_url = models.URLField(max_length=1000)
    query_key = models.CharField(max_length=128, db_index=True)
    city_id = models.PositiveIntegerField()
    city_name = models.CharField(max_length=80)
    movie_id = models.CharField(max_length=32)
    movie_name = models.CharField(max_length=200)
    show_date = models.DateField()
    cinema_id = models.CharField(max_length=32, blank=True)
    cinema_name = models.CharField(max_length=200)
    normalized_cinema_name = models.CharField(max_length=200)
    booking_url = models.URLField(max_length=1000, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.MONITORING)
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=200, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    next_check_at = models.DateTimeField(null=True, blank=True, db_index=True)
    detected_at = models.DateTimeField(null=True, blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["city_id", "movie_id", "show_date", "normalized_cinema_name"],
                condition=Q(status__in=["MONITORING", "PAUSED", "DETECTED", "ERROR"]),
                name="one_unfinished_task_per_target",
            )
        ]

    def __str__(self):
        return f"{self.movie_name} - {self.city_name} - {self.cinema_name}"


class CheckRun(models.Model):
    class Status(models.TextChoices):
        SUCCEEDED = "SUCCEEDED", "成功"
        TEMPORARY_ERROR = "TEMPORARY_ERROR", "临时错误"
        RATE_LIMITED = "RATE_LIMITED", "限流"
        STRUCTURE_ERROR = "STRUCTURE_ERROR", "结构错误"
        CONFIG_ERROR = "CONFIG_ERROR", "配置错误"

    task = models.ForeignKey(MonitorTask, on_delete=models.CASCADE, related_name="checks")
    status = models.CharField(max_length=24, choices=Status.choices)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField()
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    content_fingerprint = models.CharField(max_length=64, blank=True)
    cinema_count = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=80, blank=True)
    error_summary = models.CharField(max_length=200, blank=True)

    def __str__(self):
        return f"{self.task} - {self.get_status_display()}"


class Notification(models.Model):
    class Type(models.TextChoices):
        OPENING = "OPENING", "开票"
        EXPIRY = "EXPIRY", "到期"

    class Status(models.TextChoices):
        PENDING = "PENDING", "待发送"
        SENDING = "SENDING", "发送中"
        SENT = "SENT", "成功"
        FAILED = "FAILED", "失败"
        PERMANENT_FAILED = "PERMANENT_FAILED", "永久失败"

    task = models.ForeignKey(MonitorTask, on_delete=models.CASCADE, related_name="notifications")
    notification_type = models.CharField(max_length=16, choices=Type.choices)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    retry_count = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    message_id = models.CharField(max_length=255, blank=True)
    transport_response = models.CharField(max_length=200, blank=True)
    last_error = models.CharField(max_length=200, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["task", "notification_type"], name="unique_task_notification_type"
            )
        ]

    def __str__(self):
        return f"{self.task} - {self.get_notification_type_display()}"
