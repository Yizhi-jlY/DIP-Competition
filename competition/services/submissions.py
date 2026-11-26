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


def remaining_attempts(student: User, task: TaskSpec, image_key: str) -> int:
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
    if used >= task.max_submissions:
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


def confirm_final(student: User, task: TaskSpec) -> List[Submission]:
    """把每张图最近一次提交标记为最终作品，其余取消标记。"""
    phase = current_phase()
    if not phase.submission_open:
        raise SubmitError(f"当前阶段为「{phase.label}」，无法再确认最终作品。")

    finals: List[Submission] = []
    with transaction.atomic():
        for img in task.images:
            latest = (
                Submission.objects.filter(
                    student=student, task=task.slug, image_key=img.key, is_baseline=False
                )
                .order_by("-created_at", "-id")
                .first()
            )
            if latest is None:
                raise SubmitError(f"{img.label} 还没有提交结果，无法确认最终作品。")
            Submission.objects.filter(
                student=student, task=task.slug, image_key=img.key, is_final=True
            ).exclude(pk=latest.pk).update(is_final=False)
            if not latest.is_final:
                latest.is_final = True
                latest.save(update_fields=["is_final"])
            finals.append(latest)
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
