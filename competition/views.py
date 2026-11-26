"""学生端与教师端页面。"""
from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import login as auth_login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.views import LoginView, LogoutView, PasswordChangeView
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from competition.config import TaskSpec, get_task, load_config
from competition.forms import LoginForm, UploadForm
from competition.imaging import ImageValidationError, reference_size
from competition.models import AppSetting, Comparison, ReviewAssignment, Submission
from competition.services import export as export_service
from competition.services import leaderboard, reviews
from competition.services.phase import current_phase
from competition.services.submissions import (
    SubmitError,
    attempts_used,
    confirm_final,
    create_submission,
    final_submissions,
    latest_submissions,
)

logger = logging.getLogger(__name__)


def _base(request, template: str, context: Optional[Dict] = None, status: int = 200):
    ctx = {"tasks": load_config().tasks}
    if context:
        ctx.update(context)
    return render(request, template, ctx, status=status)


# --------------------------------------------------------------- 认证
class CompetitionLoginView(LoginView):
    template_name = "login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def form_valid(self, form):
        response = super().form_valid(form)
        profile = getattr(self.request.user, "profile", None)
        if profile and profile.must_change_password:
            messages.info(self.request, "首次登录，请先修改初始密码。")
            return redirect("password_change")
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["tasks"] = load_config().tasks
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


# --------------------------------------------------------------- 首页
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
            }
        )

    done, total = (0, 0)
    if config.peer_review.enabled:
        review_task = config.task(config.peer_review.task)
        if review_task:
            done, total = reviews.progress_for(request.user, review_task)

    return _base(
        request,
        "home.html",
        {
            "config": config,
            "phase": phase,
            "setting": setting,
            "my_tasks": my_tasks,
            "review_done": done,
            "review_total": total,
            "config_json": config,
        },
    )


# --------------------------------------------------------------- 任务页
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
    for img in task.images:
        path = task.public_image(img.key)
        size = reference_size(task, img.key)
        sub = latest.get(img.key)
        images_ctx.append(
            {
                "spec": img,
                "url": reverse("public_image", args=[task.slug, img.key]),
                "exists": path.exists(),
                "size": size,
                "latest": sub,
                "attempts": attempts_used(request.user, task, img.key),
                "remaining": max(0, task.max_submissions - attempts_used(request.user, task, img.key)),
                "is_final": img.key in finals,
                "metrics": sub.metrics if sub else {},
            }
        )

    history = (
        Submission.objects.filter(student=request.user, task=task.slug)
        .order_by("-created_at")[:50]
    )

    return _base(
        request,
        "task.html",
        {
            "task": task,
            "phase": phase,
            "images_ctx": images_ctx,
            "history": history,
            "final_ready": len(finals) == len(task.images),
            "max_submissions": task.max_submissions,
            "metric_specs": task.metrics,
            "config": load_config(),
        },
    )


@login_required
@require_POST
def confirm_final_view(request: HttpRequest, slug: str):
    task = get_task(slug)
    try:
        finals = confirm_final(request.user, task)
    except SubmitError as exc:
        messages.error(request, str(exc))
        return redirect("task_detail", slug=slug)
    codes = "、".join(s.image_key for s in finals)
    messages.success(request, f"已确认最终作品（{codes}），将以此参与排名。截止前可再次提交并重新确认。")
    return redirect("task_detail", slug=slug)


# --------------------------------------------------------------- 排行榜
def _board_allowed(user) -> bool:
    if user.is_staff:
        return True
    try:
        return AppSetting.get().leaderboard_public
    except Exception:  # noqa: BLE001
        return True


@login_required
def leaderboard_view(request: HttpRequest):
    config = load_config()
    allowed = _board_allowed(request.user)
    board = request.GET.get("board", "denoise")
    if board not in {"denoise", "decolor-objective", "decolor-subjective"}:
        board = "denoise"

    denoise = config.task("denoise")
    decolor = config.task("decolor")

    ctx: Dict[str, object] = {
        "config": config,
        "board": board,
        "allowed": allowed,
        "denoise": denoise,
        "decolor": decolor,
    }

    if not allowed:
        return _base(request, "leaderboard.html", ctx)

    if board == "denoise" and denoise:
        rows, incomplete, baselines = leaderboard.objective_rows(denoise)
        ctx.update(
            {
                "rows": rows,
                "incomplete": incomplete,
                "baselines": baselines,
                "task": denoise,
                "primary": denoise.metric(denoise.ranking_metric),
                "secondary": denoise.metric(denoise.tiebreak_metric),
            }
        )
    elif board == "decolor-objective" and decolor:
        rows, incomplete, baselines = leaderboard.objective_rows(decolor)
        subj = {r.student.id: r for r in leaderboard.subjective_rows(decolor)}
        ctx.update(
            {
                "rows": rows,
                "incomplete": incomplete,
                "baselines": baselines,
                "task": decolor,
                "primary": decolor.metric(decolor.ranking_metric),
                "secondary": decolor.metric(decolor.tiebreak_metric),
                "subjective_map": subj,
                "weights": config.scoring,
            }
        )
    elif board == "decolor-subjective" and decolor:
        rows = leaderboard.subjective_rows(decolor)
        ctx.update({"rows": rows, "task": decolor, "stats": leaderboard.review_stats(decolor)})

    return _base(request, "leaderboard.html", ctx)


# --------------------------------------------------------------- 互评
@login_required
@require_GET
def review_view(request: HttpRequest):
    config = load_config()
    if not config.peer_review.enabled:
        return _base(request, "review_done.html", {"enabled": False})

    task = get_task(config.peer_review.task)
    phase = current_phase(config)
    done, total = reviews.progress_for(request.user, task)

    if not phase.review_open:
        return _base(
            request,
            "review_done.html",
            {
                "enabled": True,
                "phase": phase,
                "task": task,
                "done": done,
                "total": total,
                "not_open": True,
            },
        )

    assignment = reviews.next_assignment(request.user, task)
    if assignment is None:
        return _base(
            request,
            "review_done.html",
            {"enabled": True, "task": task, "done": done, "total": total, "finished": True},
        )

    return _base(
        request,
        "review.html",
        {
            "task": task,
            "assignment": assignment,
            "original_url": reverse("public_image", args=[task.slug, assignment.image_key]),
            "left_url": reverse("review_image", args=[assignment.id, "left"]),
            "right_url": reverse("review_image", args=[assignment.id, "right"]),
            "done": done,
            "total": total,
            "percent": round(done / total * 100) if total else 0,
            "allow_tie": config.peer_review.allow_tie,
        },
    )


@login_required
@require_POST
def review_vote(request: HttpRequest, assignment_id: int):
    config = load_config()
    task = get_task(config.peer_review.task)
    assignment = get_object_or_404(ReviewAssignment, pk=assignment_id, reviewer=request.user)
    if not current_phase(config).review_open:
        messages.error(request, "当前不在互评阶段，投票未记录。")
        return redirect("review")

    choice = request.POST.get("choice", "")
    duration = request.POST.get("duration_ms", "0")
    try:
        duration_ms = int(float(duration))
    except (TypeError, ValueError):
        duration_ms = 0

    if choice == "tie" and not config.peer_review.allow_tie:
        messages.error(request, "本次互评不允许选择「两者相近」。")
        return redirect("review")

    try:
        reviews.record_vote(assignment, choice, duration_ms)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "已记录本次评价，感谢参与。")
    return redirect("review")


# --------------------------------------------------------------- 我的提交
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

    return _base(
        request,
        "submissions.html",
        {"config": config, "data": data, "subjective": subjective},
    )


# --------------------------------------------------------------- 文件服务
def _file_response(path: Path, filename: Optional[str] = None) -> FileResponse:
    if not path.exists():
        raise Http404("文件不存在。")
    resp = FileResponse(open(path, "rb"), content_type="image/png")
    if filename:
        resp["Content-Disposition"] = f'inline; filename="{filename}"'
    resp["Cache-Control"] = "private, max-age=300"
    return resp


@login_required
def public_image(request: HttpRequest, slug: str, key: str):
    task = get_task(slug)
    if key not in {img.key for img in task.images}:
        raise Http404
    return _file_response(task.public_image(key))


@login_required
def submission_image(request: HttpRequest, pk: int):
    sub = get_object_or_404(Submission, pk=pk)
    if sub.student_id != request.user.id and not request.user.is_staff:
        raise PermissionDenied("只能查看自己的结果图。")
    return _file_response(Path(sub.file.path), Path(sub.file.name).name)


@login_required
def review_image(request: HttpRequest, assignment_id: int, side: str):
    assignment = get_object_or_404(ReviewAssignment, pk=assignment_id)
    if assignment.reviewer_id != request.user.id and not request.user.is_staff:
        raise PermissionDenied("无权查看该互评任务。")
    if side not in {"left", "right"}:
        raise Http404
    sub = assignment.left if side == "left" else assignment.right
    return _file_response(Path(sub.file.path), f"{side}.png")


@login_required
def inputs_zip(request: HttpRequest, slug: str):
    task = get_task(slug)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for img in task.images:
            path = task.public_image(img.key)
            if path.exists():
                zf.write(path, arcname=f"{task.slug}/{img.key}.png")
    buffer.seek(0)
    resp = HttpResponse(buffer.getvalue(), content_type="application/zip")
    resp["Content-Disposition"] = f'attachment; filename="{task.slug}_inputs.zip"'
    return resp


# --------------------------------------------------------------- 教师
@staff_member_required
@require_GET
def staff_dashboard(request: HttpRequest):
    config = load_config()
    decolor = config.task("decolor")
    stats = leaderboard.review_stats(decolor) if decolor else {}

    from competition.models import Profile

    student_count = Profile.objects.count()
    baseline_count = Submission.objects.filter(is_baseline=True).count()
    final_count = Submission.objects.filter(is_final=True, is_baseline=False).count()

    checks = [
        {
            "label": "比赛数据就绪（公开输入 + 私有参考）",
            "ok": all(t.images_ready() and t.reference_ready() for t in config.tasks),
            "detail": "、".join(f"{t.slug}" for t in config.tasks),
        },
        {
            "label": "经典算法基线已生成",
            "ok": baseline_count >= len(config.tasks),
            "detail": f"{baseline_count} 条基线记录",
        },
        {
            "label": "学生账号已导入",
            "ok": student_count > 0,
            "detail": f"{student_count} 个账号",
        },
        {
            "label": "最终作品已确认",
            "ok": final_count > 0,
            "detail": f"{final_count} 张图像",
        },
        {
            "label": "互评任务已分配",
            "ok": bool(stats.get("assigned")),
            "detail": f"{stats.get('assigned', 0)} 条任务",
        },
        {
            "label": "互评投票进行中",
            "ok": bool(stats.get("done")),
            "detail": f"{stats.get('done', 0)} 次已记录",
        },
        {
            "label": "排行榜已公开",
            "ok": AppSetting.get().leaderboard_public,
            "detail": "可在运行设置中切换",
        },
    ]

    return _base(
        request,
        "staff.html",
        {
            "config": config,
            "stats": stats,
            "decolor": decolor,
            "student_count": student_count,
            "baseline_count": baseline_count,
            "checks": checks,
        },
    )


@staff_member_required
@require_POST
def staff_allocate(request: HttpRequest):
    config = load_config()
    task = get_task(config.peer_review.task)
    try:
        summary = reviews.allocate_reviews(task)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("staff_dashboard")

    msg = (
        f"已生成 {summary['assignments']} 条互评任务，覆盖 {summary['reviewers']} 位学生；"
        f"每张图平均被评价 {summary['coverage']['mean']} 次，最少 {summary['coverage']['min']} 次。"
    )
    if summary["issues"]:
        messages.warning(request, msg + " 检查提示：" + "；".join(summary["issues"][:5]))
    else:
        messages.success(request, msg)
    return redirect("staff_dashboard")


@staff_member_required
@require_POST
def staff_refresh_subjective(request: HttpRequest):
    config = load_config()
    task = get_task(config.peer_review.task)
    count = leaderboard.refresh_subjective(task)
    messages.success(request, f"已重新拟合 Bradley–Terry 模型，更新 {count} 条主观排名。")
    return redirect("staff_dashboard")


@staff_member_required
@require_GET
def export_scores(request: HttpRequest):
    csv_text = export_service.scores_csv()
    resp = HttpResponse(csv_text, content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="dip_scores.csv"'
    return resp


@staff_member_required
@require_GET
def export_submissions(request: HttpRequest):
    buffer = export_service.submissions_zip()
    resp = HttpResponse(buffer.getvalue(), content_type="application/zip")
    resp["Content-Disposition"] = 'attachment; filename="dip_final_submissions.zip"'
    return resp


@require_GET
def healthz(request: HttpRequest):
    return HttpResponse("ok", content_type="text/plain")


@staff_member_required
@require_GET
def staff_gallery(request: HttpRequest):
    """课堂讲评用：每个任务展示排名最高与最低的最终作品。"""
    config = load_config()
    sections = []
    for task in config.tasks:
        rows, _, _ = leaderboard.objective_rows(task)
        if len(rows) < 2:
            continue
        picked = [(rows[0], "best"), (rows[-1], "worst")]
        if len(rows) >= 4:
            picked.append((rows[1], "best"))
            picked.append((rows[-2], "worst"))
        cards = []
        for row, kind in picked:
            subs = {
                s.image_key: s
                for s in Submission.objects.filter(
                    student=row.student, task=task.slug, is_final=True
                )
            }
            cards.append({"row": row, "subs": subs, "kind": kind})
        sections.append({"task": task, "cards": cards})
    return _base(request, "gallery.html", {"config": config, "sections": sections})
