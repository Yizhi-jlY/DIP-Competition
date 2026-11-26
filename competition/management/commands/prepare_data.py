"""准备比赛数据：从 SIDD 与 CADIK24 生成公开输入图与私有参考图。

用法示例：
    python manage.py prepare_data \
        --sidd-root /mnt/share/public/Datasets/SIDD \
        --cadik-root data/_sources/cadik/cadik08perceptualEvaluation-images

输出：
    data/public/denoise/D01.png   真实噪声输入
    data/public/denoise/D02.png   合成高斯噪声输入
    data/private/ground_truth/D01.png, D02.png   对应干净参考
    data/public/decolor/C01.png, C02.png         去色输入（彩色）
    data/private/manifest.json                   数据来源记录
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from PIL import Image

from competition.imaging import save_png
from competition.config import load_config

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


def _find_sidd_data(root: Path) -> Path:
    candidates = [
        root / "Data",
        root / "medium" / "mnt" / "d" / "SIDD_Medium_Srgb" / "Data",
        root / "mnt" / "d" / "SIDD_Medium_Srgb" / "Data",
    ]
    for cand in candidates:
        if cand.is_dir():
            return cand
    for path in root.rglob("SIDD_Medium_Srgb"):
        data = path / "Data"
        if data.is_dir():
            return data
    raise CommandError(f"在 {root} 下找不到 SIDD Medium sRGB 的 Data 目录。")


def _texture_score(gray: np.ndarray) -> float:
    if cv2 is None:
        gx, gy = np.gradient(gray.astype(np.float32))
        return float(np.mean(np.abs(gx)) + np.mean(np.abs(gy)))
    lap = cv2.Laplacian(gray.astype(np.uint8), cv2.CV_64F)
    return float(lap.var())


def _best_crop(gt_path: Path, size: int) -> Tuple[int, int]:
    """在干净参考图上找平均梯度能量最高的 size×size 区域（含文字、边缘与纹理）。"""
    with Image.open(gt_path) as im:
        scale = 4
        small = im.convert("L").resize((max(1, im.width // scale), max(1, im.height // scale)))
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


def _crop(path: Path, box: Tuple[int, int], size: int) -> np.ndarray:
    with Image.open(path) as im:
        im = im.convert("RGB")
        left = min(max(0, box[0]), max(0, im.width - size))
        top = min(max(0, box[1]), max(0, im.height - size))
        return np.asarray(im.crop((left, top, left + size, top + size)), dtype=np.uint8)


def _psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 99.0 if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)


def _pair_files(folder: Path, shot: str) -> Dict[str, Path]:
    out = {}
    for p in folder.glob("*.PNG"):
        name = p.stem  # 0001_NOISY_SRGB_010
        parts = name.split("_")
        kind = parts[1]
        idx = parts[-1]
        out[f"{kind}_{idx}"] = p
    return out


class Command(BaseCommand):
    help = "从 SIDD 与 CADIK24 准备比赛图像（公开输入 + 私有参考）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--sidd-root",
            default="/mnt/share/public/Datasets/SIDD",
            help="SIDD 数据集根目录",
        )
        parser.add_argument(
            "--cadik-root",
            default=str(Path(settings.BASE_DIR) / "data" / "_sources" / "cadik"
                        / "cadik08perceptualEvaluation-images"),
            help="CADIK24 图像目录",
        )
        parser.add_argument("--crop-size", type=int, default=768, help="去噪图像裁切尺寸")
        parser.add_argument("--sigma", type=float, default=25.0, help="合成噪声标准差（0-255）")
        parser.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
        parser.add_argument("--max-scan", type=int, default=24, help="最多扫描多少个 SIDD 场景用于选图")
        parser.add_argument(
            "--prefer",
            default="0036_002_GP_06400_03200_3200_N,0146_007_N6_00400_00400_4400_N",
            help="优先使用的 SIDD 场景（逗号分隔，按顺序作为 D01、D02）",
        )

    def handle(self, *args, **options):
        config = load_config()
        denoise = config.task("denoise")
        decolor = config.task("decolor")
        if not denoise or not decolor:
            raise CommandError("competition.yaml 中必须同时配置 denoise 与 decolor 任务。")

        root = Path(options["sidd_root"])
        data_root = _find_sidd_data(root)
        self.stdout.write(f"SIDD 数据目录：{data_root}")

        size = int(options["crop_size"])
        sigma = float(options["sigma"])
        manifest: Dict[str, object] = {"generated_at": None, "sources": {}}

        # ---------------- 图像 1：真实噪声 ----------------
        preferred = [x.strip() for x in str(options["prefer"]).split(",") if x.strip()]
        scenes: List[Tuple[Path, Dict[str, Path]]] = []
        for name in preferred:
            scene = self._load_scene(data_root, name)
            if scene is not None:
                scenes.append(scene)
            else:
                self.stderr.write(f"未找到优先场景 {name}，将从扫描结果中自动选择。")
        chosen = {s[0] for s in scenes}
        scenes += [
            s for s in self._rank_scenes(data_root, max_scan=int(options["max_scan"]))
            if s[0] not in chosen
        ]
        if not scenes:
            raise CommandError("没有找到可用的 SIDD 噪声/参考图像对。")

        d01_scene, d01_pair = scenes[0]
        manifest["sources"]["D01"] = {
            "dataset": "SIDD Medium sRGB",
            "scene": d01_scene.name,
            "noisy": d01_pair["noisy"].name,
            "gt": d01_pair["gt"].name,
            "crop_size": size,
            "note": "真实手机噪声，原始分辨率裁切",
        }
        self._write_denoise_pair(
            denoise, "D01", d01_pair["noisy"], d01_pair["gt"], size,
            options["force"], rotate=0,
        )

        # ---------------- 图像 2：合成高斯噪声 ----------------
        d02_scene, d02_pair = next((s for s in scenes if s[0] != d01_scene), (None, None))  # type: ignore
        if d02_scene is None:
            raise CommandError("需要至少两个 SIDD 场景来生成第二张去噪图像。")

        manifest["sources"]["D02"] = {
            "dataset": "SIDD Medium sRGB（干净参考 + 人工噪声）",
            "scene": d02_scene.name,
            "gt": d02_pair["gt"].name,
            "crop_size": size,
            "sigma": sigma,
            "note": f"Y = clip(X + N(0, ({sigma}/255)^2), 0, 1)",
        }
        self._write_synthetic_pair(
            denoise, "D02", d02_pair["gt"], size, sigma, options["force"]
        )

        # ---------------- 去色图像 ----------------
        cadik_root = Path(options["cadik_root"])
        if not cadik_root.is_dir():
            raise CommandError(f"找不到 CADIK 图像目录：{cadik_root}")
        for key, folder in (("C01", "C8TZ7768"), ("C02", "fruits")):
            src = self._find_original(cadik_root, folder)
            if src is None:
                raise CommandError(f"CADIK 目录中找不到 {folder} 的原始图像。")
            out = decolor.public_image(key)
            out.parent.mkdir(parents=True, exist_ok=True)
            if out.exists() and not options["force"]:
                self.stdout.write(f"跳过已存在的 {out.relative_to(settings.BASE_DIR)}")
            else:
                with Image.open(src) as im:
                    im.convert("RGB").save(out, format="PNG", optimize=True)
                self.stdout.write(f"写入 {out.relative_to(settings.BASE_DIR)}（{im.width}×{im.height}）")
            manifest["sources"][key] = {
                "dataset": "CADIK color-to-gray evaluation images (2008)",
                "folder": folder,
                "file": src.name,
                "note": "原始彩色图像，转存为 8-bit PNG",
            }

        manifest["generated_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
        manifest_path = Path(settings.BASE_DIR) / "data" / "private" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        self.stdout.write(self.style.SUCCESS("数据准备完成。"))
        for img in denoise.images:
            pub = denoise.public_image(img.key)
            gt = denoise.ground_truth(img.key)
            with Image.open(pub) as im:
                self.stdout.write(f"  {img.key}: 输入 {pub.name} {im.width}×{im.height}  参考 {gt.name}")
        for img in decolor.images:
            pub = decolor.public_image(img.key)
            with Image.open(pub) as im:
                self.stdout.write(f"  {img.key}: 输入 {pub.name} {im.width}×{im.height}")

    # ------------------------------------------------------------------
    def _load_scene(
        self, data_root: Path, name: str
    ) -> Optional[Tuple[Path, Dict[str, Path]]]:
        folder = data_root / name
        if not folder.is_dir():
            return None
        files = _pair_files(folder, "")
        noisy = files.get("NOISY_010")
        gt = files.get("GT_010")
        if not noisy or not gt:
            return None
        return (folder, {"noisy": noisy, "gt": gt})

    def _rank_scenes(
        self, data_root: Path, max_scan: int = 24
    ) -> List[Tuple[Path, Dict[str, Path]]]:
        ranked = []
        folders = sorted(p for p in data_root.iterdir() if p.is_dir())[:max_scan]
        for folder in folders:
            files = _pair_files(folder, "")
            noisy = next((v for k, v in files.items() if k.startswith("NOISY_010")), None)
            gt_same = files.get("GT_010")
            gt_other = files.get("GT_011")
            if not noisy or not gt_same:
                continue
            # 用中心裁切验证配对方向，避免把两幅不同照片配成一对
            try:
                with Image.open(noisy) as im:
                    crop_box = (im.width // 2 - 128, im.height // 2 - 128,
                                im.width // 2 + 128, im.height // 2 + 128)
                    n = np.asarray(im.convert("RGB").crop(crop_box))
                with Image.open(gt_same) as im:
                    g_same = np.asarray(im.convert("RGB").crop(crop_box))
                g_other = None
                if gt_other:
                    with Image.open(gt_other) as im:
                        g_other = np.asarray(im.convert("RGB").crop(crop_box))
            except OSError:
                continue
            p_same = _psnr(n, g_same)
            if g_other is not None and _psnr(n, g_other) > p_same + 0.5:
                continue
            if p_same < 15:
                continue
            score = _texture_score(np.asarray(Image.fromarray(g_same).convert("L")))
            ranked.append((score, folder, {"noisy": noisy, "gt": gt_same}))
        ranked.sort(key=lambda t: -t[0])
        return [(folder, pair) for _, folder, pair in ranked[:20]]

    def _write_denoise_pair(
        self, task, key: str, noisy: Path, gt: Path, size: int, force: bool, rotate: int
    ) -> None:
        pub = task.public_image(key)
        ref = task.ground_truth(key)
        if pub.exists() and ref.exists() and not force:
            self.stdout.write(f"跳过已存在的 {key}")
            return
        box = _best_crop(gt, size)
        noisy_arr = _crop(noisy, box, size)
        gt_arr = _crop(gt, box, size)
        if rotate:
            noisy_arr = np.rot90(noisy_arr.copy(), rotate)
            gt_arr = np.rot90(gt_arr.copy(), rotate)
        save_png(noisy_arr, pub)
        save_png(gt_arr, ref)
        self.stdout.write(
            f"{key}: 写入真实噪声对，裁切 box={box}，初始 PSNR={_psnr(noisy_arr, gt_arr):.2f} dB"
        )

    def _write_synthetic_pair(
        self, task, key: str, gt: Path, size: int, sigma: float, force: bool
    ) -> None:
        pub = task.public_image(key)
        ref = task.ground_truth(key)
        if pub.exists() and ref.exists() and not force:
            self.stdout.write(f"跳过已存在的 {key}")
            return
        box = _best_crop(gt, size)
        clean = _crop(gt, box, size).astype(np.float64)
        rng = np.random.default_rng(20261126)
        noisy = np.clip(clean + rng.normal(0.0, sigma, clean.shape), 0, 255)
        noisy = np.round(noisy).astype(np.uint8)
        clean_u8 = np.round(clean).astype(np.uint8)
        save_png(noisy, pub)
        save_png(clean_u8, ref)
        self.stdout.write(
            f"{key}: 写入合成噪声对 σ={sigma}/255，裁切 box={box}，初始 PSNR={_psnr(noisy, clean_u8):.2f} dB"
        )

    def _find_original(self, root: Path, folder: str) -> Optional[Path]:
        d = root / folder
        if not d.is_dir():
            return None
        for ext in (".ppm", ".png", ".tif", ".tiff", ".jpg", ".jpeg", ".bmp"):
            cand = d / f"{folder}{ext}"
            if cand.exists():
                return cand
        for p in sorted(d.iterdir()):
            if p.suffix.lower() in {".ppm", ".png", ".tif", ".tiff", ".jpg", ".jpeg", ".bmp"}:
                return p
        return None
