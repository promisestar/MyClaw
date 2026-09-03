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
    # tool_finish.result 中**不得**出现的子串（反向断言）。
    # 用于验证「某类行为绝不会发生」，例如副作用工具永不被委托给子代理。
    # 空列表表示不检查。
    forbid_result_contains_any: List[str] = field(default_factory=list)
    error_contains_any: List[str] = field(default_factory=list)
    # 最终 done.content 最短长度
    min_done_chars: int = 0
    # 若为 True：出现 cancelled 视为 hard_pass 条件之一
    expect_cancelled: bool = False


@dataclass
class JudgeConfig:
    """L2.2 LLM-as-judge 的场景级配置。

    `enabled=false` 的场景完全跳过 judge。门控类场景（如 ask_readonly_gate）
    规则判据已充分，开 judge 只会引入噪声，应显式关闭。
    """

    enabled: bool = False
    # 维度权重；缺省时用 DEFAULT_JUDGE_WEIGHTS，未列出的维度权重为 0
    weights: Dict[str, float] = field(default_factory=dict)
    # 场景专属 rubric；为空则只用通用 rubric
    rubric: str = ""


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
    # 多模态附件列表（每个元素为 Attachment：stored_path/filename/mime_type/kind/size）
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    expect: Expectation = field(default_factory=Expectation)
    tags: List[str] = field(default_factory=list)
    # L2.2 LLM-as-judge 配置；None 表示未声明（视为关闭）
    judge: Optional[JudgeConfig] = None
    # 多模态附件列表（透传给 /api/chat/send/stream 的 attachments）
    # 每项结构对齐 src.api.chat.Attachment：
    #   {stored_path, filename, mime_type, kind, size}
    # stored_path 是相对工作区根的 POSIX 路径
    attachments: List[Dict[str, Any]] = field(default_factory=list)


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
    # L2.1 轨迹级确定性指标（dict 形式，来自 TraceMetrics.to_dict()）
    trace_metrics: Optional[Dict[str, Any]] = None
    # L2.2 LLM-as-judge 结果（dict 形式，来自 JudgeResult.to_dict()）；未启用为 None
    judge: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "hard_pass": self.hard_pass,
            "soft_score": self.soft_score,
            "violations": self.violations,
            "metrics": self.metrics,
            "session_id": self.session_id,
        }
        # 仅在有值时输出，保持旧报告结构向后兼容
        if self.trace_metrics is not None:
            out["trace_metrics"] = self.trace_metrics
        if self.judge is not None:
            out["judge"] = self.judge
        return out
