"""任务 → 指标族的显式映射。

评分入口只有一个 `evaluate(task, image_key, result_path)`：
它按任务声明的 `evaluator` 选择指标族，读该图的输入/参考图，返回指标字典。

    denoise  PSNR, SSIM          参考图 = 私有真值图，与结果图逐像素比较
    decolor  CCPR, CCFR, E-score 输入图 = 公开彩色原图，比较颜色对比度是否保真

`evaluator` 在 yaml 里显式声明（`evaluator: denoise`）；未声明时按通道数推断，
与重构前的行为一致，旧 yaml 不改也能跑。

**新增一个指标族**（例如换成 LPIPS 或加入噪声估计误差）：
1. 在 `competition/metrics/` 新增一个模块，写 `evaluate_files(...) -> dict`；
2. 在下面 `evaluate` 里加一个 `if kind == "xxx":` 分支；
3. yaml 的任务里写 `evaluator: xxx`，并把 `metrics:` 列表与 `ranking_metric` 改成新指标名。
第 3 步不做的话，页面会显示旧指标名而榜单按新指标排序，两者对不上。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

from competition.config import TaskSpec, load_config
from competition.metrics import decolor, denoise
from competition.research import dataset


def _options() -> Dict[str, float]:
    return load_config().metrics_options


def evaluate(task: TaskSpec, image_key: str, result_path: Path) -> Dict[str, float]:
    """对一张结果图计算该任务声明的全部客观指标。"""
    kind = task.evaluator_kind()
    opts = _options()
    image = dataset.plan(task, image_key)

    if kind == "denoise":
        if image.reference_path is None or not image.reference_path.exists():
            raise FileNotFoundError(f"缺少参考图：{image.key}.png")
        return denoise.evaluate_files(
            image.reference_path,
            result_path,
            data_range=float(opts["data_range"]),
            ssim_win_size=int(opts["ssim_win_size"]),
        )

    if kind == "decolor":
        if not image.input_path.exists():
            raise FileNotFoundError(f"缺少输入图：{image.input_path.name}")
        return decolor.evaluate_files(
            image.input_path,
            result_path,
            tau=float(opts["decolor_tau"]),
            rounds=int(opts["decolor_pairs_per_pixel"]),
            seed=int(opts["decolor_seed"]),
        )

    raise ValueError(f"未知的 evaluator：{kind}（任务 {task.slug}）")
