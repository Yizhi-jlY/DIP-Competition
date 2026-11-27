"""准备比赛数据：按 yaml 里每张图的退化声明，从源数据生成输入图与参考图。

    python manage.py prepare_data \
        --sidd-root /mnt/share/public/Datasets/SIDD \
        --cadik-root data/_sources/cadik/cadik08perceptualEvaluation-images

每张图怎么来，由 `config/competition.yaml` 的 `images[].degradation` 决定：

    kind: real      源数据自带的真实噪声对，同框裁切（去噪 D01）
    kind: gaussian  干净图裁切后加高斯噪声（去噪 D02），σ 取 yaml > --sigma > params
    kind: none      原图直接作为输入，没有参考图（去色 C01/C02）

退化本身的数学在 `competition/research/degradation.py`，
源数据定位在 `competition/research/sources.py`，本命令只做编排与写盘。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from PIL import Image

from competition.config import TaskSpec, load_config
from competition.imaging import save_png
from competition.research import dataset, degradation, params, sources


class Command(BaseCommand):
    help = "按 yaml 的退化声明生成比赛输入图与私有参考图"

    def add_arguments(self, parser):
        parser.add_argument("--sidd-root", default="/mnt/share/public/Datasets/SIDD",
                            help="SIDD 数据集根目录")
        parser.add_argument(
            "--cadik-root",
            default=str(Path(settings.BASE_DIR) / "data" / "_sources" / "cadik"
                        / "cadik08perceptualEvaluation-images"),
            help="CADIK24 图像目录",
        )
        parser.add_argument("--crop-size", type=int, default=params.CROP_SIZE,
                            help=f"裁切边长（默认 {params.CROP_SIZE}）")
        parser.add_argument("--sigma", type=float, default=params.SYNTHETIC_SIGMA,
                            help=f"合成高斯噪声标准差，yaml 未指定时使用（默认 {params.SYNTHETIC_SIGMA}）")
        parser.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
        parser.add_argument("--max-scan", type=int, default=24,
                            help="最多扫描多少个 SIDD 场景用于选图")
        parser.add_argument(
            "--prefer",
            default="0036_002_GP_06400_03200_3200_N,0146_007_N6_00400_00400_4400_N",
            help="优先使用的 SIDD 场景（逗号分隔，按顺序分配给声明 kind=real/gaussian 的图）",
        )

    def handle(self, *args, **options):
        config = load_config()
        self.size = int(options["crop_size"])
        self.force = bool(options["force"])
        self.cli_sigma = float(options["sigma"])
        self.manifest: Dict[str, object] = {"generated_at": None, "sources": {}}

        self._sidd_pool: Optional[List[sources.SiddPair]] = None
        self._sidd_used: set[str] = set()
        self._sidd_root = Path(options["sidd_root"])
        self._cadik_root = Path(options["cadik_root"])
        self._prefer = [x.strip() for x in str(options["prefer"]).split(",") if x.strip()]
        self._max_scan = int(options["max_scan"])

        for task in config.tasks:
            for plan in dataset.plans(task):
                self._build(task, plan)

        self.manifest["generated_at"] = datetime.now().isoformat(timespec="seconds")
        manifest_path = Path(settings.BASE_DIR) / "data" / "private" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.stdout.write(self.style.SUCCESS(f"数据准备完成，来源记录写入 {manifest_path.name}"))

    # ------------------------------------------------------------------
    def _build(self, task: TaskSpec, plan: dataset.ImagePlan) -> None:
        kind = str(plan.degradation.get("kind", "") or "").lower()
        source = str(plan.degradation.get("source", "") or "").lower()

        if kind == "real":
            self._build_real(task, plan)
        elif kind == "gaussian":
            self._build_gaussian(task, plan)
        elif kind == "none":
            self._build_plain(task, plan, source)
        else:
            raise CommandError(
                f"{task.slug}/{plan.key} 的 degradation.kind 必须是 real / gaussian / none，"
                f"当前为 {kind or '空'}。请在 competition.yaml 的 images[] 里补上。"
            )

    def _should_skip(self, plan: dataset.ImagePlan) -> bool:
        need = [plan.input_path] + ([plan.reference_path] if plan.reference_path else [])
        if all(p.exists() for p in need) and not self.force:
            self.stdout.write(f"跳过已存在的 {plan.key}")
            return True
        return False

    def _build_real(self, task: TaskSpec, plan: dataset.ImagePlan) -> None:
        """真实噪声：源数据自带 (noisy, clean) 对，裁同一块，像素不改。"""
        if self._should_skip(plan):
            return
        pair = self._next_scene()
        with Image.open(pair.reference) as gt_img:
            box = degradation.texture_crop_box(gt_img, self.size)
        with Image.open(pair.noisy) as im:
            noisy = degradation.crop_square(im, box, self.size)
        with Image.open(pair.reference) as im:
            clean = degradation.crop_square(im, box, self.size)

        save_png(noisy, plan.input_path)
        save_png(clean, plan.reference_path)
        self.manifest["sources"][plan.key] = {
            "task": task.slug,
            "kind": "real",
            "dataset": "SIDD Medium sRGB",
            "scene": pair.folder.name,
            "noisy": pair.noisy.name,
            "reference": pair.reference.name,
            "crop_box": list(box),
            "crop_size": self.size,
        }
        self.stdout.write(
            f"{plan.key}: 真实噪声对，场景 {pair.folder.name}，裁切 box={box}，"
            f"初始 PSNR={degradation.psnr_of_pair(noisy, clean):.2f} dB"
        )

    def _build_gaussian(self, task: TaskSpec, plan: dataset.ImagePlan) -> None:
        """合成噪声：干净图裁切 → Y = clip(X + N(0, σ²), 0, 255)。"""
        if self._should_skip(plan):
            return
        pair = self._next_scene()
        sigma = float(plan.degradation.get("sigma", self.cli_sigma))
        seed = int(plan.degradation.get("seed", params.SYNTHETIC_SEED))
        with Image.open(pair.reference) as im:
            box = degradation.texture_crop_box(im, self.size)
            clean = degradation.crop_square(im, box, self.size)

        noisy = degradation.gaussian_noise(clean, sigma=sigma, seed=seed)
        save_png(noisy, plan.input_path)
        save_png(clean, plan.reference_path)
        self.manifest["sources"][plan.key] = {
            "task": task.slug,
            "kind": "gaussian",
            "dataset": "SIDD Medium sRGB（干净参考 + 人工噪声）",
            "scene": pair.folder.name,
            "reference": pair.reference.name,
            "crop_box": list(box),
            "crop_size": self.size,
            "sigma": sigma,
            "seed": seed,
            "formula": "Y = clip(X + N(0, sigma^2), 0, 255)",
        }
        self.stdout.write(
            f"{plan.key}: 合成噪声 σ={sigma}/255，场景 {pair.folder.name}，裁切 box={box}，"
            f"初始 PSNR={degradation.psnr_of_pair(noisy, clean):.2f} dB"
        )

    def _build_plain(self, task: TaskSpec, plan: dataset.ImagePlan, source: str) -> None:
        """无退化：CADIK 彩色原图直接转存为输入。"""
        if self._should_skip(plan):
            return
        if source != "cadik":
            raise CommandError(
                f"{plan.key}: kind=none 目前只支持 source=cadik，"
                f"当前为 {source or '空'}。直接放图的话把文件拷到 {plan.input_path} 即可。"
            )
        folder = str(plan.degradation.get("folder", "") or "")
        if not folder:
            raise CommandError(f"{plan.key}: kind=none 需要 degradation.folder 指定 CADIK 子目录。")
        if not self._cadik_root.is_dir():
            raise CommandError(f"找不到 CADIK 图像目录：{self._cadik_root}")

        src = sources.find_cadik_image(self._cadik_root, folder)
        if src is None:
            raise CommandError(f"CADIK 目录中找不到 {folder} 的原始图像。")

        with Image.open(src) as im:
            plan.input_path.parent.mkdir(parents=True, exist_ok=True)
            im.convert("RGB").save(plan.input_path, format="PNG", optimize=True)
            width, height = im.size
        self.manifest["sources"][plan.key] = {
            "task": task.slug,
            "kind": "none",
            "dataset": "CADIK color-to-gray evaluation images (2008)",
            "folder": folder,
            "file": src.name,
            "size": [width, height],
        }
        self.stdout.write(f"{plan.key}: 写入 {plan.input_path.name}（{width}×{height}）")

    # ------------------------------------------------------------------
    def _next_scene(self) -> sources.SiddPair:
        """按 prefer 顺序取一个还没用过的 SIDD 场景（避免两张题图同一内容）。"""
        if self._sidd_pool is None:
            self._sidd_pool = self._load_sidd_pool()
        while self._sidd_pool:
            pair = self._sidd_pool.pop(0)
            if pair.folder.name in self._sidd_used:
                continue
            self._sidd_used.add(pair.folder.name)
            return pair
        raise CommandError("SIDD 里没有更多可用场景，请增加 --max-scan 或减少去噪图数量。")

    def _load_sidd_pool(self) -> List[sources.SiddPair]:
        try:
            data_root = sources.find_sidd_data(self._sidd_root)
        except FileNotFoundError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"SIDD 数据目录：{data_root}")

        pool: List[sources.SiddPair] = []
        for name in self._prefer:
            pair = sources.load_scene(data_root, name)
            if pair is not None:
                pool.append(pair)
            else:
                self.stderr.write(f"未找到优先场景 {name}，将从扫描结果中自动选择。")
        chosen = {p.folder.name for p in pool}
        pool += [p for p in sources.rank_scenes(data_root, self._max_scan)
                 if p.folder.name not in chosen]
        if not pool:
            raise CommandError("没有找到可用的 SIDD 噪声/参考图像对。")
        return pool
