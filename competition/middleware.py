"""账号状态拦截：待审核 / 已拒绝的账号不能使用站内功能。"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect

from competition.models import Profile

# 这些路径不拦截，否则会把自己挡在登录页外面
SKIP_PREFIXES = ("/static/", "/login/", "/register/", "/healthz", "/admin/")


class ProfileStatusMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and not user.is_staff:
            profile = getattr(user, "profile", None)
            if profile is not None and profile.status != Profile.STATUS_ACTIVE:
                logout(request)
                if request.path.startswith(SKIP_PREFIXES):
                    return self.get_response(request)
                messages.error(request, "账号尚未通过教师审核，暂时无法使用。")
                return redirect("login")
        return self.get_response(request)
