"""生成一整套演示数据：账号、作品、互评分配、投票、榜单与赛程阶段。

    python manage.py seed_demo --students 140     # 建 140 个账号并灌满数据
    python manage.py seed_demo --students 140 --no-votes
    python manage.py seed_demo --clear            # 一键删除全部演示数据

演示账号：demo001…demo140，统一密码 demo12345；参赛编号 S101…S240。
作品不是纯随机噪声：以平台已有的经典算法基线为上限，按每个学生的「能力值」
在基线与原始输入之间插值，PSNR / E-score 会自然拉开档次；互评投票也用同一
能力值模拟，拟合出的主观排名与客观分数正相关，看起来就像真上过一轮课。
"""
from __future__ import annotations

import io
import random
import time
from collections import defaultdict
from datetime import timedelta
from typing import Dict, List, Optional

import numpy as np
from PIL import Image
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from competition.config import load_config
from competition.models import AppSetting, Profile, ReviewAssignment, Submission
from competition.services import leaderboard, reviews
from competition.services.submissions import SubmitError, confirm_final, create_submission

PREFIX = "demo"
PASSWORD = "demo12345"
CODE_START = 101
STUDENT_ID_START = 20260001


def _png_bytes(array: np.ndarray, mode: str) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(array.astype(np.uint8), mode=mode).save(buf, format="PNG")
    return buf.getvalue()


def _load(path, mode: str = "RGB") -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(im.convert(mode), dtype=np.uint8)


def _luminance(rgb: np.ndarray) -> np.ndarray:
    arr = rgb.astype(np.float32)
    return 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]


def _best_baseline(task_slug: str, image_key: str) -> Optional[Submission]:
    return (
        Submission.objects.filter(task=task_slug, image_key=image_key, is_baseline=True)
        .order_by("-rank_value")
        .first()
    )


def _sigmoid(x: float) -> float:
    if x > 30:
        return 1.0
    if x < -30:
        return 0.0
    return float(1.0 / (1.0 + np.exp(-x)))


class Command(BaseCommand):
    help = "生成演示学生、作品、互评与投票（演练用），或清理演示数据"

    def add_arguments(self, parser):
        parser.add_argument("--students", type=int, default=140)
        parser.add_argument("--clear", action="store_true", help="删除全部演示数据")
        parser.add_argument("--no-votes", action="store_true", help="只分配互评，不模拟投票")
        parser.add_argument(
            "--idle", type=int, default=10, help="留几个学生完全没投票（默认 10）"
        )
        parser.add_argument("--seed", type=int, default=20261127)
        parser.add_argument("--teacher-password", default="teacher")
        parser.add_argument("--keep-phase", action="store_true", help="不改赛程阶段")
        parser.add_argument("--keep-teacher", action="store_true", help="不改教师密码")

    # ------------------------------------------------------------------ 入口
    def handle(self, *args, **options):
        if options["clear"]:
            self._clear()
            return

        config = load_config()
        count = int(options["students"])
        if count < 2:
            raise CommandError("至少需要 2 个学生才能互评。")

        started = time.time()
        rng = random.Random(options["seed"])
        if not options["keep_teacher"]:
            self._ensure_teacher(options["teacher_password"])

        skills = self._create_users(count)
        self._seed_submissions(config, skills, keep_phase=options["keep_phase"])
        self._allocate_reviews(config)
        if not options["no_votes"]:
            self._simulate_votes(config, rng, idle=options["idle"])
        if not options["keep_phase"]:
            self._set_phase()

        self._summary(config, count, time.time() - started)

    # ------------------------------------------------------------ 教师 / 学生
    def _ensure_teacher(self, password: str) -> None:
        user, created = User.objects.get_or_create(
            username="teacher",
            defaults={"is_staff": True, "is_superuser": True, "email": ""},
        )
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        user.set_password(password)
        user.save()
        self.stdout.write(
            self.style.SUCCESS(
                f"{'新建' if created else '已重置'}教师账号 teacher / {password}（staff + superuser）"
            )
        )

    def _create_users(self, count: int) -> Dict[str, float]:
        """建账号，同时给每人一个 0–1 的能力值（决定作品质量与投票倾向）。"""
        skills: Dict[str, float] = {}
        created = 0
        for i in range(1, count + 1):
            username = f"{PREFIX}{i:03d}"
            skill = min(0.97, max(0.05, random.Random(1000 + i).betavariate(2.4, 2.0)))
            skills[username] = skill
            if User.objects.filter(username=username).exists():
                continue
            with transaction.atomic():
                user = User.objects.create_user(username=username, password=PASSWORD)
                Profile.objects.create(
                    user=user,
                    student_id=str(STUDENT_ID_START + i - 1),
                    real_name=f"演示学生{i:03d}",
                    contest_code=f"S{CODE_START + i - 1:03d}",
                    must_change_password=False,
                )
            created += 1
        self.stdout.write(f"学生账号：新建 {created} 个，共 {count} 个（密码 {PASSWORD}）")
        return skills

    # ---------------------------------------------------------------- 作品
    def _seed_submissions(self, config, skills, keep_phase: bool = False) -> None:
        """在临时开放的提交窗口里，真实走一遍上传 + 评分 + 确认最终作品。"""
        setting = AppSetting.get()
        backup = (setting.submission_open, setting.submission_close)
        now = timezone.now()
        setting.submission_open = now - timedelta(days=3)
        setting.submission_close = now + timedelta(hours=6)
        setting.save(update_fields=["submission_open", "submission_close", "updated_at"])

        users = sorted(
            User.objects.filter(username__startswith=PREFIX).select_related("profile"),
            key=lambda u: u.username,
        )
        total = 0
        try:
            for idx, user in enumerate(users, start=1):
                skill = skills.get(user.username, 0.5)
                attempts = 2 if idx % 4 == 0 else 1
                for task in config.tasks:
                    for img in task.images:
                        baseline = _best_baseline(task.slug, img.key)
                        if baseline is None or not baseline.file:
                            raise CommandError(
                                f"缺少 {task.name} {img.key} 的算法基线，请先运行 run_baselines。"
                            )
                        src = _load(task.public_image(img.key), "RGB")
                        if task.channels == 3:
                            base = _load(baseline.file.path, "RGB")
                            base_gray = None
                            lum = None
                        else:
                            base = None
                            base_gray = _load(baseline.file.path, "L")
                            lum = _luminance(src)

                        for n in range(attempts):
                            # 有两次提交时，第一次手生、第二次正常发挥
                            level = skill - 0.12 if (attempts == 2 and n == 0) else skill
                            alpha = 0.30 + 0.695 * min(0.99, max(0.02, level))
                            if task.channels == 3:
                                out = alpha * base.astype(np.float32) + (1 - alpha) * src.astype(
                                    np.float32
                                )
                                array, mode = np.clip(out, 0, 255).astype(np.uint8), "RGB"
                            else:
                                out = alpha * base_gray.astype(np.float32) + (1 - alpha) * lum
                                array, mode = np.clip(out, 0, 255).astype(np.uint8), "L"
                            uploaded = SimpleUploadedFile(
                                f"{user.username}_{img.key}_a{n + 1}.png",
                                _png_bytes(array, mode),
                                content_type="image/png",
                            )
                            try:
                                create_submission(user, task, img.key, uploaded)
                                total += 1
                            except SubmitError as exc:
                                self.stderr.write(f"  {user.username} {task.slug} {img.key}: {exc}")
                    confirm_final(user, task)
                if idx % 20 == 0 or idx == len(users):
                    self.stdout.write(f"  已生成 {idx}/{len(users)} 名学生的作品（{total} 次提交）")
        finally:
            if keep_phase:
                setting.submission_open, setting.submission_close = backup
                setting.save(
                    update_fields=["submission_open", "submission_close", "updated_at"]
                )

    # ---------------------------------------------------------------- 互评
    def _allocate_reviews(self, config) -> None:
        for task in config.review_tasks():
            info = reviews.allocate_reviews(task, seed=2026)
            issues = info.get("issues") or []
            self.stdout.write(
                f"互评分配 · {task.name}：每人 {info['per_student']} 份，"
                f"{info['assignments']} 条任务，覆盖 {info['reviewers']} 人，"
                f"问题 {len(issues)} 个"
            )
            for issue in issues[:3]:
                self.stdout.write(f"    · {issue}")

    def _simulate_votes(self, config, rng, idle: int = 10) -> None:
        """按能力值决定投票倾向：强的人更容易被选中，弱的人偶尔翻车。"""
        idle_users = {f"{PREFIX}{i:03d}" for i in range(1, idle + 1)}

        for task in config.review_tasks():
            finals = (
                Submission.objects.filter(task=task.slug, is_final=True, student__isnull=False)
                .values("id", "student__username", "rank_value")
            )
            values: Dict[str, List[float]] = defaultdict(list)
            sub_owner: Dict[int, str] = {}
            for row in finals:
                sub_owner[row["id"]] = row["student__username"]
                if row["rank_value"] is not None:
                    values[row["student__username"]].append(float(row["rank_value"]))

            means = {u: sum(v) / len(v) for u, v in values.items() if v}
            mu = float(np.mean(list(means.values()))) if means else 0.0
            sd = float(np.std(list(means.values()))) or 1.0
            theta = {u: (m - mu) / sd for u, m in means.items()}

            spec = config.peer_review.for_task(task.slug)
            allow_tie = bool(getattr(spec, "allow_tie", False))
            pending = list(
                ReviewAssignment.objects.filter(task=task.slug, comparison__isnull=True)
                .select_related("reviewer")
            )
            voted = 0
            for assignment in pending:
                if assignment.reviewer.username in idle_users:
                    continue
                tl = theta.get(sub_owner.get(assignment.left_id), 0.0) + rng.gauss(0, 0.55)
                tr = theta.get(sub_owner.get(assignment.right_id), 0.0) + rng.gauss(0, 0.55)
                diff = tl - tr
                if allow_tie and abs(diff) < 0.35 and rng.random() < 0.12:
                    choice = "tie"
                else:
                    choice = "left" if rng.random() < _sigmoid(diff * 1.4) else "right"
                reviews.record_vote(assignment, choice, duration_ms=rng.randint(4000, 26000))
                voted += 1
                if voted % 500 == 0:
                    self.stdout.write(f"  {task.name} 已模拟 {voted}/{len(pending)} 次投票")
            self.stdout.write(f"投票 · {task.name}：{voted} 条")

    # ---------------------------------------------------------------- 阶段
    def _set_phase(self) -> None:
        setting = AppSetting.get()
        now = timezone.now()
        setting.submission_open = None
        setting.submission_close = now - timedelta(hours=2)
        setting.review_open = now - timedelta(hours=1)
        setting.review_close = now + timedelta(days=7)
        setting.save()
        self.stdout.write("赛程：提交已截止（2 小时前），互评进行中（7 天后截止）")

    # ---------------------------------------------------------------- 汇总
    def _summary(self, config, count: int, seconds: float) -> None:
        subs = Submission.objects.filter(student__isnull=False).count()
        finals = Submission.objects.filter(student__isnull=False, is_final=True).count()
        votes = ReviewAssignment.objects.filter(comparison__isnull=False).count()
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"演示数据就绪：{count} 个学生 · {subs} 次提交 · {finals} 件最终作品 · "
                f"{votes} 次互评，耗时 {seconds:.0f}s"
            )
        )
        for task in config.review_tasks():
            start = time.time()
            rows = leaderboard.subjective_rows(task)
            cost = time.time() - start
            head = "，".join(f"{r.code}={r.score:.1f}" for r in rows[:3])
            self.stdout.write(f"  {task.name} 主观榜 {len(rows)} 条（拟合 {cost:.2f}s）：{head}")

    # ---------------------------------------------------------------- 清理
    def _clear(self) -> None:
        users = User.objects.filter(username__startswith=PREFIX)
        subs = list(Submission.objects.filter(student__in=users))
        files = 0
        for submission in subs:
            if submission.file:
                submission.file.delete(save=False)
                files += 1
        count = users.count()
        users.delete()

        setting = AppSetting.get()
        setting.submission_open = None
        setting.submission_close = None
        setting.review_open = None
        setting.review_close = None
        setting.save()
        self.stdout.write(
            self.style.SUCCESS(
                f"已删除 {count} 个演示账号、{len(subs)} 条提交（{files} 个文件），"
                f"赛程恢复为 yaml 默认。"
            )
        )
