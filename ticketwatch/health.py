from django.http import JsonResponse

from core.services.runtime_health import collect_runtime_status


def healthz(request):
    return JsonResponse({"status": "ok"})


def statusz(request):
    return JsonResponse(collect_runtime_status())
