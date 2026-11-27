"""退化算子：从干净图构造比赛用的输入图。

全部是纯数组 / 纯图像运算，不读写磁盘、不依赖 Django。
`prepare_data` 命令只负责找到源图、调用这里的函数、把结果写成 PNG。

三种退化（对应 yaml 里每张图的 `degradation.kind`）：

    real      真实噪声：源数据自带 (noisy, clean) 成对图，只做同框裁切，不改像素
    gaussian  合成噪声：Y = clip(X + N(0, σ²), 0, 255)，σ 见 params.SYNTHETIC_SIGMA
    none      无退化：彩色原图直接作为输入（去色任务），参考图就是它自己

裁切策略 `texture_crop_box` 在干净图上找梯度能量最高的方形窗口，
让题目尽量包含边缘、文字与纹理，而不是大片平坦天空。
"""

from __future__ import annotations

from typing import Tuple

import numpy as np
from PIL import Image

from competition.research import params


def texture_crop_box(image: Image.Image, size: int, scale: int = 4) -> Tuple[int, int]:
    """在 image 上找 size×size 内梯度能量最高的位置，返回原图坐标 (left, top)。

    为了快，先把图缩到 1/scale 再滑窗；梯度用前向差分近似 |∂x g| + |∂y g|，
    窗口能量用积分图 O(1) 求，避免逐窗口重算。
    """
    small = image.convert("L").resize(
        (max(1, image.width // scale), max(1, image.height // scale))
    )
    g = np.asarray(small, dtype=np.float32)
    gx = np.abs(np.diff(g, axis=1, prepend=g[:, :1]))
    gy = np.abs(np.diff(g, axis=0, prepend=g[:1, :]))
    energy = gx + gy

    win = max(8, size // scale)
    if g.shape[0] <= win or g.shape[1] <= win:
        return 0, 0

    integral = np.zeros((energy.shape[0] + 1, energy.shape[1] + 1), dtype=np.float64)
    integral[1:, 1:] = energy.cumsum(axis=0).cumsum(axis=1)

    def window_sum(y: int, x: int) -> float:
        return float(
            integral[y + win, x + win] - integral[y, x + win]
            - integral[y + win, x] + integral[y, x]
        )

    best_score, best_x, best_y = -1.0, 0, 0
    step = max(8, win // 4)
    for y in range(0, g.shape[0] - win + 1, step):
        for x in range(0, g.shape[1] - win + 1, step):
            score = window_sum(y, x)
            if score > best_score:
                best_score, best_x, best_y = score, x, y

    left = min(best_x * scale, max(0, g.shape[1] * scale - size))
    top = min(best_y * scale, max(0, g.shape[0] * scale - size))
    return left, top


def crop_square(image: Image.Image, box: Tuple[int, int], size: int) -> np.ndarray:
    """按 (left, top) 裁出 size×size 的 RGB uint8 数组，越界时贴边裁。"""
    left = min(max(0, box[0]), max(0, image.width - size))
    top = min(max(0, box[1]), max(0, image.height - size))
    return np.asarray(
        image.convert("RGB").crop((left, top, left + size, top + size)), dtype=np.uint8
    )


def gaussian_noise(
    clean: np.ndarray, sigma: float = None, seed: int = None
) -> np.ndarray:
    """Y = clip(X + N(0, σ²), 0, 255)，返回 uint8。

    σ 与像素值同尺度（0–255）。同一 clean、σ、seed 必得同一结果，
    保证 D02 这类合成噪声图可复现。
    """
    sigma = params.SYNTHETIC_SIGMA if sigma is None else float(sigma)
    seed = params.SYNTHETIC_SEED if seed is None else int(seed)
    rng = np.random.default_rng(seed)
    x = clean.astype(np.float64)
    noisy = np.clip(x + rng.normal(0.0, sigma, x.shape), 0, 255)
    return np.round(noisy).astype(np.uint8)


def identity(image: np.ndarray) -> np.ndarray:
    """无退化：原样返回。去色任务的输入图就是彩色原图本身。"""
    return image


def psnr_of_pair(a: np.ndarray, b: np.ndarray) -> float:
    """两张 uint8 图的 PSNR，只用于数据准备时的自检输出（不参与评分）。"""
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 99.0 if mse == 0 else float(10 * np.log10(255.0 ** 2 / mse))
