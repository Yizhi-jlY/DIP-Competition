"""学号自助注册。

规则只有一条：教师面板里「开放自助注册」打开后，学生即可用学号自建账号。
注册成功即建立 User + Profile，参赛编号按现有规则顺延，账号立即可用（无需审核），
注册时学生自己设定的密码就是登录密码，不强制改密。
"""
from __future__ import annotations

import re
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


def check_student_id(value: str) -> str:
    sid = (value or "").strip()
    if not STUDENT_ID_RE.match(sid):
        raise RegistrationError("学号应为 4–20 位数字。")
    if Profile.objects.filter(student_id=sid).exists() or User.objects.filter(username=sid).exists():
        raise RegistrationError("该学号已注册，请直接登录；忘记密码请联系教师。")
    return sid


def register_student(
    student_id: str,
    real_name: str,
    password: str,
    setting: Optional[AppSetting] = None,
) -> Profile:
    """创建账号并直接可用。"""
    setting = setting or policy()
    check_open(setting)

    name = (real_name or "").strip()
    if len(name) < 2:
        raise RegistrationError("请填写真实姓名（至少 2 个字符）。")

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
                    must_change_password=False,
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
