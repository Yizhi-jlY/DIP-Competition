"""学号自助注册。

规则全部来自教师面板的「注册设置」：
    开放注册 / 邀请码 / 学号前缀 / 是否需要审核 / 注册后是否强制改密。

注册成功即建立 User + Profile，并按现有参赛编号规则分配下一个编号；
如果教师要求审核，账号先进入 pending，登录会被拦截，直到教师在面板中通过。
"""
from __future__ import annotations

import re
import secrets
from typing import Optional

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction

from competition.config import load_config
from competition.models import AppSetting, Profile

STUDENT_ID_RE = re.compile(r"^\d{4,20}$")


class RegistrationError(ValueError):
    """注册校验失败，消息可直接展示给学生。"""


def policy() -> AppSetting:
    return AppSetting.get()


def next_contest_code(prefix: Optional[str] = None) -> str:
    """按已有编号取下一个，如 S001 → S002。"""
    prefix = prefix if prefix is not None else load_config().contest_code_prefix
    used = Profile.objects.filter(contest_code__startswith=prefix).values_list(
        "contest_code", flat=True
    )
    max_no = 0
    for code in used:
        tail = code[len(prefix):]
        if tail.isdigit():
            max_no = max(max_no, int(tail))
    return f"{prefix}{max_no + 1:03d}"


def check_open(setting: Optional[AppSetting] = None) -> None:
    setting = setting or policy()
    if not setting.registration_open:
        raise RegistrationError("当前未开放自助注册，请联系任课教师。")


def check_code(value: str, setting: Optional[AppSetting] = None) -> None:
    setting = setting or policy()
    expected = (setting.registration_code or "").strip()
    if expected and not secrets.compare_digest((value or "").strip(), expected):
        raise RegistrationError("注册码不正确，请向教师确认。")


def check_student_id(value: str, setting: Optional[AppSetting] = None) -> str:
    setting = setting or policy()
    sid = (value or "").strip()
    if not STUDENT_ID_RE.match(sid):
        raise RegistrationError("学号应为 4–20 位数字。")
    prefix = (setting.registration_prefix or "").strip()
    if prefix and not sid.startswith(prefix):
        raise RegistrationError(f"本次注册的学号需要以 {prefix} 开头。")
    if Profile.objects.filter(student_id=sid).exists() or User.objects.filter(username=sid).exists():
        raise RegistrationError("该学号已注册，请直接登录；忘记密码请联系教师。")
    return sid


def register_student(
    student_id: str,
    real_name: str,
    password: str,
    setting: Optional[AppSetting] = None,
) -> Profile:
    """创建账号。审核策略与强制改密由教师设置决定。"""
    setting = setting or policy()
    check_open(setting)

    name = (real_name or "").strip()
    if len(name) < 2:
        raise RegistrationError("请填写真实姓名（至少 2 个字符）。")

    status = (
        Profile.STATUS_PENDING
        if setting.registration_require_approval
        else Profile.STATUS_ACTIVE
    )
    prefix = load_config().contest_code_prefix

    for _ in range(6):
        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    username=student_id, password=password, first_name=name[:30]
                )
                return Profile.objects.create(
                    user=user,
                    student_id=student_id,
                    real_name=name,
                    contest_code=next_contest_code(prefix),
                    status=status,
                    must_change_password=setting.registration_force_password_change,
                )
        except IntegrityError:
            # 并发注册可能撞到同学号或同编号，区分后重试/报错
            if (
                Profile.objects.filter(student_id=student_id).exists()
                or User.objects.filter(username=student_id).exists()
            ):
                raise RegistrationError("该学号已注册，请直接登录。")
            continue
    raise RegistrationError("参赛编号分配失败，请稍后重试。")
