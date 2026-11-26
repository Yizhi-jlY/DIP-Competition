"""客观评价指标：图像去噪（PSNR / SSIM）。

实现直接调用 scikit-image，并显式固定参数（data_range、win_size、通道位置），
保证同一份结果图在任何机器上得到完全一致的分数。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

import numpy as np
from skimage.metrics import peak_signal_noise_ratio, structural_similarity

from competition.imaging import load_rgb_u8


def psnr_ssim(
    ground_truth: np.ndarray,
    result: np.ndarray,
    data_range: float = 255.0,
    ssim_win_size: int = 7,
) -> Dict[str, float]:
    """输入两张同尺寸 uint8 RGB 图像，返回 psnr / ssim。"""
    if ground_truth.shape != result.shape:
        raise ValueError(
            f"结果图尺寸 {result.shape[1]}x{result.shape[0]} 与参考图 "
            f"{ground_truth.shape[1]}x{ground_truth.shape[0]} 不一致"
        )

    psnr = float(peak_signal_noise_ratio(ground_truth, result, data_range=data_range))
    if not np.isfinite(psnr):
        # 与参考图完全一致时 PSNR 为无穷，落库前截断，避免 JSON 校验失败
        psnr = 99.0

    min_side = min(result.shape[0], result.shape[1])
    win = int(ssim_win_size)
    if win % 2 == 0:
        win -= 1
    win = max(3, min(win, min_side if min_side % 2 == 1 else min_side - 1))
    ssim = float(
        structural_similarity(
            ground_truth,
            result,
            data_range=data_range,
            channel_axis=-1,
            win_size=win,
            gaussian_weights=False,
        )
    )
    if not np.isfinite(ssim):
        ssim = 1.0
    return {"psnr": round(psnr, 4), "ssim": round(ssim, 6)}


def evaluate_files(
    gt_path: Path | str,
    result_path: Path | str,
    data_range: float = 255.0,
    ssim_win_size: int = 7,
) -> Dict[str, float]:
    return psnr_ssim(
        load_rgb_u8(gt_path),
        load_rgb_u8(result_path),
        data_range=data_range,
        ssim_win_size=ssim_win_size,
    )
