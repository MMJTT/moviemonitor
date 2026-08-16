from datetime import timedelta

from django.core import signing
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from core.adapters.base import AdapterError
from core.adapters.maoyan import MaoyanAdapter
from core.forms import (
    MAOYAN_CITIES,
    AppSettingForm,
    SMTPConfigForm,
    TaskConfirmForm,
    TaskPreviewForm,
)
from core.models import AppSetting, MonitorTask, SMTPConfig
from core.scheduler import wake_scheduler
from core.services.smtp import sanitize_smtp_error, test_smtp_config
from core.services.tasks import (
    PreviewCinema,
    TaskCreationError,
    TaskPreviewPayload,
    TaskTransitionError,
    cancel_task,
    create_task,
    sign_preview,
    task_transition,
)


def dashboard(request):
    task = MonitorTask.objects.order_by("-created_at").first()
    notification = None
    if task is not None:
        notification = task.notifications.order_by("-created_at", "-pk").first()
    return render(
        request,
        "core/dashboard.html",
        {
            "task": task,
            "notification": notification,
            "smtp": SMTPConfig.get_solo(),
            "setting": AppSetting.get_solo(),
        },
    )


@require_http_methods(["GET", "POST"])
def settings_edit(request):
    setting = AppSetting.get_solo()
    form = AppSettingForm(request.POST or None, instance=setting)
    if request.method == "POST" and form.is_valid():
        with task_transition(), transaction.atomic():
            setting = form.save()
            now = timezone.now()
            MonitorTask.objects.filter(status=MonitorTask.Status.MONITORING).update(
                next_check_at=now + timedelta(seconds=setting.poll_interval_seconds),
                updated_at=now,
            )
            transaction.on_commit(wake_scheduler)
        return redirect("core:settings")
    return render(request, "core/settings_form.html", {"form": form, "setting": setting})


def task_detail(request, task_id):
    task = get_object_or_404(MonitorTask, pk=task_id)
    checks = task.checks.order_by("-started_at", "-pk")[:20]
    notifications = task.notifications.order_by("-created_at", "-pk")[:20]
    return render(
        request,
        "core/task_detail.html",
        {"task": task, "checks": checks, "notifications": notifications},
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
        if request.method == "GET":
            return redirect("core:smtp-edit")
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
            task = create_task(
                form.cleaned_data["signed_preview"],
                form.cleaned_data["cinema_id"],
                form.cleaned_data["manual_cinema_name"],
            )
        except (TaskCreationError, signing.BadSignature) as exc:
            form.add_error(None, str(exc) or "预览数据无效，请重新预览。")
        else:
            return redirect("core:task-detail", task_id=task.pk)

    return HttpResponseBadRequest(render(request, "core/task_confirm.html", {"form": form}).content)


def _transition_conflict():
    return HttpResponse("任务当前状态不允许此操作。", status=409)


def _lifecycle_redirect(task_id):
    return redirect("core:task-detail", task_id=task_id)


@require_POST
def task_pause(request, task_id):
    with task_transition(), transaction.atomic():
        task = get_object_or_404(
            MonitorTask.objects.select_for_update(),
            pk=task_id,
        )
        if task.status not in {MonitorTask.Status.MONITORING, MonitorTask.Status.ERROR}:
            return _transition_conflict()
        task.status = MonitorTask.Status.PAUSED
        task.next_check_at = None
        task.save(update_fields=["status", "next_check_at", "updated_at"])
    return _lifecycle_redirect(task_id)


@require_POST
def task_resume(request, task_id):
    with task_transition(), transaction.atomic():
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
    return _lifecycle_redirect(task_id)


@require_POST
def task_cancel(request, task_id):
    try:
        cancel_task(task_id, now=timezone.now())
    except MonitorTask.DoesNotExist as exc:
        raise Http404 from exc
    except TaskTransitionError:
        return _transition_conflict()
    return _lifecycle_redirect(task_id)


@require_POST
def task_run_now(request, task_id):
    with task_transition(), transaction.atomic():
        task = get_object_or_404(
            MonitorTask.objects.select_for_update(),
            pk=task_id,
        )
        if task.status != MonitorTask.Status.MONITORING:
            return _transition_conflict()
        task.next_check_at = timezone.now()
        task.save(update_fields=["next_check_at", "updated_at"])
        transaction.on_commit(wake_scheduler)
    return _lifecycle_redirect(task_id)
