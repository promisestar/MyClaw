"""评测公共类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Expectation:
    """场景硬断言与软指标配置。"""

    # 必须出现的 SSE 事件名
    require_events: List[str] = field(default_factory=list)
    # 禁止出现成功 finish 的工具名（大小写敏感，用真实注册名）
    forbid_successful_tools: List[str] = field(default_factory=list)
    # 至少调用过一次的工具名（任一即可时用 require_any_tools）
    require_tools: List[str] = field(default_factory=list)
    require_any_tools: List[str] = field(default_factory=list)
    # plan_generated.plan 长度区间
    plan_min_items: Optional[int] = None
    plan_max_items: Optional[int] = None
    # tool_finish.result / error 文本应包含的子串
    result_contains_any: List[str] = field(default_factory=list)
    error_contains_any: List[str] = field(default_factory=list)
    # 最终 done.content 最短长度
    min_done_chars: int = 0
    # 若为 True：出现 cancelled 视为 hard_pass 条件之一
    expect_cancelled: bool = False


@dataclass
class Scenario:
    """L2 场景定义。"""

    id: str
    title: str
    message: str
    mode: str = "craft"
    plan_confirmed: bool = False
    skill: Optional[str] = None
    session_id: Optional[str] = None
    # 若设置，请求体带 workspace_path（需已授权）
    workspace_path: Optional[str] = None
    # 复用上一场景的 session（由 runner 填）
    reuse_session_from: Optional[str] = None
    timeout_s: float = 180.0
    # 流式开始 N 秒后调用 /api/chat/cancel（用于取消类场景；None 表示不取消）
    cancel_after_s: Optional[float] = None
    expect: Expectation = field(default_factory=Expectation)
    tags: List[str] = field(default_factory=list)


@dataclass
class StreamTrace:
    """一次 stream 调用的完整轨迹。"""

    events: List[Dict[str, Any]] = field(default_factory=list)
    session_id: Optional[str] = None
    chunks: List[str] = field(default_factory=list)
    tool_starts: List[Dict[str, Any]] = field(default_factory=list)
    tool_finishes: List[Dict[str, Any]] = field(default_factory=list)
    plan: Optional[List[Dict[str, Any]]] = None
    done_content: str = ""
    context_usage: Optional[Dict[str, Any]] = None
    errors: List[str] = field(default_factory=list)
    cancelled_reason: Optional[str] = None
    latency_ms: float = 0.0

    @property
    def event_names(self) -> List[str]:
        return [e.get("event", "") for e in self.events]

    @property
    def tools_started(self) -> List[str]:
        return [t.get("tool", "") for t in self.tool_starts]

    @property
    def tools_finished(self) -> List[str]:
        return [t.get("tool", "") for t in self.tool_finishes]


@dataclass
class CaseResult:
    """单场景评测结果。"""

    id: str
    title: str
    hard_pass: bool
    soft_score: float = 0.0
    violations: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    session_id: Optional[str] = None
    raw: Optional[StreamTrace] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "hard_pass": self.hard_pass,
            "soft_score": self.soft_score,
            "violations": self.violations,
            "metrics": self.metrics,
            "session_id": self.session_id,
        }
