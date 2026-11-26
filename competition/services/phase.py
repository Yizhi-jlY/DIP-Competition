"""赛程阶段判断。教师可在管理后台用 AppSetting 覆盖 yaml 中的时间。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from django.utils import timezone

from competition.config import Config, Schedule, load_config
from competition.models import AppSetting


@dataclass
class PhaseState:
    code: str
    label: str
    submission_open: bool
    review_open: bool
    submission_closed: bool
    next_deadline: Optional[datetime]
    next_deadline_label: str

    @property
    def can_review(self) -> bool:
        return self.review_open

    @property
    def is_finished(self) -> bool:
        return self.code == "finished"


def effective_schedule(config: Optional[Config] = None) -> Schedule:
    config = config or load_config()
    base = config.schedule
    try:
        setting = AppSetting.get()
    except Exception:  # 数据库尚未迁移时保持可用
        return base

    def pick(override, fallback):
        return override if override else fallback

    return Schedule(
        submission_open=pick(setting.submission_open, base.submission_open),
        submission_close=pick(setting.submission_close, base.submission_close),
        review_open=pick(setting.review_open, base.review_open),
        review_close=pick(setting.review_close, base.review_close),
    )


def current_phase(config: Optional[Config] = None) -> PhaseState:
    config = config or load_config()
    sched = effective_schedule(config)
    now = timezone.now()

    s_open = sched.submission_open
    s_close = sched.submission_close
    r_open = sched.review_open
    r_close = sched.review_close

    submission_open = bool(s_open and now >= s_open and (s_close is None or now <= s_close))
    submission_closed = bool(s_close and now > s_close)
    review_open = bool(r_open and now >= r_open and (r_close is None or now <= r_close))

    if s_open and now < s_open:
        return PhaseState(
            code="before",
            label="提交尚未开始",
            submission_open=False,
            review_open=False,
            submission_closed=False,
            next_deadline=s_open,
            next_deadline_label="距离开放提交",
        )
    if submission_open:
        return PhaseState(
            code="submission",
            label="提交进行中",
            submission_open=True,
            review_open=False,
            submission_closed=False,
            next_deadline=s_close,
            next_deadline_label="距离提交截止",
        )
    if not submission_closed:
        # 没有配置截止时间时视为长期开放
        return PhaseState(
            code="submission",
            label="提交进行中",
            submission_open=True,
            review_open=False,
            submission_closed=False,
            next_deadline=None,
            next_deadline_label="",
        )
    if review_open:
        return PhaseState(
            code="review",
            label="互评进行中",
            submission_open=False,
            review_open=True,
            submission_closed=True,
            next_deadline=r_close,
            next_deadline_label="距离互评截止",
        )
    if r_open and now < r_open:
        return PhaseState(
            code="between",
            label="提交已截止，等待互评",
            submission_open=False,
            review_open=False,
            submission_closed=True,
            next_deadline=r_open,
            next_deadline_label="距离互评开始",
        )
    return PhaseState(
        code="finished",
        label="竞赛已结束",
        submission_open=False,
        review_open=False,
        submission_closed=True,
        next_deadline=None,
        next_deadline_label="",
    )


def countdown(deadline: Optional[datetime]) -> str:
    if not deadline:
        return ""
    delta = deadline - timezone.now()
    total = int(delta.total_seconds())
    if total <= 0:
        return "已到期"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分钟"
