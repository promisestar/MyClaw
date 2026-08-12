"""Skill 写入来源（前台对话 vs Curator/后台）。"""

from __future__ import annotations

from contextvars import ContextVar

_write_origin: ContextVar[str] = ContextVar("skill_write_origin", default="foreground")


def set_write_origin(origin: str) -> None:
    _write_origin.set(origin or "foreground")


def get_write_origin() -> str:
    return _write_origin.get() or "foreground"


def is_background_review() -> bool:
    return get_write_origin() in ("background_review", "curator")
