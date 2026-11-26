"""提交校验、保存、次数限制与最终作品冻结。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from django.contrib.auth.models import User
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.utils import timezone

from competition.config import TaskSpec
from competition.imaging import ImageMeta, ImageValidationError, inspect_image, reference_size
from competition.models import Submission
from competition.services.phase import current_phase
from competition.services.scoring import score_submission

logger = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024


class SubmitError(Exception):
    """提交被业务规则拒绝时抛出。"""


def validate_upload(
    task: TaskSpec,
    image_key: str,
    uploaded: UploadedFile,
    tmp_path: Optional[Path] = None,
) -> ImageMeta:
    if uploaded.size is None or uploaded.size == 0:
        raise ImageValidationError("上传的文件为空。")
    if uploaded.size > MAX_BYTES:
        raise ImageValidationError(
            f"文件大小 {uploaded.size / 1024 / 1024:.1f} MB 超过 20 MB 上限。"
        )
    name = (uploaded.name or "").lower()
    if not name.endswith(".png"):
        raise ImageValidationError("请上传 PNG 格式的结果图（扩展名 .png）。")

    try:
        meta = inspect_image(uploaded)
    except ImageValidationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ImageValidationError("图像无法解析，请确认文件未损坏。") from exc

    if meta.format != "PNG":
        raise ImageValidationError(f"检测到实际格式为 {meta.format or '未知'}，请另存为 PNG。")

    if task.channels == 3 and meta.mode != "RGB":
        raise ImageValidationError(
            f"去噪任务需要 8-bit RGB 三通道 PNG，当前为 {meta.mode}（{meta.channels} 通道）。"
        )
    if task.channels == 1 and meta.mode != "L":
        raise ImageValidationError(
            f"去色任务需要 8-bit 灰度 PNG（单通道 L），当前为 {meta.mode}（{meta.channels} 通道）。"
        )

    ref = reference_size(task, image_key)
    if ref and (meta.width, meta.height) != ref:
        raise ImageValidationError(
            f"尺寸必须与输入图完全一致：要求 {ref[0]}×{ref[1]}，当前 {meta.width}×{meta.height}。"
        )
    return meta


def attempts_used(student: User, task: TaskSpec, image_key: str) -> int:
    return Submission.objects.filter(
        student=student, task=task.slug, image_key=image_key, is_baseline=False
    ).count()


def remaining_attempts(student: User, task: TaskSpec, image_key: str) -> Optional[int]:
    """剩余提交次数；不限制时返回 None。"""
    if task.unlimited:
        return None
    return max(0, task.max_submissions - attempts_used(student, task, image_key))


def create_submission(
    student: User, task: TaskSpec, image_key: str, uploaded: UploadedFile
) -> Tuple[Submission, Dict[str, float]]:
    phase = current_phase()
    if not phase.submission_open:
        raise SubmitError(f"当前阶段为「{phase.label}」，不接受新的提交。")

    image_keys = [img.key for img in task.images]
    if image_key not in image_keys:
        raise SubmitError("无效的图像编号。")

    used = attempts_used(student, task, image_key)
    if not task.unlimited and used >= task.max_submissions:
        raise SubmitError(
            f"{image_key} 已提交 {used} 次，达到单图 {task.max_submissions} 次上限。"
        )

    meta = validate_upload(task, image_key, uploaded)

    submission = Submission(
        student=student,
        task=task.slug,
        image_key=image_key,
        original_name=(uploaded.name or "")[:128],
        width=meta.width,
        height=meta.height,
        channels=meta.channels,
        size_bytes=meta.size_bytes,
        attempt=used + 1,
    )
    with transaction.atomic():
        submission.file.save(
            f"{student.username}_{task.slug}_{image_key}_{used + 1}.png",
            uploaded,
            save=False,
        )
        submission.save()

    metrics = score_submission(submission, task)
    if not metrics:
        error = submission.score_error
        submission.delete()
        raise SubmitError(
            "评分失败，请确认图像尺寸与通道正确后重试，或联系教师。"
            + (f"（{error}）" if error else "")
        )

    logger.info(
        "submit student=%s task=%s image=%s attempt=%s metrics=%s",
        student.username, task.slug, image_key, submission.attempt, metrics,
    )
    return submission, metrics


def candidates(student: User, task: TaskSpec, image_key: str) -> List[Submission]:
    """某张图的全部候选提交，按排名指标从好到底排序（无法评分排在最后）。"""
    rows = list(
        Submission.objects.filter(
            student=student, task=task.slug, image_key=image_key, is_baseline=False
        )
    )
    return sorted(rows, key=lambda s: (_metric_value(s, task), s.created_at, s.pk), reverse=True)


def _metric_value(submission: Submission, task: TaskSpec) -> float:
    metric = task.metric(task.ranking_metric)
    if metric is None:
        return float("-inf")
    value = (submission.metrics or {}).get(metric.key)
    if isinstance(value, (int, float)) and value == value:  # 排除 NaN
        return float(value)
    return float("-inf")


def best_submission(student: User, task: TaskSpec, image_key: str) -> Optional[Submission]:
    """该图像指标最好的一次提交（同分取更晚提交的那次）。"""
    rows = candidates(student, task, image_key)
    return rows[0] if rows else None


def confirm_final(
    student: User, task: TaskSpec, choices: Optional[Dict[str, int]] = None
) -> List[Submission]:
    """确认最终作品。

    choices: {image_key: submission_id}。未指定的图像默认取该图排名指标最好的提交。
    每张被确认的作品会记录 finalized_at，作为「何时确认了哪一次提交」的凭据。
    """
    phase = current_phase()
    if not phase.submission_open:
        raise SubmitError(f"当前阶段为「{phase.label}」，无法再确认最终作品。")

    choices = choices or {}
    now = timezone.now()
    finals: List[Submission] = []
    with transaction.atomic():
        for img in task.images:
            pool = candidates(student, task, img.key)
            if not pool:
                raise SubmitError(f"{img.label} 还没有提交结果，无法确认最终作品。")

            chosen: Optional[Submission] = None
            requested = choices.get(img.key)
            if requested:
                chosen = next((s for s in pool if s.pk == requested), None)
                if chosen is None:
                    raise SubmitError(f"{img.label} 选择的提交不存在，请刷新页面后重试。")
            if chosen is None:
                chosen = pool[0]

            Submission.objects.filter(
                student=student, task=task.slug, image_key=img.key, is_final=True
            ).exclude(pk=chosen.pk).update(is_final=False, finalized_at=None)
            if not chosen.is_final or chosen.finalized_at is None:
                chosen.is_final = True
                chosen.finalized_at = now
                chosen.save(update_fields=["is_final", "finalized_at"])
            finals.append(chosen)
    return finals


def final_submissions(student: User, task: TaskSpec) -> Dict[str, Submission]:
    rows = Submission.objects.filter(
        student=student, task=task.slug, is_final=True, is_baseline=False
    )
    return {row.image_key: row for row in rows}


def latest_submissions(student: User, task: TaskSpec) -> Dict[str, Submission]:
    out: Dict[str, Submission] = {}
    for img in task.images:
        row = (
            Submission.objects.filter(student=student, task=task.slug, image_key=img.key)
            .order_by("-created_at", "-id")
            .first()
        )
        if row:
            out[img.key] = row
    return out
