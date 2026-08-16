from django.core import signing
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from core.adapters.base import AdapterError
from core.adapters.maoyan import MaoyanAdapter
from core.forms import MAOYAN_CITIES, SMTPConfigForm, TaskConfirmForm, TaskPreviewForm
from core.models import MonitorTask, Notification, SMTPConfig
from core.scheduler import wake_scheduler
from core.services.notifications import TASK_NO_LONGER_DETECTED
from core.services.smtp import sanitize_smtp_error, test_smtp_config
from core.services.tasks import (
    PreviewCinema,
    TaskCreationError,
    TaskPreviewPayload,
    create_task,
    sign_preview,
)


@require_http_methods(["GET", "POST"])
def smtp_edit(request):
    config = SMTPConfig.get_solo()
    form = SMTPConfigForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        config = form.save()
        try:
            test_smtp_config(config)
        except Exception as exc:
            config.is_verified = False
            config.verified_at = None
            config.last_error = sanitize_smtp_error(exc)
            config.save()
            return render(request, "core/smtp_form.html", {"form": form, "config": config})

        config.is_verified = True
        config.verified_at = timezone.now()
        config.last_error = ""
        config.save()
        return redirect("core:smtp-edit")

    return render(request, "core/smtp_form.html", {"form": form, "config": config})


@require_POST
def smtp_test(request):
    config = SMTPConfig.get_solo()
    try:
        test_smtp_config(config)
    except Exception as exc:
        config.is_verified = False
        config.verified_at = None
        config.last_error = sanitize_smtp_error(exc)
        config.save()
        return render(
            request,
            "core/smtp_form.html",
            {"form": SMTPConfigForm(instance=config), "config": config},
        )

    config.is_verified = True
    config.verified_at = timezone.now()
    config.last_error = ""
    config.save()
    return redirect("core:smtp-edit")


@require_http_methods(["GET", "POST"])
def task_preview(request):
    if not SMTPConfig.objects.filter(is_verified=True).exists():
        return HttpResponseForbidden("请先验证 SMTP 配置。")

    form = TaskPreviewForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        city_id = form.cleaned_data["city_id"]
        city_name = MAOYAN_CITIES[city_id]
        adapter = MaoyanAdapter()
        try:
            target = adapter.validate_target(form.cleaned_data["source_url"], city_id, city_name)
            result = adapter.fetch(target)
        except AdapterError as exc:
            form.add_error(None, str(exc))
            return HttpResponseBadRequest(
                render(request, "core/task_preview_form.html", {"form": form}).content
            )
        payload = TaskPreviewPayload(
            city_id=target.city_id,
            city_name=target.city_name,
            source_url=target.source_url,
            normalized_url=target.normalized_url,
            query_key=target.query_key,
            movie_id=target.movie_id,
            movie_name=result.movie_name,
            show_date=target.show_date.isoformat(),
            cinemas=tuple(
                PreviewCinema(id=cinema.cinema_id, name=cinema.name)
                for cinema in result.cinemas
            ),
        )
        signed_preview = sign_preview(payload)
        confirm_form = TaskConfirmForm(initial={"signed_preview": signed_preview})
        return render(
            request,
            "core/task_confirm.html",
            {"form": confirm_form, "payload": payload, "signed_preview": signed_preview},
        )

    return render(request, "core/task_preview_form.html", {"form": form})


@require_POST
def task_confirm(request):
    if not SMTPConfig.objects.filter(is_verified=True).exists():
        return HttpResponseForbidden("请先验证 SMTP 配置。")

    form = TaskConfirmForm(request.POST)
    if form.is_valid():
        try:
            create_task(
                form.cleaned_data["signed_preview"],
                form.cleaned_data["cinema_id"],
                form.cleaned_data["manual_cinema_name"],
            )
        except (TaskCreationError, signing.BadSignature) as exc:
            form.add_error(None, str(exc) or "预览数据无效，请重新预览。")
        else:
            return redirect("core:task-preview")

    return HttpResponseBadRequest(render(request, "core/task_confirm.html", {"form": form}).content)


def _transition_conflict():
    return HttpResponse("任务当前状态不允许此操作。", status=409)


def _lifecycle_redirect():
    return redirect("core:task-preview")


@require_POST
def task_pause(request, task_id):
    with transaction.atomic():
        task = get_object_or_404(
            MonitorTask.objects.select_for_update(),
            pk=task_id,
        )
        if task.status not in {MonitorTask.Status.MONITORING, MonitorTask.Status.ERROR}:
            return _transition_conflict()
        task.status = MonitorTask.Status.PAUSED
        task.next_check_at = None
        task.save(update_fields=["status", "next_check_at", "updated_at"])
    return _lifecycle_redirect()


@require_POST
def task_resume(request, task_id):
    with transaction.atomic():
        task = get_object_or_404(
            MonitorTask.objects.select_for_update(),
            pk=task_id,
        )
        if task.status not in {MonitorTask.Status.PAUSED, MonitorTask.Status.ERROR}:
            return _transition_conflict()
        task.status = MonitorTask.Status.MONITORING
        task.consecutive_failures = 0
        task.last_error = ""
        task.next_check_at = timezone.now()
        task.save(
            update_fields=[
                "status",
                "consecutive_failures",
                "last_error",
                "next_check_at",
                "updated_at",
            ]
        )
        transaction.on_commit(wake_scheduler)
    return _lifecycle_redirect()


@require_POST
def task_cancel(request, task_id):
    unfinished_statuses = {
        MonitorTask.Status.MONITORING,
        MonitorTask.Status.PAUSED,
        MonitorTask.Status.DETECTED,
        MonitorTask.Status.ERROR,
    }
    now = timezone.now()
    with transaction.atomic():
        pending_opening = (
            Notification.objects.select_for_update()
            .filter(
                task_id=task_id,
                notification_type=Notification.Type.OPENING,
                status=Notification.Status.PENDING,
            )
            .first()
        )
        task = get_object_or_404(
            MonitorTask.objects.select_for_update(),
            pk=task_id,
        )
        if task.status not in unfinished_statuses:
            return _transition_conflict()
        task.status = MonitorTask.Status.CANCELLED
        task.cancelled_at = now
        task.next_check_at = None
        task.save(
            update_fields=["status", "cancelled_at", "next_check_at", "updated_at"]
        )
        if pending_opening is not None:
            pending_opening.status = Notification.Status.PERMANENT_FAILED
            pending_opening.next_attempt_at = None
            pending_opening.last_error = TASK_NO_LONGER_DETECTED
            pending_opening.save(
                update_fields=[
                    "status",
                    "next_attempt_at",
                    "last_error",
                    "updated_at",
                ]
            )
    return _lifecycle_redirect()


@require_POST
def task_run_now(request, task_id):
    with transaction.atomic():
        task = get_object_or_404(
            MonitorTask.objects.select_for_update(),
            pk=task_id,
        )
        if task.status != MonitorTask.Status.MONITORING:
            return _transition_conflict()
        task.next_check_at = timezone.now()
        task.save(update_fields=["next_check_at", "updated_at"])
        transaction.on_commit(wake_scheduler)
    return _lifecycle_redirect()
