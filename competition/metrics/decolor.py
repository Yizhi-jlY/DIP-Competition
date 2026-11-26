"""客观评价指标：图像去色（CCPR / CCFR / E-score）。

本模块是 Cewu Lu、Li Xu、Jiaya Jia 在 IJCV 2014 论文
"Contrast Preserving Decolorization with Perception-Based Quality Metrics"
所公布 MATLAB 评价代码（metric.m）的 Python 移植，计算过程与原实现一一对应：

1. 颜色对比（CCPR 用）：CIELab 空间中对随机像素对求欧氏距离 ΔE。
   原实现用 randperm 为每个像素抽取 10 个伙伴像素；这里用固定随机种子生成
   等量的像素对，并对所有学生使用同一组像素对，保证评分完全可复现。
2. 颜色内容对比（CCFR 用）：对 Lab 三个通道分别做 [1,-1]、[1;-1] 一阶差分，
   再合成为梯度幅值 |∇L|、|∇a|、|∇b|。
3. 灰度对比：把灰度图复制成三通道后转 Lab，取 L 通道；
   像素对对比为 |L(p)-L(q)|，内容对比为 |ΔL|。
4. 指标（τ 为 Lab 距离阈值，原代码遍历 1..40，论文常用 τ=5）：
   CCPR(τ) = #{ΔE>τ 且 |Δg|>τ} / #{ΔE>τ}
   CCFR(τ) = 1 - #{|∇g|>τ 且 ΔE<τ} / #{|∇g|>τ}
   E-score = 2·CCPR·CCFR / (CCPR+CCFR)
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
from skimage.color import rgb2lab

from competition.imaging import load_gray_u8, load_rgb_u8

DEFAULT_TAU = 5.0
DEFAULT_ROUNDS = 10
DEFAULT_SEED = 20140101

_CACHE_SIZE = 8
_cache: Dict[tuple, Dict[str, np.ndarray]] = {}
_cache_lock = threading.Lock()


def _lab_from_rgb(rgb_u8: np.ndarray) -> np.ndarray:
    return rgb2lab(rgb_u8.astype(np.float64) / 255.0)


def _lab_from_gray(gray_u8: np.ndarray) -> np.ndarray:
    rgb = np.repeat(gray_u8.astype(np.float64)[:, :, None] / 255.0, 3, axis=2)
    return rgb2lab(rgb)


def _grad_magnitudes(lab: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """对应 imfilter(...,[1,-1]) 与 imfilter(...,[1;-1])（零填充、相关）。"""
    dx = np.zeros_like(lab)
    dx[:, :-1, :] = lab[:, :-1, :] - lab[:, 1:, :]
    dx[:, -1, :] = lab[:, -1, :]

    dy = np.zeros_like(lab)
    dy[:-1, :, :] = lab[:-1, :, :] - lab[1:, :, :]
    dy[-1, :, :] = lab[-1, :, :]

    gx = np.sqrt(np.square(dx).sum(axis=2))
    gy = np.sqrt(np.square(dy).sum(axis=2))
    return gx, gy


def _pair_indices(n: int, rounds: int, seed: int) -> Tuple[np.ndarray, np.ndarray]:
    """与原实现一致：每轮用两个独立随机排列配对像素。"""
    key = ("pairs", int(n), int(rounds), int(seed))
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit["i1"], hit["i2"]  # type: ignore[index]

    rng = np.random.default_rng(seed)
    first = np.empty((rounds, n), dtype=np.int32)
    second = np.empty((rounds, n), dtype=np.int32)
    for r in range(rounds):
        first[r] = rng.permutation(n)
        second[r] = rng.permutation(n)
    i1 = first.reshape(-1)
    i2 = second.reshape(-1)

    with _cache_lock:
        if len(_cache) >= _CACHE_SIZE:
            _cache.pop(next(iter(_cache)))
        _cache[key] = {"i1": i1, "i2": i2}
    return i1, i2


def _color_statistics(
    rgb_u8: np.ndarray, rounds: int, seed: int
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (像素对 ΔE, 水平梯度, 垂直梯度)，全部为一维 float32。"""
    lab = _lab_from_rgb(rgb_u8)
    h, w = lab.shape[:2]
    n = h * w
    flat = lab.reshape(-1, 3).astype(np.float32)

    i1, i2 = _pair_indices(n, rounds, seed)
    delta = np.sqrt(np.square(flat[i1] - flat[i2]).sum(axis=1)).astype(np.float32)

    gx, gy = _grad_magnitudes(lab)
    return delta, gx.reshape(-1).astype(np.float32), gy.reshape(-1).astype(np.float32)


def _gray_statistics(
    gray_u8: np.ndarray, rounds: int, seed: int
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    lab = _lab_from_gray(gray_u8)
    lum = lab[:, :, 0].astype(np.float32)
    h, w = lum.shape
    n = h * w

    i1, i2 = _pair_indices(n, rounds, seed)
    delta = np.abs(lum.reshape(-1)[i1] - lum.reshape(-1)[i2]).astype(np.float32)

    gx, gy = _grad_magnitudes(lab)
    return delta, gx.reshape(-1).astype(np.float32), gy.reshape(-1).astype(np.float32)


def _cached_color(path: Path, rounds: int, seed: int) -> Dict[str, np.ndarray]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    key = (str(path.resolve()), mtime, int(rounds), int(seed))
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None and "c_delta" in hit:
        return hit

    rgb = load_rgb_u8(path)
    delta, gx, gy = _color_statistics(rgb, rounds, seed)
    data = {"c_delta": delta, "c_gx": gx, "c_gy": gy,
            "shape": np.array(rgb.shape[:2], dtype=np.int32)}
    with _cache_lock:
        if len(_cache) >= _CACHE_SIZE:
            _cache.pop(next(iter(_cache)))
        _cache[key] = data
    return data


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def ratios(
    color_delta: np.ndarray,
    color_grad: np.ndarray,
    gray_delta: np.ndarray,
    gray_grad: np.ndarray,
    tau: float,
) -> Dict[str, float]:
    """按官方定义在单一阈值 τ 上计算 CCPR / CCFR / E-score。"""
    color_mask = color_delta > tau
    if color_mask.any():
        kept = np.count_nonzero(gray_delta[color_mask] > tau)
        ccpr = kept / float(np.count_nonzero(color_mask))
    else:
        ccpr = 0.0

    gray_mask = gray_grad > tau
    if gray_mask.any():
        invented = np.count_nonzero(color_grad[gray_mask] < tau)
        ccfr = 1.0 - invented / float(np.count_nonzero(gray_mask))
    else:
        ccfr = 1.0

    if ccpr + ccfr > 0:
        e_score = 2.0 * ccpr * ccfr / (ccpr + ccfr)
    else:
        e_score = 0.0

    return {
        "ccpr": float(ccpr),
        "ccfr": float(ccfr),
        "escore": float(e_score),
        "tau": float(tau),
        "color_pairs": int(np.count_nonzero(color_mask)),
        "gray_pairs": int(np.count_nonzero(gray_mask)),
    }


def evaluate_files(
    color_path: Path | str,
    gray_path: Path | str,
    tau: float = DEFAULT_TAU,
    rounds: int = DEFAULT_ROUNDS,
    seed: int = DEFAULT_SEED,
) -> Dict[str, float]:
    color_path = Path(color_path)
    data = _cached_color(color_path, rounds, seed)
    gray = load_gray_u8(gray_path)
    h, w = data["shape"].tolist()
    if gray.shape != (h, w):
        raise ValueError(
            f"灰度结果尺寸 {gray.shape[1]}x{gray.shape[0]} 与输入图 {w}x{h} 不一致"
        )

    gray_delta, ggx, ggy = _gray_statistics(gray, rounds, seed)
    color_grad = np.concatenate([data["c_gx"], data["c_gy"]])
    gray_grad = np.concatenate([ggx, ggy])
    result = ratios(data["c_delta"], color_grad, gray_delta, gray_grad, tau)
    result["ccpr"] = round(result["ccpr"], 6)
    result["ccfr"] = round(result["ccfr"], 6)
    result["escore"] = round(result["escore"], 6)
    return result


def evaluate_arrays(
    color_rgb_u8: np.ndarray,
    gray_u8: np.ndarray,
    tau: float = DEFAULT_TAU,
    rounds: int = DEFAULT_ROUNDS,
    seed: int = DEFAULT_SEED,
) -> Dict[str, float]:
    """便于单元测试的纯数组接口。"""
    if color_rgb_u8.shape[:2] != gray_u8.shape[:2]:
        raise ValueError("输入图与灰度结果尺寸不一致")
    c_delta, c_gx, c_gy = _color_statistics(color_rgb_u8, rounds, seed)
    g_delta, g_gx, g_gy = _gray_statistics(gray_u8, rounds, seed)
    return ratios(
        c_delta,
        np.concatenate([c_gx, c_gy]),
        g_delta,
        np.concatenate([g_gx, g_gy]),
        tau,
    )


def luminance_grayscale(rgb_u8: np.ndarray) -> np.ndarray:
    """标准亮度灰度（sRGB→Lab 的 L 通道），也是网站提供的一个基线。"""
    return np.round(_lab_from_rgb(rgb_u8)[:, :, 0] * 255.0 / 100.0).clip(0, 255).astype(np.uint8)


def sweep(
    color_path: Path | str,
    gray_path: Path | str,
    thresholds: Optional[np.ndarray] = None,
    rounds: int = DEFAULT_ROUNDS,
    seed: int = DEFAULT_SEED,
) -> list[Dict[str, float]]:
    """给出 τ=1..40 的完整曲线，便于教师课堂讲解指标随阈值的变化。"""
    thresholds = np.arange(1, 41) if thresholds is None else np.asarray(thresholds)
    color_path = Path(color_path)
    data = _cached_color(color_path, rounds, seed)
    gray = load_gray_u8(gray_path)
    g_delta, g_gx, g_gy = _gray_statistics(gray, rounds, seed)
    color_grad = np.concatenate([data["c_gx"], data["c_gy"]])
    gray_grad = np.concatenate([g_gx, g_gy])
    out = []
    for tau in thresholds:
        row = ratios(data["c_delta"], color_grad, g_delta, gray_grad, float(tau))
        row["tau"] = float(tau)
        out.append(row)
    return out
