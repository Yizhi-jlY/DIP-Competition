"""互评分配与 Bradley–Terry 排名的测试。"""
from __future__ import annotations

import io

import numpy as np
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image

from competition.config import get_task, load_config
from competition.models import AppSetting, Profile, ReviewAssignment, Submission
from competition.peer_review import allocation, ranking
from competition.services import leaderboard, reviews
from competition.services.scoring import score_submission
from tests.utils import Workspace


class AllocationTests(TestCase):
    def test_invariants(self):
        entries = {1: [101, 102, 103, 104, 105, 106]}
        reviewers = [
            ("r1", {1: 101}), ("r2", {1: 102}), ("r3", {1: 103}),
            ("r4", {1: 104}), ("r5", {1: 105}), ("r6", {1: 106}),
        ]
        report = allocation.allocate(entries, reviewers, 3, seed=11)
        self.assertEqual(len(report.assignments), 18)

        pairs = {}
        for a in report.assignments:
            self.assertNotIn(a.reviewer_key, {None}, "reviewer 不能为空")
            self.assertNotEqual(a.a_id, a.b_id)
            owner = dict(reviewers)[a.reviewer_key][1]
            self.assertNotIn(owner, (a.a_id, a.b_id), "不能评价自己的作品")
            key = (a.reviewer_key, frozenset((a.a_id, a.b_id)))
            self.assertNotIn(key, pairs, "同一评价者不能重复评价同一对")
            pairs[key] = 1
        for sid in entries[1]:
            self.assertGreater(report.degree[(1, sid)], 0, "每张作品都应至少被比较一次")

    def test_fewer_than_two_entries(self):
        report = allocation.allocate({1: [1]}, [("r1", {1: 1})], 2, seed=1)
        self.assertEqual(report.assignments, [])


class RankingTests(TestCase):
    def test_transitive_order(self):
        items = [1, 2, 3]
        matches = [(1, 2, 1.0), (1, 3, 1.0), (2, 3, 1.0)]
        result = ranking.bradley_terry(items, matches, regularization=0.1)
        self.assertGreater(result.theta[1], result.theta[2])
        self.assertGreater(result.theta[2], result.theta[3])
        scores = ranking.normalized_scores(result)
        self.assertTrue(0 <= scores[3] <= 100)
        self.assertGreater(scores[1], scores[3])

    def test_all_ties_give_equal_scores(self):
        items = [1, 2]
        result = ranking.bradley_terry(items, [(1, 2, 0.5), (2, 1, 0.5)])
        self.assertAlmostEqual(result.theta[1], result.theta[2], places=6)

    def test_win_rate(self):
        scores = ranking.win_rate_scores([1, 2], [(1, 2, 1.0), (1, 2, 1.0)])
        self.assertEqual(scores[1], 1.0)
        self.assertEqual(scores[2], 0.0)


def png_bytes(array: np.ndarray, mode: str) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(array, mode=mode).save(buf, format="PNG")
    return buf.getvalue()


class PeerReviewFlowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ws = Workspace()

    @classmethod
    def tearDownClass(cls):
        cls.ws.cleanup()
        super().tearDownClass()

    def test_full_review_flow(self):
        task = get_task("decolor")
        rgb = np.asarray(Image.open(task.public_image("C01")))
        students = []
        for i in range(6):
            user = User.objects.create_user(username=f"2026010{i}", password="pass12345")
            Profile.objects.create(
                user=user, student_id=f"2026010{i}", real_name=f"学生{i}",
                contest_code=f"S10{i}", must_change_password=False,
            )
            students.append(user)

            # 不同的灰度权重量，制造质量差异
            weights = [0.299, 0.25, 0.4, 0.2, 0.35, 0.5]
            w = weights[i]
            gray = (
                w * rgb[:, :, 0] + (1 - w) * 0.5 * rgb[:, :, 1] + (1 - w) * 0.5 * rgb[:, :, 2]
            )
            gray = np.clip(gray, 0, 255).astype(np.uint8)
            sub = Submission(
                student=user, task="decolor", image_key="C01", is_final=True,
                width=gray.shape[1], height=gray.shape[0], channels=1,
            )
            sub.file.save(
                f"{user.username}.png", SimpleUploadedFile("g.png", png_bytes(gray, "L")), save=True
            )
            score_submission(sub, task)

        summary = reviews.allocate_reviews(task, per_student=3, seed=5)
        self.assertEqual(summary["assignments"], 18)
        self.assertEqual(ReviewAssignment.objects.count(), 18)

        # 每个学生按自己的偏好投票（灰度更接近亮度灰度的作品胜出）
        for user in students:
            done, total = reviews.progress_for(user, task)
            self.assertEqual(total, 3)
            for assignment in ReviewAssignment.objects.filter(reviewer=user, task=task.slug):
                reviews.record_vote(assignment, "left", duration_ms=1500)

        rows = leaderboard.subjective_rows(task)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(0 <= r.score <= 100 for r in rows))
        self.assertEqual([r.rank for r in rows][0], 1)
        self.assertGreaterEqual(rows[0].score, rows[-1].score)

        count = leaderboard.refresh_subjective(task)
        self.assertEqual(count, 6)

    def test_no_duplicate_vote(self):
        task = get_task("decolor")
        user = User.objects.create_user(username="20269999", password="pass12345")
        Profile.objects.create(
            user=user, student_id="20269999", real_name="x", contest_code="S999",
            must_change_password=False,
        )
        a = Submission.objects.create(student=user, task="decolor", image_key="C01", is_final=False)
        b = Submission.objects.create(student=user, task="decolor", image_key="C01", is_final=False)
        assignment = ReviewAssignment.objects.create(
            reviewer=user, task="decolor", image_key="C01", left=a, right=b
        )
        reviews.record_vote(assignment, "left")
        with self.assertRaises(ValueError):
            reviews.record_vote(assignment, "right")


class MultiTaskReviewTests(TestCase):
    """两个任务各自独立配置份数、独立分配。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ws = Workspace(
            review_tasks={
                "denoise": {"enabled": True, "comparisons_per_student": 2},
                "decolor": {"enabled": True, "comparisons_per_student": 2},
            }
        )

    @classmethod
    def tearDownClass(cls):
        cls.ws.cleanup()
        super().tearDownClass()

    def _students(self, count=4):
        users = []
        for i in range(count):
            user = User.objects.create_user(username=f"2026200{i}", password="pass12345")
            Profile.objects.create(
                user=user, student_id=f"2026200{i}", real_name=f"学生{i}",
                contest_code=f"S20{i}", must_change_password=False,
            )
            users.append(user)
        return users

    def _denoise_sub(self, user, index):
        task = get_task("denoise")
        base = np.asarray(Image.open(task.public_image("D01")))
        arr = np.clip(base.astype(int) + index * 3, 0, 255).astype(np.uint8)
        sub = Submission(
            student=user, task=task.slug, image_key="D01", is_final=True,
            width=arr.shape[1], height=arr.shape[0], channels=3,
        )
        sub.file.save(
            f"{user.username}.png", SimpleUploadedFile("d.png", png_bytes(arr, "RGB")), save=True
        )
        score_submission(sub, task)

    def _decolor_sub(self, user, index):
        task = get_task("decolor")
        rgb = np.asarray(Image.open(task.public_image("C01")))
        w = 0.2 + index * 0.08
        gray = np.clip(
            w * rgb[:, :, 0] + (1 - w) * 0.5 * rgb[:, :, 1] + (1 - w) * 0.5 * rgb[:, :, 2],
            0, 255,
        ).astype(np.uint8)
        sub = Submission(
            student=user, task=task.slug, image_key="C01", is_final=True,
            width=gray.shape[1], height=gray.shape[0], channels=1,
        )
        sub.file.save(
            f"{user.username}.png", SimpleUploadedFile("g.png", png_bytes(gray, "L")), save=True
        )
        score_submission(sub, task)

    def test_review_tasks_and_effective_count_override(self):
        config = load_config()
        self.assertEqual([t.slug for t in config.review_tasks()], ["denoise", "decolor"])

        setting = AppSetting.get()
        setting.review_counts = {"denoise": 6}
        setting.save()
        self.assertEqual(reviews.effective_count(get_task("denoise"), config), 6)
        # 未覆盖的任务继续使用 yaml 中的份数
        self.assertEqual(reviews.effective_count(get_task("decolor"), config), 2)

    def test_allocate_both_tasks_independently(self):
        users = self._students()
        for i, user in enumerate(users):
            self._denoise_sub(user, i)
            self._decolor_sub(user, i)

        denoise = get_task("denoise")
        decolor = get_task("decolor")
        reviews.allocate_reviews(denoise, per_student=2, seed=1)
        reviews.allocate_reviews(decolor, per_student=2, seed=2)

        self.assertEqual(ReviewAssignment.objects.filter(task="denoise").count(), 8)
        self.assertEqual(ReviewAssignment.objects.filter(task="decolor").count(), 8)
        for user in users:
            for task in (denoise, decolor):
                done, total = reviews.progress_for(user, task)
                self.assertEqual((done, total), (0, 2))

        # 只投去噪的票，两个任务的主观榜互不影响
        for user in users:
            for assignment in ReviewAssignment.objects.filter(reviewer=user, task="denoise"):
                reviews.record_vote(assignment, "left")

        self.assertEqual(len(leaderboard.subjective_rows(denoise)), 4)
        self.assertEqual(leaderboard.subjective_rows(decolor), [])
