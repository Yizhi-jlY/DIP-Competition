"""排行榜页面：客观榜、各任务主观榜、综合主观榜。"""
from __future__ import annotations

from typing import Dict

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest

from competition.config import load_config
from competition.services import leaderboard, reviews
from competition.views.base import leaderboard_visible, render_page


@login_required
def leaderboard_view(request: HttpRequest):
    config = load_config()
    allowed = leaderboard_visible(request.user)
    board = request.GET.get("board", "denoise")
    if board == "decolor-objective":  # 旧链接兼容
        board = "decolor"
    slugs = list(config.task_map())
    valid = set(slugs) | {f"{slug}-subjective" for slug in slugs} | {"combined", "total"}
    if board not in valid:
        board = slugs[0] if slugs else "denoise"

    board_tabs = []
    for task in config.tasks:
        board_tabs.append({"slug": task.slug, "label": task.short_name})
        if config.peer_review.for_task(task.slug).enabled:
            board_tabs.append(
                {"slug": f"{task.slug}-subjective", "label": f"{task.short_name} · 主观"}
            )
    subjective_slugs = [t.slug for t in config.tasks if config.peer_review.for_task(t.slug).enabled]
    if len(subjective_slugs) >= 2:
        board_tabs.append({"slug": "combined", "label": "综合 · 主观"})
        board_tabs.append({"slug": "total", "label": "总排名"})

    ctx: Dict[str, object] = {
        "config": config,
        "board": board,
        "allowed": allowed,
        "denoise": config.task("denoise"),
        "decolor": config.task("decolor"),
        "boards": board_tabs,
    }

    if not allowed:
        return render_page(request, "leaderboard.html", ctx)

    if board == "total":
        rows, pending, total_weights, total_mode = leaderboard.total_rows()
        d_task, c_task = config.task("denoise"), config.task("decolor")
        ctx.update(
            {
                "rows": rows,
                "pending_rows": pending,
                "board_type": "total",
                "total_weights": total_weights,
                "total_mode": total_mode,
                "total_mode_label": leaderboard.TOTAL_MODES.get(total_mode, ""),
                "denoise_metric": d_task.metric(d_task.ranking_metric) if d_task else None,
                "decolor_metric": c_task.metric(c_task.ranking_metric) if c_task else None,
            }
        )
    elif board == "combined":
        rows, pending, weights = leaderboard.combined_subjective_rows()
        ctx.update(
            {
                "rows": rows,
                "pending_rows": pending,
                "board_type": "combined",
                "combined_weights": weights,
            }
        )
    elif board.endswith("-subjective"):
        task = config.task(board[: -len("-subjective")])
        if task:
            ctx.update(
                {
                    "rows": leaderboard.subjective_rows(task),
                    "task": task,
                    "board_type": "subjective",
                    "stats": leaderboard.review_stats(task),
                    "review_count": reviews.effective_count(task, config),
                    "ranking_rule": task.ranking_rule(),
                }
            )
    else:
        task = config.task(board)
        if task:
            rows, incomplete, baselines = leaderboard.objective_rows(task)
            ctx.update(
                {
                    "rows": rows,
                    "incomplete": incomplete,
                    "baselines": baselines,
                    "task": task,
                    "board_type": "objective",
                    "primary": task.metric(task.ranking_metric),
                    "secondary": task.metric(task.tiebreak_metric),
                    "ranking_rule": task.ranking_rule(),
                }
            )
            if config.peer_review.for_task(task.slug).enabled:
                ctx["show_subjective"] = True
                ctx["subjective_map"] = {
                    r.student.id: r for r in leaderboard.subjective_rows(task)
                }
            if task.slug == "decolor":
                ctx["weights"] = config.scoring

    return render_page(request, "leaderboard.html", ctx)
