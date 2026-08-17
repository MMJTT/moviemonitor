from datetime import timedelta

from django.core import signing
from django.db import transaction
from django.db.models import Prefetch
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from core.adapters.base import AdapterError
from core.adapters.maoyan import MaoyanAdapter
from core.forms import (
    MAOYAN_CITIES,
    AgentMailConfigForm,
    AppSettingForm,
    TaskConfirmForm,
    TaskPreviewForm,
)
from core.models import AgentMailConfig, AppSetting, MonitorTask, Notification
from core.scheduler import wake_scheduler
from core.services.agent_mail import AgentMailError, test_agent_mail_config
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
    active_statuses = [
        MonitorTask.Status.MONITORING,
        MonitorTask.Status.PAUSED,
        MonitorTask.Status.DETECTED,
        MonitorTask.Status.ERROR,
    ]
    task_query = MonitorTask.objects.prefetch_related(
        Prefetch(
            "notifications",
            queryset=Notification.objects.order_by("-created_at", "-pk"),
            to_attr="dashboard_notifications",
        )
    )
    active_tasks = task_query.filter(status__in=active_statuses).order_by(
        "show_date", "created_at"
    )
    recent_tasks = task_query.exclude(status__in=active_statuses).order_by("-updated_at")[:10]
    return render(
        request,
        "core/dashboard.html",
        {
            "active_tasks": active_tasks,
            "recent_tasks": recent_tasks,
            "mail": AgentMailConfig.get_solo(),
            "setting": AppSetting.get_solo(),
        },
    )


def _mark_agent_mail_verified(config):
    now = timezone.now()
    with transaction.atomic():
        updated = AgentMailConfig.objects.filter(
            pk=config.pk,
            recipient_email=config.recipient_email,
            updated_at=config.updated_at,
        ).update(
            is_verified=True,
            verified_at=now,
            last_error="",
            updated_at=now,
        )
        if updated != 1:
            return False
        Notification.objects.filter(status=Notification.Status.PENDING).update(
            next_attempt_at=now,
            updated_at=now,
        )
        transaction.on_commit(wake_scheduler)
    return True


def _mark_agent_mail_failed(config, error):
    now = timezone.now()
    return (
        AgentMailConfig.objects.filter(
            pk=config.pk,
            recipient_email=config.recipient_email,
            updated_at=config.updated_at,
        ).update(
            is_verified=False,
            verified_at=None,
            last_error=error,
            updated_at=now,
        )
        == 1
    )


def _begin_agent_mail_test(config):
    config.is_verified = False
    config.verified_at = None
    config.last_error = ""
    config.save()
    return config


@require_http_methods(["GET", "POST"])
def mail_edit(request):
    config = AgentMailConfig.get_solo()
    form = AgentMailConfigForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        config = _begin_agent_mail_test(form.save(commit=False))
        try:
            test_agent_mail_config(config)
        except AgentMailError as exc:
            _mark_agent_mail_failed(config, str(exc))
        else:
            _mark_agent_mail_verified(config)
            return redirect("core:mail-edit")
        config = AgentMailConfig.get_solo()
        form = AgentMailConfigForm(instance=config)
    return render(request, "core/mail_form.html", {"form": form, "config": config})


@require_POST
def mail_test(request):
    config = _begin_agent_mail_test(AgentMailConfig.get_solo())
    try:
        test_agent_mail_config(config)
    except AgentMailError as exc:
        _mark_agent_mail_failed(config, str(exc))
    else:
        _mark_agent_mail_verified(config)
    return redirect("core:mail-edit")


def legacy_smtp_redirect(request):
    return redirect("core:mail-edit")


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
def task_preview(request):
    if not AgentMailConfig.objects.filter(is_verified=True).exists():
        if request.method == "GET":
            return redirect("core:mail-edit")
        return HttpResponseForbidden("请先验证 Agent Mail 配置。")

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
    if not AgentMailConfig.objects.filter(is_verified=True).exists():
        return HttpResponseForbidden("请先验证 Agent Mail 配置。")

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
