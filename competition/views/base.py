"""页面层公共能力。

只被多个页面复用的东西放这里：

- :func:`render_page` —— 统一注入 ``tasks`` 后渲染模板；
- :func:`file_response` / :func:`thumbnail` —— 图片与下载响应、缩略图落盘缓存；
- :func:`leaderboard_visible` —— 排行榜是否对当前用户公开。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

from django.conf import settings
from django.http import FileResponse, Http404
from django.shortcuts import render

from competition.config import load_config
from competition.models import AppSetting


def render_page(request, template: str, context: Optional[Dict] = None, status: int = 200):
    """渲染页面并统一带上 ``tasks``（导航栏用）。"""
    ctx = {"tasks": load_config().tasks}
    if context:
        ctx.update(context)
    return render(request, template, ctx, status=status)


def leaderboard_visible(user) -> bool:
    """排行榜默认公开；教师始终可见。数据库未初始化时按公开处理。"""
    if user.is_staff:
        return True
    try:
        return AppSetting.get().leaderboard_public
    except Exception:  # noqa: BLE001
        return True


def file_response(
    path: Path,
    filename: Optional[str] = None,
    cache: str = "private, max-age=300",
    content_type: str = "image/png",
) -> FileResponse:
    if not path.exists():
        raise Http404("文件不存在。")
    resp = FileResponse(open(path, "rb"), content_type=content_type)
    if filename:
        resp["Content-Disposition"] = f'inline; filename="{filename}"'
    resp["Cache-Control"] = cache
    return resp


def thumbnail(src: Path, width: int = 640) -> Path:
    """按需生成缩略图并落盘缓存。

    输入图是 768×768 的无损 PNG（约 1.3–1.5 MB），140 人同时刷新任务页会重复下载
    近 400 MB。页面上改用 640px 的 JPEG 缩略图（噪声图用 PNG 仍接近 900 KB，
    JPEG 约 120 KB），"下载输入图"仍给原 PNG。
    """
    from PIL import Image

    try:
        rel = src.resolve().relative_to((settings.BASE_DIR / "data").resolve())
    except ValueError:
        return src
    dst = settings.BASE_DIR / "storage" / "thumbs" / rel.parent / f"{src.stem}_{width}.jpg"
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(f".{os.getpid()}.tmp")
    with Image.open(src) as im:
        if im.mode == "L":
            work = im.convert("L")
        else:
            work = im.convert("RGB")
        work.thumbnail((width, width), Image.LANCZOS)
        work.save(tmp, format="JPEG", quality=82, optimize=True, progressive=True)
    tmp.replace(dst)  # 原子替换，避免并发下读到半张图
    return dst
