"""路由表。"""
from django.urls import path

from competition import views
from competition.views import (
    CompetitionLoginView,
    CompetitionLogoutView,
    CompetitionPasswordChangeView,
)

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", CompetitionLoginView.as_view(), name="login"),
    path("register/", views.register_view, name="register"),
    path("logout/", CompetitionLogoutView.as_view(), name="logout"),
    path("password/", CompetitionPasswordChangeView.as_view(), name="password_change"),
    path("task/<slug:slug>/", views.task_detail, name="task_detail"),
    path("task/<slug:slug>/confirm/", views.confirm_final_view, name="confirm_final"),
    path("leaderboard/", views.leaderboard_view, name="leaderboard"),
    path("review/", views.review_view, name="review"),
    path("review/<int:assignment_id>/vote/", views.review_vote, name="review_vote"),
    path("submissions/", views.my_submissions, name="my_submissions"),
    path("files/public/<slug:slug>/<str:key>.png", views.public_image, name="public_image"),
    path("files/submission/<int:pk>.png", views.submission_image, name="submission_image"),
    path(
        "files/review/<int:assignment_id>/<str:side>.png",
        views.review_image,
        name="review_image",
    ),
    path("download/<slug:slug>/inputs.zip", views.inputs_zip, name="inputs_zip"),
    path("staff/", views.staff_dashboard, name="staff_dashboard"),
    path("staff/gallery/", views.staff_gallery, name="staff_gallery"),
    path("staff/allocate/", views.staff_allocate, name="staff_allocate"),
    path("staff/settings/", views.staff_settings, name="staff_settings"),
    path("staff/subjective/", views.staff_refresh_subjective, name="staff_refresh_subjective"),
    path("export/scores.csv", views.export_scores, name="export_scores"),
    path("export/submissions.zip", views.export_submissions, name="export_submissions"),
    path("healthz", views.healthz, name="healthz"),
]
