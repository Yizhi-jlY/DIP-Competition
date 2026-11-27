"""学术参数的唯一默认值。

全项目的指标口径、退化强度、数据集构造尺寸都在这里定义一次；
`config/competition.yaml` 里写了同名项就以 yaml 为准（教师面板可热改），
没写就落回这里的值。**不要在别处再写一遍默认值。**

改这里 = 改全项目口径。改完需要重启服务（`systemctl --user restart dip-competition`）。
"""

from __future__ import annotations

from typing import Any, Dict

# ======================================================================
# 一、客观指标
# ======================================================================

# PSNR / SSIM 的像素动态范围。uint8 图像固定 255，
# 与 MATLAB `psnr(x, y)` 的默认峰值一致。
DATA_RANGE: float = 255.0

# SSIM 的方形窗口边长。论文取 11；比赛图裁到 768 后局部纹理更细，取 7。
# 必须是奇数；若大于图像短边，指标函数会自动缩到不超过短边的最大奇数。
SSIM_WIN_SIZE: int = 7

# 去色指标：Lu, Xu & Jia, IJCV 2014 "Contrast Preserving Decolorization
# with Perception-Based Quality Metrics" 中 metric.m 的定义。
#
#   τ 是 CIELab 欧氏距离阈值，用来判定「一对像素在颜色上是否可区分」：
#     CCPR(τ) = #{ΔE>τ 且 Δg>τ} / #{ΔE>τ}      原图能区分，灰度也还能区分
#     CCFR(τ) = 1 - #{|∇g|>τ 且 ΔE<τ} / #{|∇g|>τ}   灰度不该凭空造出对比
#     E-score = 2·CCPR·CCFR / (CCPR+CCFR)        二者的调和平均
DECOLOR_TAU: float = 5.0

# 每个像素抽取的伙伴像素个数（对应官方 MATLAB 的 randperm 轮数），官方取 10。
# 调大更稳但更慢，一图约 O(rounds × 像素数) 次距离计算。
DECOLOR_PAIRS_PER_PIXEL: int = 10

# 像素对抽样种子。固定后全班使用同一组像素对，任何机器上分数完全一致；
# 改动会让历史分数与新的分数不可比。
DECOLOR_SEED: int = 20140101

# ======================================================================
# 二、数据集构造（离线命令 `python manage.py prepare_data` 使用）
# ======================================================================

# 从 SIDD / CADIK 原始大图裁出的方形边长（像素）。
CROP_SIZE: int = 768

# 合成高斯噪声的标准差，单位与像素值同尺度（0–255）：
#   Y = clip(X + N(0, σ²), 0, 255)，σ=25 对应 σ/255 ≈ 0.098
SYNTHETIC_SIGMA: float = 25.0

# 合成噪声的随机种子。固定后每次重跑得到同一张含噪图。
SYNTHETIC_SEED: int = 20261126

# ======================================================================
# 三、汇总出口
# ======================================================================


def metric_options() -> Dict[str, Any]:
    """指标参数的默认值，供 `config.py` 与 yaml 的 metrics_options 合并。"""
    return {
        "data_range": DATA_RANGE,
        "ssim_win_size": SSIM_WIN_SIZE,
        "decolor_tau": DECOLOR_TAU,
        "decolor_pairs_per_pixel": DECOLOR_PAIRS_PER_PIXEL,
        "decolor_seed": DECOLOR_SEED,
    }
