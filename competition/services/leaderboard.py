"""排行榜计算：客观榜、基线榜与主观（Bradley–Terry）榜。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count

from competition.config import TaskSpec, load_config
from competition.models import (
    Comparison,
    ObjectiveRanking,
    Submission,
    SubjectiveRanking,
)
from competition.peer_review import ranking as bt

BASELINE_LABELS = {
    "identity": "原始含噪图",
    "gaussian": "Gaussian Filter",
    "median": "Median Filter",
    "bilateral": "Bilateral Filter",
    "nlm": "Non-Local Means",
    "bm3d": "BM3D",
    "rgb2gray": "RGB2Gray (Rec.709)",
    "opencv_decolor": "OpenCV decolor",
    "light_linear": "加权线性灰度",
}


@dataclass
class ObjectiveRow:
    code: str
    name: str
    student: Optional[User]
    per_image: Dict[str, dict] = field(default_factory=dict)
    mean: Optional[float] = None
    tiebreak: Optional[float] = None
    rank: Optional[int] = None
    missing: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)


@dataclass
class SubjectiveRow:
    code: str
    name: str
    student: Optional[User]
    score: float
    per_image: Dict[str, float] = field(default_factory=dict)
    comparisons: int = 0
    rank: Optional[int] = None


@dataclass
class CombinedRow:
    """两个任务主观榜的综合行：综合分 = 加权平均，缺一项则 score 为 None。"""

    code: str
    name: str
    student: Optional[User]
    score: Optional[float] = None
    denoise_score: Optional[float] = None
    denoise_rank: Optional[int] = None
    decolor_score: Optional[float] = None
    decolor_rank: Optional[int] = None
    comparisons: int = 0
    rank: Optional[int] = None
    missing: List[str] = field(default_factory=list)


def _mean(values: Sequence[Optional[float]]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def _final_submissions(task: TaskSpec, include_baseline: bool = False):
    qs = Submission.objects.filter(task=task.slug, is_final=True).select_related(
        "student", "student__profile"
    )
    if not include_baseline:
        qs = qs.filter(is_baseline=False, student__isnull=False)
    return qs.order_by("student_id", "image_key")


def objective_rows(task: TaskSpec) -> Tuple[List[ObjectiveRow], List[ObjectiveRow], List[ObjectiveRow]]:
    """返回 (完整排名, 未完成作品, 基线排名)。"""
    rows: List[ObjectiveRow] = []
    incomplete: List[ObjectiveRow] = []
    baselines: List[ObjectiveRow] = []

    by_student: Dict[int, Dict[str, Submission]] = {}
    by_baseline: Dict[str, Dict[str, Submission]] = {}

    for sub in _final_submissions(task, include_baseline=True):
        if sub.is_baseline:
            by_baseline.setdefault(sub.baseline_method, {})[sub.image_key] = sub
        elif sub.student_id:
            by_student.setdefault(sub.student_id, {})[sub.image_key] = sub

    for student_id, per_image in by_student.items():
        student = next(iter(per_image.values())).student
        profile = getattr(student, "profile", None)
        row = ObjectiveRow(
            code=profile.contest_code if profile else student.username,
            name=profile.real_name if profile else "",
            student=student,
            per_image={k: v.metrics for k, v in per_image.items()},
        )
        missing = [img.key for img in task.images if img.key not in per_image]
        row.missing = missing
        if missing:
            incomplete.append(row)
            continue
        row.mean = _mean([v.metrics.get(task.ranking_metric) for v in per_image.values()])
        row.tiebreak = _mean([v.metrics.get(task.tiebreak_metric) for v in per_image.values()])
        rows.append(row)

    rows.sort(key=lambda r: (-(r.mean or 0), -(r.tiebreak or 0)))
    for i, row in enumerate(rows, start=1):
        row.rank = i

    for method, per_image in by_baseline.items():
        row = ObjectiveRow(
            code=BASELINE_LABELS.get(method, method), name=method, student=None,
            per_image={k: v.metrics for k, v in per_image.items()},
        )
        row.mean = _mean([v.metrics.get(task.ranking_metric) for v in per_image.values()])
        row.tiebreak = _mean([v.metrics.get(task.tiebreak_metric) for v in per_image.values()])
        baselines.append(row)
    baselines.sort(key=lambda r: -(r.mean or 0))

    return rows, incomplete, baselines


def _matches_for_image(task: TaskSpec, image_key: str) -> Tuple[List[int], List[Tuple[int, int, float]], Dict[int, int]]:
    """返回 (参赛提交 id 列表, 比赛记录, id → 参赛次数)。"""
    items = list(
        Submission.objects.filter(
            task=task.slug, image_key=image_key, is_final=True, is_baseline=False
        ).values_list("id", flat=True)
    )
    item_set = set(items)
    matches: List[Tuple[int, int, float]] = []
    counts: Dict[int, int] = {i: 0 for i in items}

    comps = (
        Comparison.objects.filter(assignment__task=task.slug, assignment__image_key=image_key)
        .select_related("assignment")
    )
    for comp in comps:
        left = comp.assignment.left_id
        right = comp.assignment.right_id
        if left not in item_set or right not in item_set:
            continue
        if comp.choice == "left":
            outcome = 1.0
        elif comp.choice == "right":
            outcome = 0.0
        else:
            outcome = 0.5
        matches.append((left, right, outcome))
        counts[left] = counts.get(left, 0) + 1
        counts[right] = counts.get(right, 0) + 1
    return items, matches, counts


def subjective_rows(task: TaskSpec) -> List[SubjectiveRow]:
    """按原图分别拟合 Bradley–Terry，再按学生汇总为 0–100 的主观分。"""
    config = load_config()
    reg = config.peer_review.regularization

    subs = {
        s.id: s
        for s in Submission.objects.filter(
            task=task.slug, is_final=True, is_baseline=False
        ).select_related("student", "student__profile")
    }
    # student_id -> {image_key: 预测胜率(%)}
    per_student: Dict[int, Dict[str, float]] = {}
    times: Dict[int, int] = {}

    for img in task.images:
        items, matches, counts = _matches_for_image(task, img.key)
        if len(items) < 2 or not matches:
            continue
        result = bt.bradley_terry(items, matches, regularization=reg)
        for item, prob in result.win_prob.items():
            sub = subs.get(item)
            if sub is None or sub.student_id is None:
                continue
            per_student.setdefault(sub.student_id, {})[img.key] = round(prob * 100.0, 2)
            times[sub.student_id] = times.get(sub.student_id, 0) + counts.get(item, 0)

    rows: List[SubjectiveRow] = []
    for student_id, per_image in per_student.items():
        student = next(
            (s.student for s in subs.values() if s.student_id == student_id), None
        )
        if student is None:
            continue
        profile = getattr(student, "profile", None)
        score = _mean(list(per_image.values())) or 0.0
        rows.append(
            SubjectiveRow(
                code=profile.contest_code if profile else student.username,
                name=profile.real_name if profile else "",
                student=student,
                score=round(score, 2),
                per_image=per_image,
                comparisons=times.get(student_id, 0),
            )
        )

    rows.sort(key=lambda r: -r.score)
    rank = 0
    last_score = None
    for i, row in enumerate(rows, start=1):
        if last_score is None or abs(row.score - last_score) > 1e-9:
            rank = i
            last_score = row.score
        row.rank = rank
    return rows


def composite_rows(task: TaskSpec):
    """去色综合参考分：Q = 100*(w_obj*E + w_subj*R)。"""
    config = load_config()
    w_obj = config.scoring.weight_objective
    w_subj = config.scoring.weight_subjective
    try:
        from competition.models import AppSetting

        setting = AppSetting.get()
        if setting.weight_objective is not None and setting.weight_subjective is not None:
            w_obj, w_subj = setting.weight_objective, setting.weight_subjective
    except Exception:  # noqa: BLE001
        pass

    obj_rows, _, _ = objective_rows(task)
    subj = {r.student_id: r for r in subjective_rows(task)}
    out = []
    for row in obj_rows:
        subj_row = subj.get(row.student.id if row.student else None)
        e = row.mean or 0.0
        r = (subj_row.score / 100.0) if subj_row else None
        q = None
        if r is not None:
            q = 100.0 * (w_obj * e + w_subj * r)
        out.append({"row": row, "subjective": subj_row, "q": q})
    return out, w_obj, w_subj


def combined_subjective_rows():
    """两个任务各自 BT 拟合后，合成一张综合主观榜。

    综合分 = w_denoise × 去噪主观分 + w_decolor × 去色主观分（权重来自 yaml，自动归一化）。
    两个任务都有主观分的学生才参与排名；只完成一项的放在「未纳入」，避免半张成绩
    被当成完整成绩和别人比。
    """
    config = load_config()
    weights = config.scoring.combined_weights()
    denoise = config.task("denoise")
    decolor = config.task("decolor")

    d_rows = {r.student.id: r for r in subjective_rows(denoise) if r.student} if denoise else {}
    c_rows = {r.student.id: r for r in subjective_rows(decolor) if r.student} if decolor else {}

    rows: List[CombinedRow] = []
    pending: List[CombinedRow] = []

    for student_id in set(d_rows) | set(c_rows):
        d, c = d_rows.get(student_id), c_rows.get(student_id)
        student = (d or c).student
        profile = getattr(student, "profile", None)
        row = CombinedRow(
            code=profile.contest_code if profile else student.username,
            name=profile.real_name if profile else "",
            student=student,
            denoise_score=d.score if d else None,
            denoise_rank=d.rank if d else None,
            decolor_score=c.score if c else None,
            decolor_rank=c.rank if c else None,
            comparisons=(d.comparisons if d else 0) + (c.comparisons if c else 0),
        )
        if d and c:
            row.score = round(
                weights["denoise"] * d.score + weights["decolor"] * c.score, 2
            )
            rows.append(row)
        else:
            row.missing = [
                t.short_name for t, r in ((denoise, d), (decolor, c))
                if r is None and t is not None
            ]
            pending.append(row)

    rows.sort(
        key=lambda r: (
            -(r.score or 0),
            -min(r.denoise_score or 0, r.decolor_score or 0),
            -r.comparisons,
        )
    )
    rank = 0
    last = None
    for i, row in enumerate(rows, start=1):
        if last is None or abs((row.score or 0) - last) > 1e-9:
            rank = i
            last = row.score
        row.rank = rank

    pending.sort(key=lambda r: r.code)
    return rows, pending, weights


def combined_by_student() -> Dict[int, CombinedRow]:
    """导出/个人页用：学生 → 综合行（含未纳入者，score 为 None）。"""
    rows, pending, _ = combined_subjective_rows()
    out: Dict[int, CombinedRow] = {}
    for row in rows + pending:
        if row.student:
            out[row.student.id] = row
    return out


def review_stats(task: TaskSpec) -> Dict[str, object]:
    """互评进度与覆盖情况，供教师后台查看。"""
    total = Submission.objects.filter(
        task=task.slug, is_final=True, is_baseline=False
    ).count()
    from competition.models import ReviewAssignment

    assigned = ReviewAssignment.objects.filter(task=task.slug).count()
    done = Comparison.objects.filter(assignment__task=task.slug).count()
    per_image: Dict[str, Dict[str, int]] = {}
    for img in task.images:
        _, matches, counts = _matches_for_image(task, img.key)
        per_image[img.key] = {
            "entries": len(counts),
            "comparisons": len(matches),
            "min": min(counts.values()) if counts else 0,
            "max": max(counts.values()) if counts else 0,
            "uncovered": sum(1 for v in counts.values() if v == 0),
        }
    return {
        "finals": total,
        "assigned": assigned,
        "done": done,
        "progress": round(done / assigned * 100, 1) if assigned else 0.0,
        "per_image": per_image,
    }


def refresh_subjective(task: TaskSpec) -> int:
    """把主观排名写入数据库快照，返回更新条数。"""
    rows = subjective_rows(task)
    SubjectiveRanking.objects.filter(task=task.slug).delete()
    SubjectiveRanking.objects.bulk_create(
        [
            SubjectiveRanking(
                student=r.student,
                task=task.slug,
                score=r.score,
                per_image=r.per_image,
                comparisons=r.comparisons,
                rank=r.rank or 0,
            )
            for r in rows
        ]
    )
    return len(rows)


def refresh_objective(task: TaskSpec) -> int:
    """把客观排名（按 ``task.ranking_metric`` 均值）写入快照，返回更新条数。

    排行榜页面仍然实时计算；这张表只服务教师后台的浏览、筛选与导出，
    所以每次重算都整表替换该任务的行，避免留下过期名次。
    """
    rows, _, _ = objective_rows(task)
    with transaction.atomic():
        ObjectiveRanking.objects.filter(task=task.slug).delete()
        ObjectiveRanking.objects.bulk_create(
            [
                ObjectiveRanking(
                    student=r.student,
                    task=task.slug,
                    metric=task.ranking_metric,
                    score=r.mean,
                    tiebreak=r.tiebreak,
                    per_image={k: v for k, v in r.per_image.items()},
                    rank=r.rank or 0,
                )
                for r in rows
                if r.student is not None
            ]
        )
    return len(rows)


def refresh_all_objective() -> Dict[str, int]:
    """按配置里的每个任务重算客观排名，返回 {slug: 条数}。"""
    return {task.slug: refresh_objective(task) for task in load_config().tasks}
