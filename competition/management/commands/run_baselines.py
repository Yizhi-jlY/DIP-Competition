"""生成并评测经典算法基线，作为学生成绩的参照。

去噪：原始含噪图、Gaussian、Median、Bilateral、Non-Local Means、BM3D（可选）
去色：Rec.709 亮度灰度、OpenCV decolor（对比度保持）、加权线性灰度
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from PIL import Image

from competition.config import load_config
from competition.imaging import load_gray_u8, load_rgb_u8, save_png
from competition.models import Submission
from competition.services.scoring import evaluate_path

logger = logging.getLogger(__name__)

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


# 真实噪声在暗部更强，单一全局 σ 估计会低估；教师可为每张图指定 BM3D 的 σ
BM3D_SIGMA = {"D01": 0.15, "D02": 0.10}
BM3D_DEFAULT_SIGMA = 0.10


def _denoise_identity(noisy: np.ndarray, key: str = "") -> np.ndarray:
    return noisy


def _denoise_gaussian(noisy: np.ndarray, key: str = "") -> np.ndarray:
    return cv2.GaussianBlur(noisy, (5, 5), 0)


def _denoise_median(noisy: np.ndarray, key: str = "") -> np.ndarray:
    return cv2.medianBlur(noisy, 5)


def _denoise_bilateral(noisy: np.ndarray, key: str = "") -> np.ndarray:
    return cv2.bilateralFilter(noisy, 9, 75, 75)


def _denoise_nlm(noisy: np.ndarray, key: str = "") -> np.ndarray:
    return cv2.fastNlMeansDenoisingColored(noisy, None, 15, 15, 7, 21)


def _denoise_bm3d(noisy: np.ndarray, key: str = "") -> np.ndarray:
    import bm3d  # type: ignore

    sigma = BM3D_SIGMA.get(key, BM3D_DEFAULT_SIGMA)
    out = bm3d.bm3d(noisy.astype(np.float32) / 255.0, sigma, stage_arg=bm3d.BM3DStages.ALL_STAGES)
    return np.clip(out * 255.0, 0, 255).astype(np.uint8)


def _decolor_rgb2gray(rgb: np.ndarray, key: str = "") -> np.ndarray:
    from skimage.color import rgb2gray

    gray = rgb2gray(rgb.astype(np.float64) / 255.0)
    return np.round(gray * 255.0).clip(0, 255).astype(np.uint8)


def _decolor_opencv(rgb: np.ndarray, key: str = "") -> np.ndarray:
    gray, _ = cv2.decolor(rgb)
    if gray.ndim == 3:
        gray = gray[:, :, 0]
    return gray.astype(np.uint8)


def _decolor_light_linear(rgb: np.ndarray, key: str = "") -> np.ndarray:
    arr = rgb.astype(np.float64)
    out = 0.25 * arr[:, :, 0] + 0.5 * arr[:, :, 1] + 0.25 * arr[:, :, 2]
    return np.round(out).clip(0, 255).astype(np.uint8)


DENOISE_METHODS = {
    "identity": _denoise_identity,
    "gaussian": _denoise_gaussian,
    "median": _denoise_median,
    "bilateral": _denoise_bilateral,
    "nlm": _denoise_nlm,
    "bm3d": _denoise_bm3d,
}

DECOLOR_METHODS = {
    "rgb2gray": _decolor_rgb2gray,
    "opencv_decolor": _decolor_opencv,
    "light_linear": _decolor_light_linear,
}


class Command(BaseCommand):
    help = "生成并评测经典算法基线"

    def add_arguments(self, parser):
        parser.add_argument("--task", default="all", choices=["all", "denoise", "decolor"])
        parser.add_argument("--keep", action="store_true", help="保留旧基线记录（默认重建）")

    def handle(self, *args, **options):
        if cv2 is None:
            self.stderr.write("未安装 OpenCV，部分基线不可用。")
        config = load_config()
        slugs = ["denoise", "decolor"] if options["task"] == "all" else [options["task"]]

        for slug in slugs:
            task = config.task(slug)
            if not task:
                self.stderr.write(f"未配置任务 {slug}")
                continue
            if not task.images_ready() or not task.reference_ready():
                self.stderr.write(f"{slug}: 数据尚未准备，跳过。请先运行 prepare_data。")
                continue
            self._run_task(task, keep=options["keep"])

    def _run_task(self, task, keep: bool) -> None:
        if not keep:
            Submission.objects.filter(task=task.slug, is_baseline=True).delete()

        methods = DENOISE_METHODS if task.channels == 3 else DECOLOR_METHODS
        wanted = [m for m in task.baseline_methods if m in methods] or list(methods)
        base_dir = Path(settings.MEDIA_ROOT) / "baselines" / task.slug

        for method in wanted:
            func = methods[method]
            for img in task.images:
                out_path = base_dir / method / f"{img.key}.png"
                src = task.public_image(img.key)
                if not src.exists():
                    self.stderr.write(f"缺少输入图 {src}")
                    continue
                try:
                    array = func(load_rgb_u8(src), img.key)
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(f"基线 {method} 在 {img.key} 上失败：{exc}")
                    continue
                save_png(array, out_path)

                try:
                    metrics = evaluate_path(task, img.key, out_path)
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(f"基线 {method} 评分失败：{exc}")
                    continue

                rel = out_path.relative_to(Path(settings.MEDIA_ROOT)).as_posix()
                Submission.objects.update_or_create(
                    task=task.slug,
                    image_key=img.key,
                    is_baseline=True,
                    baseline_method=method,
                    defaults={
                        "file": rel,
                        "original_name": out_path.name,
                        "width": array.shape[1],
                        "height": array.shape[0],
                        "channels": 1 if array.ndim == 2 else array.shape[2],
                        "size_bytes": out_path.stat().st_size,
                        "metrics": metrics,
                        "rank_value": metrics.get(task.ranking_metric),
                        "is_final": True,
                        "scored_at": timezone.now(),
                    },
                )
                primary = task.metric(task.ranking_metric)
                value = metrics.get(task.ranking_metric, 0)
                self.stdout.write(
                    f"  {task.slug}/{method}/{img.key}: "
                    f"{primary.name if primary else task.ranking_metric}={value:.4f}"
                )
