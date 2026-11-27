"""互评任务分配与投票（两个任务各自独立分配、独立排名）。"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from competition.config import Config, TaskSpec, load_config
from competition.models import AppSetting, Comparison, ReviewAssignment, Submission
from competition.peer_review import allocation

logger = logging.getLogger(__name__)


def review_tasks(config: Optional[Config] = None) -> List[TaskSpec]:
    """已开放匿名互评的任务列表。"""
    return (config or load_config()).review_tasks()


def effective_count(task: TaskSpec, config: Optional[Config] = None) -> int:
    """该任务每人需要评价的份数：教师面板设置优先，其次 yaml。"""
    config = config or load_config()
    raw = (AppSetting.get().review_counts or {}).get(task.slug)
    if raw is None:
        raw = config.peer_review.for_task(task.slug).comparisons_per_student
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return config.peer_review.for_task(task.slug).comparisons_per_student


def _finals_by_image(task: TaskSpec) -> Dict[str, List[Submission]]:
    out: Dict[str, List[Submission]] = {img.key: [] for img in task.images}
    rows = (
        Submission.objects.filter(task=task.slug, is_final=True, is_baseline=False)
        .select_related("student")
        .order_by("id")
    )
    for sub in rows:
        out.setdefault(sub.image_key, []).append(sub)
    return out


def allocate_reviews(
    task: TaskSpec,
    per_student: Optional[int] = None,
    seed: int = 2026,
    replace: bool = True,
) -> dict:
    per_student = effective_count(task) if per_student is None else per_student
    if per_student <= 0:
        raise ValueError(f"{task.name}的每人评价份数为 0，请先在教师面板中设置。")
    finals = _finals_by_image(task)

    entries_by_image = {k: [s.id for s in v] for k, v in finals.items() if len(v) >= 2}
    if not entries_by_image:
        raise ValueError(f"{task.name}没有足够的最终作品，无法分配互评任务。")

    own_map: Dict[int, Dict[str, int]] = {}
    reviewers: List[Tuple[str, Dict[str, int]]] = []
    for image_key, subs in finals.items():
        for sub in subs:
            own_map.setdefault(sub.student_id, {})[image_key] = sub.id

    images = list(entries_by_image.keys())
    quotas: Dict[str, Dict[str, int]] = {}
    for student_id in own_map:
        base, rem = divmod(per_student, len(images))
        quotas[str(student_id)] = {
            image_key: base + (1 if idx < rem else 0)
            for idx, image_key in enumerate(images)
        }
        reviewers.append((str(student_id), own_map[student_id]))

    report = allocation.allocate(entries_by_image, reviewers, quotas, seed=seed)

    issues = allocation.validate(
        [(a.reviewer_key, a.image_key, a.a_id, a.b_id) for a in report.assignments],
        {str(k): v for k, v in own_map.items()},
        expected_per_reviewer=per_student,
    )

    with transaction.atomic():
        if replace:
            ReviewAssignment.objects.filter(task=task.slug).delete()
        existing = set(
            ReviewAssignment.objects.filter(task=task.slug).values_list(
                "reviewer_id", "image_key", "left_id", "right_id"
            )
        )
        users = User.objects.in_bulk([int(r[0]) for r in reviewers])
        counters: Dict[int, int] = {}
        to_create: List[ReviewAssignment] = []
        for item in report.assignments:
            reviewer_id = int(item.reviewer_key)
            if reviewer_id not in users:
                continue
            key = (reviewer_id, item.image_key, item.a_id, item.b_id)
            if key in existing:
                continue
            pos = counters.get(reviewer_id, 0) + 1
            counters[reviewer_id] = pos
            to_create.append(
                ReviewAssignment(
                    reviewer_id=reviewer_id,
                    task=task.slug,
                    image_key=item.image_key,
                    left_id=item.a_id,
                    right_id=item.b_id,
                    position=pos,
                )
            )
        ReviewAssignment.objects.bulk_create(to_create)

    setting = AppSetting.get()
    setting.reviews_allocated_at = timezone.now()
    setting.save(update_fields=["reviews_allocated_at", "updated_at"])

    summary = {
        "task": task.slug,
        "task_name": task.name,
        "assignments": len(to_create),
        "reviewers": len(reviewers),
        "per_student": per_student,
        "coverage": allocation.coverage_summary(report),
        "repaired": report.repaired,
        "issues": issues,
    }
    logger.info("allocate_reviews task=%s %s", task.slug, summary)
    return summary


def progress_for(user: User, task: TaskSpec) -> Tuple[int, int]:
    qs = ReviewAssignment.objects.filter(reviewer=user, task=task.slug)
    total = qs.count()
    done = Comparison.objects.filter(assignment__reviewer=user, assignment__task=task.slug).count()
    return done, total


def progress_all(user: User, config: Optional[Config] = None) -> List[dict]:
    """每个互评任务的完成情况，供首页与互评页展示。"""
    rows: List[dict] = []
    for task in review_tasks(config):
        done, total = progress_for(user, task)
        rows.append(
            {
                "task": task,
                "done": done,
                "total": total,
                "percent": round(done / total * 100) if total else 0,
                "remaining": max(0, total - done),
                "finished": total > 0 and done >= total,
            }
        )
    return rows


def next_assignment(user: User, task: TaskSpec) -> Optional[ReviewAssignment]:
    return (
        ReviewAssignment.objects.filter(reviewer=user, task=task.slug, comparison__isnull=True)
        .select_related("left", "right")
        .order_by("position", "id")
        .first()
    )


def record_vote(assignment: ReviewAssignment, choice: str, duration_ms: int = 0) -> Comparison:
    valid = {c[0] for c in Comparison.CHOICES}
    if choice not in valid:
        raise ValueError("无效的选择。")
    if hasattr(assignment, "comparison"):
        raise ValueError("该比较已经提交过。")
    return Comparison.objects.create(
        assignment=assignment,
        choice=choice,
        duration_ms=max(0, min(int(duration_ms or 0), 10 * 60 * 1000)),
    )
