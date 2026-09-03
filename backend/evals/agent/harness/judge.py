"""L2.2 LLM-as-judge：基于轨迹的语义质量评分。

与硬断言的关系
--------------
本模块**完全独立**于 `scorers.score_scenario`：

- judge 分数**绝不参与 `hard_pass`**——红线必须确定性，LLM 有随机性。
- judge 结果写入 `CaseResult.judge`，与 `soft_score`（规则分）并存，
  便于对比二者做一致性分析，反过来验证 judge 是否真的有效。
- judge 默认关闭，需 `--judge` 显式启用。

防偏设计
--------
- **锚定偏见**：TraceDigest 只含「用户请求 + rubric + 轨迹事实」，
  绝不包含 violations / hard_pass / expect——judge 若知道"已通过"会系统性抬高评分。
- **自评偏见**：judge 模型由 `EVAL_JUDGE_*` 独立配置，与被测模型同名时告警。
- **长度偏见**：rubric 明示"简洁不啰嗦也是优点"，禁止以回答长度论优劣。
- **随机性**：temperature=0。

健壮性
------
任何失败（缺配置 / 调用异常 / JSON 解析失败）都降级为 `JudgeResult(ok=False)`，
**不影响 hard_pass，不让评测崩溃**。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .trace_metrics import extract_steps, is_error_result
from .types import JudgeConfig, Scenario, StreamTrace

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 压缩阈值
# --------------------------------------------------------------------------

MAX_ARG_CHARS = 300
MAX_RESULT_CHARS = 800
MAX_ANSWER_CHARS = 2000
MAX_TOOL_CALLS = 40  # 超出只保留首尾，防极端轨迹打爆 prompt

# --------------------------------------------------------------------------
# 评分维度
# --------------------------------------------------------------------------

JUDGE_DIMENSIONS: Sequence[str] = (
    "task_completion",
    "tool_efficiency",
    "trajectory_quality",
    "error_recovery",
    "response_quality",
)

DEFAULT_JUDGE_WEIGHTS: Dict[str, float] = {
    "task_completion": 0.35,
    "tool_efficiency": 0.25,
    "trajectory_quality": 0.20,
    "error_recovery": 0.10,
    "response_quality": 0.10,
}

_DIMENSION_LABELS: Dict[str, str] = {
    "task_completion": "任务完成度",
    "tool_efficiency": "工具效率",
    "trajectory_quality": "轨迹质量",
    "error_recovery": "错误恢复",
    "response_quality": "回答质量",
}

# 通用 rubric。场景可在 YAML 的 judge.rubric 中追加专属要求。
GENERAL_RUBRIC = """\
通用评分标准（各维度 1–5 分）：

【任务完成度 task_completion】是否真正达成用户目标，而非"做了动作"。
  5 = 完全达成且有验证；3 = 部分达成；1 = 答非所问或基本没做。

【工具效率 tool_efficiency】有无冗余调用、是否选对工具。
  5 = 调用精炼且工具选择恰当；3 = 有些冗余但不影响结果；
  1 = 大量重复调用，或反复用 shell 命令做专用工具能做的事。
  注意：用 bash/cat/grep 读写文件，而不用 Read/search_content，属于工具选择不当。

【轨迹质量 trajectory_quality】步骤顺序是否合理，是否盲目试错。
  5 = 步骤清晰、有条理；3 = 顺序基本合理；1 = 混乱、明显盲目试错。
  若存在 plan，实际执行与 plan 严重脱节应扣分。

【错误恢复 error_recovery】遇错是否重试或换策略，而非放弃或编造结果。
  5 = 主动恢复并成功；3 = 尝试过但未完全解决；1 = 遇错即弃或隐瞒。
  若全程无错误，给 5 分（不惩罚"没机会犯错"）。

【回答质量 response_quality】最终回答是否准确、完整、无幻觉。
  5 = 准确完整且与轨迹一致；3 = 基本准确但有遗漏或含糊；
  1 = 与轨迹矛盾，或声称做了轨迹中根本没发生的事。

重要：
- 简洁不啰嗦也是优点，**禁止以回答长度论优劣**。
- 只依据上面给出的轨迹事实评分，不要臆测未出现在轨迹中的行为。
- 严重问题（幻觉、隐瞒失败、破坏性操作、答非所问）请写入 red_flags，
  即使整体打分不低也应指出。
"""

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class ToolCallDigest:
    """单次工具调用的压缩摘要。"""

    index: int
    tool: str
    args: str
    result: str
    is_error: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "tool": self.tool,
            "args": self.args,
            "result": self.result,
            "is_error": self.is_error,
        }


@dataclass
class TraceDigest:
    """喂给 judge 的压缩、脱敏轨迹。

    刻意**不含** violations / hard_pass / expect 规则断言内容。
    """

    scenario_id: str = ""
    title: str = ""
    user_request: str = ""
    mode: str = "craft"
    tool_calls: List[ToolCallDigest] = field(default_factory=list)
    steps_used: Optional[int] = None
    max_steps: Optional[int] = None
    plan: Optional[List[str]] = None
    final_answer: str = ""
    errors: List[str] = field(default_factory=list)
    cancelled: bool = False
    latency_ms: float = 0.0

    def render(self) -> str:
        """渲染为 prompt 片段。"""
        lines: List[str] = []
        lines.append(f"场景 id: {self.scenario_id}")
        lines.append(f"任务标题: {self.title}")
        lines.append(f"执行模式: {self.mode}")
        if self.plan:
            lines.append("计划（plan）:")
            for i, item in enumerate(self.plan, 1):
                lines.append(f"  {i}. {item}")
        if self.steps_used is not None and self.max_steps:
            lines.append(f"步数: {self.steps_used}/{self.max_steps}")
        lines.append(f"耗时: {self.latency_ms:.0f} ms")
        lines.append("")
        lines.append("【用户请求】")
        lines.append(self.user_request or "(空)")
        lines.append("")
        n = len(self.tool_calls)
        lines.append(f"【执行轨迹】共 {n} 次工具调用")
        if n == 0:
            lines.append("  （未调用任何工具）")
        for call in self.tool_calls:
            flag = " [失败]" if call.is_error else ""
            lines.append(f"  [{call.index}] {call.tool}{flag}")
            if call.args:
                lines.append(f"      参数: {call.args}")
            lines.append(f"      结果: {call.result}")
        lines.append("")
        lines.append("【最终回答】")
        lines.append(self.final_answer or "(无)")
        if self.errors:
            lines.append("")
            lines.append("【SSE 错误事件】")
            for e in self.errors[:5]:
                lines.append(f"  - {e}")
        if self.cancelled:
            lines.append("")
            lines.append("【注意】该次执行被用户中途取消。")
        return "\n".join(lines)


@dataclass
class JudgeResult:
    """judge 输出。ok=False 表示未成功评分（不写分，只记原因）。"""

    ok: bool = False
    model: str = ""
    scores: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    judge_score: Optional[float] = None  # 1–5 加权总分
    summary: str = ""
    red_flags: List[str] = field(default_factory=list)
    error: Optional[str] = None
    raw_response: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "model": self.model,
            "judge_score": self.judge_score,
            "scores": self.scores,
            "summary": self.summary,
            "red_flags": self.red_flags,
            "error": self.error,
        }


# --------------------------------------------------------------------------
# Digest 构建
# --------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"…[截断 {len(text) - limit} 字符]"


def _render_args(args: Any) -> str:
    if args is None:
        return ""
    if isinstance(args, str):
        return _truncate(args, MAX_ARG_CHARS)
    try:
        return _truncate(json.dumps(args, ensure_ascii=False, default=str), MAX_ARG_CHARS)
    except (TypeError, ValueError):
        return _truncate(str(args), MAX_ARG_CHARS)


def build_digest(scenario: Scenario, trace: StreamTrace) -> TraceDigest:
    """从轨迹构建脱敏摘要。"""
    calls: List[ToolCallDigest] = []
    for i, fin in enumerate(trace.tool_finishes):
        result = str(fin.get("result") or "")
        calls.append(
            ToolCallDigest(
                index=i + 1,
                tool=str(fin.get("tool") or ""),
                args=(
                    _render_args(trace.tool_starts[i].get("args"))
                    if i < len(trace.tool_starts)
                    else ""
                ),
                result=_truncate(result, MAX_RESULT_CHARS),
                is_error=is_error_result(result),
            )
        )

    # 极端长轨迹只保留首尾，避免 prompt 爆炸
    if len(calls) > MAX_TOOL_CALLS:
        head = calls[: MAX_TOOL_CALLS // 2]
        tail = calls[-(MAX_TOOL_CALLS // 2) :]
        omitted = len(calls) - len(head) - len(tail)
        calls = head + [
            ToolCallDigest(
                index=-1,
                tool="…",
                args=f"中间省略 {omitted} 次调用",
                result="",
                is_error=False,
            )
        ] + tail

    plan: Optional[List[str]] = None
    if trace.plan:
        plan = [
            str(it.get("description") or it.get("title") or "")
            for it in trace.plan
            if isinstance(it, dict)
        ]

    # 步数口径与 trace_metrics 保持一致
    steps_used, max_steps = extract_steps(trace)

    return TraceDigest(
        scenario_id=scenario.id,
        title=scenario.title,
        user_request=scenario.message,
        mode=scenario.mode,
        tool_calls=calls,
        steps_used=steps_used,
        max_steps=max_steps,
        plan=plan or None,
        final_answer=_truncate(trace.done_content, MAX_ANSWER_CHARS),
        errors=list(trace.errors),
        cancelled=bool(trace.cancelled_reason),
        latency_ms=trace.latency_ms,
    )


# --------------------------------------------------------------------------
# Judge
# --------------------------------------------------------------------------


def resolve_judge_model() -> tuple[Optional[str], Optional[str], Optional[str]]:
    """解析 judge 模型配置：EVAL_JUDGE_* 优先，回退 LLM_*。"""
    model = os.getenv("EVAL_JUDGE_MODEL_ID") or os.getenv("LLM_MODEL_ID")
    api_key = os.getenv("EVAL_JUDGE_API_KEY") or os.getenv("LLM_API_KEY")
    base_url = os.getenv("EVAL_JUDGE_BASE_URL") or os.getenv("LLM_BASE_URL")
    return model, api_key, base_url


def _check_self_eval_bias(judge_model: Optional[str]) -> None:
    """judge 与被测模型同名 → 自评偏见告警。"""
    if not judge_model:
        return
    target = os.getenv("LLM_MODEL_ID")
    if target and judge_model == target:
        logger.warning(
            "judge 模型与被测模型同名（%s）：存在自评偏见，"
            "建议配置独立的 EVAL_JUDGE_MODEL_ID",
            judge_model,
        )


class TraceJudge:
    """基于 LLM 的轨迹评分器。"""

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.0,
    ) -> None:
        m, k, u = resolve_judge_model()
        self.model = model or m or ""
        self.api_key = api_key or k
        self.base_url = base_url or u
        self.temperature = temperature
        self._llm: Any = None
        self._init_error: Optional[str] = None
        self._ensure_llm()

    def _ensure_llm(self) -> None:
        if self._llm is not None or self._init_error is not None:
            return
        try:
            from hello_agents.core.llm import HelloAgentsLLM
        except ImportError as exc:
            self._init_error = f"hello_agents 不可用: {exc}"
            logger.warning("judge 未启用：%s", self._init_error)
            return
        try:
            self._llm = HelloAgentsLLM(
                model=self.model or None,
                api_key=self.api_key,
                base_url=self.base_url,
                temperature=self.temperature,
            )
            self.model = getattr(self._llm, "model", self.model) or self.model
            _check_self_eval_bias(self.model)
        except Exception as exc:  # noqa: BLE001 — judge 失败不能拖垮评测
            self._init_error = f"LLM 初始化失败: {exc}"
            logger.warning("judge 未启用：%s", self._init_error)

    @property
    def available(self) -> bool:
        return self._llm is not None

    @property
    def disabled_reason(self) -> Optional[str]:
        return self._init_error

    # ---------------- 对外主入口 ----------------

    def judge(
        self, digest: TraceDigest, config: Optional[JudgeConfig] = None
    ) -> JudgeResult:
        """对单条 digest 评分。任何失败都返回 ok=False，不抛异常。"""
        if not self.available:
            return JudgeResult(
                ok=False, model=self.model, error=self._init_error or "judge 不可用"
            )

        cfg = config or JudgeConfig(enabled=True)
        prompt = self._build_prompt(digest, cfg)
        try:
            resp = self._llm.invoke(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是严格的 Agent 轨迹质量评审员。"
                            "只输出符合要求的 JSON，不要任何额外文字。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ]
            )
            raw = getattr(resp, "content", None) or str(resp)
        except Exception as exc:  # noqa: BLE001
            logger.warning("judge 调用失败: %s", exc)
            return JudgeResult(ok=False, model=self.model, error=f"调用失败: {exc}")

        return self._parse(raw, cfg)

    # ---------------- prompt ----------------

    def _build_prompt(self, digest: TraceDigest, cfg: JudgeConfig) -> str:
        weights = cfg.weights or DEFAULT_JUDGE_WEIGHTS
        active = [d for d in JUDGE_DIMENSIONS if float(weights.get(d, 0.0)) > 0]
        if not active:
            active = list(JUDGE_DIMENSIONS)

        score_fields = ",\n".join(
            f'    "{d}": {{"score": <1-5的整数>, "reason": "<不超过50字>"}}'
            for d in active
        )
        weight_note = "、".join(
            f"{_DIMENSION_LABELS[d]}({d}) 权重 {float(weights.get(d, 0.0)):.2f}"
            for d in active
        )

        rubric = GENERAL_RUBRIC
        if cfg.rubric:
            rubric += "\n\n【本场景专属要求】\n" + cfg.rubric.strip()

        return (
            "请依据下面的 rubric，对一次 Agent 执行轨迹打分。\n\n"
            f"{digest.render()}\n\n"
            "========== 评分标准 ==========\n"
            f"{rubric}\n\n"
            f"本次需要评分的维度及权重：{weight_note}。\n\n"
            "========== 输出要求 ==========\n"
            "只输出一个 JSON 对象，不要任何解释文字，不要用 markdown 代码块包裹：\n"
            "{\n"
            '  "scores": {\n'
            f"{score_fields}\n"
            "  },\n"
            '  "summary": "<不超过80字的总体评价>",\n'
            '  "red_flags": ["<严重问题；没有则空数组>"]\n'
            "}\n"
        )

    # ---------------- 解析 ----------------

    def _parse(self, raw: str, cfg: JudgeConfig) -> JudgeResult:
        text = (raw or "").strip()
        fence = _JSON_FENCE.search(text)
        if fence:
            text = fence.group(1).strip()
        # 兜底：截取首个 { 到末个 }
        if not text.startswith("{"):
            i, j = text.find("{"), text.rfind("}")
            if i != -1 and j > i:
                text = text[i : j + 1]

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning("judge JSON 解析失败: %s", exc)
            return JudgeResult(
                ok=False,
                model=self.model,
                error=f"JSON 解析失败: {exc}",
                raw_response=(raw or "")[:500],
            )
        if not isinstance(data, dict):
            return JudgeResult(
                ok=False, model=self.model, error=f"返回不是对象: {type(data).__name__}"
            )

        raw_scores = data.get("scores")
        scores: Dict[str, Dict[str, Any]] = {}
        if isinstance(raw_scores, dict):
            for dim, val in raw_scores.items():
                dim = str(dim)
                if dim not in JUDGE_DIMENSIONS:
                    continue
                if isinstance(val, dict):
                    s = val.get("score")
                    reason = str(val.get("reason") or "")
                else:
                    s, reason = val, ""
                try:
                    s = float(s)
                except (TypeError, ValueError):
                    continue
                # 越界截断而非丢弃：模型偶尔给出 4.5 或 0
                s = max(1.0, min(5.0, s))
                scores[dim] = {"score": s, "reason": reason}

        if not scores:
            return JudgeResult(
                ok=False,
                model=self.model,
                error="未解析出任何有效维度分",
                raw_response=(raw or "")[:500],
            )

        weights = cfg.weights or DEFAULT_JUDGE_WEIGHTS
        total_w = 0.0
        acc = 0.0
        for dim, item in scores.items():
            w = float(weights.get(dim, 0.0))
            if w <= 0:
                continue
            total_w += w
            acc += w * float(item["score"])
        # 权重全为 0（如场景只配了未启用维度）时退化为等权
        judge_score = (acc / total_w) if total_w > 0 else (
            sum(float(i["score"]) for i in scores.values()) / len(scores)
        )

        flags = data.get("red_flags")
        red_flags = (
            [str(x) for x in flags if str(x).strip()]
            if isinstance(flags, list)
            else []
        )

        return JudgeResult(
            ok=True,
            model=self.model,
            scores=scores,
            judge_score=round(float(judge_score), 3),
            summary=str(data.get("summary") or "")[:500],
            red_flags=red_flags,
            raw_response=(raw or "")[:2000],
        )


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """同时支持 JudgeResult 与其 to_dict() 结果，便于报告层复用。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def summarize_judge(results: Sequence[Any]) -> Dict[str, Any]:
    """汇总 judge 结果，写入报告 summary。

    接受 JudgeResult 或其 to_dict() 字典，二者混用亦可。
    """
    scored = [r for r in results if _field(r, "ok") and _field(r, "judge_score") is not None]
    failed = [r for r in results if not _field(r, "ok")]
    models = {m for m in (_field(r, "model") for r in results) if m}
    scores = [float(_field(r, "judge_score")) for r in scored]
    return {
        "model": (sorted(models)[0] if len(models) == 1 else ", ".join(sorted(models))),
        "scored": len(scored),
        "skipped": len(failed),
        "avg_judge_score": (round(sum(scores) / len(scores), 4) if scores else None),
        "min_judge_score": (round(min(scores), 3) if scores else None),
        "max_judge_score": (round(max(scores), 3) if scores else None),
        "red_flags_count": sum(len(_field(r, "red_flags") or []) for r in results),
        "errors": [e for e in (_field(r, "error") for r in failed) if e][:10],
    }
