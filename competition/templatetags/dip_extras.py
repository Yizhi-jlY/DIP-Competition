"""模板过滤器与站点级标签。"""
from __future__ import annotations

from django import template

register = template.Library()


@register.filter
def get_item(mapping, key):
    if mapping is None:
        return None
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def fmt(value, digits=4):
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.{int(digits)}f}"
    except (TypeError, ValueError):
        return str(value)


@register.filter
def pct(value, digits=1):
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.{int(digits)}f}%"
    except (TypeError, ValueError):
        return str(value)


@register.filter
def metric_of(submission, key):
    if not submission:
        return None
    try:
        return (submission.metrics or {}).get(key)
    except AttributeError:
        return None


@register.filter
def medal(rank):
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, str(rank) if rank else "—")
