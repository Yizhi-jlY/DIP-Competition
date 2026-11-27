"""把一张已保存的结果图评分，并把指标写回提交记录。

指标怎么算不在这里 —— 见 `competition/research/evaluators.py`（任务 → 指标族）
与 `competition/research/params.py`（阈值、窗口、种子）。
本模块只负责「调用 → 成功写回 / 失败留痕」。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

from django.utils import timezone

from competition.config import TaskSpec
from competition.models import Submission
from competition.research import evaluators

logger = logging.getLogger(__name__)


def score_submission(submission: Submission, task: TaskSpec) -> Dict[str, float]:
    """对已保存的提交评分，并写回 metrics / rank_value。异常写入 score_error。"""
    try:
        metrics = evaluators.evaluate(
            task, submission.image_key, Path(submission.file.path)
        )
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
