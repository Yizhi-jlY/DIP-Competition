"""互评分配与 Bradley–Terry 排名的测试。"""
from __future__ import annotations

import io
from datetime import timedelta

import numpy as np
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from competition.config import get_task, load_config
from competition.models import (
    AppSetting,
    ObjectiveRanking,
    Profile,
    ReviewAssignment,
    Submission,
)
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

    def test_review_and_staff_pages_render(self):
        users = self._students()
        for i, user in enumerate(users):
            self._denoise_sub(user, i)
            self._decolor_sub(user, i)

        reviews.allocate_reviews(get_task("denoise"), per_student=2, seed=1)
        reviews.allocate_reviews(get_task("decolor"), per_student=2, seed=2)

        setting = AppSetting.get()
        # 模拟赛程进入互评阶段：提交已截止、互评窗口已打开
        setting.submission_close = timezone.now() - timedelta(hours=2)
        setting.review_open = timezone.now() - timedelta(hours=1)
        setting.review_close = timezone.now() + timedelta(days=1)
        setting.save()

        self.client.force_login(users[0])
        resp = self.client.get(reverse("review"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "结果 A")
        self.assertContains(resp, "结果 B")
        self.assertContains(resp, "去噪")
        # 版式：图片在上、单选列表在下（radio 而非按钮直接提交）
        self.assertContains(resp, 'name="choice"')
        self.assertContains(resp, 'type="radio"')
        self.assertContains(resp, "请选择更合理的")

        resp = self.client.get(reverse("review") + "?task=decolor")
        self.assertEqual(resp.status_code, 200)

        staff = User.objects.create_user(username="teacher2", password="pass12345", is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(reverse("staff_dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "每人份数")
        self.assertContains(resp, "学生注册")

    def test_combined_subjective_board(self):
        users = self._students()
        for i, user in enumerate(users):
            self._denoise_sub(user, i)
            self._decolor_sub(user, i)

        setting = AppSetting.get()
        setting.submission_close = timezone.now() - timedelta(hours=2)
        setting.review_open = timezone.now() - timedelta(hours=1)
        setting.review_close = timezone.now() + timedelta(days=1)
        setting.save()

        denoise, decolor = get_task("denoise"), get_task("decolor")
        reviews.allocate_reviews(denoise, per_student=2, seed=1)
        reviews.allocate_reviews(decolor, per_student=2, seed=2)
        for user in users:
            for task in (denoise, decolor):
                for assignment in ReviewAssignment.objects.filter(reviewer=user, task=task.slug):
                    reviews.record_vote(assignment, "left")

        rows, pending, weights = leaderboard.combined_subjective_rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual(pending, [])
        self.assertAlmostEqual(weights["denoise"], 0.5)
        self.assertAlmostEqual(weights["decolor"], 0.5)

        d_map = {r.student.id: r.score for r in leaderboard.subjective_rows(denoise)}
        c_map = {r.student.id: r.score for r in leaderboard.subjective_rows(decolor)}
        for row in rows:
            expected = round(0.5 * d_map[row.student.id] + 0.5 * c_map[row.student.id], 2)
            self.assertAlmostEqual(row.score, expected)
            self.assertIsNotNone(row.denoise_rank)
            self.assertIsNotNone(row.decolor_rank)
        scores = [r.score for r in rows]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(rows[0].rank, 1)

        # 只有单任务主观分的学生不参与综合排名
        lone = User.objects.create_user(username="20262099", password="pass12345")
        Profile.objects.create(
            user=lone, student_id="20262099", real_name="单项", contest_code="S299",
            must_change_password=False,
        )
        self._decolor_sub(lone, 9)
        left = Submission.objects.filter(
            student=users[0], task="decolor", image_key="C01"
        ).first()
        right = Submission.objects.filter(
            student=lone, task="decolor", image_key="C01"
        ).first()
        assignment = ReviewAssignment.objects.create(
            reviewer=lone, task="decolor", image_key="C01", left=left, right=right
        )
        reviews.record_vote(assignment, "left")

        rows2, pending2, _ = leaderboard.combined_subjective_rows()
        self.assertNotIn(lone.id, [r.student.id for r in rows2])
        lone_row = next(r for r in pending2 if r.student.id == lone.id)
        self.assertIsNone(lone_row.score)
        self.assertEqual(lone_row.missing, ["去噪"])

        # 学生与教师都能打开综合榜；页签可见
        self.client.force_login(users[0])
        resp = self.client.get(reverse("leaderboard") + "?board=combined")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "综合主观榜")
        self.assertContains(resp, "综合 · 主观")

    def test_combined_weights_are_normalized(self):
        from competition.config import ScoringSpec

        spec = ScoringSpec(denoise_subjective_weight=2, decolor_subjective_weight=1)
        self.assertAlmostEqual(spec.combined_weights()["denoise"], 2 / 3)
        zero = ScoringSpec(denoise_subjective_weight=0, decolor_subjective_weight=0)
        self.assertEqual(zero.combined_weights(), {"denoise": 0.5, "decolor": 0.5})


class ObjectiveRankingTests(TestCase):
    """客观排名快照：教师后台看到的按指标排名要与榜单页一致。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ws = Workspace()

    @classmethod
    def tearDownClass(cls):
        cls.ws.cleanup()
        super().tearDownClass()

    def _make_students(self, task, count=4, final=True):
        base = np.asarray(Image.open(task.public_image("D01")).convert("RGB"))
        rng = np.random.default_rng(3)
        users = []
        for i in range(count):
            user = User.objects.create_user(username=f"2026020{i}", password="pass12345")
            Profile.objects.create(
                user=user, student_id=f"2026020{i}", real_name=f"学生{i}",
                contest_code=f"S20{i}", must_change_password=False,
            )
            users.append(user)
            # 噪声越大 PSNR 越低，名次应当随 i 递减
            noisy = np.clip(
                base.astype(np.float32) + rng.normal(0, i * 6, base.shape), 0, 255
            ).astype(np.uint8)
            sub = Submission(
                student=user, task=task.slug, image_key="D01", is_final=final,
                width=noisy.shape[1], height=noisy.shape[0], channels=3,
            )
            sub.file.save(
                f"{user.username}.png",
                SimpleUploadedFile("n.png", png_bytes(noisy, "RGB")),
                save=True,
            )
            score_submission(sub, task)
        return users

    def test_snapshot_matches_leaderboard(self):
        task = get_task("denoise")
        users = self._make_students(task)

        rows, _, _ = leaderboard.objective_rows(task)
        self.assertEqual(len(rows), len(users))

        count = leaderboard.refresh_objective(task)
        self.assertEqual(count, len(users))

        snap = list(ObjectiveRanking.objects.filter(task="denoise").order_by("rank"))
        self.assertEqual([s.student_id for s in snap], [r.student.id for r in rows])
        self.assertEqual([s.rank for s in snap], list(range(1, len(users) + 1)))
        for row, saved in zip(rows, snap):
            self.assertAlmostEqual(saved.score, row.mean, places=6)
            self.assertEqual(saved.metric, "psnr")
            self.assertEqual(list(saved.per_image), ["D01"])

        scores = [s.score for s in snap]
        self.assertEqual(scores, sorted(scores, reverse=True), "名次要按指标从高到低")
        self.assertGreater(scores[0], scores[-1])

        # 重复重算不会产生重复行
        leaderboard.refresh_objective(task)
        self.assertEqual(ObjectiveRanking.objects.filter(task="denoise").count(), len(users))

    def test_confirm_final_refreshes_snapshot_on_commit(self):
        """学生确认最终作品后，客观排名快照要跟着更新（事务提交后才重算）。"""
        from competition.services.submissions import confirm_final

        task = get_task("denoise")
        users = self._make_students(task, count=2, final=False)
        self.assertEqual(ObjectiveRanking.objects.count(), 0)

        with self.captureOnCommitCallbacks(execute=True):
            confirm_final(users[0], task)

        saved = ObjectiveRanking.objects.filter(task="denoise")
        self.assertEqual(saved.count(), 1)
        self.assertEqual(saved.first().student_id, users[0].id)

    def test_admin_lists_snapshot_readonly(self):
        from django.test import RequestFactory

        from competition.admin import ObjectiveRankingAdmin

        task = get_task("denoise")
        self._make_students(task)
        leaderboard.refresh_objective(task)

        staff = User.objects.create_user(
            username="teacher3", password="pass12345", is_staff=True, is_superuser=True
        )
        self.client.force_login(staff)
        resp = self.client.get(reverse("admin:competition_objectiveranking_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "客观排名")
        self.assertContains(resp, "S200")
        self.assertContains(resp, "学生0")

        resp = self.client.get(reverse("admin:competition_subjectiveranking_changelist"))
        self.assertEqual(resp.status_code, 200)

        admin_obj = ObjectiveRankingAdmin(ObjectiveRanking, None)
        request = RequestFactory().get("/")
        request.user = staff
        self.assertFalse(admin_obj.has_add_permission(request))
        self.assertFalse(admin_obj.has_delete_permission(request))
