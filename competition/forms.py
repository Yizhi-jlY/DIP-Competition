"""表单。"""
from __future__ import annotations

from django import forms
from django.contrib.auth.forms import AuthenticationForm


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
