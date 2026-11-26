"""主观偏好排名的数学实现。

Bradley–Terry 模型：给每张结果图一个潜在质量参数 θ_i，
    P(i ≻ j) = exp(θ_i) / (exp(θ_i) + exp(θ_j))
用 MM（minorization-maximization）迭代求解，并加入轻量 L2 正则，避免
「全胜 / 全负」的孤立图像把参数推到无穷。平局按 0.5 胜处理。

最终把每张图相对其余所有参赛图的平均预测胜率映射到 0–100 分，
两张原图分别拟合后再取平均，得到学生的去色主观分。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, Sequence, Tuple

import numpy as np

Outcome = float  # 1.0 表示前者胜，0.0 表示后者胜，0.5 表示平局


@dataclass
class BTResult:
    theta: Dict[int, float] = field(default_factory=dict)
    win_prob: Dict[int, float] = field(default_factory=dict)
    wins: Dict[int, float] = field(default_factory=dict)
    losses: Dict[int, float] = field(default_factory=dict)
    ties: Dict[int, float] = field(default_factory=dict)
    games: Dict[int, int] = field(default_factory=dict)
    iterations: int = 0
    converged: bool = False


def win_rate_scores(
    items: Sequence[int], matches: Iterable[Tuple[int, int, Outcome]]
) -> Dict[int, float]:
    """简单胜率：(W + 0.5T) / (W + T + L)。作为对照与兜底。"""
    stat = {i: {"w": 0.0, "t": 0.0, "l": 0.0} for i in items}
    for a, b, outcome in matches:
        if a not in stat or b not in stat or a == b:
            continue
        if outcome >= 1.0:
            stat[a]["w"] += 1
            stat[b]["l"] += 1
        elif outcome <= 0.0:
            stat[b]["w"] += 1
            stat[a]["l"] += 1
        else:
            stat[a]["t"] += 1
            stat[b]["t"] += 1
    out = {}
    for i, s in stat.items():
        total = s["w"] + s["t"] + s["l"]
        out[i] = 0.5 if total == 0 else (s["w"] + 0.5 * s["t"]) / total
    return out


def bradley_terry(
    items: Sequence[int],
    matches: Sequence[Tuple[int, int, Outcome]],
    regularization: float = 1.0,
    max_iter: int = 500,
    tol: float = 1e-9,
) -> BTResult:
    """items: 参赛条目 id 列表；matches: (a, b, outcome) 列表。"""
    idx = {item: k for k, item in enumerate(items)}
    n = len(items)
    result = BTResult()
    if n == 0:
        return result

    wins = np.zeros(n)
    games = np.zeros((n, n))
    l_cnt = np.zeros(n)
    t_cnt = np.zeros(n)

    for a, b, outcome in matches:
        if a not in idx or b not in idx or a == b:
            continue
        i, j = idx[a], idx[b]
        games[i, j] += 1
        games[j, i] += 1
        wins[i] += outcome
        wins[j] += 1.0 - outcome
        if outcome >= 1.0:
            l_cnt[j] += 1
        elif outcome <= 0.0:
            l_cnt[i] += 1
        else:
            t_cnt[i] += 1
            t_cnt[j] += 1

    theta = np.zeros(n)
    converged = False
    it = 0
    for it in range(1, max_iter + 1):
        p = 1.0 / (1.0 + np.exp(-(theta[:, None] - theta[None, :])))
        denom = (games * p).sum(axis=1) + regularization
        new_theta = wins / np.maximum(denom, 1e-12)
        new_theta -= new_theta.mean()
        delta = float(np.max(np.abs(new_theta - theta)))
        theta = new_theta
        if delta < tol:
            converged = True
            break

    p = 1.0 / (1.0 + np.exp(-(theta[:, None] - theta[None, :])))
    np.fill_diagonal(p, np.nan)
    with np.errstate(invalid="ignore"):
        win_prob = np.nanmean(p, axis=1)
    win_prob = np.nan_to_num(win_prob, nan=0.5)

    result.theta = {item: float(theta[idx[item]]) for item in items}
    result.win_prob = {item: float(win_prob[idx[item]]) for item in items}
    result.wins = {item: float(wins[idx[item]]) for item in items}
    result.losses = {item: float(l_cnt[idx[item]]) for item in items}
    result.ties = {item: float(t_cnt[idx[item]]) for item in items}
    result.games = {item: int(round(games[idx[item]].sum())) for item in items}
    result.iterations = it
    result.converged = converged
    return result


def normalized_scores(result: BTResult) -> Dict[int, float]:
    """把平均预测胜率线性映射到 0–100。"""
    return {item: round(100.0 * prob, 2) for item, prob in result.win_prob.items()}
