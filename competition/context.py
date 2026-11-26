"""模板上下文：所有页面共享的竞赛状态。"""
from __future__ import annotations

from django.db import OperationalError, ProgrammingError

from competition.config import load_config
from competition.models import AppSetting, ReviewAssignment
from competition.services.phase import countdown, current_phase, effective_schedule


def site_context(request):
    config = load_config()
    try:
        phase = current_phase(config)
        setting = AppSetting.get()
        sched = effective_schedule(config)
    except (OperationalError, ProgrammingError):  # 数据库尚未初始化
        return {"site": config, "phase": None, "setting": None, "countdown": ""}

    pending = 0
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and config.peer_review.enabled:
        pending = ReviewAssignment.objects.filter(
            reviewer=user, task=config.peer_review.task, comparison__isnull=True
        ).count()

    return {
        "site": config,
        "phase": phase,
        "setting": setting,
        "schedule": sched,
        "countdown": countdown(phase.next_deadline),
        "review_pending": pending,
    }
