"""提交校验、评分与最终作品流程的测试。"""
from __future__ import annotations

import io

import numpy as np
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image

from competition.models import Profile, Submission
from competition.services import leaderboard
from competition.config import get_task
from tests.utils import Workspace


def png_bytes(array: np.ndarray, mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    Image.fromarray(array, mode=mode).save(buf, format="PNG")
    return buf.getvalue()


def make_student(username: str, code: str, password: str = "pass12345"):
    user = User.objects.create_user(username=username, password=password)
    Profile.objects.create(
        user=user, student_id=username, real_name=username, contest_code=code,
        must_change_password=False,
    )
    return user


class SubmissionFlowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ws = Workspace()

    @classmethod
    def tearDownClass(cls):
        cls.ws.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.student = make_student("20260001", "S001")
        self.client.force_login(self.student)
        self.task = get_task("denoise")

    def _upload(self, data: bytes, name: str = "result.png", image_key: str = "D01"):
        return self.client.post(
            f"/task/{self.task.slug}/",
            {"image_key": image_key, "file": SimpleUploadedFile(name, data, content_type="image/png")},
            follow=True,
        )

    def test_valid_submission_is_scored(self):
        clean = np.asarray(Image.open(self.task.ground_truth("D01")))
        response = self._upload(png_bytes(clean))
        self.assertEqual(response.status_code, 200)
        sub = Submission.objects.get(student=self.student, task="denoise")
        self.assertIn("psnr", sub.metrics)
        self.assertGreater(sub.metrics["psnr"], 40)
        self.assertAlmostEqual(sub.rank_value, sub.metrics["psnr"], places=6)

    def test_wrong_size_rejected(self):
        small = np.zeros((32, 32, 3), dtype=np.uint8)
        response = self._upload(png_bytes(small))
        self.assertContains(response, "尺寸必须与输入图完全一致")
        self.assertFalse(Submission.objects.filter(student=self.student).exists())

    def test_wrong_channels_rejected(self):
        rgba = np.zeros((64, 64, 4), dtype=np.uint8)
        response = self._upload(png_bytes(rgba, mode="RGBA"))
        self.assertContains(response, "三通道")
        self.assertFalse(Submission.objects.filter(student=self.student).exists())

    def test_non_png_rejected(self):
        clean = np.asarray(Image.open(self.task.ground_truth("D01")))
        buf = io.BytesIO()
        Image.fromarray(clean, "RGB").save(buf, format="JPEG")
        response = self._upload(buf.getvalue(), name="result.jpg")
        self.assertContains(response, "PNG")
        self.assertFalse(Submission.objects.filter(student=self.student).exists())

    def test_attempt_limit(self):
        clean = np.asarray(Image.open(self.task.ground_truth("D01")))
        self._upload(png_bytes(clean))
        self._upload(png_bytes(clean))
        response = self._upload(png_bytes(clean))
        self.assertContains(response, "上限")
        self.assertEqual(
            Submission.objects.filter(student=self.student, image_key="D01").count(), 2
        )

    def test_confirm_final_and_leaderboard(self):
        clean = np.asarray(Image.open(self.task.ground_truth("D01")))
        self._upload(png_bytes(clean))
        response = self.client.post("/task/denoise/confirm/", follow=True)
        self.assertContains(response, "已确认最终作品")
        sub = Submission.objects.get(student=self.student, task="denoise")
        self.assertTrue(sub.is_final)

        rows, _, _ = leaderboard.objective_rows(self.task)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].code, "S001")

    def test_cannot_view_others_submission(self):
        clean = np.asarray(Image.open(self.task.ground_truth("D01")))
        other = make_student("20260002", "S002")
        sub = Submission(student=other, task="denoise", image_key="D01")
        sub.file.save("x.png", SimpleUploadedFile("x.png", png_bytes(clean)), save=True)

        response = self.client.get(f"/files/submission/{sub.pk}.png")
        self.assertEqual(response.status_code, 403)

    def test_ground_truth_not_served(self):
        response = self.client.get("/files/public/denoise/D01.png")
        self.assertEqual(response.status_code, 200)
        response = self.client.get("/files/public/denoise/..%2F..%2Fprivate%2Fground_truth%2FD01.png")
        self.assertIn(response.status_code, (403, 404))


class LeaderboardOrderTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ws = Workspace()

    @classmethod
    def tearDownClass(cls):
        cls.ws.cleanup()
        super().tearDownClass()

    def test_better_psnr_ranks_first(self):
        task = get_task("denoise")
        clean = np.asarray(Image.open(task.ground_truth("D01")))
        noisy = np.asarray(Image.open(task.public_image("D01")))
        blurred = cv_blur(noisy)

        for username, code, array in (
            ("20260011", "S011", clean),
            ("20260012", "S012", blurred),
        ):
            user = make_student(username, code)
            sub = Submission(
                student=user, task="denoise", image_key="D01", is_final=True,
                width=array.shape[1], height=array.shape[0], channels=3,
            )
            sub.file.save(f"{username}.png", SimpleUploadedFile("r.png", png_bytes(array)), save=True)
            from competition.services.scoring import score_submission

            score_submission(sub, task)

        rows, _, _ = leaderboard.objective_rows(task)
        self.assertEqual([r.code for r in rows], ["S011", "S012"])
        self.assertEqual(rows[0].rank, 1)


def cv_blur(array: np.ndarray) -> np.ndarray:
    out = array.astype(int).copy()
    out[1:-1, 1:-1] = (
        array[:-2, 1:-1].astype(int) + array[2:, 1:-1] + array[1:-1, :-2] + array[1:-1, 2:]
    ) // 4
    return out.astype(np.uint8)
