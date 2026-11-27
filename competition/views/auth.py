"""账号相关页面：登录、登出、改密、自助注册。"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth.views import LoginView, LogoutView, PasswordChangeView
from django.http import HttpRequest
from django.shortcuts import redirect, render
from django.urls import reverse_lazy

from competition.config import load_config
from competition.forms import LoginForm, RegistrationForm
from competition.models import AppSetting
from competition.services import registration as registration_service


class CompetitionLoginView(LoginView):
    template_name = "login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def form_valid(self, form):
        user = form.get_user()
        profile = getattr(user, "profile", None)

        response = super().form_valid(form)
        if profile and profile.must_change_password:
            messages.info(self.request, "首次登录，请先修改初始密码。")
            return redirect("password_change")
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        config = load_config()
        # Django 的 LoginView 会注入自己的 "site"(RequestSite) 覆盖同名变量，
        # 因此站点信息另用 brand 键传给模板。
        ctx["brand"] = config
        ctx["tasks"] = config.tasks
        try:
            ctx["registration_open"] = AppSetting.get().registration_open
        except Exception:  # noqa: BLE001 - 数据库未初始化时仍可显示登录页
            ctx["registration_open"] = False
        return ctx


class CompetitionLogoutView(LogoutView):
    next_page = reverse_lazy("login")


class CompetitionPasswordChangeView(PasswordChangeView):
    template_name = "password_change.html"
    success_url = reverse_lazy("home")

    def form_valid(self, form):
        response = super().form_valid(form)
        profile = getattr(self.request.user, "profile", None)
        if profile and profile.must_change_password:
            profile.must_change_password = False
            profile.save(update_fields=["must_change_password"])
        messages.success(self.request, "密码已更新。")
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["tasks"] = load_config().tasks
        return ctx


def register_view(request: HttpRequest):
    """学生用学号自助注册；教师面板只控制「开放/关闭」。"""
    config = load_config()
    if request.user.is_authenticated:
        return redirect("home")

    setting = AppSetting.get()
    form = None

    if setting.registration_open:
        form = RegistrationForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            try:
                profile = registration_service.register_student(
                    form.cleaned_data["student_id"],
                    form.cleaned_data["real_name"],
                    form.cleaned_data["password1"],
                    setting=setting,
                )
            except registration_service.RegistrationError as exc:
                form.add_error(None, str(exc))
            else:
                auth_login(
                    request, profile.user, backend="django.contrib.auth.backends.ModelBackend"
                )
                messages.success(
                    request, f"注册成功，你的参赛编号是 {profile.contest_code}。"
                )
                return redirect("home")

    return render(
        request,
        "register.html",
        {
            "brand": config,
            "tasks": config.tasks,
            "form": form,
            "open": setting.registration_open,
        },
        status=200 if setting.registration_open else 403,
    )
