from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from core.adapters.base import AdapterError
from core.adapters.maoyan import MaoyanAdapter
from core.forms import (
    MAOYAN_CITIES,
    AppSettingForm,
    InvitationForm,
    InvitationRegistrationForm,
    TaskConfirmForm,
    TaskPreviewForm,
)
from core.models import AgentMailConfig, AppSetting, Invitation, MonitorTask, Notification
from core.scheduler import wake_scheduler
from core.services.invitations import InvitationError, issue_invitation, valid_invitation
from core.services.mail_verification import request_mail_verification
from core.services.runtime_health import collect_runtime_status
from core.services.scheduling import next_check_at_for
from core.services.tasks import (
    PreviewCinema,
    TaskCreationError,
    TaskPreviewPayload,
    TaskTransitionError,
    cancel_task,
    create_task,
    payload_from_signed_preview,
    sign_preview,
)


def staff_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            return HttpResponseForbidden("此页面仅限管理员访问。")
        return view(request, *args, **kwargs)

    return wrapped


@login_required
def dashboard(request):
    active_statuses = [
        MonitorTask.Status.MONITORING,
        MonitorTask.Status.PAUSED,
        MonitorTask.Status.DETECTED,
        MonitorTask.Status.ERROR,
    ]
    task_query = MonitorTask.objects.filter(owner=request.user).prefetch_related(
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
    runtime_status = collect_runtime_status()
    runtime_status["tasks"] = {
        "monitoring": task_query.filter(status=MonitorTask.Status.MONITORING).count(),
        "error": task_query.filter(status=MonitorTask.Status.ERROR).count(),
        "detected": task_query.filter(status=MonitorTask.Status.DETECTED).count(),
        "pending_notifications": Notification.objects.filter(
            task__owner=request.user,
            status=Notification.Status.PENDING,
        ).count(),
    }
    return render(
        request,
        "core/dashboard.html",
        {
            "active_tasks": active_tasks,
            "recent_tasks": recent_tasks,
            "mail": AgentMailConfig.get_solo(),
            "setting": AppSetting.get_solo(),
            "runtime_status": runtime_status,
        },
    )


@staff_required
@require_http_methods(["GET"])
def mail_edit(request):
    config = AgentMailConfig.get_solo()
    return render(
        request,
        "core/mail_form.html",
        {
            "config": config,
            "runtime_status": collect_runtime_status(),
        },
    )


@staff_required
@require_POST
def mail_test(request):
    AgentMailConfig.objects.update_or_create(
        pk=1,
        defaults={"recipient_email": request.user.email},
    )
    request_mail_verification()
    return redirect("core:mail-edit")


@staff_required
def legacy_smtp_redirect(request):
    return redirect("core:mail-edit")


@staff_required
@require_http_methods(["GET", "POST"])
def settings_edit(request):
    setting = AppSetting.get_solo()
    form = AppSettingForm(request.POST or None, instance=setting)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            setting = form.save()
            now = timezone.now()
            monitoring_tasks = list(
                MonitorTask.objects.select_for_update().filter(
                    status=MonitorTask.Status.MONITORING
                )
            )
            for task in monitoring_tasks:
                task.next_check_at = next_check_at_for(task.show_date, now, setting)
                task.save(update_fields=["next_check_at", "updated_at"])
            transaction.on_commit(wake_scheduler)
        return redirect("core:settings")
    return render(request, "core/settings_form.html", {"form": form, "setting": setting})


@login_required
def task_detail(request, task_id):
    task = get_object_or_404(MonitorTask, pk=task_id, owner=request.user)
    checks = task.checks.order_by("-started_at", "-pk")[:20]
    notifications = task.notifications.order_by("-created_at", "-pk")[:20]
    return render(
        request,
        "core/task_detail.html",
        {"task": task, "checks": checks, "notifications": notifications},
    )


@login_required
@require_http_methods(["GET", "POST"])
def task_preview(request):
    if not AgentMailConfig.objects.filter(is_verified=True).exists():
        if request.method == "GET":
            if request.user.is_staff:
                return redirect("core:mail-edit")
            messages.error(request, "系统邮件尚未就绪，请联系管理员。")
            return redirect("core:dashboard")
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


@login_required
@require_POST
def task_confirm(request):
    if not AgentMailConfig.objects.filter(is_verified=True).exists():
        return HttpResponseForbidden("请先验证 Agent Mail 配置。")

    form = TaskConfirmForm(request.POST)
    payload = None
    if form.is_valid():
        try:
            task = create_task(
                form.cleaned_data["signed_preview"],
                form.cleaned_data["cinema_id"],
                form.cleaned_data["manual_cinema_name"],
                owner=request.user,
            )
        except (TaskCreationError, signing.BadSignature) as exc:
            form.add_error(None, str(exc) or "预览数据无效，请重新预览。")
        else:
            return redirect("core:task-detail", task_id=task.pk)

    signed_preview = form.cleaned_data.get("signed_preview")
    if signed_preview:
        try:
            payload = payload_from_signed_preview(signed_preview)
        except (TaskCreationError, signing.BadSignature):
            pass
    return HttpResponseBadRequest(
        render(
            request,
            "core/task_confirm.html",
            {"form": form, "payload": payload},
        ).content
    )


def _transition_conflict():
    return HttpResponse("任务当前状态不允许此操作。", status=409)


def _lifecycle_redirect(task_id):
    return redirect("core:task-detail", task_id=task_id)


@login_required
@require_POST
def task_pause(request, task_id):
    with transaction.atomic():
        task = get_object_or_404(
            MonitorTask.objects.select_for_update(),
            pk=task_id,
            owner=request.user,
        )
        if task.status not in {MonitorTask.Status.MONITORING, MonitorTask.Status.ERROR}:
            return _transition_conflict()
        task.status = MonitorTask.Status.PAUSED
        task.next_check_at = None
        task.claim_token = None
        task.claim_expires_at = None
        task.save(
            update_fields=[
                "status",
                "next_check_at",
                "claim_token",
                "claim_expires_at",
                "updated_at",
            ]
        )
    return _lifecycle_redirect(task_id)


@login_required
@require_POST
def task_resume(request, task_id):
    with transaction.atomic():
        task = get_object_or_404(
            MonitorTask.objects.select_for_update(),
            pk=task_id,
            owner=request.user,
        )
        if task.status not in {MonitorTask.Status.PAUSED, MonitorTask.Status.ERROR}:
            return _transition_conflict()
        task.status = MonitorTask.Status.MONITORING
        task.consecutive_failures = 0
        task.consecutive_terminal_failures = 0
        task.last_error = ""
        task.next_check_at = timezone.now()
        task.claim_token = None
        task.claim_expires_at = None
        task.save(
            update_fields=[
                "status",
                "consecutive_failures",
                "consecutive_terminal_failures",
                "last_error",
                "next_check_at",
                "claim_token",
                "claim_expires_at",
                "updated_at",
            ]
        )
        transaction.on_commit(wake_scheduler)
    return _lifecycle_redirect(task_id)


@login_required
@require_POST
def task_cancel(request, task_id):
    try:
        cancel_task(task_id, now=timezone.now(), owner=request.user)
    except MonitorTask.DoesNotExist as exc:
        raise Http404 from exc
    except TaskTransitionError:
        return _transition_conflict()
    return _lifecycle_redirect(task_id)


@login_required
@require_POST
def task_run_now(request, task_id):
    with transaction.atomic():
        task = get_object_or_404(
            MonitorTask.objects.select_for_update(),
            pk=task_id,
            owner=request.user,
        )
        if task.status != MonitorTask.Status.MONITORING:
            return _transition_conflict()
        task.next_check_at = timezone.now()
        task.save(update_fields=["next_check_at", "updated_at"])
        transaction.on_commit(wake_scheduler)
    return _lifecycle_redirect(task_id)


@staff_required
@never_cache
@require_http_methods(["GET", "POST"])
def invitations(request):
    issued_url = None
    form = InvitationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            issued = issue_invitation(form.cleaned_data["email"], request.user)
        except InvitationError as exc:
            form.add_error("email", str(exc))
        else:
            path = reverse("core:invite-register", args=[issued.token])
            issued_url = request.build_absolute_uri(path)
            form = InvitationForm()
    invite_rows = Invitation.objects.select_related("accepted_by", "created_by")[:50]
    return render(
        request,
        "core/invitations.html",
        {
            "form": form,
            "invitations": invite_rows,
            "issued_url": issued_url,
            "now": timezone.now(),
        },
    )


@never_cache
@sensitive_post_parameters("password1", "password2")
@require_http_methods(["GET", "POST"])
def invite_register(request, token):
    if request.user.is_authenticated:
        return redirect("core:dashboard")
    invitation = valid_invitation(token)
    if invitation is None:
        return render(request, "registration/invite_invalid.html", status=410)
    form = InvitationRegistrationForm(
        request.POST or None,
        invitation=invitation,
    )
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                locked = valid_invitation(token, for_update=True)
                if locked is None:
                    raise InvitationError("邀请链接已失效。")
                if get_user_model().objects.filter(email__iexact=locked.email).exists():
                    raise InvitationError("该邮箱已经注册。")
                form.invitation = locked
                user = form.save()
                locked.accepted_by = user
                locked.accepted_at = timezone.now()
                locked.save(update_fields=["accepted_by", "accepted_at"])
        except (InvitationError, IntegrityError) as exc:
            form.add_error(None, str(exc) or "注册失败，请联系管理员重新邀请。")
        else:
            login(request, user)
            return redirect("core:dashboard")
    return render(
        request,
        "registration/invite_register.html",
        {"form": form, "invitation": invitation},
    )


@staff_required
@require_POST
def invitation_revoke(request, invitation_id):
    with transaction.atomic():
        invitation = get_object_or_404(
            Invitation.objects.select_for_update(),
            pk=invitation_id,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
        )
        invitation.revoked_at = timezone.now()
        invitation.save(update_fields=["revoked_at"])
    return redirect("core:invitations")


@staff_required
@require_http_methods(["GET"])
def users(request):
    user_rows = get_user_model().objects.order_by("-is_active", "date_joined", "email")
    return render(request, "core/users.html", {"users": user_rows})


@staff_required
@require_POST
def user_toggle(request, user_id):
    if request.user.pk == user_id:
        return HttpResponse("不能停用当前登录账号。", status=409)
    User = get_user_model()
    with transaction.atomic():
        tasks = list(
            MonitorTask.objects.select_for_update()
            .filter(owner_id=user_id)
            .order_by("pk")
        )
        target = get_object_or_404(User.objects.select_for_update(), pk=user_id)
        target.is_active = not target.is_active
        target.save(update_fields=["is_active"])
        if not target.is_active:
            for task in tasks:
                if task.status in {MonitorTask.Status.MONITORING, MonitorTask.Status.ERROR}:
                    task.status = MonitorTask.Status.PAUSED
                    task.next_check_at = None
                    task.claim_token = None
                    task.claim_expires_at = None
                    task.save(
                        update_fields=[
                            "status",
                            "next_check_at",
                            "claim_token",
                            "claim_expires_at",
                            "updated_at",
                        ]
                    )
    return redirect("core:users")
