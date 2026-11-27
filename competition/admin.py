"""教师管理后台。"""
from __future__ import annotations

from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django.db.models import Count
from django.utils import timezone
from django.utils.html import format_html

from competition.config import load_config
from competition.models import (
    AppSetting,
    Comparison,
    ObjectiveRanking,
    Profile,
    ReviewAssignment,
    Submission,
    SubjectiveRanking,
)
from competition.services import leaderboard
from competition.services.scoring import score_submission

admin.site.site_header = "数字图像处理课程竞赛平台 · 教师后台"
admin.site.site_title = "DIP 竞赛平台"
admin.site.index_title = "比赛管理"


class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False
    extra = 0
    verbose_name_plural = "学生档案"


class UserAdmin(BaseUserAdmin):
    inlines = [ProfileInline]
    list_display = ("username", "profile_code", "profile_name", "is_staff", "is_active", "last_login")
    search_fields = ("username", "profile__student_id", "profile__real_name", "profile__contest_code")

    @admin.display(description="参赛编号")
    def profile_code(self, obj):
        profile = getattr(obj, "profile", None)
        return profile.contest_code if profile else "—"

    @admin.display(description="姓名")
    def profile_name(self, obj):
        profile = getattr(obj, "profile", None)
        return profile.real_name if profile else "—"


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("contest_code", "student_id", "real_name", "user", "must_change_password")
    search_fields = ("contest_code", "student_id", "real_name")
    list_filter = ("must_change_password",)
    ordering = ("contest_code",)


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = (
        "id", "task", "image_key", "owner", "attempt", "original_name",
        "rank_display", "is_final", "finalized_at", "is_baseline", "created_at",
    )
    list_filter = ("task", "image_key", "is_final", "is_baseline", "baseline_method")
    search_fields = (
        "student__username", "student__profile__student_id",
        "student__profile__contest_code", "original_name",
    )
    readonly_fields = (
        "metrics", "rank_value", "scored_at", "score_error", "created_at",
        "finalized_at", "width", "height", "channels", "size_bytes", "preview",
    )
    autocomplete_fields = ("student",)
    actions = ("rescore", "mark_final", "unmark_final")

    @admin.display(description="参赛者")
    def owner(self, obj):
        return obj.owner_code

    @admin.display(description="排名指标")
    def rank_display(self, obj):
        return "—" if obj.rank_value is None else f"{obj.rank_value:.4f}"

    @admin.display(description="预览")
    def preview(self, obj):
        if not obj or not obj.pk:
            return "—"
        return format_html(
            '<img src="{}" style="max-height:240px;border:1px solid #ddd" />',
            f"/files/submission/{obj.pk}.png",
        )

    @admin.action(description="重新计算所选提交的指标")
    def rescore(self, request, queryset):
        tasks = load_config().task_map()
        ok, fail = 0, 0
        for sub in queryset:
            task = tasks.get(sub.task)
            if not task:
                fail += 1
                continue
            metrics = score_submission(sub, task)
            if metrics:
                ok += 1
            else:
                fail += 1
        leaderboard.refresh_all_objective()
        self.message_user(request, f"重新评分完成：成功 {ok}，失败 {fail}。", messages.INFO)

    @admin.action(description="设为最终提交（同图其他提交自动取消）")
    def mark_final(self, request, queryset):
        for sub in queryset:
            if not sub.student_id:
                continue
            Submission.objects.filter(
                student_id=sub.student_id, task=sub.task, image_key=sub.image_key, is_final=True
            ).exclude(pk=sub.pk).update(is_final=False)
            Submission.objects.filter(pk=sub.pk).update(
                is_final=True, finalized_at=timezone.now()
            )
        leaderboard.refresh_all_objective()
        self.message_user(request, "已更新最终提交标记。", messages.INFO)

    @admin.action(description="取消最终提交标记")
    def unmark_final(self, request, queryset):
        updated = queryset.update(is_final=False, finalized_at=None)
        leaderboard.refresh_all_objective()
        self.message_user(request, f"已取消 {updated} 条最终提交标记。", messages.INFO)


@admin.register(ReviewAssignment)
class ReviewAssignmentAdmin(admin.ModelAdmin):
    list_display = ("id", "reviewer", "image_key", "left", "right", "position", "voted")
    list_filter = ("task", "image_key")
    search_fields = ("reviewer__username", "reviewer__profile__contest_code")
    autocomplete_fields = ("reviewer", "left", "right")

    @admin.display(boolean=True, description="已投票")
    def voted(self, obj):
        return hasattr(obj, "comparison")


@admin.register(Comparison)
class ComparisonAdmin(admin.ModelAdmin):
    list_display = ("id", "assignment", "choice", "duration_ms", "created_at")
    list_filter = ("choice",)
    search_fields = ("assignment__reviewer__username",)


@admin.register(SubjectiveRanking)
class SubjectiveRankingAdmin(admin.ModelAdmin):
    list_display = ("rank", "student", "task", "score", "comparisons", "computed_at")
    list_filter = ("task",)
    ordering = ("rank",)


@admin.register(ObjectiveRanking)
class ObjectiveRankingAdmin(admin.ModelAdmin):
    """按指标算出的客观排名（排行榜页面实时计算，这里是最近一次快照）。"""

    list_display = (
        "rank", "code", "name", "task", "metric", "score_display", "tiebreak_display",
        "computed_at",
    )
    list_filter = ("task", "metric")
    search_fields = (
        "student__username", "student__profile__student_id", "student__profile__contest_code",
    )
    ordering = ("task", "rank")
    list_per_page = 50
    actions = ("recompute",)
    readonly_fields = (
        "student", "task", "metric", "score", "tiebreak", "per_image", "rank", "computed_at",
    )

    @admin.display(description="参赛编号")
    def code(self, obj):
        profile = getattr(obj.student, "profile", None)
        return profile.contest_code if profile else obj.student_id

    @admin.display(description="姓名")
    def name(self, obj):
        profile = getattr(obj.student, "profile", None)
        return profile.real_name if profile else "—"

    @admin.display(description="排名指标均值", ordering="score")
    def score_display(self, obj):
        return "—" if obj.score is None else f"{obj.score:.4f}"

    @admin.display(description="并列指标均值", ordering="tiebreak")
    def tiebreak_display(self, obj):
        return "—" if obj.tiebreak is None else f"{obj.tiebreak:.4f}"

    @admin.display(description="分图指标")
    def per_image_display(self, obj):
        task = load_config().task_map().get(obj.task)
        keys = [img.key for img in task.images] if task else sorted(obj.per_image)
        parts = []
        for key in keys:
            values = obj.per_image.get(key) or {}
            shown = " / ".join(
                f"{k.upper()} {v:.4f}" if isinstance(v, (int, float)) else f"{k} {v}"
                for k, v in values.items()
            )
            parts.append(f"{key}: {shown or '—'}")
        return "；".join(parts)

    @admin.action(description="按当前指标重算客观排名（全部任务）")
    def recompute(self, request, queryset):
        counts = leaderboard.refresh_all_objective()
        detail = "、".join(f"{k} {v} 条" for k, v in counts.items()) or "无任务"
        self.message_user(request, f"客观排名已重算：{detail}。", messages.INFO)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AppSetting)
class AppSettingAdmin(admin.ModelAdmin):
    list_display = (
        "id", "submission_open", "submission_close", "review_open", "review_close",
        "leaderboard_public", "reviews_allocated_at",
    )
    fieldsets = (
        ("赛程（留空则使用 config/competition.yaml）", {
            "fields": ("submission_open", "submission_close", "review_open", "review_close")
        }),
        ("排行榜与计分", {
            "fields": ("leaderboard_public", "weight_objective", "weight_subjective")
        }),
        ("匿名互评份数（按任务覆盖 yaml）", {"fields": ("review_counts",)}),
        ("自助注册", {"fields": ("registration_open",)}),
        ("公告", {"fields": ("announcement",)}),
        ("系统", {"fields": ("reviews_allocated_at", "updated_at")}),
    )
    readonly_fields = ("reviews_allocated_at", "updated_at")

    def has_add_permission(self, request):
        return not AppSetting.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.unregister(User)
admin.site.register(User, UserAdmin)

