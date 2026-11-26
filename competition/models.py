"""数据模型。

设计原则：竞赛规则（任务、图像、指标、时间）来自 config/competition.yaml，
数据库只保存「人、作品、评分、投票」这些不可推导的事实。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q
from django.utils import timezone


def submission_upload_to(instance: "Submission", filename: str) -> str:
    now = timezone.localtime()
    ext = Path(filename).suffix.lower() or ".png"
    return f"submissions/{now:%Y/%m}/{uuid.uuid4().hex}{ext}"


class Profile(models.Model):
    """学生档案：真实身份只对教师可见，排行榜只显示匿名编号。"""

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="profile", verbose_name="账号"
    )
    student_id = models.CharField("学号", max_length=32, unique=True)
    real_name = models.CharField("姓名", max_length=64, blank=True)
    contest_code = models.CharField("参赛编号", max_length=16, unique=True)
    imported_at = models.DateTimeField("导入时间", auto_now_add=True)
    must_change_password = models.BooleanField("首次登录需改密", default=True)

    class Meta:
        verbose_name = "学生档案"
        verbose_name_plural = "学生档案"
        ordering = ["contest_code"]

    def __str__(self) -> str:
        return f"{self.contest_code} {self.real_name}({self.student_id})"

    @property
    def display_name(self) -> str:
        if getattr(settings, "COMPETITION_ANONYMOUS", True):
            return self.contest_code
        return self.real_name or self.student_id


class Submission(models.Model):
    """一次结果图提交。基线（教师预置的经典算法）也用同一张表，便于统一评分。"""

    student = models.ForeignKey(
        User, on_delete=models.CASCADE, null=True, blank=True,
        related_name="submissions", verbose_name="学生",
    )
    task = models.CharField("任务", max_length=32)
    image_key = models.CharField("图像编号", max_length=16)
    file = models.FileField("结果图", upload_to=submission_upload_to)
    original_name = models.CharField("原始文件名", max_length=128, blank=True)
    width = models.PositiveIntegerField("宽", default=0)
    height = models.PositiveIntegerField("高", default=0)
    channels = models.PositiveSmallIntegerField("通道数", default=0)
    size_bytes = models.PositiveIntegerField("文件大小", default=0)

    metrics = models.JSONField("评测指标", default=dict, blank=True)
    rank_value = models.FloatField("排名指标值", null=True, blank=True)
    attempt = models.PositiveSmallIntegerField("第几次提交", default=1)

    is_final = models.BooleanField("已确认为最终提交", default=False)
    is_baseline = models.BooleanField("基线作品", default=False)
    baseline_method = models.CharField("基线方法", max_length=32, blank=True)

    created_at = models.DateTimeField("提交时间", auto_now_add=True)
    scored_at = models.DateTimeField("评分时间", null=True, blank=True)
    score_error = models.CharField("评分错误", max_length=200, blank=True)

    class Meta:
        verbose_name = "提交"
        verbose_name_plural = "提交"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["task", "image_key", "is_final"]),
            models.Index(fields=["student", "task", "image_key", "-created_at"]),
            models.Index(fields=["is_baseline", "task", "image_key"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "task", "image_key"],
                condition=Q(is_final=True),
                name="uniq_final_submission_per_image",
            )
        ]

    def __str__(self) -> str:
        who = self.baseline_method or (self.student.profile.contest_code if self.student_id else "—")
        return f"{self.task}/{self.image_key} #{self.pk} {who}"

    @property
    def owner_code(self) -> str:
        if self.is_baseline:
            return f"基线·{self.baseline_method}"
        if self.student_id and hasattr(self.student, "profile"):
            return self.student.profile.contest_code
        return "—"


class AppSetting(models.Model):
    """教师可在线调整的运行期设置（覆盖 yaml 中的同名项）。"""

    singleton_id = models.PositiveSmallIntegerField("单例", default=1, unique=True, editable=False)
    submission_open = models.DateTimeField("开放提交", null=True, blank=True)
    submission_close = models.DateTimeField("截止提交", null=True, blank=True)
    review_open = models.DateTimeField("开放互评", null=True, blank=True)
    review_close = models.DateTimeField("截止互评", null=True, blank=True)
    leaderboard_public = models.BooleanField("公开排行榜", default=True)
    weight_objective = models.FloatField("去色客观分权重", default=0.4)
    weight_subjective = models.FloatField("去色主观分权重", default=0.6)
    announcement = models.TextField("公告", blank=True)
    reviews_allocated_at = models.DateTimeField("互评分配时间", null=True, blank=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "运行设置"
        verbose_name_plural = "运行设置"

    def save(self, *args, **kwargs):
        self.singleton_id = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls) -> "AppSetting":
        obj, _ = cls.objects.get_or_create(singleton_id=1)
        return obj

    def __str__(self) -> str:
        return "运行设置"


class ReviewAssignment(models.Model):
    """一条互评任务：某位评价者对同一原图的两张匿名结果做比较。"""

    reviewer = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="review_assignments", verbose_name="评价者"
    )
    task = models.CharField("任务", max_length=32)
    image_key = models.CharField("图像编号", max_length=16)
    left = models.ForeignKey(
        Submission, on_delete=models.CASCADE, related_name="assigned_as_left", verbose_name="左侧"
    )
    right = models.ForeignKey(
        Submission, on_delete=models.CASCADE, related_name="assigned_as_right", verbose_name="右侧"
    )
    position = models.PositiveSmallIntegerField("顺序", default=0)
    created_at = models.DateTimeField("分配时间", auto_now_add=True)

    class Meta:
        verbose_name = "互评任务"
        verbose_name_plural = "互评任务"
        ordering = ["reviewer", "position", "id"]
        indexes = [models.Index(fields=["reviewer", "task", "position"])]

    def __str__(self) -> str:
        return f"{self.reviewer.username} {self.image_key} #{self.left_id} vs #{self.right_id}"


class Comparison(models.Model):
    """一次已完成的投票。"""

    CHOICES = [
        ("left", "左侧更好"),
        ("right", "右侧更好"),
        ("tie", "两者相近"),
        ("both_bad", "两者均存在明显问题"),
    ]

    assignment = models.OneToOneField(
        ReviewAssignment, on_delete=models.CASCADE, related_name="comparison",
        verbose_name="互评任务",
    )
    choice = models.CharField("选择", max_length=12, choices=CHOICES)
    duration_ms = models.PositiveIntegerField("用时(ms)", default=0)
    created_at = models.DateTimeField("投票时间", auto_now_add=True)

    class Meta:
        verbose_name = "互评记录"
        verbose_name_plural = "互评记录"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"#{self.assignment_id} {self.get_choice_display()}"

    @property
    def winner_id(self) -> Optional[int]:
        if self.choice == "left":
            return self.assignment.left_id
        if self.choice == "right":
            return self.assignment.right_id
        return None


class SubjectiveRanking(models.Model):
    """互评排名结果快照，便于导出与课堂讨论。"""

    student = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="subjective_rankings",
        verbose_name="学生",
    )
    task = models.CharField("任务", max_length=32)
    score = models.FloatField("主观得分(0-100)", default=0.0)
    per_image = models.JSONField("分图预测胜率", default=dict, blank=True)
    comparisons = models.PositiveIntegerField("参与比较次数", default=0)
    rank = models.PositiveIntegerField("名次", default=0)
    computed_at = models.DateTimeField("计算时间", auto_now=True)

    class Meta:
        verbose_name = "主观排名"
        verbose_name_plural = "主观排名"
        ordering = ["rank"]
        constraints = [
            models.UniqueConstraint(fields=["student", "task"], name="uniq_subjective_per_student")
        ]

    def __str__(self) -> str:
        return f"{self.student_id} {self.score:.1f} #{self.rank}"
