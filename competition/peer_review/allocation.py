"""匿名互评的任务分配。

要求（与方案一致）：
- 每位学生完成固定次数的两两比较，并在两张原图上平均分配；
- 不评价自己的作品；
- 同一评价者不重复评价同一对图像；
- 每张图被评价的次数尽量均衡，且所有作品至少被比较一次（连通、可排名）；
- 分配结果可复现（固定随机种子）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

import numpy as np


@dataclass
class PlannedAssignment:
    reviewer_key: str
    image_key: str
    a_id: int
    b_id: int


@dataclass
class AllocationReport:
    assignments: List[PlannedAssignment] = field(default_factory=list)
    degree: Dict[Tuple[str, int], int] = field(default_factory=dict)
    per_reviewer: Dict[str, int] = field(default_factory=dict)
    per_image: Dict[str, int] = field(default_factory=dict)
    repaired: int = 0
    uncovered: List[Tuple[str, int]] = field(default_factory=list)


def _round_robin_matches(
    entries: Sequence[int], rng: np.random.Generator
) -> List[Tuple[int, int]]:
    order = list(entries)
    rng.shuffle(order)
    return [(order[i], order[i + 1]) for i in range(0, len(order) - 1, 2)]


def allocate(
    entries_by_image: Mapping[str, Sequence[int]],
    reviewers: Sequence[Tuple[str, Mapping[int, int]]],
    quota_per_image,
    seed: int = 2026,
) -> AllocationReport:
    """reviewers: [(reviewer_key, {image_key: own_submission_id}), ...]。

    quota_per_image 可以是统一的整数，也可以是
    {reviewer_key: {image_key: quota}} 形式的逐人配额。
    """
    rng = np.random.default_rng(seed)
    report = AllocationReport()
    if not reviewers:
        return report

    images = [k for k, v in entries_by_image.items() if len(v) >= 2]
    for image_key in images:
        entries = sorted(entries_by_image[image_key])
        report.degree.update({(image_key, e): 0 for e in entries})
        report.per_image[image_key] = 0

    for reviewer_key, own in reviewers:
        total = 0
        for image_key in images:
            quota = _quota_for(quota_per_image, reviewer_key, image_key)
            if quota <= 0:
                continue
            own_id = own.get(image_key)
            total += _assign_for_image(
                report, image_key, entries_by_image[image_key], reviewer_key,
                own_id, quota, rng,
            )
        report.per_reviewer[reviewer_key] = total

    _repair(report, entries_by_image, reviewers, images, rng)
    report.uncovered = [k for k, v in report.degree.items() if v == 0]
    return report


def _quota_for(quota, reviewer_key: str, image_key: str) -> int:
    if isinstance(quota, Mapping):
        return int((quota.get(reviewer_key) or {}).get(image_key, 0))
    return int(quota)


def _assign_for_image(
    report: AllocationReport,
    image_key: str,
    entries: Sequence[int],
    reviewer_key: str,
    own_id: int | None,
    quota: int,
    rng: np.random.Generator,
) -> int:
    pool = _build_pool([int(e) for e in entries], quota, rng)
    used_pairs = {
        (a.image_key, frozenset((a.a_id, a.b_id)))
        for a in report.assignments
        if a.reviewer_key == reviewer_key
    }
    assigned = 0
    for _ in range(quota):
        candidates = [
            (
                report.degree.get((image_key, a), 0) + report.degree.get((image_key, b), 0),
                count,
                rng.random(),
                a,
                b,
            )
            for (a, b), count in pool.items()
            if a != own_id and b != own_id
            and (image_key, frozenset((a, b))) not in used_pairs
        ]
        if not candidates:
            break
        # 先让“被评价次数最少”的作品入选，其次才是候选池中使用较少的图像对
        candidates.sort(key=lambda t: (t[0], t[1], t[2]))
        _, _, _, a, b = candidates[0]
        pair = (a, b) if rng.random() < 0.5 else (b, a)
        report.assignments.append(
            PlannedAssignment(reviewer_key, image_key, pair[0], pair[1])
        )
        pool[(a, b)] += 1
        used_pairs.add((image_key, frozenset((a, b))))
        report.degree[(image_key, a)] = report.degree.get((image_key, a), 0) + 1
        report.degree[(image_key, b)] = report.degree.get((image_key, b), 0) + 1
        report.per_image[image_key] = report.per_image.get(image_key, 0) + 1
        assigned += 1
    return assigned


def _build_pool(
    entries: Sequence[int], quota: int, rng: np.random.Generator
) -> Dict[Tuple[int, int], int]:
    """候选图像对池。

    小规模（≤40 张）时直接枚举全部组合，保证配额总能被满足；
    大规模时用多轮随机配对采样，避免 O(k²) 的内存与计算开销。
    """
    k = len(entries)
    if k < 2:
        return {}
    pool: Dict[Tuple[int, int], int] = {}
    if k <= 40:
        for i in range(k):
            for j in range(i + 1, k):
                a, b = entries[i], entries[j]
                pool[(a, b) if a < b else (b, a)] = 0
        return pool

    per_round = k // 2
    rounds = max(2, int(np.ceil((quota + 1) / max(per_round, 1) * k / 2)))
    rounds = min(rounds, 40)
    for _ in range(rounds):
        for a, b in _round_robin_matches(entries, rng):
            key = (a, b) if a < b else (b, a)
            pool.setdefault(key, 0)
    return pool


def _repair(
    report: AllocationReport,
    entries_by_image: Mapping[str, Sequence[int]],
    reviewers: Sequence[Tuple[str, Mapping[int, int]]],
    images: Sequence[str],
    rng: np.random.Generator,
) -> None:
    """保证零覆盖的作品至少被比较一次。"""
    for image_key in images:
        entries = [int(e) for e in entries_by_image[image_key]]
        for entry in entries:
            if report.degree.get((image_key, entry), 0) > 0:
                continue
            others = [e for e in entries if e != entry]
            if not others:
                continue
            for reviewer_key, own in reviewers:
                if own.get(image_key) == entry:
                    continue
                used = {
                    frozenset((a.a_id, a.b_id))
                    for a in report.assignments
                    if a.reviewer_key == reviewer_key and a.image_key == image_key
                }
                partner = None
                for cand in sorted(others, key=lambda e: report.degree.get((image_key, e), 0)):
                    if frozenset((entry, cand)) not in used:
                        partner = cand
                        break
                if partner is None:
                    continue
                a, b = (entry, partner) if rng.random() < 0.5 else (partner, entry)
                report.assignments.append(
                    PlannedAssignment(reviewer_key, image_key, a, b)
                )
                report.degree[(image_key, entry)] += 1
                report.degree[(image_key, partner)] = report.degree.get((image_key, partner), 0) + 1
                report.per_reviewer[reviewer_key] = report.per_reviewer.get(reviewer_key, 0) + 1
                report.per_image[image_key] = report.per_image.get(image_key, 0) + 1
                report.repaired += 1
                break


def validate(
    assignments: Iterable[Tuple[str, str, int, int]],
    own_submissions: Mapping[str, Mapping[str, int]],
    expected_per_reviewer: int | None = None,
) -> List[str]:
    """检查分配结果的硬约束，返回问题列表（教师后台与测试使用）。"""
    issues: List[str] = []
    seen: Dict[Tuple[str, str, frozenset], int] = {}
    per_reviewer: Dict[str, int] = {}
    degree: Dict[Tuple[str, int], int] = {}
    for reviewer, image_key, a, b in assignments:
        if a == b:
            issues.append(f"{reviewer} 在 {image_key} 上收到同一张图与自己比较")
        own_id = own_submissions.get(reviewer, {}).get(image_key)
        if own_id is not None and own_id in (a, b):
            issues.append(f"{reviewer} 在 {image_key} 上评价了自己的作品")
        key = (reviewer, image_key, frozenset((a, b)))
        seen[key] = seen.get(key, 0) + 1
        per_reviewer[reviewer] = per_reviewer.get(reviewer, 0) + 1
        degree[(image_key, a)] = degree.get((image_key, a), 0) + 1
        degree[(image_key, b)] = degree.get((image_key, b), 0) + 1
    dup = [k for k, v in seen.items() if v > 1]
    if dup:
        issues.append(f"存在 {len(dup)} 组重复分配（同一评价者重复评价同一对）")
    if expected_per_reviewer is not None:
        for reviewer, count in per_reviewer.items():
            if count != expected_per_reviewer:
                issues.append(f"{reviewer} 的评价次数 {count} ≠ 配额 {expected_per_reviewer}")
    uncovered = [k for k, v in degree.items() if v == 0]
    if uncovered:
        issues.append(f"有 {len(uncovered)} 张作品从未被评价")
    return issues


def coverage_summary(report: AllocationReport) -> Dict[str, float]:
    degrees = list(report.degree.values())
    if not degrees:
        return {"entries": 0, "min": 0, "max": 0, "mean": 0, "uncovered": 0}
    return {
        "entries": len(degrees),
        "min": min(degrees),
        "max": max(degrees),
        "mean": round(sum(degrees) / len(degrees), 2),
        "uncovered": sum(1 for d in degrees if d == 0),
    }
