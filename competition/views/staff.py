"""教师端：总览、互评分配、主观榜重拟合、运行设置、导出、作品讲评。

教师端的每个动作都直接对应教师组会用到的操作，日常只需要改 ``config/competition.yaml``
与 ``competition/research/params.py``，不需要动这里的代码。
"""
from __future__ import annotations

import logging
from typing import Dict

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_GET, require_POST

from competition.config import load_config
from competition.models import AppSetting, Profile, Submission
from competition.services import export as export_service
from competition.services import leaderboard, reviews
from competition.views.base import render_page

logger = logging.getLogger(__name__)


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
            "label": "学生账号可用",
            "ok": student_count > 0,
            "detail": f"{student_count} 个账号",
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

    return render_page(
        request,
        "staff.html",
        {
            "config": config,
            "setting": setting,
            "review_rows": review_rows,
            "review_totals": review_totals,
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
    """教师面板的参数设置：互评份数与注册开关。"""
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
        setting.save(update_fields=["registration_open", "updated_at"])
        messages.success(
            request,
            "已开放自助注册，学生可用学号在注册页自建账号。"
            if setting.registration_open
            else "已关闭自助注册，注册页不再接受新账号。",
        )
    else:
        messages.error(request, "未知的设置项。")

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
    return render_page(request, "gallery.html", {"config": config, "sections": sections})
