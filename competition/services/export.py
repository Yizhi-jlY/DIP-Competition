"""成绩与作品导出（CSV / ZIP）。"""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from django.contrib.auth.models import User
from django.utils import timezone

from competition.config import load_config
from competition.models import Profile, Submission
from competition.services import leaderboard


def _when(value) -> str:
    if not value:
        return ""
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S")


def _final_columns(sub: Optional[Submission]) -> Dict[str, str]:
    """最终作品的「文件名 / 提交时间 / 确认时间」，方便教师核对用的是哪一次。"""
    if sub is None:
        return {"文件名": "", "提交时间": "", "确认时间": ""}
    return {
        "文件名": sub.original_name or "",
        "提交时间": _when(sub.created_at),
        "确认时间": _when(sub.finalized_at),
    }


def _fmt(value: Optional[float], digits: int = 4) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def score_rows() -> List[Dict[str, str]]:
    config = load_config()
    denoise = config.task("denoise")
    decolor = config.task("decolor")

    obj_rank: Dict[int, int] = {}
    denoise_metrics: Dict[int, Dict[str, Optional[float]]] = {}
    if denoise:
        rows, incomplete, _ = leaderboard.objective_rows(denoise)
        for row in rows:
            if row.student:
                obj_rank[row.student.id] = row.rank or 0
                denoise_metrics[row.student.id] = {
                    "mean": row.mean,
                    "tie": row.tiebreak,
                }
        for row in incomplete:
            if row.student:
                denoise_metrics[row.student.id] = {"mean": None, "tie": None}

    decolor_rank: Dict[int, int] = {}
    decolor_metrics: Dict[int, Dict[str, Optional[float]]] = {}
    if decolor:
        rows, incomplete, _ = leaderboard.objective_rows(decolor)
        for row in rows:
            if row.student:
                decolor_rank[row.student.id] = row.rank or 0
                decolor_metrics[row.student.id] = {"mean": row.mean, "tie": row.tiebreak}
        for row in incomplete:
            if row.student:
                decolor_metrics[row.student.id] = {"mean": None, "tie": None}

    subj = {r.student.id: r for r in leaderboard.subjective_rows(decolor)} if decolor else {}
    subj_denoise = (
        {r.student.id: r for r in leaderboard.subjective_rows(denoise)} if denoise else {}
    )
    combined = leaderboard.combined_by_student()

    finals: Dict[int, Dict[str, Submission]] = {}
    for sub in Submission.objects.filter(is_final=True, is_baseline=False).select_related("student"):
        finals.setdefault(sub.student_id, {})[f"{sub.task}:{sub.image_key}"] = sub

    out: List[Dict[str, str]] = []
    profiles = Profile.objects.select_related("user").order_by("contest_code")
    for profile in profiles:
        student: User = profile.user
        row: Dict[str, str] = {
            "学号": profile.student_id,
            "姓名": profile.real_name,
            "参赛编号": profile.contest_code,
        }
        if denoise:
            means_psnr, means_ssim = [], []
            for img in denoise.images:
                sub = finals.get(student.id, {}).get(f"{denoise.slug}:{img.key}")
                metrics = sub.metrics if sub else {}
                row[f"{img.key}_PSNR"] = _fmt(metrics.get("psnr"), 2)
                row[f"{img.key}_SSIM"] = _fmt(metrics.get("ssim"), 4)
                for k, v in _final_columns(sub).items():
                    row[f"{img.key}_{k}"] = v
                if metrics.get("psnr") is not None:
                    means_psnr.append(metrics["psnr"])
                if metrics.get("ssim") is not None:
                    means_ssim.append(metrics["ssim"])
            dm = denoise_metrics.get(student.id, {})
            row["去噪平均PSNR"] = _fmt(dm.get("mean"), 2)
            row["去噪平均SSIM"] = _fmt(dm.get("tie"), 4)
            row["去噪名次"] = str(obj_rank.get(student.id, ""))
            sd = subj_denoise.get(student.id)
            row["去噪主观得分"] = _fmt(sd.score if sd else None, 2)
            row["去噪主观名次"] = str(sd.rank if sd else "")

        if decolor:
            for img in decolor.images:
                sub = finals.get(student.id, {}).get(f"{decolor.slug}:{img.key}")
                metrics = sub.metrics if sub else {}
                row[f"{img.key}_CCPR"] = _fmt(metrics.get("ccpr"), 4)
                row[f"{img.key}_CCFR"] = _fmt(metrics.get("ccfr"), 4)
                row[f"{img.key}_E"] = _fmt(metrics.get("escore"), 4)
                for k, v in _final_columns(sub).items():
                    row[f"{img.key}_{k}"] = v
            dm = decolor_metrics.get(student.id, {})
            row["去色平均E"] = _fmt(dm.get("mean"), 4)
            row["去色平均CCPR"] = _fmt(dm.get("tie"), 4)
            row["去色客观名次"] = str(decolor_rank.get(student.id, ""))
            sr = subj.get(student.id)
            row["去色主观得分"] = _fmt(sr.score if sr else None, 2)
            row["去色主观名次"] = str(sr.rank if sr else "")
            if sr and dm.get("mean") is not None:
                w_obj = config.scoring.weight_objective
                w_subj = config.scoring.weight_subjective
                q = 100 * (w_obj * dm["mean"] + w_subj * sr.score / 100.0)
                row["综合参考分"] = _fmt(q, 2)
            else:
                row["综合参考分"] = ""

        cr = combined.get(student.id)
        row["主观综合分"] = _fmt(cr.score if cr else None, 2)
        row["主观综合名次"] = str(cr.rank) if cr and cr.rank else ""
        if cr and cr.missing:
            row["主观综合缺项"] = "、".join(cr.missing)
        else:
            row["主观综合缺项"] = ""

        row["去噪最终作品"] = "✓" if any(
            k.startswith("denoise:") for k in finals.get(student.id, {})
        ) else ""
        row["去色最终作品"] = "✓" if any(
            k.startswith("decolor:") for k in finals.get(student.id, {})
        ) else ""
        out.append(row)
    return out


def scores_csv() -> str:
    rows = score_rows()
    buffer = io.StringIO()
    buffer.write("\ufeff")
    if not rows:
        buffer.write("学号,姓名,参赛编号\n")
        return buffer.getvalue()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def submissions_zip() -> io.BytesIO:
    """打包所有最终作品，按 任务/原图/参赛编号.png 组织，附 manifest.csv。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = io.StringIO()
        writer = csv.writer(manifest)
        writer.writerow(["任务", "原图", "参赛编号", "学号", "提交时间", "指标", "文件名"])
        for sub in Submission.objects.filter(is_final=True, is_baseline=False).select_related(
            "student", "student__profile"
        ):
            profile = getattr(sub.student, "profile", None)
            code = profile.contest_code if profile else sub.student.username
            arc = f"{sub.task}/{sub.image_key}/{code}.png"
            try:
                with open(sub.file.path, "rb") as fh:
                    zf.writestr(arc, fh.read())
            except OSError:
                continue
            writer.writerow([
                sub.task, sub.image_key, code,
                profile.student_id if profile else "",
                sub.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                ";".join(f"{k}={v}" for k, v in (sub.metrics or {}).items()),
                arc,
            ])
        zf.writestr("manifest.csv", "\ufeff" + manifest.getvalue())
    buffer.seek(0)
    return buffer
