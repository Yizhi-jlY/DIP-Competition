"""竞赛配置的读取与封装。

所有比赛参数集中在 config/competition.yaml，网站与评测代码只依赖这里暴露的对象，
因此新增任务只需要改配置 + 增加对应指标函数，不必改动页面逻辑。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import yaml
from django.conf import settings

from competition.research import params as research_params

_lock = threading.Lock()
_cache: Dict[str, Any] = {"key": None, "config": None}


@dataclass(frozen=True)
class MetricSpec:
    key: str
    name: str
    unit: str = ""
    digits: int = 4
    higher_better: bool = True
    hint: str = ""

    def format(self, value: Optional[float]) -> str:
        if value is None:
            return "—"
        return f"{value:.{self.digits}f}{self.unit}"


@dataclass(frozen=True)
class TaskImage:
    """一张比赛图。后三个字段可选，省略时按约定推导路径与退化方式。"""

    key: str
    label: str
    note: str = ""
    input_file: str = ""      # 相对 data/public/<任务>/ 的文件名，默认 <key>.png
    reference_file: str = ""  # 相对 data/private/ground_truth/ 的文件名，默认 <key>.png
    # 退化声明，供 `manage.py prepare_data` 用，例如 {"kind": "gaussian", "sigma": 25}
    degradation: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskSpec:
    slug: str
    name: str
    short_name: str
    description: str
    icon: str = "◆"
    tagline: str = ""
    review_hint: str = ""
    review_input_label: str = "原始输入图"
    review_result_label: str = "结果图"
    channels: int = 3
    color_mode: str = "RGB"
    file_format: str = "PNG"
    max_submissions: int = 0  # 0 或负数表示不限制提交次数
    # 用哪套客观指标：denoise（PSNR/SSIM）或 decolor（CCPR/CCFR/E-score）。
    # 留空时按 channels 推断，保持对旧配置的兼容。
    evaluator: str = ""
    ranking_metric: str = ""
    tiebreak_metric: str = ""
    images: tuple[TaskImage, ...] = ()
    metrics: tuple[MetricSpec, ...] = ()
    baseline_methods: tuple[str, ...] = ()
    public_dir_name: str = ""

    # ---- 路径 ----
    @property
    def public_dir(self) -> Path:
        return Path(settings.BASE_DIR) / "data" / "public" / self.slug

    @property
    def ground_truth_dir(self) -> Path:
        return Path(settings.BASE_DIR) / "data" / "private" / "ground_truth"

    def image_spec(self, key: str) -> Optional["TaskImage"]:
        for img in self.images:
            if img.key == key:
                return img
        return None

    def public_image(self, key: str) -> Path:
        spec = self.image_spec(key)
        name = spec.input_file if spec and spec.input_file else f"{key}.png"
        return self.public_dir / name

    def ground_truth(self, key: str) -> Path:
        spec = self.image_spec(key)
        name = spec.reference_file if spec and spec.reference_file else f"{key}.png"
        return self.ground_truth_dir / name

    def evaluator_kind(self) -> str:
        """该任务使用的指标族；未显式声明时按通道数推断。"""
        if self.evaluator:
            return self.evaluator.strip().lower()
        return "denoise" if self.channels == 3 else "decolor"

    def metric(self, key: str) -> Optional[MetricSpec]:
        for m in self.metrics:
            if m.key == key:
                return m
        return None

    @property
    def unlimited(self) -> bool:
        """单图提交次数是否不受限制。"""
        return self.max_submissions is None or self.max_submissions <= 0

    @property
    def limit_label(self) -> str:
        return "不限提交次数" if self.unlimited else f"单图上限 {self.max_submissions} 次"

    def ranking_rule(self) -> str:
        """一句话说明排行榜的排序依据，供页面直接展示。"""
        primary = self.metric(self.ranking_metric)
        if primary is None:
            return ""
        secondary = self.metric(self.tiebreak_metric)
        unit = f"（{primary.unit}）" if primary.unit else ""
        count = len(self.images) if self.images else 1
        text = f"按 {count} 张图的平均 {primary.name}{unit} 从高到低排名"
        if secondary is not None:
            text += f"，{primary.name} 相同时比较平均 {secondary.name}"
        return text

    def images_ready(self) -> bool:
        return all(self.public_image(img.key).exists() for img in self.images)

    def reference_ready(self) -> bool:
        if self.evaluator_kind() != "denoise":
            return True
        return all(self.ground_truth(img.key).exists() for img in self.images)


@dataclass(frozen=True)
class Schedule:
    submission_open: Optional[datetime] = None
    submission_close: Optional[datetime] = None
    review_open: Optional[datetime] = None
    review_close: Optional[datetime] = None

    def as_dict(self) -> Dict[str, Optional[datetime]]:
        return {
            "submission_open": self.submission_open,
            "submission_close": self.submission_close,
            "review_open": self.review_open,
            "review_close": self.review_close,
        }


@dataclass(frozen=True)
class TaskReviewSpec:
    """单个任务的互评设置。"""

    enabled: bool = True
    comparisons_per_student: int = 10


@dataclass(frozen=True)
class PeerReviewSpec:
    enabled: bool = True
    task: str = "decolor"  # 旧版单任务配置的兼容字段
    comparisons_per_student: int = 10
    allow_tie: bool = True
    random_swap: bool = True
    exclude_own_submission: bool = True
    anonymous: bool = True
    ranking_method: str = "bradley_terry"
    tie_weight: float = 0.5
    regularization: float = 1.0
    tasks: Dict[str, TaskReviewSpec] = field(default_factory=dict)

    def for_task(self, slug: str) -> TaskReviewSpec:
        """返回某任务的互评设置；未单独配置的任务默认不开放互评。"""
        if slug in self.tasks:
            spec = self.tasks[slug]
            return TaskReviewSpec(
                enabled=self.enabled and spec.enabled,
                comparisons_per_student=spec.comparisons_per_student,
            )
        if not self.tasks and slug == self.task:
            return TaskReviewSpec(self.enabled, self.comparisons_per_student)
        return TaskReviewSpec(False, self.comparisons_per_student)

    def enabled_task_slugs(self) -> List[str]:
        if self.tasks:
            return [slug for slug, spec in self.tasks.items() if self.enabled and spec.enabled]
        return [self.task] if self.enabled else []


@dataclass(frozen=True)
class ScoringSpec:
    weight_objective: float = 0.4
    weight_subjective: float = 0.6
    # 综合主观分 = w_denoise × 去噪主观分 + w_decolor × 去色主观分（加载时自动归一化）
    denoise_subjective_weight: float = 0.5
    decolor_subjective_weight: float = 0.5

    def combined_weights(self) -> Dict[str, float]:
        d = max(0.0, float(self.denoise_subjective_weight))
        c = max(0.0, float(self.decolor_subjective_weight))
        total = d + c
        if total <= 0:
            return {"denoise": 0.5, "decolor": 0.5}
        return {"denoise": d / total, "decolor": c / total}


@dataclass(frozen=True)
class Config:
    title: str
    subtitle: str
    intro: str
    rules: List[str]
    data_note: str
    timezone: str
    contest_code_prefix: str
    anonymous: bool
    schedule: Schedule
    tasks: tuple[TaskSpec, ...]
    peer_review: PeerReviewSpec
    scoring: ScoringSpec
    metrics_options: Dict[str, Any] = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def task(self, slug: str) -> Optional[TaskSpec]:
        for t in self.tasks:
            if t.slug == slug:
                return t
        return None

    def task_map(self) -> Dict[str, TaskSpec]:
        return {t.slug: t for t in self.tasks}

    def review_tasks(self) -> List["TaskSpec"]:
        """已开放匿名互评的任务（按任务在页面上的展示顺序）。"""
        return [t for t in self.tasks if self.peer_review.for_task(t.slug).enabled]


def _dt(value: Any, tz: ZoneInfo) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def _parse(raw: Dict[str, Any]) -> Config:
    comp = raw.get("competition", {}) or {}
    tz = ZoneInfo(comp.get("timezone", "Asia/Shanghai"))

    tasks: List[TaskSpec] = []
    for item in raw.get("tasks", []) or []:
        metrics = tuple(
            MetricSpec(
                key=m["key"],
                name=m.get("name", m["key"].upper()),
                unit=m.get("unit", ""),
                digits=int(m.get("digits", 4)),
                higher_better=bool(m.get("higher_better", True)),
                hint=m.get("hint", ""),
            )
            for m in item.get("metrics", []) or []
        )
        images = tuple(
            TaskImage(
                key=i["key"],
                label=i.get("label", i["key"]),
                note=i.get("note", ""),
                input_file=i.get("input", "") or "",
                reference_file=i.get("reference", "") or "",
                degradation=dict(i.get("degradation", {}) or {}),
            )
            for i in item.get("images", []) or []
        )
        tasks.append(
            TaskSpec(
                slug=item["slug"],
                name=item.get("name", item["slug"]),
                short_name=item.get("short_name", item.get("name", item["slug"])),
                description=item.get("description", ""),
                icon=item.get("icon", "◆"),
                tagline=item.get("tagline", ""),
                review_hint=item.get("review_hint", ""),
                review_input_label=item.get("review_input_label", "原始输入图"),
                review_result_label=item.get("review_result_label", "结果图"),
                channels=int(item.get("channels", 3)),
                color_mode=item.get("color_mode", "RGB"),
                file_format=item.get("file_format", "PNG"),
                max_submissions=int(item.get("max_submissions", 0) or 0),
                evaluator=item.get("evaluator", "") or "",
                ranking_metric=item.get("ranking_metric", ""),
                tiebreak_metric=item.get("tiebreak_metric", ""),
                images=images,
                metrics=metrics,
                baseline_methods=tuple(item.get("baseline_methods", []) or []),
                public_dir_name=item.get("slug", ""),
            )
        )

    sched_raw = raw.get("schedule", {}) or {}
    schedule = Schedule(
        submission_open=_dt(sched_raw.get("submission_open"), tz),
        submission_close=_dt(sched_raw.get("submission_close"), tz),
        review_open=_dt(sched_raw.get("review_open"), tz),
        review_close=_dt(sched_raw.get("review_close"), tz),
    )

    pr_raw = raw.get("peer_review", {}) or {}
    default_count = int(pr_raw.get("comparisons_per_student", 10) or 0)
    review_tasks: Dict[str, TaskReviewSpec] = {}
    for slug, value in (pr_raw.get("tasks", {}) or {}).items():
        if isinstance(value, bool):
            review_tasks[str(slug)] = TaskReviewSpec(enabled=value, comparisons_per_student=default_count)
        elif isinstance(value, dict):
            review_tasks[str(slug)] = TaskReviewSpec(
                enabled=bool(value.get("enabled", True)),
                comparisons_per_student=int(value.get("comparisons_per_student", default_count) or 0),
            )
    peer_review = PeerReviewSpec(
        enabled=bool(pr_raw.get("enabled", True)),
        task=pr_raw.get("task", "decolor"),
        comparisons_per_student=default_count,
        allow_tie=bool(pr_raw.get("allow_tie", True)),
        random_swap=bool(pr_raw.get("random_swap", True)),
        exclude_own_submission=bool(pr_raw.get("exclude_own_submission", True)),
        anonymous=bool(pr_raw.get("anonymous", True)),
        ranking_method=pr_raw.get("ranking_method", "bradley_terry"),
        tie_weight=float(pr_raw.get("tie_weight", 0.5)),
        regularization=float(pr_raw.get("regularization", 1.0)),
        tasks=review_tasks,
    )

    sc_raw = raw.get("scoring", {}) or {}
    scoring = ScoringSpec(
        weight_objective=float(sc_raw.get("weight_objective", 0.4)),
        weight_subjective=float(sc_raw.get("weight_subjective", 0.6)),
        denoise_subjective_weight=float(sc_raw.get("denoise_subjective_weight", 0.5)),
        decolor_subjective_weight=float(sc_raw.get("decolor_subjective_weight", 0.5)),
    )

    # 学术参数：默认值只在 research/params.py 定义一次，yaml 写了就覆盖
    metric_options = research_params.metric_options()
    metric_options.update(dict(raw.get("metrics_options", {}) or {}))

    return Config(
        title=comp.get("title", "数字图像处理课程竞赛平台"),
        subtitle=comp.get("subtitle", ""),
        intro=comp.get("intro", ""),
        rules=list(comp.get("rules", []) or []),
        data_note=comp.get("data_note", ""),
        timezone=comp.get("timezone", "Asia/Shanghai"),
        contest_code_prefix=comp.get("contest_code_prefix", "S"),
        anonymous=bool(comp.get("anonymous", True)),
        schedule=schedule,
        tasks=tuple(tasks),
        peer_review=peer_review,
        scoring=scoring,
        metrics_options=metric_options,
    )


def load_config(force: bool = False) -> Config:
    """按文件路径与修改时间缓存配置，教师改 yaml 后无需重启即可生效。"""
    path = Path(settings.COMPETITION_CONFIG)
    try:
        key = (str(path.resolve()), path.stat().st_mtime)
    except OSError:
        key = (str(path), None)
    with _lock:
        if force or _cache["config"] is None or _cache["key"] != key:
            if path.exists():
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            else:
                raw = {}
            _cache["config"] = _parse(raw)
            _cache["key"] = key
        return _cache["config"]


def get_task(slug: str) -> TaskSpec:
    task = load_config().task(slug)
    if task is None:
        from django.http import Http404

        raise Http404(f"未配置的任务：{slug}")
    return task
