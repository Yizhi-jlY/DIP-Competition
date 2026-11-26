"""生成模拟学生与作品，用于上线前的端到端演练。

    python manage.py seed_demo --students 12
    python manage.py seed_demo --clear     # 删除全部演示数据

演示账号用户名形如 demo001，参赛编号 T001，密码 demo12345。
"""
from __future__ import annotations

import numpy as np
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from competition.config import load_config
from competition.imaging import load_rgb_u8
from competition.models import Profile, Submission
from competition.services.scoring import evaluate_path

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


def _variant_denoise(noisy: np.ndarray, k: int) -> np.ndarray:
    d = 5 + 2 * (k % 5)
    sigma = 25 + 15 * (k % 4)
    base = cv2.bilateralFilter(noisy, d, sigma, sigma)
    alpha = 0.55 + 0.08 * (k % 6)
    out = alpha * base.astype(np.float32) + (1 - alpha) * noisy.astype(np.float32)
    return np.clip(out, 0, 255).astype(np.uint8)


def _variant_decolor(rgb: np.ndarray, k: int) -> np.ndarray:
    arr = rgb.astype(np.float32)
    weights = [
        (0.299, 0.587, 0.114),
        (0.25, 0.5, 0.25),
        (0.4, 0.4, 0.2),
        (0.2, 0.45, 0.35),
        (0.35, 0.35, 0.3),
    ]
    wr, wg, wb = weights[k % len(weights)]
    gray = wr * arr[:, :, 0] + wg * arr[:, :, 1] + wb * arr[:, :, 2]
    gamma = 0.8 + 0.1 * (k % 5)
    gray = 255.0 * np.power(np.clip(gray / 255.0, 0, 1), gamma)
    return np.clip(gray, 0, 255).astype(np.uint8)


class Command(BaseCommand):
    help = "生成模拟学生与作品（演练用），或清理演示数据"

    def add_arguments(self, parser):
        parser.add_argument("--students", type=int, default=12)
        parser.add_argument("--clear", action="store_true")

    def handle(self, *args, **options):
        if options["clear"]:
            self._clear()
            return
        if cv2 is None:
            self.stderr.write("需要 OpenCV 生成演示作品。")
            return

        config = load_config()
        count = int(options["students"])
        created = 0
        for i in range(1, count + 1):
            username = f"demo{i:03d}"
            if User.objects.filter(username=username).exists():
                continue
            with transaction.atomic():
                user = User.objects.create_user(username=username, password="demo12345")
                Profile.objects.create(
                    user=user,
                    student_id=f"T{2026000 + i}",
                    real_name=f"演示学生{i:02d}",
                    contest_code=f"T{i:03d}",
                    must_change_password=False,
                )
            created += 1

        users = list(User.objects.filter(username__startswith="demo").select_related("profile"))
        for user in users:
            idx = int(user.username.replace("demo", ""))
            for task in config.tasks:
                for img in task.images:
                    if Submission.objects.filter(
                        student=user, task=task.slug, image_key=img.key, is_final=True
                    ).exists():
                        continue
                    src = load_rgb_u8(task.public_image(img.key))
                    if task.channels == 3:
                        array = _variant_denoise(src, idx)
                        mode = "RGB"
                    else:
                        array = _variant_decolor(src, idx)
                        mode = "L"
                    tmp = task.ground_truth_dir / f".demo_{user.id}_{task.slug}_{img.key}.png"
                    tmp.parent.mkdir(parents=True, exist_ok=True)
                    from PIL import Image

                    Image.fromarray(array, mode=mode).save(tmp, format="PNG")
                    metrics = evaluate_path(task, img.key, tmp)
                    sub = Submission(
                        student=user,
                        task=task.slug,
                        image_key=img.key,
                        original_name=f"{user.username}_{img.key}.png",
                        width=array.shape[1],
                        height=array.shape[0],
                        channels=1 if array.ndim == 2 else 3,
                        size_bytes=tmp.stat().st_size,
                        metrics=metrics,
                        rank_value=metrics.get(task.ranking_metric),
                        is_final=True,
                        scored_at=timezone.now(),
                    )
                    sub.file.save(
                        f"{user.username}_{task.slug}_{img.key}.png",
                        ContentFile(tmp.read_bytes()),
                        save=False,
                    )
                    sub.save()
                    tmp.unlink(missing_ok=True)

        self.stdout.write(self.style.SUCCESS(
            f"演示数据就绪：新建 {created} 个账号（密码 demo12345），"
            f"当前演示账号 {len(users)} 个。"
        ))

    def _clear(self) -> None:
        users = User.objects.filter(username__startswith="demo")
        count = users.count()
        users.delete()
        self.stdout.write(self.style.SUCCESS(f"已删除 {count} 个演示账号及其作品、投票。"))
