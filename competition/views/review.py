"""互评页面：出题（下一份待评）、投票。"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from competition.config import load_config
from competition.models import ReviewAssignment
from competition.services import reviews
from competition.services.phase import current_phase
from competition.views.base import render_page


@login_required
@require_GET
def review_view(request: HttpRequest):
    config = load_config()
    if not config.peer_review.enabled:
        return render_page(request, "review_done.html", {"enabled": False})

    task_list = config.review_tasks()
    if not task_list:
        return render_page(request, "review_done.html", {"enabled": False})

    progress = reviews.progress_all(request.user, config)
    by_slug = {p["task"].slug: p for p in progress}

    # 默认跳到「还有待评任务」的第一个；都完成了就停在第一个任务
    slug = request.GET.get("task")
    if slug not in by_slug:
        slug = next(
            (p["task"].slug for p in progress if p["total"] > p["done"]),
            task_list[0].slug,
        )
    task = by_slug[slug]["task"]
    done, total = by_slug[slug]["done"], by_slug[slug]["total"]
    phase = current_phase(config)

    base_ctx = {
        "enabled": True,
        "phase": phase,
        "task": task,
        "review_tasks": progress,
        "done": done,
        "total": total,
    }

    if not phase.review_open:
        return render_page(request, "review_done.html", {**base_ctx, "not_open": True})

    assignment = reviews.next_assignment(request.user, task)
    if assignment is None:
        all_done = all(p["total"] and p["done"] >= p["total"] for p in progress)
        next_pending = next(
            (p for p in progress if p["total"] > p["done"] and p["task"].slug != task.slug),
            None,
        )
        return render_page(
            request,
            "review_done.html",
            {
                **base_ctx,
                "finished": True,
                "all_done": all_done,
                "next_pending": next_pending,
            },
        )

    return render_page(
        request,
        "review.html",
        {
            **base_ctx,
            "assignment": assignment,
            "original_url": reverse("public_image", args=[task.slug, assignment.image_key]),
            "left_url": reverse("review_image", args=[assignment.id, "left"]),
            "right_url": reverse("review_image", args=[assignment.id, "right"]),
            "percent": round(done / total * 100) if total else 0,
            "allow_tie": config.peer_review.allow_tie,
        },
    )


@login_required
@require_POST
def review_vote(request: HttpRequest, assignment_id: int):
    config = load_config()
    assignment = get_object_or_404(ReviewAssignment, pk=assignment_id, reviewer=request.user)
    back = redirect(f"{reverse('review')}?task={assignment.task}")

    if not config.peer_review.for_task(assignment.task).enabled:
        messages.error(request, "该任务当前未开放互评。")
        return back
    if not current_phase(config).review_open:
        messages.error(request, "当前不在互评阶段，投票未记录。")
        return back

    choice = request.POST.get("choice", "")
    duration = request.POST.get("duration_ms", "0")
    try:
        duration_ms = int(float(duration))
    except (TypeError, ValueError):
        duration_ms = 0

    if choice == "tie" and not config.peer_review.allow_tie:
        messages.error(request, "本次互评不允许选择「两者相近」。")
        return back

    try:
        reviews.record_vote(assignment, choice, duration_ms)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "已记录本次评价，感谢参与。")
    return back
