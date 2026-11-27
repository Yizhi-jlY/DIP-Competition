"""学生端与教师端页面。"""
from __future__ import annotations

import io
import logging
import os
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from django.contrib import messages
from django.conf import settings
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
from competition.forms import LoginForm, RegistrationForm, UploadForm
from competition.imaging import ImageValidationError, reference_size
from competition.models import AppSetting, Comparison, Profile, ReviewAssignment, Submission
from competition.services import export as export_service
from competition.services import leaderboard, registration as registration_service, reviews
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
        user = form.get_user()
        profile = getattr(user, "profile", None)
        if profile is not None and profile.status != Profile.STATUS_ACTIVE:
            if profile.status == Profile.STATUS_PENDING:
                form.add_error(None, "账号正在等待教师审核，通过后即可登录。")
            else:
                form.add_error(None, "注册申请未通过，请联系任课教师。")
            return self.form_invalid(form)

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


# --------------------------------------------------------------- 自助注册
def register_view(request: HttpRequest):
    """学生用学号自助注册；开关、邀请码、审核策略均由教师面板控制。"""
    config = load_config()
    if request.user.is_authenticated:
        return redirect("home")

    setting = AppSetting.get()
    require_code = bool((setting.registration_code or "").strip())
    form = None

    if setting.registration_open:
        form = RegistrationForm(
            request.POST or None,
            require_code=require_code,
        )
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
                if profile.status == Profile.STATUS_PENDING:
                    messages.success(
                        request, "注册申请已提交，教师审核通过后即可用学号登录。"
                    )
                    return redirect("login")
                auth_login(
                    request, profile.user, backend="django.contrib.auth.backends.ModelBackend"
                )
                messages.success(
                    request, f"注册成功，你的参赛编号是 {profile.contest_code}。"
                )
                if profile.must_change_password:
                    return redirect("password_change")
                return redirect("home")

    return render(
        request,
        "register.html",
        {
            "brand": config,
            "tasks": config.tasks,
            "form": form,
            "open": setting.registration_open,
            "require_code": require_code,
            "setting": setting,
        },
        status=200 if setting.registration_open else 403,
    )


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
                "primary": task.metric(task.ranking_metric),
                "ranking_rule": task.ranking_rule(),
            }
        )

    review_progress = []
    if config.peer_review.enabled:
        review_progress = reviews.progress_all(request.user, config)
    review_done = sum(p["done"] for p in review_progress)
    review_total = sum(p["total"] for p in review_progress)

    return _base(
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

    return _base(
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
    if board == "decolor-objective":  # 旧链接兼容
        board = "decolor"
    slugs = list(config.task_map())
    valid = set(slugs) | {f"{slug}-subjective" for slug in slugs}
    if board not in valid:
        board = slugs[0] if slugs else "denoise"

    board_tabs = []
    for task in config.tasks:
        board_tabs.append({"slug": task.slug, "label": task.short_name})
        if config.peer_review.for_task(task.slug).enabled:
            board_tabs.append(
                {"slug": f"{task.slug}-subjective", "label": f"{task.short_name} · 主观"}
            )

    ctx: Dict[str, object] = {
        "config": config,
        "board": board,
        "allowed": allowed,
        "denoise": config.task("denoise"),
        "decolor": config.task("decolor"),
        "boards": board_tabs,
    }

    if not allowed:
        return _base(request, "leaderboard.html", ctx)

    if board.endswith("-subjective"):
        task = config.task(board[: -len("-subjective")])
        if task:
            ctx.update(
                {
                    "rows": leaderboard.subjective_rows(task),
                    "task": task,
                    "board_type": "subjective",
                    "stats": leaderboard.review_stats(task),
                    "review_count": reviews.effective_count(task, config),
                    "ranking_rule": task.ranking_rule(),
                }
            )
    else:
        task = config.task(board)
        if task:
            rows, incomplete, baselines = leaderboard.objective_rows(task)
            ctx.update(
                {
                    "rows": rows,
                    "incomplete": incomplete,
                    "baselines": baselines,
                    "task": task,
                    "board_type": "objective",
                    "primary": task.metric(task.ranking_metric),
                    "secondary": task.metric(task.tiebreak_metric),
                    "ranking_rule": task.ranking_rule(),
                }
            )
            if config.peer_review.for_task(task.slug).enabled:
                ctx["show_subjective"] = True
                ctx["subjective_map"] = {
                    r.student.id: r for r in leaderboard.subjective_rows(task)
                }
            if task.slug == "decolor":
                ctx["weights"] = config.scoring

    return _base(request, "leaderboard.html", ctx)


# --------------------------------------------------------------- 互评
@login_required
@require_GET
def review_view(request: HttpRequest):
    config = load_config()
    if not config.peer_review.enabled:
        return _base(request, "review_done.html", {"enabled": False})

    task_list = config.review_tasks()
    if not task_list:
        return _base(request, "review_done.html", {"enabled": False})

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
        return _base(request, "review_done.html", {**base_ctx, "not_open": True})

    assignment = reviews.next_assignment(request.user, task)
    if assignment is None:
        all_done = all(p["total"] and p["done"] >= p["total"] for p in progress)
        next_pending = next(
            (p for p in progress if p["total"] > p["done"] and p["task"].slug != task.slug),
            None,
        )
        return _base(
            request,
            "review_done.html",
            {
                **base_ctx,
                "finished": True,
                "all_done": all_done,
                "next_pending": next_pending,
            },
        )

    return _base(
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
def _file_response(path: Path, filename: Optional[str] = None, cache: str = "private, max-age=300",
                   content_type: str = "image/png") -> FileResponse:
    if not path.exists():
        raise Http404("文件不存在。")
    resp = FileResponse(open(path, "rb"), content_type=content_type)
    if filename:
        resp["Content-Disposition"] = f'inline; filename="{filename}"'
    resp["Cache-Control"] = cache
    return resp


def _thumbnail(src: Path, width: int = 640) -> Path:
    """按需生成缩略图并落盘缓存。

    输入图是 768×768 的无损 PNG（约 1.3–1.5 MB），140 人同时刷新任务页会重复下载
    近 400 MB。页面上改用 640px 的 JPEG 缩略图（噪声图用 PNG 仍接近 900 KB，
    JPEG 约 120 KB），"下载输入图"仍给原 PNG。
    """
    from PIL import Image

    try:
        rel = src.resolve().relative_to((settings.BASE_DIR / "data").resolve())
    except ValueError:
        return src
    dst = settings.BASE_DIR / "storage" / "thumbs" / rel.parent / f"{src.stem}_{width}.jpg"
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(f".{os.getpid()}.tmp")
    with Image.open(src) as im:
        if im.mode == "L":
            work = im.convert("L")
        else:
            work = im.convert("RGB")
        work.thumbnail((width, width), Image.LANCZOS)
        work.save(tmp, format="JPEG", quality=82, optimize=True, progressive=True)
    tmp.replace(dst)  # 原子替换，避免并发下读到半张图
    return dst


@login_required
def public_image(request: HttpRequest, slug: str, key: str):
    task = get_task(slug)
    if key not in {img.key for img in task.images}:
        raise Http404
    path = task.public_image(key)
    if request.GET.get("w") == "thumb":
        # 输入图在赛程内不会变化，允许浏览器长缓存，避免反复下载
        return _file_response(_thumbnail(path, 640), cache="private, max-age=86400",
                              content_type="image/jpeg")
    return _file_response(path, cache="private, max-age=86400")


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
    setting = AppSetting.get()

    review_rows = []
    for task in config.review_tasks():
        stats = leaderboard.review_stats(task)
        review_rows.append(
            {
                "task": task,
                "stats": stats,
                "count": reviews.effective_count(task, config),
            }
        )
    assigned = sum(r["stats"]["assigned"] for r in review_rows)
    done = sum(r["stats"]["done"] for r in review_rows)
    review_totals = {
        "assigned": assigned,
        "done": done,
        "progress": round(done / assigned * 100, 1) if assigned else 0,
    }

    student_count = Profile.objects.filter(status=Profile.STATUS_ACTIVE).count()
    pending_profiles = list(
        Profile.objects.filter(status=Profile.STATUS_PENDING)
        .select_related("user")
        .order_by("imported_at")
    )
    reg_counts = {
        "active": student_count,
        "pending": len(pending_profiles),
        "rejected": Profile.objects.filter(status=Profile.STATUS_REJECTED).count(),
    }
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
            "label": "学生账号可用",
            "ok": student_count > 0,
            "detail": f"{student_count} 个正常账号，{reg_counts['pending']} 个待审核",
        },
        {
            "label": "最终作品已确认",
            "ok": final_count > 0,
            "detail": f"{final_count} 张图像",
        },
        {
            "label": "两个任务的互评任务均已分配",
            "ok": bool(review_rows) and all(r["stats"]["assigned"] for r in review_rows),
            "detail": "、".join(
                f"{r['task'].short_name} {r['stats']['assigned']} 条" for r in review_rows
            ) or "未配置互评任务",
        },
        {
            "label": "互评投票进行中",
            "ok": done > 0,
            "detail": f"{done} 次已记录",
        },
        {
            "label": "自助注册已开放",
            "ok": setting.registration_open,
            "detail": "教师面板可随时开关",
        },
        {
            "label": "排行榜已公开",
            "ok": setting.leaderboard_public,
            "detail": "可在运行设置中切换",
        },
    ]

    return _base(
        request,
        "staff.html",
        {
            "config": config,
            "setting": setting,
            "review_rows": review_rows,
            "review_totals": review_totals,
            "pending_profiles": pending_profiles,
            "reg_counts": reg_counts,
            "student_count": student_count,
            "baseline_count": baseline_count,
            "final_count": final_count,
            "checks": checks,
        },
    )


@staff_member_required
@require_POST
def staff_allocate(request: HttpRequest):
    config = load_config()
    slug = request.POST.get("task", "")
    if slug in {"", "all"}:
        targets = config.review_tasks()
        if not targets:
            messages.error(request, "配置中没有开放互评的任务。")
            return redirect("staff_dashboard")
    else:
        target = config.task(slug)
        if target is None or not config.peer_review.for_task(slug).enabled:
            messages.error(request, "该任务未开放互评。")
            return redirect("staff_dashboard")
        targets = [target]

    lines, problems = [], []
    for task in targets:
        try:
            summary = reviews.allocate_reviews(task)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        lines.append(
            f"{task.name}：生成 {summary['assignments']} 条任务，覆盖 {summary['reviewers']} 位学生，"
            f"每人 {summary['per_student']} 份，每张图平均被评 {summary['coverage']['mean']} 次"
            f"（最少 {summary['coverage']['min']} 次）"
        )
        problems.extend(summary["issues"][:3])

    if lines:
        messages.success(request, "；".join(lines) + "。")
    if problems:
        messages.warning(request, "检查提示：" + "；".join(problems[:5]))

    # 分配完成后顺手刷新主观榜，教师无需再点一次
    if lines:
        for task in targets:
            try:
                leaderboard.refresh_subjective(task)
            except Exception:  # noqa: BLE001 - 没有投票时排序为空是正常的
                logger.exception("refresh_subjective failed task=%s", task.slug)
    return redirect("staff_dashboard")


@staff_member_required
@require_POST
def staff_refresh_subjective(request: HttpRequest):
    config = load_config()
    updated = []
    for task in config.review_tasks():
        count = leaderboard.refresh_subjective(task)
        updated.append(f"{task.short_name} {count} 条")
    if updated:
        messages.success(request, "已重新拟合 Bradley–Terry 模型，更新 " + "、".join(updated) + "。")
    else:
        messages.error(request, "配置中没有开放互评的任务。")
    return redirect("staff_dashboard")


@staff_member_required
@require_POST
def staff_settings(request: HttpRequest):
    """教师面板的参数设置：互评份数与注册策略。"""
    config = load_config()
    setting = AppSetting.get()
    section = request.POST.get("section")

    if section == "review":
        counts: Dict[str, int] = {}
        for task in config.review_tasks():
            raw = request.POST.get(f"count_{task.slug}")
            if raw is None or raw == "":
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                messages.error(request, f"{task.name}的份数必须是整数。")
                return redirect("staff_dashboard")
            if value < 0 or value > 200:
                messages.error(request, f"{task.name}的份数需在 0–200 之间。")
                return redirect("staff_dashboard")
            counts[task.slug] = value
        setting.review_counts = counts
        setting.save(update_fields=["review_counts", "updated_at"])
        messages.success(
            request,
            "评价份数已保存：" + "、".join(
                f"{t.short_name} {counts.get(t.slug, 0)} 份" for t in config.review_tasks()
            ) + "。修改份数后请重新分配对应任务的互评。",
        )

    elif section == "registration":
        setting.registration_open = request.POST.get("registration_open") == "on"
        setting.registration_code = (request.POST.get("registration_code") or "").strip()[:64]
        setting.registration_prefix = (request.POST.get("registration_prefix") or "").strip()[:16]
        setting.registration_require_approval = (
            request.POST.get("registration_require_approval") == "on"
        )
        setting.registration_force_password_change = (
            request.POST.get("registration_force_password_change") == "on"
        )
        setting.save(
            update_fields=[
                "registration_open",
                "registration_code",
                "registration_prefix",
                "registration_require_approval",
                "registration_force_password_change",
                "updated_at",
            ]
        )
        messages.success(
            request,
            "注册设置已更新："
            + ("已开放注册" if setting.registration_open else "已关闭注册")
            + ("，需要邀请码" if setting.registration_code else "")
            + ("，注册后需审核" if setting.registration_require_approval else "，注册后直接可用"),
        )
    else:
        messages.error(request, "未知的设置项。")

    return redirect("staff_dashboard")


@staff_member_required
@require_POST
def staff_registration_action(request: HttpRequest):
    """通过 / 拒绝自助注册申请。"""
    profile = get_object_or_404(Profile, user_id=request.POST.get("user_id") or 0)
    action = request.POST.get("action")
    if action == "approve":
        profile.status = Profile.STATUS_ACTIVE
        profile.user.is_active = True
        profile.user.save(update_fields=["is_active"])
        profile.save(update_fields=["status"])
        messages.success(request, f"已通过 {profile.real_name}（{profile.student_id}）的注册申请。")
    elif action == "reject":
        profile.status = Profile.STATUS_REJECTED
        profile.save(update_fields=["status"])
        messages.warning(request, f"已拒绝 {profile.real_name}（{profile.student_id}）的注册申请。")
    else:
        messages.error(request, "未知操作。")
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
