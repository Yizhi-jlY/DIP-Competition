"""每张比赛图的来源：输入图、参考图、退化声明。

一个任务的每张图在 `config/competition.yaml` 里由 `tasks[].images[]` 声明：

    - key: D02
      label: 图 2 · 合成噪声
      degradation: {kind: gaussian, sigma: 25, source: sidd}

字段都可省略，省略时按约定推导（与重构前完全一致，所以旧 yaml 不用改）：

    输入图  data/public/<任务>/<key>.png
    参考图  data/private/ground_truth/<key>.png

想给某张图换文件名、换参考图、或指定退化方式，只改 yaml 那一行；
想改退化强度又不愿动 yaml，改 `research/params.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from competition.config import TaskImage, TaskSpec


@dataclass(frozen=True)
class ImagePlan:
    """一张图在磁盘上的位置与它的退化声明。"""

    key: str
    label: str
    input_path: Path
    reference_path: Optional[Path]  # 去色任务没有参考图，为 None
    degradation: Dict[str, Any]

    @property
    def has_reference(self) -> bool:
        return self.reference_path is not None


def _image_spec(task: TaskSpec, key: str) -> TaskImage:
    for img in task.images:
        if img.key == key:
            return img
    raise KeyError(f"任务 {task.slug} 没有图像 {key}")


def reference_required(task: TaskSpec) -> bool:
    """该任务的指标是否需要参考图（去噪需要，去色不需要）。"""
    return task.evaluator_kind() == "denoise"


def plan(task: TaskSpec, key: str) -> ImagePlan:
    img = _image_spec(task, key)
    return ImagePlan(
        key=img.key,
        label=img.label,
        input_path=task.public_image(img.key),
        reference_path=task.ground_truth(img.key) if reference_required(task) else None,
        degradation=dict(img.degradation),
    )


def plans(task: TaskSpec) -> List[ImagePlan]:
    return [plan(task, img.key) for img in task.images]
