"""图像读写与基础校验工具。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import IO, Optional, Tuple

import numpy as np
from PIL import Image, UnidentifiedImageError


class ImageValidationError(ValueError):
    """提交图像不合法时抛出，message 直接展示给学生。"""


@dataclass
class ImageMeta:
    width: int
    height: int
    mode: str
    channels: int
    format: str
    size_bytes: int


def inspect_image(src) -> ImageMeta:
    """只解析文件头，读取真实尺寸/通道/格式。src 可以是路径或文件对象。"""
    if isinstance(src, (str, Path)):
        path = Path(src)
        size_bytes = path.stat().st_size
        fh: IO[bytes] = path.open("rb")
        own = True
    else:
        fh = src  # type: ignore[assignment]
        size_bytes = int(getattr(src, "size", 0) or 0)
        own = False

    try:
        fh.seek(0)
        with Image.open(fh) as im:
            fmt = (im.format or "").upper()
            mode = im.mode
            width, height = im.size
            channels = len(im.getbands())
            im.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageValidationError("无法解析该文件，请确认上传的是完整、未损坏的图像。") from exc
    finally:
        if own:
            fh.close()
        else:
            try:
                fh.seek(0)
            except Exception:  # noqa: BLE001
                pass

    return ImageMeta(
        width=width,
        height=height,
        mode=mode,
        channels=channels,
        format=fmt,
        size_bytes=size_bytes,
    )


def load_rgb_u8(path: Path | str) -> np.ndarray:
    with Image.open(path) as im:
        arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
    return arr


def load_gray_u8(path: Path | str) -> np.ndarray:
    with Image.open(path) as im:
        if im.mode == "L":
            arr = np.asarray(im, dtype=np.uint8)
        else:
            arr = np.asarray(im.convert("L"), dtype=np.uint8)
    return arr


def save_png(array: np.ndarray, path: Path | str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if array.ndim == 2:
        img = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8), mode="L")
    else:
        img = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8), mode="RGB")
    img.save(p, format="PNG", optimize=True)
    return p


def reference_size(task, key: str) -> Optional[Tuple[int, int]]:
    """输入图像（公开）的尺寸，作为提交图像的尺寸基准。"""
    path = task.public_image(key)
    if not path.exists():
        return None
    with Image.open(path) as im:
        return im.size
