"""学生端与教师端页面。

原来的单文件 ``views.py`` 按「谁在用」拆成 8 个模块，``urls.py`` 与外部 import 的写法
（``from competition import views`` / ``from competition.views import X``）都保持不变：

=================  ========================================  ==============================
模块               内容                                      主要使用者
=================  ========================================  ==============================
``auth``           登录、登出、改密、自助注册                全体
``student``        首页、任务页、确认最终提交、我的提交      学生
``leaderboard``    客观榜 / 主观榜 / 综合主观榜               全体（可配置是否公开）
``review``         互评出题与投票                            学生
``files``          输入图、结果图、互评图、输入包下载        全体（带权限校验）
``staff``          教师总览、分配互评、重拟合、设置、导出    教师
``base``           渲染、文件响应、缩略图缓存、榜单可见性    内部复用
``system``         健康检查                                  运维
=================  ========================================  ==============================
"""
from __future__ import annotations

from competition.views.auth import (
    CompetitionLoginView,
    CompetitionLogoutView,
    CompetitionPasswordChangeView,
    register_view,
)
from competition.views.files import inputs_zip, public_image, review_image, submission_image
from competition.views.leaderboard import leaderboard_view
from competition.views.review import review_view, review_vote
from competition.views.staff import (
    export_scores,
    export_submissions,
    staff_allocate,
    staff_dashboard,
    staff_gallery,
    staff_refresh_subjective,
    staff_settings,
)
from competition.views.student import confirm_final_view, home, my_submissions, task_detail
from competition.views.system import healthz

__all__ = [
    # auth
    "CompetitionLoginView",
    "CompetitionLogoutView",
    "CompetitionPasswordChangeView",
    "register_view",
    # student
    "home",
    "task_detail",
    "confirm_final_view",
    "my_submissions",
    # leaderboard
    "leaderboard_view",
    # review
    "review_view",
    "review_vote",
    # files
    "public_image",
    "submission_image",
    "review_image",
    "inputs_zip",
    # staff
    "staff_dashboard",
    "staff_gallery",
    "staff_allocate",
    "staff_settings",
    "staff_refresh_subjective",
    "export_scores",
    "export_submissions",
    # system
    "healthz",
]
