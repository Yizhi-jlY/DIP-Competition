"""表单。"""
from __future__ import annotations

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError

from competition.services import registration as registration_service


class LoginForm(AuthenticationForm):
    username = forms.CharField(
        label="学号 / 账号",
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "placeholder": "请输入学号",
                "autocomplete": "username",
                "autofocus": True,
            }
        ),
    )
    password = forms.CharField(
        label="密码",
        widget=forms.PasswordInput(
            attrs={"class": "input", "placeholder": "请输入密码", "autocomplete": "current-password"}
        ),
    )


class UploadForm(forms.Form):
    image_key = forms.CharField(widget=forms.HiddenInput)
    file = forms.FileField(
        label="结果图像",
        widget=forms.ClearableFileInput(attrs={"class": "file-input", "accept": ".png,image/png"}),
    )

    def clean_file(self):
        f = self.cleaned_data["file"]
        if f.size and f.size > 20 * 1024 * 1024:
            raise forms.ValidationError("文件不能超过 20 MB。")
        return f


class RegistrationForm(forms.Form):
    """学号 + 姓名 + 密码。"""

    student_id = forms.CharField(
        label="学号",
        max_length=32,
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "placeholder": "请输入学号",
                "autocomplete": "username",
                "inputmode": "numeric",
                "autofocus": True,
            }
        ),
    )
    real_name = forms.CharField(
        label="姓名",
        max_length=64,
        widget=forms.TextInput(attrs={"class": "input", "placeholder": "请输入真实姓名"}),
    )
    password1 = forms.CharField(
        label="设置密码",
        widget=forms.PasswordInput(
            attrs={"class": "input", "placeholder": "至少 8 位", "autocomplete": "new-password"}
        ),
    )
    password2 = forms.CharField(
        label="确认密码",
        widget=forms.PasswordInput(
            attrs={"class": "input", "placeholder": "再输入一次", "autocomplete": "new-password"}
        ),
    )

    def clean_student_id(self):
        try:
            return registration_service.check_student_id(self.cleaned_data["student_id"])
        except registration_service.RegistrationError as exc:
            raise forms.ValidationError(str(exc)) from exc

    def clean_real_name(self):
        name = (self.cleaned_data["real_name"] or "").strip()
        if len(name) < 2:
            raise forms.ValidationError("请填写真实姓名（至少 2 个字符）。")
        return name

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password1")
        p2 = cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "两次输入的密码不一致。")
        if p1:
            dummy = User(username=cleaned.get("student_id") or "")
            try:
                validate_password(p1, user=dummy)
            except DjangoValidationError as exc:
                self.add_error("password1", " ".join(exc.messages))
        return cleaned
