"""运维探活接口（供 systemd / 反向代理健康检查使用）。"""
from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.views.decorators.http import require_GET


@require_GET
def healthz(request: HttpRequest):
    return HttpResponse("ok", content_type="text/plain")
