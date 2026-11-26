"""提交图像的自动评分。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

from django.utils import timezone

from competition.config import TaskSpec, load_config
from competition.metrics import decolor, denoise
from competition.models import Submission

logger = logging.getLogger(__name__)


def evaluate_path(task: TaskSpec, image_key: str, path: Path) -> Dict[str, float]:
    """按任务类型调用对应指标。去噪需要私有参考图，去色使用公开彩色输入。"""
    options = load_config().metrics_options

    if task.channels == 3:
        gt = task.ground_truth(image_key)
        if not gt.exists():
            raise FileNotFoundError(f"缺少参考图：{gt.name}")
        return denoise.evaluate_files(
            gt,
            path,
            data_range=float(options.get("data_range", 255)),
            ssim_win_size=int(options.get("ssim_win_size", 7)),
        )

    color = task.public_image(image_key)
    if not color.exists():
        raise FileNotFoundError(f"缺少输入图：{color.name}")
    # 保证结果图与输入图尺寸一致
    return decolor.evaluate_files(
        color,
        path,
        tau=float(options.get("decolor_tau", decolor.DEFAULT_TAU)),
        rounds=int(options.get("decolor_pairs_per_pixel", decolor.DEFAULT_ROUNDS)),
        seed=int(options.get("decolor_seed", decolor.DEFAULT_SEED)),
    )


def score_submission(submission: Submission, task: TaskSpec) -> Dict[str, float]:
    """对已保存的提交评分，并写回 metrics / rank_value。异常写入 score_error。"""
    try:
        metrics = evaluate_path(task, submission.image_key, Path(submission.file.path))
    except Exception as exc:  # noqa: BLE001 - 评分失败必须留痕而不是 500
        submission.score_error = str(exc)[:200]
        submission.scored_at = timezone.now()
        submission.save(update_fields=["score_error", "scored_at"])
        logger.warning("评分失败 submission=%s: %s", submission.pk, exc)
        return {}

    submission.metrics = metrics
    submission.rank_value = metrics.get(task.ranking_metric)
    submission.score_error = ""
    submission.scored_at = timezone.now()
    submission.save(update_fields=["metrics", "rank_value", "score_error", "scored_at"])
    return metrics
