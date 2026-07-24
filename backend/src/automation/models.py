"""定时任务数据模型 — AutomationTask + AutomationRun。

所有时间戳以 ISO 8601 UTC 存储，避免服务器时区差异。
schedule_type: once | interval | rrule
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional


def _utc_now() -> datetime:
    """获取当前 UTC 时间。"""
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    """获取当前 UTC 时间的 ISO 8601 字符串。"""
    return _utc_now().isoformat()


def _parse_iso(iso_str: str) -> Optional[datetime]:
    """解析 ISO 8601 时间字符串（带时区）。"""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


@dataclass
class AutomationTask:
    """定时任务定义。

    schedule_type / schedule_config 组合：
    - once: {"datetime": "2026-03-20T14:30:00+00:00"}
    - interval: {"interval_seconds": 3600}
    - rrule: {"rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"}
    """

    id: str = ""
    name: str = ""
    prompt: str = ""
    schedule_type: str = "once"  # once | interval | rrule
    schedule_config: Dict[str, Any] = field(default_factory=dict)
    webhook_url: Optional[str] = None
    enabled: bool = True
    workspace_path: str = ""
    created_at: str = ""
    last_run_at: Optional[str] = None
    next_run_at: Optional[str] = None
    run_count: int = 0
    max_runs: Optional[int] = None
    max_duration_minutes: Optional[int] = None

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:8]
        if not self.created_at:
            self.created_at = _utc_now_iso()

    def compute_next_run(self, after: Optional[datetime] = None) -> Optional[datetime]:
        """根据 schedule_type 计算下一次触发时间。

        Args:
            after: 基准时间（默认当前 UTC）

        Returns:
            下一次触发时间（UTC），或 None（已过期/无法计算）
        """
        if after is None:
            after = _utc_now()

        if self.schedule_type == "once":
            dt_str = self.schedule_config.get("datetime", "")
            dt = _parse_iso(dt_str)
            if dt is None:
                return None
            # 一次性任务：如果时间已过，返回 None
            if dt <= after:
                return None
            return dt

        elif self.schedule_type == "interval":
            interval_s = int(self.schedule_config.get("interval_seconds", 0))
            if interval_s <= 0:
                return None
            # 从上次运行时间或创建时间开始计算
            base = _parse_iso(self.last_run_at) or _parse_iso(self.created_at) or after
            next_run = base + timedelta(seconds=interval_s)
            # 如果计算出的时间已过，从当前时间开始
            while next_run <= after:
                next_run = next_run + timedelta(seconds=interval_s)
            return next_run

        elif self.schedule_type == "rrule":
            rrule_str = self.schedule_config.get("rrule", "")
            if not rrule_str:
                return None
            try:
                from dateutil.rrule import rrulestr
                # 确保 rrule 以 DTSTART 开始（dateutil 需要）
                rule = rrulestr(rrule_str, dtstart=after)
                next_occurrence = rule.after(after, inc=False)
                return next_occurrence
            except Exception:
                return None

        return None

    def is_due(self, now: Optional[datetime] = None) -> bool:
        """检查任务是否到期（该执行了）。"""
        if not self.enabled:
            return False
        if self.max_runs is not None and self.run_count >= self.max_runs:
            return False
        if now is None:
            now = _utc_now()
        if self.next_run_at:
            next_run = _parse_iso(self.next_run_at)
            if next_run and next_run <= now:
                return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        """转为字典（用于 JSON 序列化）。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AutomationTask":
        """从字典创建实例。"""
        return cls(**{
            k: v for k, v in data.items()
            if k in cls.__dataclass_fields__  # type: ignore[attr-defined]
        })


@dataclass
class AutomationRun:
    """定时任务执行记录。"""

    run_id: str = ""
    task_id: str = ""
    prompt: str = ""
    result: str = ""
    success: bool = False
    error: Optional[str] = None
    started_at: str = ""
    finished_at: str = ""

    def __post_init__(self):
        if not self.run_id:
            self.run_id = uuid.uuid4().hex[:12]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AutomationRun":
        return cls(**{
            k: v for k, v in data.items()
            if k in cls.__dataclass_fields__  # type: ignore[attr-defined]
        })
