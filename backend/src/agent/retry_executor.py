"""统一重试执行器。

将同步路径 (`_execute_tool_call_with_retry_sync`) 和异步路径
(`_yield_tool_call_execution`) 中几乎完全复制的重试循环抽取为统一实现。

核心能力：
- 双正则错误分类（不可重试优先 > 可重试 > 保守兜底）
- 指数退避 + 随机抖动延迟计算
- 结构化日志记录（含 agent_name + error_type 审计字段）
- 同步/异步统一接口，仅 sleep 方式不同

使用方式::

    executor = RetryExecutor(
        max_retries=2,
        base_delay=1.0,
        max_delay=15.0,
        backoff=2.0,
        jitter=0.2,
        agent_name="myclaw",
    )

    # 同步
    result = executor.execute_sync(
        execute_fn=lambda args: tool.run(args),
        tool_name="read_file",
        tool_call_id="tc_001",
        arguments={"path": "src/main.py"},
        session_id="sess_abc",
    )

    # 异步
    result = await executor.execute_async(
        execute_fn=lambda args: tool.run(args),
        tool_name="read_file",
        tool_call_id="tc_001",
        arguments={"path": "src/main.py"},
        session_id="sess_abc",
    )
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..logging.tool_logger import ToolCallLogger


# ── 错误分类正则模式 ──────────────────────────────────────────────

# 可重试的错误特征（正则匹配工具返回的错误文本）
_RETRYABLE_ERROR_PATTERNS: List[re.Pattern] = [
    # 网络 / HTTP
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"too\s+many\s+requests", re.IGNORECASE),
    re.compile(r"429|503|502|504", re.IGNORECASE),
    re.compile(r"connection\s+(error|refused|reset|timed?\s*out)", re.IGNORECASE),
    re.compile(r"network\s+(error|unreachable)", re.IGNORECASE),
    re.compile(r"timeout|timed?\s*out|读取超时|连接超时|请求超时", re.IGNORECASE),
    re.compile(r"temporar(?:y|ily)\s+(?:unavailable|down)", re.IGNORECASE),
    re.compile(r"service\s+unavailable", re.IGNORECASE),
    # MCP
    re.compile(r"mcp\s+(?:server\s+)?(?:disconnect|connection|transport)", re.IGNORECASE),
    re.compile(r"mcp\s+error", re.IGNORECASE),
    re.compile(r"session\s+(?:expired|closed|disconnected)", re.IGNORECASE),
    # 通用临时性错误
    re.compile(r"retry|重试", re.IGNORECASE),
    re.compile(r"try\s+again\s+later", re.IGNORECASE),
    re.compile(r"internal\s+server\s+error", re.IGNORECASE),
    re.compile(r"bad\s+gateway", re.IGNORECASE),
]

# 明确不可重试的错误特征（优先级高于可重试匹配）
_NON_RETRYABLE_ERROR_PATTERNS: List[re.Pattern] = [
    re.compile(r"文件(?:不)?存在|file\s+not\s+found|no\s+such\s+file", re.IGNORECASE),
    re.compile(r"权限|permission\s+denied|access\s+denied", re.IGNORECASE),
    re.compile(r"参数.*(?:格式|错误|无效)|invalid\s+(?:argument|parameter|input)", re.IGNORECASE),
    re.compile(r"未找到工具|tool\s+not\s+found", re.IGNORECASE),
    re.compile(r"json\s*(?:解析|decode|格式)", re.IGNORECASE),
    re.compile(r"验证失败|validat(?:e|ion)\s+(?:failed|error)", re.IGNORECASE),
    re.compile(r"not\s+implemented|unsupported", re.IGNORECASE),
    re.compile(r"quota\s+exceeded|insufficient_quota|billing", re.IGNORECASE),
]


# ── 错误类型标签 ──────────────────────────────────────────────────

# 不可重试错误 → error_type 标签映射
_NON_RETRYABLE_TYPE_MAP: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"file\s+not\s+found|no\s+such\s+file|文件", re.IGNORECASE), "file_not_found"),
    (re.compile(r"permission|权限|access", re.IGNORECASE), "permission_denied"),
    (re.compile(r"invalid|参数|argument|parameter", re.IGNORECASE), "invalid_argument"),
    (re.compile(r"tool\s+not\s+found|未找到工具", re.IGNORECASE), "tool_not_found"),
    (re.compile(r"json", re.IGNORECASE), "json_decode"),
    (re.compile(r"validat", re.IGNORECASE), "validation_failed"),
    (re.compile(r"not\s+implemented|unsupported", re.IGNORECASE), "not_implemented"),
    (re.compile(r"quota|billing", re.IGNORECASE), "quota_exceeded"),
]

# 可重试错误 → error_type 标签映射
_RETRYABLE_TYPE_MAP: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"rate.?limit|too\s+many|429", re.IGNORECASE), "rate_limit"),
    (re.compile(r"503|502|504|bad\s+gateway|service\s+unavailable", re.IGNORECASE), "server_error"),
    (re.compile(r"connection|network|refused|reset", re.IGNORECASE), "network_error"),
    (re.compile(r"timeout|timed?\s*out|超时", re.IGNORECASE), "timeout"),
    (re.compile(r"mcp", re.IGNORECASE), "mcp_error"),
    (re.compile(r"session", re.IGNORECASE), "session_expired"),
    (re.compile(r"retry|重试|try\s+again", re.IGNORECASE), "transient"),
]


# ── 数据结构 ──────────────────────────────────────────────────────

@dataclass
class RetryResult:
    """重试执行结果。"""

    result: str               # 最终结果文本
    retry_count: int          # 总重试次数
    duration_ms: float        # 累计耗时（毫秒）
    status: str               # "done" | "error"
    error_type: Optional[str] = None  # 错误类型标签（如 "timeout", "rate_limit"）


# ── 分类函数 ──────────────────────────────────────────────────────

def classify_error(error_text: str) -> Tuple[bool, str, Optional[str]]:
    """判断工具返回的错误是否可重试，返回 3 元组。

    Args:
        error_text: 工具返回的错误文本（通常以 ❌ 开头）

    Returns:
        (is_retryable, reason, error_type):
        - is_retryable: 是否可重试
        - reason: 原因简述（用于日志）
        - error_type: 错误类型标签（用于审计），无错误时为 None
    """
    if not error_text:
        return False, "empty result", None

    # 非错误结果不重试
    if not error_text.startswith("❌"):
        return False, "not an error", None

    # 1) 先匹配不可重试特征（优先级更高）
    for pattern in _NON_RETRYABLE_ERROR_PATTERNS:
        if pattern.search(error_text):
            error_type = _match_type_label(error_text, _NON_RETRYABLE_TYPE_MAP, "non_retryable")
            return False, f"non-retryable: {pattern.pattern}", error_type

    # 2) 再匹配可重试特征
    for pattern in _RETRYABLE_ERROR_PATTERNS:
        if pattern.search(error_text):
            error_type = _match_type_label(error_text, _RETRYABLE_TYPE_MAP, "retryable")
            return True, f"retryable: {pattern.pattern}", error_type

    # 3) 未命中任何模式 → 保守策略：不重试
    return False, "unknown error (conservative)", "unknown"


def _match_type_label(
    error_text: str,
    type_map: List[Tuple[re.Pattern, str]],
    default: str,
) -> str:
    """从错误文本中匹配类型标签。"""
    for pattern, label in type_map:
        if pattern.search(error_text):
            return label
    return default


def compute_retry_delay(
    attempt: int,
    base_delay: float,
    max_delay: float,
    backoff: float,
    jitter: float,
) -> float:
    """计算第 N 次重试的等待延迟（指数退避 + 随机抖动）。

    delay = min(base_delay * backoff^(attempt-1), max_delay) * (1 ± jitter)
    """
    delay = min(base_delay * (backoff ** (attempt - 1)), max_delay)
    if jitter > 0:
        delay *= 1.0 + random.uniform(-jitter, jitter)
    return max(delay, 0.05)


# ── 重试执行器 ────────────────────────────────────────────────────

class RetryExecutor:
    """统一重试执行器 — 同步/异步共享错误分类和延迟计算。"""

    def __init__(
        self,
        max_retries: int = 2,
        base_delay: float = 1.0,
        max_delay: float = 15.0,
        backoff: float = 2.0,
        jitter: float = 0.2,
        agent_name: str = "agent",
    ):
        self.max_retries = max(max_retries, 0)
        self.base_delay = max(base_delay, 0.1)
        self.max_delay = max(max_delay, 1.0)
        self.backoff = max(backoff, 1.0)
        self.jitter = max(min(jitter, 0.5), 0.0)
        self.agent_name = agent_name

    @property
    def max_attempts(self) -> int:
        return 1 + self.max_retries

    def execute_sync(
        self,
        execute_fn: Callable[[Dict[str, Any]], str],
        tool_name: str,
        tool_call_id: str,
        arguments: Dict[str, Any],
        session_id: Optional[str] = None,
    ) -> RetryResult:
        """同步执行工具调用（含智能重试）。

        Args:
            execute_fn: 执行函数，接收 arguments 字典，返回结果字符串
            tool_name: 工具名称
            tool_call_id: 工具调用 ID
            arguments: 调用参数
            session_id: 会话 ID（用于日志）

        Returns:
            RetryResult: 重试执行结果
        """
        exec_result = ""
        duration_ms = 0.0
        retry_count = 0
        error_type: Optional[str] = None

        for attempt in range(1, self.max_attempts + 1):
            t_start = time.perf_counter()
            exec_result = execute_fn(arguments)
            duration_ms += (time.perf_counter() - t_start) * 1000

            is_error = exec_result.startswith("❌")
            if not is_error:
                break

            is_retryable, reason, error_type = classify_error(exec_result)

            if is_retryable and attempt < self.max_attempts:
                retry_count += 1
                delay = compute_retry_delay(
                    attempt=retry_count,
                    base_delay=self.base_delay,
                    max_delay=self.max_delay,
                    backoff=self.backoff,
                    jitter=self.jitter,
                )
                result_preview = exec_result[:120] + "..." if len(exec_result) > 120 else exec_result
                print(
                    f"🔄 工具 {tool_name} 第 {attempt}/{self.max_attempts} 次失败 ({reason})，"
                    f"{delay:.1f}s 后重试… → {result_preview}"
                )

                ToolCallLogger.log(
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    args=arguments,
                    result=exec_result,
                    session_id=session_id,
                    status="retry",
                    duration_ms=duration_ms,
                    retry_attempt=retry_count,
                    agent_name=self.agent_name,
                    error_type=error_type,
                )

                time.sleep(delay)
                continue

            # 不再重试
            break

        # 最终结果
        final_status = "error" if exec_result.startswith("❌") else "done"

        ToolCallLogger.log(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            args=arguments,
            result=exec_result,
            session_id=session_id,
            status=final_status,
            duration_ms=duration_ms,
            retry_count=retry_count,
            agent_name=self.agent_name,
            error_type=error_type if final_status == "error" else None,
        )

        return RetryResult(
            result=exec_result,
            retry_count=retry_count,
            duration_ms=duration_ms,
            status=final_status,
            error_type=error_type if final_status == "error" else None,
        )

    async def execute_async(
        self,
        execute_fn: Callable[[Dict[str, Any]], str],
        tool_name: str,
        tool_call_id: str,
        arguments: Dict[str, Any],
        session_id: Optional[str] = None,
    ) -> RetryResult:
        """异步执行工具调用（含智能重试）。

        与 execute_sync 逻辑完全一致，仅将 time.sleep 替换为 await asyncio.sleep。

        Args:
            execute_fn: 执行函数，接收 arguments 字典，返回结果字符串
            tool_name: 工具名称
            tool_call_id: 工具调用 ID
            arguments: 调用参数
            session_id: 会话 ID（用于日志）

        Returns:
            RetryResult: 重试执行结果
        """
        exec_result = ""
        duration_ms = 0.0
        retry_count = 0
        error_type: Optional[str] = None

        for attempt in range(1, self.max_attempts + 1):
            t_start = time.perf_counter()
            # execute_fn 可能是同步的（如 tool.run），用 to_thread 避免阻塞事件循环
            if asyncio.iscoroutinefunction(execute_fn):
                exec_result = await execute_fn(arguments)
            else:
                exec_result = await asyncio.to_thread(execute_fn, arguments)
            duration_ms += (time.perf_counter() - t_start) * 1000

            is_error = exec_result.startswith("❌")
            if not is_error:
                break

            is_retryable, reason, error_type = classify_error(exec_result)

            if is_retryable and attempt < self.max_attempts:
                retry_count += 1
                delay = compute_retry_delay(
                    attempt=retry_count,
                    base_delay=self.base_delay,
                    max_delay=self.max_delay,
                    backoff=self.backoff,
                    jitter=self.jitter,
                )
                result_preview = exec_result[:120] + "..." if len(exec_result) > 120 else exec_result
                print(
                    f"🔄 工具 {tool_name} 第 {attempt}/{self.max_attempts} 次失败 ({reason})，"
                    f"{delay:.1f}s 后重试… → {result_preview}"
                )

                ToolCallLogger.log(
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    args=arguments,
                    result=exec_result,
                    session_id=session_id,
                    status="retry",
                    duration_ms=duration_ms,
                    retry_attempt=retry_count,
                    agent_name=self.agent_name,
                    error_type=error_type,
                )

                await asyncio.sleep(delay)
                continue

            # 不再重试
            break

        # 最终结果
        final_status = "error" if exec_result.startswith("❌") else "done"

        ToolCallLogger.log(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            args=arguments,
            result=exec_result,
            session_id=session_id,
            status=final_status,
            duration_ms=duration_ms,
            retry_count=retry_count,
            agent_name=self.agent_name,
            error_type=error_type if final_status == "error" else None,
        )

        return RetryResult(
            result=exec_result,
            retry_count=retry_count,
            duration_ms=duration_ms,
            status=final_status,
            error_type=error_type if final_status == "error" else None,
        )
