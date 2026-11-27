"""文件服务：公开输入图、结果图、互评对比图、输入图打包下载。"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404

from competition.config import get_task
from competition.models import ReviewAssignment, Submission
from competition.views.base import file_response, thumbnail


@login_required
def public_image(request: HttpRequest, slug: str, key: str):
    task = get_task(slug)
    if key not in {img.key for img in task.images}:
        raise Http404
    path = task.public_image(key)
    if request.GET.get("w") == "thumb":
        # 输入图在赛程内不会变化，允许浏览器长缓存，避免反复下载
        return file_response(
            thumbnail(path, 640), cache="private, max-age=86400", content_type="image/jpeg"
        )
    return file_response(path, cache="private, max-age=86400")


@login_required
def submission_image(request: HttpRequest, pk: int):
    sub = get_object_or_404(Submission, pk=pk)
    if sub.student_id != request.user.id and not request.user.is_staff:
        raise PermissionDenied("只能查看自己的结果图。")
    return file_response(Path(sub.file.path), Path(sub.file.name).name)


@login_required
def review_image(request: HttpRequest, assignment_id: int, side: str):
    assignment = get_object_or_404(ReviewAssignment, pk=assignment_id)
    if assignment.reviewer_id != request.user.id and not request.user.is_staff:
        raise PermissionDenied("无权查看该互评任务。")
    if side not in {"left", "right"}:
        raise Http404
    sub = assignment.left if side == "left" else assignment.right
    return file_response(Path(sub.file.path), f"{side}.png")


@login_required
def inputs_zip(request: HttpRequest, slug: str):
    task = get_task(slug)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for img in task.images:
            path = task.public_image(img.key)
            if path.exists():
                zf.write(path, arcname=f"{task.slug}/{img.key}.png")
    buffer.seek(0)
    resp = HttpResponse(buffer.getvalue(), content_type="application/zip")
    resp["Content-Disposition"] = f'attachment; filename="{task.slug}_inputs.zip"'
    return resp
