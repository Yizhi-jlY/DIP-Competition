"""学生端页面：首页、任务页、确认最终提交、我的提交。"""
from __future__ import annotations

from typing import Dict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from competition.config import get_task, load_config
from competition.forms import UploadForm
from competition.imaging import ImageValidationError, reference_size
from competition.models import AppSetting, Submission
from competition.services import leaderboard, reviews
from competition.services.phase import current_phase
from competition.services.submissions import (
    SubmitError,
    attempts_used,
    candidates,
    confirm_final,
    create_submission,
    final_submissions,
    latest_submissions,
)
from competition.views.base import render_page


@login_required
def home(request: HttpRequest):
    config = load_config()
    phase = current_phase(config)
    setting = AppSetting.get()

    my_tasks = []
    for task in config.tasks:
        finals = final_submissions(request.user, task)
        latest = latest_submissions(request.user, task)
        my_tasks.append(
            {
                "task": task,
                "finals": finals,
                "latest": latest,
                "complete": len(finals) == len(task.images),
                "images_ready": task.images_ready(),
                "primary": task.metric(task.ranking_metric),
                "ranking_rule": task.ranking_rule(),
            }
        )

    review_progress = []
    if config.peer_review.enabled:
        review_progress = reviews.progress_all(request.user, config)
    review_done = sum(p["done"] for p in review_progress)
    review_total = sum(p["total"] for p in review_progress)

    return render_page(
        request,
        "home.html",
        {
            "config": config,
            "phase": phase,
            "setting": setting,
            "my_tasks": my_tasks,
            "review_progress": review_progress,
            "review_done": review_done,
            "review_total": review_total,
            "config_json": config,
        },
    )


@login_required
def task_detail(request: HttpRequest, slug: str):
    task = get_task(slug)
    phase = current_phase()

    if request.method == "POST":
        form = UploadForm(request.POST, request.FILES)
        if not form.is_valid():
            messages.error(request, "上传失败：请选择要提交的 PNG 文件。")
            return redirect("task_detail", slug=slug)
        try:
            submission, metrics = create_submission(
                request.user, task, form.cleaned_data["image_key"], form.cleaned_data["file"]
            )
        except (ImageValidationError, SubmitError) as exc:
            messages.error(request, str(exc))
            return redirect("task_detail", slug=slug)

        parts = []
        for spec in task.metrics:
            value = metrics.get(spec.key)
            if isinstance(value, (int, float)):
                parts.append(f"{spec.name} {spec.format(value)}")
        summary = "、".join(parts)
        messages.success(
            request,
            f"{submission.image_key} 第 {submission.attempt} 次提交成功：{summary}。"
            "确认无误后请点击「确认为最终提交」。",
        )
        return redirect("task_detail", slug=slug)

    latest = latest_submissions(request.user, task)
    finals = final_submissions(request.user, task)

    images_ctx = []
    confirm_ctx = []
    for img in task.images:
        path = task.public_image(img.key)
        size = reference_size(task, img.key)
        sub = latest.get(img.key)
        used = attempts_used(request.user, task, img.key)
        pool = candidates(request.user, task, img.key)
        confirmed = finals.get(img.key)
        images_ctx.append(
            {
                "spec": img,
                "url": reverse("public_image", args=[task.slug, img.key]),
                "exists": path.exists(),
                "size": size,
                "latest": sub,
                "attempts": used,
                "can_submit": task.unlimited or used < task.max_submissions,
                "is_final": confirmed is not None,
                "metrics": sub.metrics if sub else {},
            }
        )
        best = pool[0] if pool else None
        confirm_ctx.append(
            {
                "spec": img,
                "pool": pool,
                "confirmed": confirmed,
                # 表单默认值始终取该图指标最好的那次
                "default": best,
                "confirmed_differs": bool(
                    confirmed and best and confirmed.pk != best.pk
                ),
            }
        )

    history = (
        Submission.objects.filter(student=request.user, task=task.slug)
        .order_by("-created_at")[:50]
    )

    return render_page(
        request,
        "task.html",
        {
            "task": task,
            "phase": phase,
            "images_ctx": images_ctx,
            "confirm_ctx": confirm_ctx,
            "confirmed_any": bool(finals),
            "confirm_has_pool": any(row["pool"] for row in confirm_ctx),
            "all_confirmed": len(finals) == len(task.images),
            "history": history,
            "metric_specs": task.metrics,
            "ranking_rule": task.ranking_rule(),
            "primary": task.metric(task.ranking_metric),
            "secondary": task.metric(task.tiebreak_metric),
            "config": load_config(),
        },
    )


@login_required
@require_POST
def confirm_final_view(request: HttpRequest, slug: str):
    task = get_task(slug)
    choices: Dict[str, int] = {}
    for img in task.images:
        raw = request.POST.get(f"pick_{img.key}")
        if not raw:
            continue
        try:
            choices[img.key] = int(raw)
        except (TypeError, ValueError):
            messages.error(request, "提交选择无效，请刷新页面后重试。")
            return redirect("task_detail", slug=slug)
    try:
        finals = confirm_final(request.user, task, choices)
    except SubmitError as exc:
        messages.error(request, str(exc))
        return redirect("task_detail", slug=slug)

    detail = "；".join(
        f"{s.image_key} 用 {s.original_name or '未命名'}（{timezone.localtime(s.created_at):%m-%d %H:%M:%S} 提交）"
        for s in finals
    )
    messages.success(
        request, f"已确认最终作品：{detail}。截止前可再次提交并重新确认。"
    )
    return redirect("task_detail", slug=slug)


@login_required
def my_submissions(request: HttpRequest):
    config = load_config()
    data = []
    for task in config.tasks:
        subs = (
            Submission.objects.filter(student=request.user, task=task.slug)
            .order_by("image_key", "-created_at")
        )
        data.append(
            {
                "task": task,
                "subs": subs,
                "finals": final_submissions(request.user, task),
            }
        )

    subjective = None
    decolor = config.task("decolor")
    if decolor:
        for row in leaderboard.subjective_rows(decolor):
            if row.student and row.student.id == request.user.id:
                subjective = row
                break

    combined_row = leaderboard.combined_by_student().get(request.user.id)

    return render_page(
        request,
        "submissions.html",
        {
            "config": config,
            "data": data,
            "subjective": subjective,
            "combined": combined_row,
        },
    )
