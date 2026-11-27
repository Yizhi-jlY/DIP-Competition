"""原始数据源的定位与配对。

只回答两个问题：**哪张图是哪张**、**它的干净参考是哪个文件**。
不做裁切、不加噪、不写盘 —— 那些在 degradation.py 与 prepare_data 里。

两个数据集：
    SIDD  Medium sRGB，每场景一个目录，内含 NOISY_010 / GT_010 等成对文件
    CADIK 2008 color-to-gray 评测图，每张原图一个子目录
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image

from competition.research.degradation import psnr_of_pair

IMAGE_SUFFIXES = {".ppm", ".png", ".tif", ".tiff", ".jpg", ".jpeg", ".bmp"}


@dataclass(frozen=True)
class SiddPair:
    """一个 SIDD 场景：目录、含噪图、干净参考图。"""

    folder: Path
    noisy: Path
    reference: Path


def find_sidd_data(root: Path) -> Path:
    """在 SIDD 发布包里找到 Data 目录（不同解压方式层级不同）。"""
    for cand in (
        root / "Data",
        root / "medium" / "mnt" / "d" / "SIDD_Medium_Srgb" / "Data",
        root / "mnt" / "d" / "SIDD_Medium_Srgb" / "Data",
    ):
        if cand.is_dir():
            return cand
    for path in root.rglob("SIDD_Medium_Srgb"):
        data = path / "Data"
        if data.is_dir():
            return data
    raise FileNotFoundError(f"在 {root} 下找不到 SIDD Medium sRGB 的 Data 目录")


def _scene_files(folder: Path) -> Dict[str, Path]:
    """把 0001_NOISY_SRGB_010.PNG 这类文件名解析成 {NOISY_010: path}。"""
    out: Dict[str, Path] = {}
    for p in folder.glob("*.PNG"):
        parts = p.stem.split("_")
        if len(parts) < 3:
            continue
        out[f"{parts[1]}_{parts[-1]}"] = p
    return out


def load_scene(data_root: Path, name: str) -> Optional[SiddPair]:
    folder = data_root / name
    if not folder.is_dir():
        return None
    files = _scene_files(folder)
    noisy, reference = files.get("NOISY_010"), files.get("GT_010")
    if not noisy or not reference:
        return None
    return SiddPair(folder, noisy, reference)


def _texture_score(gray: np.ndarray) -> float:
    """用拉普拉斯方差衡量纹理丰富度，越大越适合出题。"""
    lap = cv2.Laplacian(gray.astype(np.uint8), cv2.CV_64F)
    return float(lap.var())


def _pair_is_valid(noisy: Path, reference: Path, other_reference: Optional[Path]) -> bool:
    """中心裁切自检：比值参考图更像，说明 NOISY 与 GT 配错对了。"""
    try:
        with Image.open(noisy) as im:
            box = (im.width // 2 - 128, im.height // 2 - 128,
                   im.width // 2 + 128, im.height // 2 + 128)
            n = np.asarray(im.convert("RGB").crop(box))
        with Image.open(reference) as im:
            same = np.asarray(im.convert("RGB").crop(box))
        other = None
        if other_reference is not None:
            with Image.open(other_reference) as im:
                other = np.asarray(im.convert("RGB").crop(box))
    except OSError:
        return False

    p_same = psnr_of_pair(n, same)
    if p_same < 15:  # 含噪图与参考图差得离谱，多半不是一对
        return False
    if other is not None and psnr_of_pair(n, other) > p_same + 0.5:
        return False
    return True


def rank_scenes(data_root: Path, max_scan: int = 24) -> List[SiddPair]:
    """扫前 max_scan 个场景，滤掉配对可疑的，按纹理丰富度从高到低排序。"""
    ranked: List[Tuple[float, SiddPair]] = []
    folders = sorted(p for p in data_root.iterdir() if p.is_dir())[:max_scan]
    for folder in folders:
        files = _scene_files(folder)
        noisy, reference = files.get("NOISY_010"), files.get("GT_010")
        if not noisy or not reference:
            continue
        if not _pair_is_valid(noisy, reference, files.get("GT_011")):
            continue
        with Image.open(reference) as im:
            score = _texture_score(np.asarray(im.convert("L")))
        ranked.append((score, SiddPair(folder, noisy, reference)))
    ranked.sort(key=lambda t: -t[0])
    return [pair for _, pair in ranked]


def find_cadik_image(root: Path, folder: str) -> Optional[Path]:
    """CADIK 每张原图放在同名子目录里，文件名与目录同名。"""
    d = root / folder
    if not d.is_dir():
        return None
    for ext in (".ppm", ".png", ".tif", ".tiff", ".jpg", ".jpeg", ".bmp"):
        cand = d / f"{folder}{ext}"
        if cand.exists():
            return cand
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in IMAGE_SUFFIXES:
            return p
    return None
