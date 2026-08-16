from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from core.forms import SMTPConfigForm
from core.models import SMTPConfig
from core.services.smtp import sanitize_smtp_error, test_smtp_config


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
