"""增强版 SimpleAgent - 支持流式工具调用"""

import json
import asyncio
import logging
import time
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Set, Tuple
from typing import Optional, List, Dict, Any, AsyncGenerator, TYPE_CHECKING, Union

from hello_agents.agents.simple_agent import SimpleAgent
from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.core.config import Config
from hello_agents.core.message import Message
from hello_agents.core.streaming import StreamEvent, StreamEventType

from ..context import ContextManager
from ..context.context_guard import ContextGuard
from ..core.timeouts import TimeoutConfig

# 导入 HelloClaw 专用 LLM（支持流式工具调用）
from .enhanced_llm import EnhancedHelloAgentsLLM, StreamToolEventType
from .retry_executor import RetryExecutor, RetryResult, classify_error, compute_retry_delay
from .cancel_token import CancellationToken

from ..logging.tool_logger import ToolCallLogger, get_trace_id
from ..logging.llm_usage_logger import (
    LLMUsageLogger,
    empty_usage,
    merge_usage,
    normalize_usage,
)

if TYPE_CHECKING:
    from hello_agents.tools.registry import ToolRegistry

# 错误分类和重试逻辑已抽取到 retry_executor.py，此处不再保留重复的正则模式


def compose_turn_user_content(
    input_text: str,
    turn_context: Optional[str] = None,
) -> str:
    """将本轮 ephemeral 上下文与用户原文合并为发给模型的 user content。

    历史记录应仍使用干净的 ``input_text``，不得把 ``turn_context`` 写入会话。
    若 ``input_text`` 为多模态编码串，把上下文作为前置 text part 再编码，
    以便 ``multimodal_bridge`` 补丁仍能整段解码。

    多模态路径失败时**丢弃上下文、保留原编码串**，避免破坏 ``__MM_V1__`` 前缀。
    """
    ctx = (turn_context or "").strip()
    if not ctx:
        return input_text

    prefix = f"## 本轮上下文\n{ctx}\n\n---\n\n"
    try:
        from .multimodal_bridge import (
            decode_multimodal_content,
            encode_multimodal_content,
            is_encoded_multimodal,
        )

        if is_encoded_multimodal(input_text):
            decoded = decode_multimodal_content(input_text)
            if isinstance(decoded, list):
                return encode_multimodal_content(
                    [{"type": "text", "text": prefix}] + decoded
                )
            # 非预期结构：宁可丢掉上下文，也不破坏编码串
            return input_text
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.warning(
            "compose_turn_user_content: multimodal merge failed, keeping original encoding: %s",
            e,
        )
        return input_text
    return f"{prefix}{input_text}"


class EnhancedSimpleAgent(SimpleAgent):
    """增强版 SimpleAgent，支持流式工具调用

    继承 hello_agents 的 SimpleAgent，增加：
    - 真正的流式工具调用（使用 EnhancedHelloAgentsLLM）
    - 工具调用状态的实时推送

    Note:
        推荐使用 EnhancedHelloAgentsLLM 以获得完整的流式工具调用支持。
        如果使用普通 HelloAgentsLLM，流式工具调用将回退到基类的非流式模式。
    """

    def __init__(
        self,
        name: str,
        llm: Union[HelloAgentsLLM, EnhancedHelloAgentsLLM],
        system_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        tool_registry: Optional['ToolRegistry'] = None,
        enable_tool_calling: bool = True,
        max_tool_iterations: int = 10,
        workspace_root: Optional[str] = None,
        auto_cleanup_temp_files: bool = True,
        max_tool_retries: int = 2,
        tool_retry_base_delay: float = 1.0,
        tool_retry_max_delay: float = 15.0,
        tool_retry_backoff: float = 2.0,
        tool_retry_jitter: float = 0.2,
        max_tools_per_round: int = 5,
        subagent_orchestrator=None,   # SubAgentOrchestrator 引用（可选）
        timeout_config: Optional[TimeoutConfig] = None,
    ):
        """初始化 EnhancedSimpleAgent

        Args:
            name: Agent 名称
            llm: LLM 实例（推荐使用 EnhancedHelloAgentsLLM）
            system_prompt: 系统提示词
            config: 配置对象
            tool_registry: 工具注册表（可选）
            enable_tool_calling: 是否启用工具调用
            max_tool_iterations: 最大工具调用迭代次数
            workspace_root: 工作空间根目录（用于安全清理临时文件）
            auto_cleanup_temp_files: 是否启用临时文件自动清理兜底
            max_tool_retries: 单次工具调用最大重试次数（默认 2）
            tool_retry_base_delay: 重试基础延迟（秒，默认 1.0）
            tool_retry_max_delay: 重试最大延迟上限（秒，默认 15.0）
            tool_retry_backoff: 指数退避因子（默认 2.0）
            tool_retry_jitter: 随机抖动比例（0.2 表示 ±20%，默认 0.2）
            subagent_orchestrator: SubAgentOrchestrator 引用（可选，供子代理使用）
            timeout_config: 模块级超时配置（可选，默认从 env/config.json 加载）
        """
        super().__init__(
            name=name,
            llm=llm,
            system_prompt=system_prompt,
            config=config,
            tool_registry=tool_registry,
            enable_tool_calling=enable_tool_calling,
            max_tool_iterations=max_tool_iterations,
        )

        # 检查是否支持流式工具调用
        self._supports_streaming_tools = isinstance(llm, EnhancedHelloAgentsLLM)
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self.auto_cleanup_temp_files = auto_cleanup_temp_files

        # 工具调用智能重试配置
        self.max_tool_retries = max(max_tool_retries, 0)
        self.tool_retry_base_delay = max(tool_retry_base_delay, 0.1)
        self.tool_retry_max_delay = max(tool_retry_max_delay, 1.0)
        self.tool_retry_backoff = max(tool_retry_backoff, 1.0)
        self.tool_retry_jitter = max(min(tool_retry_jitter, 0.5), 0.0)

        # 每轮工具调用上限 + 去重（防止 LLM 在单个响应中生成过多重复调用）
        self.max_tools_per_round = max(max_tools_per_round, 1)
        self._tools_executed_this_round = 0
        self._tool_call_dedup: Set[str] = set()

        self._subagent_orchestrator = subagent_orchestrator

        # 模块级超时配置
        self._timeout_config = timeout_config or TimeoutConfig()

        # 工具模式过滤器（Ask/Plan 规划期屏蔽副作用工具）
        from .tool_mode_filter import ToolMode, ToolModeFilter
        self._tool_mode_filter: Optional[ToolModeFilter] = None
        if self.tool_registry:
            self._tool_mode_filter = ToolModeFilter(self.tool_registry)

        # 统一重试执行器（同步/异步共享错误分类和延迟计算）
        self._retry_executor = RetryExecutor(
            max_retries=max_tool_retries,
            base_delay=tool_retry_base_delay,
            max_delay=tool_retry_max_delay,
            backoff=tool_retry_backoff,
            jitter=tool_retry_jitter,
            agent_name=name,
        )

        # 上下文守卫 —— 工具执行前预判输出大小，决定 inline/snip/delegate
        self._context_guard = ContextGuard(orchestrator=subagent_orchestrator)

        # 解耦的上下文管理（替代基类 Agent 内嵌的压缩逻辑）
        self.context_manager = ContextManager(
            config=self.config,
            history_manager=self.history_manager,
            token_counter=self.token_counter,
            llm=self.llm,
        )
        self.context_manager.recalculate_history_tokens()

    # ------------------------------------------------------------------ #
    # 工具模式过滤（Ask/Plan 规划期屏蔽副作用工具）
    # ------------------------------------------------------------------ #

    def set_tool_mode(self, mode) -> None:
        """设置工具访问模式。

        Args:
            mode: ToolMode.READ_ONLY（只读）或 ToolMode.FULL（全部）
        """
        if self._tool_mode_filter:
            self._tool_mode_filter.set_mode(mode)

    def _readonly_block_message_if_needed(self, tool_name: str) -> Optional[str]:
        """READ_ONLY 下若工具为副作用工具，返回拒绝文案；否则返回 None。"""
        if not self._tool_mode_filter:
            return None
        from .tool_mode_filter import SIDE_EFFECT_TOOLS, ToolMode, readonly_block_message

        if (
            self._tool_mode_filter.mode == ToolMode.READ_ONLY
            and tool_name in SIDE_EFFECT_TOOLS
        ):
            return readonly_block_message(tool_name)
        return None

    def _build_tool_schemas(self) -> list:
        """构建工具 schema 列表 — 覆盖父类方法，应用模式过滤。

        READ_ONLY 模式下过滤掉副作用工具（write/edit/bash/automation），
        仅暴露只读工具给 LLM。
        """
        if not self.tool_registry:
            return []

        # 优先使用 ToolModeFilter 过滤
        if self._tool_mode_filter:
            return self._tool_mode_filter.get_filtered_schemas()

        # 降兜：无 filter 时调用父类
        return super()._build_tool_schemas()

    def _get_available_tool_names(self) -> list:
        """获取当前模式下可用的工具名列表。"""
        if self._tool_mode_filter:
            return self._tool_mode_filter.get_available_tool_names()
        if self.tool_registry:
            return self.tool_registry.list_tools()
        return []

    @property
    def _history(self) -> List[Message]:
        return self.history_manager.get_history()

    @_history.setter
    def _history(self, value: List[Message]) -> None:
        self.history_manager.clear()
        for msg in value:
            self.history_manager.append(msg)
        self.context_manager.recalculate_history_tokens()

    def _resolve_workspace_file(self, raw_path: str) -> Optional[Path]:
        """将工具参数中的路径解析为工作空间内绝对路径。"""
        if not raw_path:
            return None

        candidate = Path(raw_path)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        elif self.workspace_root:
            resolved = (self.workspace_root / candidate).resolve()
        else:
            resolved = (Path.cwd() / candidate).resolve()

        if self.workspace_root:
            try:
                resolved.relative_to(self.workspace_root)
            except ValueError:
                return None
        return resolved

    def _is_temp_artifact_path(self, file_path: Path) -> bool:
        """判断是否是临时产物路径（避免误删业务文件）。"""
        name = file_path.name.lower()
        if re.match(r"^(tmp_|temp_|extract_)", name):
            return True
        if name.endswith((".tmp", ".temp")):
            return True
        if any(part.lower() in ("tmp", "temp", ".tmp") for part in file_path.parts):
            return True
        return False

    def _maybe_track_temp_file(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        exec_result: str,
        existed_before: bool,
        tracked_files: Set[Path],
    ) -> None:
        """在 Write 成功创建临时文件时加入待清理列表。"""
        if tool_name.lower() != "write":
            return
        if exec_result.startswith("❌"):
            return
        if existed_before:
            return

        raw_path = arguments.get("path")
        if not isinstance(raw_path, str):
            return

        resolved = self._resolve_workspace_file(raw_path)
        if not resolved or not resolved.exists() or not resolved.is_file():
            return
        if self._is_temp_artifact_path(resolved):
            tracked_files.add(resolved)

    def _cleanup_tracked_temp_files(self, tracked_files: Set[Path]) -> Tuple[int, List[str]]:
        """删除本轮跟踪到的临时文件。"""
        if not self.auto_cleanup_temp_files or not tracked_files:
            return 0, []

        deleted_count = 0
        failed: List[str] = []
        for file_path in sorted(tracked_files):
            try:
                if file_path.exists() and file_path.is_file():
                    file_path.unlink()
                    deleted_count += 1
            except Exception as exc:
                failed.append(f"{file_path}: {exc}")
        return deleted_count, failed

    @staticmethod
    def _is_retryable_error(error_text: str) -> Tuple[bool, str, Optional[str]]:
        """判断工具返回的错误是否可重试（委托给 retry_executor.classify_error）。

        Returns:
            (is_retryable, reason, error_type): 3 元组
        """
        return classify_error(error_text)

    @staticmethod
    def _compute_retry_delay(
        attempt: int,
        base_delay: float,
        max_delay: float,
        backoff: float,
        jitter: float,
    ) -> float:
        """计算重试延迟（委托给 retry_executor.compute_retry_delay）。"""
        return compute_retry_delay(attempt, base_delay, max_delay, backoff, jitter)

    def _execute_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """执行工具并返回写入 LLM 上下文的字符串。

        相对基类：成功时若 ``data.content`` 未出现在 ``text`` 中则合并进去，
        避免 Read 等工具把正文只放在 data 导致模型看不到文件内容。
        """
        blocked = self._readonly_block_message_if_needed(tool_name)
        if blocked is not None:
            return blocked

        if not self.tool_registry:
            return "❌ 错误：未配置工具注册表"

        tool = self.tool_registry.get_tool(tool_name)
        if tool:
            try:
                typed_arguments = self._convert_parameter_types(tool_name, arguments)
                response = tool.run_with_timing(typed_arguments)

                from hello_agents.tools.response import ToolStatus

                text = self._tool_response_to_llm_text(response)
                if response.status == ToolStatus.ERROR:
                    error_code = (
                        response.error_info.get("code", "UNKNOWN")
                        if response.error_info
                        else "UNKNOWN"
                    )
                    return f"❌ 错误 [{error_code}]: {text}"
                if response.status == ToolStatus.PARTIAL:
                    return f"⚠️ 部分成功: {text}"
                return text
            except Exception as exc:
                return f"❌ 工具调用失败：{exc}"

        func = self.tool_registry.get_function(tool_name)
        if func:
            try:
                input_text = arguments.get("input", "")
                response = self.tool_registry.execute_tool(tool_name, input_text)
                from hello_agents.tools.response import ToolStatus

                text = self._tool_response_to_llm_text(response)
                if response.status == ToolStatus.ERROR:
                    error_code = (
                        response.error_info.get("code", "UNKNOWN")
                        if response.error_info
                        else "UNKNOWN"
                    )
                    return f"❌ 错误 [{error_code}]: {text}"
                if response.status == ToolStatus.PARTIAL:
                    return f"⚠️ 部分成功: {text}"
                return text
            except Exception as exc:
                return f"❌ 工具调用失败：{exc}"

        return f"❌ 错误：未找到工具 '{tool_name}'"

    @staticmethod
    def _tool_response_to_llm_text(response: Any) -> str:
        """从 ToolResponse 提取给 LLM 的文本，必要时合并 data.content。"""
        text = getattr(response, "text", None) or ""
        data = getattr(response, "data", None) or {}
        content = data.get("content") if isinstance(data, dict) else None
        if isinstance(content, str) and content and content not in text:
            return f"{text}\n\n{content}" if text else content
        return text

    def _execute_tool_call_with_retry_sync(
        self,
        tool_name: str,
        tool_call_id: str,
        arguments: Dict[str, Any],
        step: int = 0,
    ) -> str:
        """同步执行工具调用（含智能重试）。

        委托给 RetryExecutor.execute_sync，共享与异步路径完全一致的重试策略。

        Returns:
            工具执行结果字符串
        """
        result = self._retry_executor.execute_sync(
            execute_fn=lambda args: self._execute_tool_call(tool_name, args),
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=arguments,
            session_id=getattr(self, "_current_session_id", None),
        )

        if result.retry_count:
            if result.status == "done":
                print(f"✅ 工具 {tool_name} 第 {result.retry_count + 1} 次重试成功")
            else:
                print(f"❌ 工具 {tool_name} 已重试 {result.retry_count} 次，最终失败")

        return result.result

    def _build_messages(
        self,
        input_text: str,
        *,
        turn_context: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """构建消息列表，并在对话开始前执行上下文管理。

        ``turn_context`` 仅合并进本轮发给模型的末条 user，不写入历史。
        """
        if self.context_manager.maybe_compress_history():
            print("📦 对话开始前已压缩历史上下文")

        messages: List[Dict[str, Any]] = []

        if self.system_prompt:
            messages.append({
                "role": "system",
                "content": self.system_prompt
            })

        for msg in self._history:
            item: Dict[str, Any] = {
                "role": msg.role,
                "content": msg.content,
            }
            metadata = getattr(msg, "metadata", None) or {}
            if msg.role == "assistant" and metadata.get("tool_calls"):
                item["tool_calls"] = metadata["tool_calls"]
                if not msg.content:
                    item["content"] = None
            elif msg.role == "tool" and metadata.get("tool_call_id"):
                item["tool_call_id"] = metadata["tool_call_id"]
            messages.append(item)

        messages.append({
            "role": "user",
            "content": compose_turn_user_content(input_text, turn_context),
        })

        if self.context_manager.maybe_compress_messages(messages, self.system_prompt):
            print("📦 对话开始前已压缩本轮消息上下文")
        return messages

    def add_message(self, message: Message):
        """添加消息到历史，由 ContextManager 负责压缩判断。"""
        self.history_manager.append(message)
        self.context_manager.on_message_added(message)

        if self.config.auto_save_enabled and self.session_store:
            history_len = len(self.history_manager.get_history())
            if history_len % self.config.auto_save_interval == 0:
                self._auto_save()

    def clear_history(self):
        """清空历史并重置上下文 token 计数。"""
        self.history_manager.clear()
        self.context_manager.reset()

    async def _yield_tool_call_execution(
        self,
        tool_name: str,
        tool_call_id: str,
        arguments: Dict[str, Any],
        tracked_temp_files: Set[Path],
        tool_call_records: List[Dict[str, Any]],
        tool_results_by_id: Dict[str, str],
    ) -> AsyncGenerator[StreamEvent, None]:
        """执行单个工具调用并 yield 流式事件（含智能重试）。"""
        print(f"🎬 调用工具: {tool_name}({arguments})")
        preexisting_file = False
        raw_path = arguments.get("path")
        if isinstance(raw_path, str):
            resolved_before = self._resolve_workspace_file(raw_path)
            preexisting_file = bool(
                resolved_before and resolved_before.exists() and resolved_before.is_file()
            )

        yield StreamEvent.create(
            StreamEventType.TOOL_CALL_START,
            self.name,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            args=arguments
        )

        await asyncio.sleep(0)

        # ── 智能重试循环（委托给 RetryExecutor） ──
        retry_result = await self._retry_executor.execute_async(
            execute_fn=lambda args: self._execute_tool_call(tool_name, args),
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=arguments,
            session_id=getattr(self, "_current_session_id", None),
        )

        exec_result = retry_result.result
        retry_count = retry_result.retry_count
        duration_ms = retry_result.duration_ms
        final_status = retry_result.status

        self._maybe_track_temp_file(
            tool_name=tool_name,
            arguments=arguments,
            exec_result=exec_result,
            existed_before=preexisting_file,
            tracked_files=tracked_temp_files,
        )

        result_preview = exec_result[:200] + "..." if len(exec_result) > 200 else exec_result
        if exec_result.startswith("❌"):
            status_label = f" (已重试 {retry_count} 次)" if retry_count else ""
            print(f"❌ 工具执行失败{status_label}: {result_preview}")
        else:
            if retry_count:
                print(f"✅ 工具 {tool_name} 第 {retry_count + 1} 次重试成功")
            else:
                print(f"👀 观察: {result_preview}")

        yield StreamEvent.create(
            StreamEventType.TOOL_CALL_FINISH,
            self.name,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            result=exec_result,
            metadata={
                "retry_count": retry_count,
            } if retry_count else {}
        )

        tool_call_records.append({
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "args": arguments,
            "result": exec_result,
            "status": final_status,
            "retry_count": retry_count,
        })
        tool_results_by_id[tool_call_id] = exec_result

    async def _try_execute_ready_tool(
        self,
        tc_state: Dict[str, Any],
        tracked_temp_files: Set[Path],
        tool_call_records: List[Dict[str, Any]],
        tool_results_by_id: Dict[str, str],
        executed_ids: Set[str],
    ) -> AsyncGenerator[StreamEvent, None]:
        """若工具参数 JSON 已完整，立即执行该工具。

        包含两个保护机制：
        1. 去重：同一轮中相同工具 + 相同参数只执行一次
        2. 限量：每轮最多执行 max_tools_per_round 个工具调用
        """
        if tc_state.get("executed"):
            return

        tool_call_id = tc_state.get("id") or ""
        tool_name = tc_state.get("name") or ""
        if not tool_call_id or not tool_name:
            return

        args_str = tc_state.get("arguments", "")
        if not args_str:
            return

        try:
            arguments = json.loads(args_str)
        except json.JSONDecodeError:
            return

        tc_state["executed"] = True
        executed_ids.add(tool_call_id)

        # ── 只读门控：流式早执行路径必须拦截（不可只依赖 FINISH 批量路径）──
        blocked = self._readonly_block_message_if_needed(tool_name)
        if blocked is not None:
            tool_results_by_id[tool_call_id] = blocked
            tool_call_records.append({
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "args": arguments,
                "result": blocked,
                "status": "blocked_readonly",
            })
            yield StreamEvent.create(
                StreamEventType.TOOL_CALL_START,
                self.name,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                args=arguments,
            )
            await asyncio.sleep(0)
            yield StreamEvent.create(
                StreamEventType.TOOL_CALL_FINISH,
                self.name,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                result=blocked,
            )
            return

        # ── 去重检查：相同工具 + 相同参数只执行一次 ──
        dedup_key = f"{tool_name}:{args_str}"
        if dedup_key in self._tool_call_dedup:
            skip_msg = f"⚠️ 重复调用已跳过（同一轮中已执行过相同参数的 {tool_name}）"
            tool_results_by_id[tool_call_id] = skip_msg
            tool_call_records.append({
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "args": arguments,
                "result": skip_msg,
                "status": "skipped_dedup"
            })
            yield StreamEvent.create(
                StreamEventType.TOOL_CALL_START,
                self.name,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                args=arguments,
            )
            await asyncio.sleep(0)
            yield StreamEvent.create(
                StreamEventType.TOOL_CALL_FINISH,
                self.name,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                result=skip_msg,
            )
            return

        # ── 限量检查：超过本轮上限时跳过 ──
        if self._tools_executed_this_round >= self.max_tools_per_round:
            skip_msg = (
                f"⚠️ 已达到本轮工具调用上限({self.max_tools_per_round})，此调用被跳过。"
                f"请基于已有工具结果继续分析，不要在同一轮中生成过多工具调用。"
            )
            tool_results_by_id[tool_call_id] = skip_msg
            tool_call_records.append({
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "args": arguments,
                "result": skip_msg,
                "status": "skipped_limit"
            })
            yield StreamEvent.create(
                StreamEventType.TOOL_CALL_START,
                self.name,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                args=arguments,
            )
            await asyncio.sleep(0)
            yield StreamEvent.create(
                StreamEventType.TOOL_CALL_FINISH,
                self.name,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                result=skip_msg,
            )
            return

        # ── 上下文守卫：预判输出大小，大工具自动委托给子代理 ──
        if (
            self._context_guard is not None
            and self._context_guard.should_delegate(tool_name)
        ):
            try:
                delegated_result = await self._context_guard.delegate_tool(
                    tool_name, arguments
                )
            except Exception as exc:
                # 委托失败 → 降级为直接执行
                print(f"⚠️ 自动委托失败 ({tool_name})，降级为直接执行: {exc}")
                delegated_result = None

            if delegated_result is not None:
                self._tool_call_dedup.add(dedup_key)
                self._tools_executed_this_round += 1

                print(f"🔄 自动委托子代理: {tool_name}({arguments})")
                yield StreamEvent.create(
                    StreamEventType.TOOL_CALL_START,
                    self.name,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    args=arguments,
                    metadata={"delegated": True},
                )
                await asyncio.sleep(0)

                yield StreamEvent.create(
                    StreamEventType.TOOL_CALL_FINISH,
                    self.name,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    result=delegated_result,
                )

                tool_call_records.append({
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "args": arguments,
                    "result": delegated_result,
                    "status": "delegated",
                })
                tool_results_by_id[tool_call_id] = delegated_result
                return

        # ── 正常执行 ──
        self._tool_call_dedup.add(dedup_key)
        self._tools_executed_this_round += 1
        async for event in self._yield_tool_call_execution(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=arguments,
            tracked_temp_files=tracked_temp_files,
            tool_call_records=tool_call_records,
            tool_results_by_id=tool_results_by_id,
        ):
            yield event

    async def _execute_tool_call_with_error(
        self,
        tool_name: str,
        tool_call_id: str,
        error_message: str,
        tool_call_records: List[Dict[str, Any]],
        tool_results_by_id: Dict[str, str],
        executed_ids: Set[str],
    ) -> AsyncGenerator[StreamEvent, None]:
        """工具参数无法解析时，记录错误结果。"""
        if tool_call_id in executed_ids:
            return

        executed_ids.add(tool_call_id)
        print(f"❌ 工具参数解析失败: {error_message}")

        yield StreamEvent.create(
            StreamEventType.TOOL_CALL_START,
            self.name,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            args={}
        )
        await asyncio.sleep(0)

        exec_result = f"错误：参数格式不正确 - {error_message}"
        yield StreamEvent.create(
            StreamEventType.TOOL_CALL_FINISH,
            self.name,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            result=exec_result
        )

        tool_call_records.append({
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "args": {},
            "result": exec_result,
            "status": "error"
        })
        tool_results_by_id[tool_call_id] = exec_result

    async def _execute_tools_batch(
        self,
        tool_calls: List[Dict[str, Any]],
        tracked_temp_files: Set[Path],
        tool_call_records: List[Dict[str, Any]],
        tool_results_by_id: Dict[str, str],
        executed_ids: Set[str],
        cancel_token: Optional[CancellationToken] = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """批量执行工具调用，无副作用工具并行，有副作用工具串行。

        基于 has_side_effects 元数据自动决策：
        - has_side_effects=False 的工具用 asyncio 并行执行
        - has_side_effects=True 的工具保持串行执行

        Args:
            tool_calls: 已过滤、已解析参数的工具调用列表，
                        每项形如 {"name": str, "id": str, "arguments": dict}
            tracked_temp_files: 临时文件追踪集合
            tool_call_records: 工具调用记录列表（会被追加）
            tool_results_by_id: 工具结果映射（会被追加）
            cancel_token: 取消令牌
        """
        if not tool_calls:
            return

        # 1. 基于 has_side_effects 分组
        parallel_calls: List[Dict[str, Any]] = []
        serial_calls: List[Dict[str, Any]] = []
        for tc in tool_calls:
            tool_name = tc["name"]
            # 工具门控：READ_ONLY 模式下副作用工具返回 None
            if self._tool_mode_filter:
                tool = self._tool_mode_filter.get_tool(tool_name)
            else:
                tool = self.tool_registry.get_tool(tool_name) if self.tool_registry else None
            # 工具被模式过滤时，跳过并返回拒绝信息
            blocked = self._readonly_block_message_if_needed(tool_name)
            if blocked is not None:
                tool_results_by_id[tc["id"]] = blocked
                executed_ids.add(tc["id"])
                continue
            if tool is None and self.tool_registry and self.tool_registry.get_tool(tool_name):
                # 其它「注册表有、过滤器无」情况（非 SIDE_EFFECT 名单）仍跳过
                continue
            has_side_effects = getattr(tool, "has_side_effects", True) if tool else True
            if has_side_effects:
                serial_calls.append(tc)
            else:
                parallel_calls.append(tc)

        # 2. 并行执行无副作用工具
        if len(parallel_calls) == 1:
            # 只有一个工具，无需并行开销
            tc = parallel_calls[0]
            async for event in self._yield_tool_call_execution(
                tool_name=tc["name"],
                tool_call_id=tc["id"],
                arguments=tc["arguments"],
                tracked_temp_files=tracked_temp_files,
                tool_call_records=tool_call_records,
                tool_results_by_id=tool_results_by_id,
            ):
                yield event
        elif len(parallel_calls) > 1:
            print(f"⚡ 并行执行 {len(parallel_calls)} 个无副作用工具: "
                  f"{[tc['name'] for tc in parallel_calls]}")
            queue: asyncio.Queue = asyncio.Queue()

            async def _run_one(tc: Dict[str, Any]):
                try:
                    async for event in self._yield_tool_call_execution(
                        tool_name=tc["name"],
                        tool_call_id=tc["id"],
                        arguments=tc["arguments"],
                        tracked_temp_files=tracked_temp_files,
                        tool_call_records=tool_call_records,
                        tool_results_by_id=tool_results_by_id,
                    ):
                        await queue.put(event)
                except Exception as exc:
                    print(f"⚠️ 并行工具执行异常 ({tc['name']}): {exc}")
                finally:
                    await queue.put(None)  # 完成标记

            tasks = [asyncio.create_task(_run_one(tc)) for tc in parallel_calls]
            pending = len(tasks)
            while pending > 0:
                event = await queue.get()
                if event is None:
                    pending -= 1
                else:
                    yield event
            await asyncio.gather(*tasks, return_exceptions=True)

        # 3. 串行执行有副作用工具
        for tc in serial_calls:
            if cancel_token and cancel_token.is_cancelled:
                break
            async for event in self._yield_tool_call_execution(
                tool_name=tc["name"],
                tool_call_id=tc["id"],
                arguments=tc["arguments"],
                tracked_temp_files=tracked_temp_files,
                tool_call_records=tool_call_records,
                tool_results_by_id=tool_results_by_id,
            ):
                yield event

    # ==================== LLM 用量记录 ====================

    def _model_name(self) -> str:
        """当前 LLM 的模型 ID（用于用量日志分组）。"""
        return str(getattr(self.llm, "model", "") or "")

    def _log_llm_usage(
        self,
        usage: Optional[Dict[str, int]],
        *,
        call_site: str,
        duration_ms: float = 0.0,
        stream: bool = True,
        iteration: Optional[int] = None,
    ) -> None:
        """把一次 LLM 调用的 token 用量写入 JSONL。

        遥测绝不能影响主流程，因此整体吞掉异常。
        usage 为 None 时也会写一条 status="no_usage" 的记录，用于暴露统计盲区。
        """
        try:
            LLMUsageLogger.log(
                model=self._model_name(),
                call_site=call_site,
                usage=usage,
                duration_ms=duration_ms,
                stream=stream,
                iteration=iteration,
                agent_name=self.name,
            )
        except Exception:
            pass

    def run(
        self,
        input_text: str,
        *,
        cancel_token: Optional[CancellationToken] = None,
        turn_context: Optional[str] = None,
        **kwargs,
    ) -> str:
        """同步运行；每轮工具迭代重建 tool_schemas（支持 MCP 渐进披露）。

        Args:
            input_text: 用户输入（写入历史的干净原文）
            cancel_token: 取消令牌，用于中断 Agent 循环
            turn_context: 仅本轮发给模型的 ephemeral 上下文（不入历史）
        """
        from datetime import datetime as dt
        from hello_agents.observability import TraceLogger

        session_start_time = dt.now()
        trace_logger = None
        if self.config.trace_enabled:
            trace_logger = TraceLogger(
                output_dir=self.config.trace_dir,
                sanitize=self.config.trace_sanitize,
                html_include_raw_response=self.config.trace_html_include_raw_response,
            )
            trace_logger.log_event(
                "session_start",
                {"agent_name": self.name, "agent_type": self.__class__.__name__},
            )

        messages = self._build_messages(input_text, turn_context=turn_context)

        if trace_logger:
            trace_logger.log_event("message_written", {"role": "user", "content": input_text})

        if not self.enable_tool_calling or not self.tool_registry:
            llm_response = self.llm.invoke(messages, **kwargs)
            response_text = (
                llm_response.content if hasattr(llm_response, "content") else str(llm_response)
            )
            self.add_message(Message(input_text, "user"))
            self.add_message(Message(response_text, "assistant"))
            if trace_logger:
                duration = (dt.now() - session_start_time).total_seconds()
                trace_logger.log_event(
                    "session_end",
                    {
                        "duration": duration,
                        "final_answer": response_text,
                        "status": "success",
                        "usage": getattr(llm_response, "usage", {}),
                        "latency_ms": getattr(llm_response, "latency_ms", 0),
                    },
                )
                trace_logger.finalize()
            return response_text

        current_iteration = 0
        final_response = ""

        while current_iteration < self.max_tool_iterations:
            if cancel_token and cancel_token.is_cancelled:
                print(f"⏹️ Agent 执行被取消（同步第 {current_iteration} 轮）")
                break
            current_iteration += 1
            tool_schemas = self._build_tool_schemas()
            print(
                f"🔧 同步第 {current_iteration} 轮可用工具 "
                f"({len(tool_schemas)}): {self._get_available_tool_names()}"
            )

            try:
                # LLM 调用重试（网络抖动时指数退避）
                response = None
                llm_max_attempts = 1 + self._timeout_config.llm_retry_max
                for llm_attempt in range(1, llm_max_attempts + 1):
                    try:
                        response = self.llm.invoke_with_tools(
                            messages=messages,
                            tools=tool_schemas,
                            tool_choice="auto",
                            **kwargs,
                        )
                        break  # 成功
                    except Exception as llm_e:
                        error_text = f"❌ {llm_e}"
                        is_retryable, reason, _ = classify_error(error_text)
                        if is_retryable and llm_attempt < llm_max_attempts:
                            delay = compute_retry_delay(
                                attempt=llm_attempt,
                                base_delay=self._timeout_config.llm_retry_base_delay,
                                max_delay=self._timeout_config.llm_retry_max_delay,
                                backoff=self._timeout_config.llm_retry_backoff,
                                jitter=self._timeout_config.llm_retry_jitter,
                            )
                            print(f"🔄 LLM 调用第 {llm_attempt}/{llm_max_attempts} 次失败 ({reason})，{delay:.1f}s 后重试…")
                            time.sleep(delay)
                        else:
                            raise  # 不可重试或已耗尽
            except Exception as e:
                print(f"❌ LLM 调用失败: {e}")
                if trace_logger:
                    trace_logger.log_event(
                        "error",
                        {"error_type": "LLM_ERROR", "message": str(e)},
                        step=current_iteration,
                    )
                break

            response_message = response.choices[0].message

            # 记录本轮 token 用量（同步链路，usage 由非流式响应直接返回）
            _sync_usage = normalize_usage(getattr(response, "usage", None))
            self._log_llm_usage(
                _sync_usage,
                call_site="main_loop",
                stream=False,
                iteration=current_iteration,
            )

            if trace_logger:
                usage = response.usage
                trace_logger.log_event(
                    "model_output",
                    {
                        "content": response_message.content,
                        "tool_calls": len(response_message.tool_calls)
                        if response_message.tool_calls
                        else 0,
                        "usage": {
                            "prompt_tokens": usage.prompt_tokens if usage else 0,
                            "completion_tokens": usage.completion_tokens if usage else 0,
                            "total_tokens": usage.total_tokens if usage else 0,
                        },
                    },
                    step=current_iteration,
                )

            tool_calls = response_message.tool_calls
            if not tool_calls:
                final_response = response_message.content or "抱歉，我无法回答这个问题。"
                break

            messages.append({
                "role": "assistant",
                "content": response_message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            })

            for tool_call in tool_calls:
                if cancel_token and cancel_token.is_cancelled:
                    break
                tool_name = tool_call.function.name
                tool_call_id = tool_call.id
                try:
                    arguments = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError as e:
                    print(f"❌ 工具参数解析失败: {e}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": f"错误：参数格式不正确 - {str(e)}",
                    })
                    continue

                if trace_logger:
                    trace_logger.log_event(
                        "tool_call",
                        {
                            "tool_name": tool_name,
                            "tool_call_id": tool_call_id,
                            "args": arguments,
                        },
                        step=current_iteration,
                    )

                # ── 智能重试（同步路径） ──
                result = self._execute_tool_call_with_retry_sync(
                    tool_name, tool_call_id, arguments, current_iteration,
                )

                if trace_logger:
                    trace_logger.log_event(
                        "tool_result",
                        {
                            "tool_name": tool_name,
                            "tool_call_id": tool_call_id,
                            "result": result,
                        },
                        step=current_iteration,
                    )

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result,
                })

            self.context_manager.maybe_compress_messages(messages, self.system_prompt)

        if current_iteration >= self.max_tool_iterations and not final_response:
            llm_response = self.llm.invoke(messages, **kwargs)
            final_response = (
                llm_response.content if hasattr(llm_response, "content") else str(llm_response)
            )

        self.add_message(Message(input_text, "user"))
        self.add_message(Message(final_response, "assistant"))

        if trace_logger:
            duration = (dt.now() - session_start_time).total_seconds()
            trace_logger.log_event(
                "session_end",
                {
                    "duration": duration,
                    "total_steps": current_iteration,
                    "final_answer": final_response,
                    "status": "success",
                },
            )
            trace_logger.finalize()

        return final_response

    async def arun_stream_with_tools(
        self,
        input_text: str,
        *,
        cancel_token: Optional[CancellationToken] = None,
        turn_context: Optional[str] = None,
        **kwargs
    ) -> AsyncGenerator[StreamEvent, None]:
        """异步流式运行（支持工具调用）

        使用 EnhancedHelloAgentsLLM 的 astream_invoke_with_tools 方法实现流式工具调用。
        每个工具调用的参数 JSON 解析完成后会立即执行，无需等待整轮流结束。

        Args:
            input_text: 用户输入（写入历史的干净原文）
            cancel_token: 取消令牌，用于中断 Agent 循环
            turn_context: 仅本轮发给模型的 ephemeral 上下文（不入历史）
            **kwargs: 其他参数

        Yields:
            StreamEvent: 流式事件
        """
        session_start_time = datetime.now()

        # 发送开始事件
        yield StreamEvent.create(
            StreamEventType.AGENT_START,
            self.name,
            input_text=input_text
        )

        # 日志展示：若 input_text 是多模态编码字符串，拆出友好提示，避免打印 base64 / 编码乱码
        try:
            from .multimodal_bridge import is_encoded_multimodal, decode_multimodal_content
            from ..multimodal import flatten_content_to_text
            if is_encoded_multimodal(input_text):
                decoded = decode_multimodal_content(input_text)
                _display = flatten_content_to_text(decoded)
                if len(_display) > 200:
                    _display = _display[:200] + "..."
                # 统计附件数量，方便排查多模态链路
                _att_count = (
                    sum(1 for p in decoded if isinstance(p, dict) and p.get("type") == "image_url")
                    if isinstance(decoded, list)
                    else 0
                )
                _suffix = f"  [📎 图片附件 x{_att_count}]" if _att_count else ""
                print(f"\n🤖 {self.name} 开始处理问题（流式）: {_display}{_suffix}")
            else:
                _display = input_text if len(input_text) <= 200 else input_text[:200] + "..."
                print(f"\n🤖 {self.name} 开始处理问题（流式）: {_display}")
        except Exception:
            # 任意异常都不应阻塞主流程，回退到原日志
            print(f"\n🤖 {self.name} 开始处理问题（流式）: {input_text[:200]}")
        tracked_temp_files: Set[Path] = set()

        try:
            # 构建消息列表，并在对话开始前检查/执行上下文压缩
            messages = self._build_messages(input_text, turn_context=turn_context)

            # 检查是否有工具
            if not self.enable_tool_calling or not self.tool_registry:
                # 纯对话模式（复用已构建并压缩过的 messages）
                async for event in self._stream_without_tools(messages, input_text, **kwargs):
                    yield event
                return

            # 检查 LLM 是否支持流式工具调用
            if not self._supports_streaming_tools:
                import warnings
                warnings.warn(
                    "当前 LLM 不支持流式工具调用，将使用非流式模式。"
                    "推荐使用 EnhancedHelloAgentsLLM 以获得更好的体验。",
                    UserWarning
                )
                # 回退到基类的非流式模式
                response = self.run(
                    input_text,
                    cancel_token=cancel_token,
                    turn_context=turn_context,
                    **kwargs,
                )
                yield StreamEvent.create(
                    StreamEventType.AGENT_FINISH,
                    self.name,
                    result=response
                )
                return

            # === 流式工具调用模式 ===
            current_iteration = 0
            final_response = ""
            # 收集工具调用记录（用于存入会话）
            tool_call_records: List[Dict[str, Any]] = []
            # 本轮对话累计的 LLM token 用量（跨所有迭代轮次）
            turn_usage: Dict[str, int] = empty_usage()
            turn_llm_calls = 0

            while current_iteration < self.max_tool_iterations:
                if cancel_token and cancel_token.is_cancelled:
                    print(f"⏹️ Agent 执行被取消（第 {current_iteration} 轮）")
                    break
                current_iteration += 1

                tool_schemas = self._build_tool_schemas()
                tool_names = self._get_available_tool_names()
                print(
                    f"🔧 第 {current_iteration} 轮可用工具 "
                    f"({len(tool_schemas)}): {tool_names}"
                )

                # 发送步骤开始事件
                yield StreamEvent.create(
                    StreamEventType.STEP_START,
                    self.name,
                    step=current_iteration,
                    max_steps=self.max_tool_iterations
                )

                print(f"\n--- 第 {current_iteration} 轮 ---")
                print("💭 LLM 输出: ", end="", flush=True)

                pending_tools: Dict[int, Dict[str, Any]] = {}
                tool_results_by_id: Dict[str, str] = {}
                executed_ids: Set[str] = set()
                iteration_tool_records: List[Dict[str, Any]] = []

                # 重置每轮工具调用计数器和去重集合
                self._tools_executed_this_round = 0
                self._tool_call_dedup = set()

                # 使用 LLM 的流式工具调用方法（含重试）
                llm_max_attempts = 1 + self._timeout_config.llm_retry_max
                llm_stream_succeeded = False
                for llm_attempt in range(1, llm_max_attempts + 1):
                    events_received = False
                    try:
                        async for event in self.llm.astream_invoke_with_tools(
                            messages=messages,
                            tools=tool_schemas,
                            tool_choice="auto",
                            **kwargs
                        ):
                            events_received = True

                            # 取消检查：LLM 流式输出过程中也可及时中断
                            if cancel_token and cancel_token.is_cancelled:
                                print("\n⏹️ LLM 流式输出被取消")
                                break

                            # 处理文本内容
                            if event.event_type == StreamToolEventType.CONTENT:
                                yield StreamEvent.create(
                                    StreamEventType.LLM_CHUNK,
                                    self.name,
                                    chunk=event.content,
                                    step=current_iteration
                                )
                                print(event.content, end="", flush=True)

                            elif event.event_type == StreamToolEventType.TOOL_CALL_START:
                                idx = event.tool_call_index
                                if idx is None:
                                    continue
                                if idx not in pending_tools:
                                    pending_tools[idx] = {
                                        "id": "",
                                        "name": "",
                                        "arguments": "",
                                        "executed": False,
                                    }
                                if event.tool_call_id:
                                    pending_tools[idx]["id"] = event.tool_call_id
                                if event.tool_name:
                                    pending_tools[idx]["name"] = event.tool_name
                                # 新工具开始时，尝试执行已完成解析的前序工具
                                for prev_idx in sorted(pending_tools.keys()):
                                    if prev_idx >= idx:
                                        break
                                    async for tool_event in self._try_execute_ready_tool(
                                        pending_tools[prev_idx],
                                        tracked_temp_files,
                                        iteration_tool_records,
                                        tool_results_by_id,
                                        executed_ids,
                                    ):
                                        yield tool_event

                            elif event.event_type == StreamToolEventType.TOOL_CALL_DELTA:
                                idx = event.tool_call_index
                                if idx is None or not event.tool_arguments_delta:
                                    continue
                                if idx not in pending_tools:
                                    pending_tools[idx] = {
                                        "id": "",
                                        "name": "",
                                        "arguments": "",
                                        "executed": False,
                                    }
                                pending_tools[idx]["arguments"] += event.tool_arguments_delta
                                async for tool_event in self._try_execute_ready_tool(
                                    pending_tools[idx],
                                    tracked_temp_files,
                                    iteration_tool_records,
                                    tool_results_by_id,
                                    executed_ids,
                                ):
                                    yield tool_event

                            elif event.event_type == StreamToolEventType.FINISH:
                                # 收集所有参数已完整但尚未执行的工具调用，批量执行
                                ready_calls: List[Dict[str, Any]] = []
                                for idx in sorted(pending_tools.keys()):
                                    tc_state = pending_tools[idx]
                                    if tc_state.get("executed"):
                                        continue

                                    tool_call_id = tc_state.get("id") or ""
                                    tool_name = tc_state.get("name") or ""
                                    args_str = tc_state.get("arguments", "")
                                    if not tool_call_id or not tool_name:
                                        continue

                                    tc_state["executed"] = True
                                    executed_ids.add(tool_call_id)

                                    # 尝试解析参数
                                    try:
                                        arguments = json.loads(args_str)
                                    except json.JSONDecodeError:
                                        async for tool_event in self._execute_tool_call_with_error(
                                            tool_name=tool_name,
                                            tool_call_id=tool_call_id,
                                            error_message="参数 JSON 不完整或格式错误",
                                            tool_call_records=iteration_tool_records,
                                            tool_results_by_id=tool_results_by_id,
                                            executed_ids=executed_ids,
                                        ):
                                            yield tool_event
                                        continue

                                    # 去重检查
                                    dedup_key = f"{tool_name}:{args_str}"
                                    if dedup_key in self._tool_call_dedup:
                                        skip_msg = f"⚠️ 重复调用已跳过（同一轮中已执行过相同参数的 {tool_name}）"
                                        tool_results_by_id[tool_call_id] = skip_msg
                                        iteration_tool_records.append({
                                            "tool_call_id": tool_call_id,
                                            "name": tool_name,
                                            "args": arguments,
                                            "result": skip_msg,
                                            "status": "skipped_dedup"
                                        })
                                        yield StreamEvent.create(
                                            StreamEventType.TOOL_CALL_START,
                                            self.name,
                                            tool_name=tool_name,
                                            tool_call_id=tool_call_id,
                                            args=arguments,
                                        )
                                        await asyncio.sleep(0)
                                        yield StreamEvent.create(
                                            StreamEventType.TOOL_CALL_FINISH,
                                            self.name,
                                            tool_name=tool_name,
                                            tool_call_id=tool_call_id,
                                            result=skip_msg,
                                        )
                                        continue

                                    # 限量检查
                                    if self._tools_executed_this_round >= self.max_tools_per_round:
                                        skip_msg = (
                                            f"⚠️ 已达到本轮工具调用上限({self.max_tools_per_round})，此调用被跳过。"
                                        )
                                        tool_results_by_id[tool_call_id] = skip_msg
                                        iteration_tool_records.append({
                                            "tool_call_id": tool_call_id,
                                            "name": tool_name,
                                            "args": arguments,
                                            "result": skip_msg,
                                            "status": "skipped_limit"
                                        })
                                        yield StreamEvent.create(
                                            StreamEventType.TOOL_CALL_START,
                                            self.name,
                                            tool_name=tool_name,
                                            tool_call_id=tool_call_id,
                                            args=arguments,
                                        )
                                        await asyncio.sleep(0)
                                        yield StreamEvent.create(
                                            StreamEventType.TOOL_CALL_FINISH,
                                            self.name,
                                            tool_name=tool_name,
                                            tool_call_id=tool_call_id,
                                            result=skip_msg,
                                        )
                                        continue

                                    # 加入批量执行列表
                                    self._tool_call_dedup.add(dedup_key)
                                    self._tools_executed_this_round += 1
                                    ready_calls.append({
                                        "name": tool_name,
                                        "id": tool_call_id,
                                        "arguments": arguments,
                                    })

                                # 批量执行（无副作用工具并行，有副作用串行）
                                if ready_calls:
                                    async for tool_event in self._execute_tools_batch(
                                        tool_calls=ready_calls,
                                        tracked_temp_files=tracked_temp_files,
                                        tool_call_records=iteration_tool_records,
                                        tool_results_by_id=tool_results_by_id,
                                        executed_ids=executed_ids,
                                        cancel_token=cancel_token,
                                    ):
                                        yield tool_event

                        print()  # 换行
                        llm_stream_succeeded = True
                        break  # 成功，退出重试循环

                    except Exception as e:
                        if events_received:
                            # 已开始输出，不能重试（会导致重复输出）
                            error_msg = f"LLM 流式中断: {str(e)}"
                            print(f"\n❌ {error_msg}")
                            yield StreamEvent.create(
                                StreamEventType.ERROR,
                                self.name,
                                error=error_msg
                            )
                            llm_stream_succeeded = True
                            break

                        is_retryable, reason, _ = classify_error(f"❌ {e}")
                        if is_retryable and llm_attempt < llm_max_attempts:
                            delay = compute_retry_delay(
                                attempt=llm_attempt,
                                base_delay=self._timeout_config.llm_retry_base_delay,
                                max_delay=self._timeout_config.llm_retry_max_delay,
                                backoff=self._timeout_config.llm_retry_backoff,
                                jitter=self._timeout_config.llm_retry_jitter,
                            )
                            print(f"🔄 LLM 调用第 {llm_attempt}/{llm_max_attempts} 次失败 ({reason})，{delay:.1f}s 后重试…")
                            await asyncio.sleep(delay)
                            continue
                        else:
                            error_msg = f"LLM 调用失败: {str(e)}"
                            print(f"\n❌ {error_msg}")
                            yield StreamEvent.create(
                                StreamEventType.ERROR,
                                self.name,
                                error=error_msg
                            )
                            llm_stream_succeeded = True
                            break

                if not llm_stream_succeeded:
                    break

                # 获取累积结果
                result = self.llm.get_last_stream_tool_result()
                if result is None:
                    break

                # 记录本轮 LLM 调用的 token 用量（含缓存命中）
                turn_llm_calls += 1
                merge_usage(turn_usage, result.usage)
                self._log_llm_usage(
                    result.usage,
                    call_site="main_loop",
                    duration_ms=getattr(result, "duration_ms", 0.0),
                    stream=True,
                    iteration=current_iteration,
                )

                complete_tool_calls = result.get_complete_tool_calls()

                # 无论是否有工具调用，都保存本轮的文本内容
                if result.content:
                    final_response = result.content

                if not complete_tool_calls:
                    # 没有工具调用，直接返回
                    if not final_response:
                        final_response = "抱歉，我无法回答这个问题。"
                    preview = final_response[:100] + "..." if len(final_response) > 100 else final_response
                    print(f"💬 直接回复: {preview}")
                    break

                # 兜底：流结束后仍未执行的工具（如未收到 FINISH 事件）
                # 先过滤、解析参数、限量检查，再批量执行（无副作用工具并行）
                batch_calls: List[Dict[str, Any]] = []
                for tc in complete_tool_calls:
                    tool_call_id = tc["id"]
                    if tool_call_id in executed_ids:
                        continue
                    tool_name = tc["name"]
                    try:
                        arguments = json.loads(tc["arguments"])
                    except json.JSONDecodeError as e:
                        async for tool_event in self._execute_tool_call_with_error(
                            tool_name=tool_name,
                            tool_call_id=tool_call_id,
                            error_message=str(e),
                            tool_call_records=iteration_tool_records,
                            tool_results_by_id=tool_results_by_id,
                            executed_ids=executed_ids,
                        ):
                            yield tool_event
                        continue

                    # 限量检查（兜底路径同样受限）
                    if self._tools_executed_this_round >= self.max_tools_per_round:
                        skip_msg = f"⚠️ 已达到本轮工具调用上限({self.max_tools_per_round})，此调用被跳过。"
                        tool_results_by_id[tool_call_id] = skip_msg
                        iteration_tool_records.append({
                            "tool_call_id": tool_call_id,
                            "name": tool_name,
                            "args": arguments,
                            "result": skip_msg,
                            "status": "skipped_limit"
                        })
                        executed_ids.add(tool_call_id)
                        yield StreamEvent.create(
                            StreamEventType.TOOL_CALL_START,
                            self.name,
                            tool_name=tool_name,
                            tool_call_id=tool_call_id,
                            args=arguments,
                        )
                        await asyncio.sleep(0)
                        yield StreamEvent.create(
                            StreamEventType.TOOL_CALL_FINISH,
                            self.name,
                            tool_name=tool_name,
                            tool_call_id=tool_call_id,
                            result=skip_msg,
                        )
                        continue

                    # 加入批量执行列表
                    batch_calls.append({
                        "name": tool_name,
                        "id": tool_call_id,
                        "arguments": arguments,
                    })
                    executed_ids.add(tool_call_id)
                    self._tools_executed_this_round += 1

                # 批量执行（无副作用工具并行，有副作用工具串行）
                if batch_calls:
                    async for tool_event in self._execute_tools_batch(
                        tool_calls=batch_calls,
                        tracked_temp_files=tracked_temp_files,
                        tool_call_records=iteration_tool_records,
                        tool_results_by_id=tool_results_by_id,
                        executed_ids=executed_ids,
                        cancel_token=cancel_token,
                    ):
                        yield tool_event

                tool_call_records.extend(iteration_tool_records)

                messages.append(result.to_assistant_message())
                for tc in complete_tool_calls:
                    tool_call_id = tc["id"]
                    if tool_call_id in tool_results_by_id:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": tool_results_by_id[tool_call_id]
                        })

                self.context_manager.maybe_compress_messages(
                    messages, self.system_prompt
                )

                # 发送步骤完成事件
                yield StreamEvent.create(
                    StreamEventType.STEP_FINISH,
                    self.name,
                    step=current_iteration
                )

            # 如果超过最大迭代次数，获取最后一次回答
            if current_iteration >= self.max_tool_iterations and not final_response:
                print("⏰ 已达到最大迭代次数，获取最终回答...")

                try:
                    async for chunk in self.llm.astream_invoke(messages, **kwargs):
                        final_response += chunk
                        yield StreamEvent.create(
                            StreamEventType.LLM_CHUNK,
                            self.name,
                            chunk=chunk
                        )
                        print(chunk, end="", flush=True)
                    print()
                except Exception as e:
                    print(f"❌ 最终回答失败: {e}")
                    result = self.llm.get_last_stream_tool_result()
                    final_response = result.content if result else "抱歉，我无法回答这个问题。"

            # 保存到历史记录（按照 OpenAI 规范格式）
            self.add_message(Message(input_text, "user"))

            # 如果有工具调用，保存工具调用消息
            if tool_call_records:
                tool_calls_for_message = [
                    {
                        "id": tc.get("tool_call_id", f"call_{i}"),
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["args"])
                        }
                    }
                    for i, tc in enumerate(tool_call_records)
                ]
                self.add_message(Message(
                    "",
                    "assistant",
                    metadata={"tool_calls": tool_calls_for_message}
                ))

                for tc in tool_call_records:
                    self.add_message(Message(
                        tc["result"],
                        "tool",
                        metadata={"tool_call_id": tc.get("tool_call_id", "")}
                    ))

            # 保存最终 assistant 回答
            if final_response:
                self.add_message(Message(final_response, "assistant"))

            duration = (datetime.now() - session_start_time).total_seconds()
            print(f"\n✅ 完成，耗时 {duration:.2f}s，共 {current_iteration} 轮")
            if turn_usage["total_tokens"]:
                print(
                    f"   📊 tokens {turn_usage['total_tokens']} "
                    f"(prompt {turn_usage['prompt_tokens']} / "
                    f"completion {turn_usage['completion_tokens']} / "
                    f"cached {turn_usage['cached_tokens']})"
                )

            # 发送完成事件（带上本轮累计用量，供前端展示）
            yield StreamEvent.create(
                StreamEventType.AGENT_FINISH,
                self.name,
                result=final_response,
                usage=turn_usage,
                llm_calls=turn_llm_calls,
            )

        except Exception as e:
            print(f"❌ Agent 执行失败: {e}")
            yield StreamEvent.create(
                StreamEventType.ERROR,
                self.name,
                error=str(e),
                error_type=type(e).__name__
            )
            # 不要 raise，确保流式响应正常结束
            # 发送完成事件以优雅结束
            yield StreamEvent.create(
                StreamEventType.AGENT_FINISH,
                self.name,
                result="",  # 空结果表示失败
                usage=turn_usage,
                llm_calls=turn_llm_calls,
            )
        finally:
            deleted_count, failed = self._cleanup_tracked_temp_files(tracked_temp_files)
            if deleted_count:
                print(f"🧹 已自动清理临时文件: {deleted_count} 个")
            if failed:
                print("⚠️ 临时文件清理失败：")
                for line in failed:
                    print(f"   - {line}")

    async def _stream_without_tools(
        self,
        messages: List[Dict[str, Any]],
        input_text: str,
        **kwargs
    ) -> AsyncGenerator[StreamEvent, None]:
        """纯对话模式（无工具调用）

        Args:
            messages: 已由 _build_messages 构建并完成上下文管理的消息列表
            input_text: 原始用户输入（用于写入历史，避免压缩后 messages 末条失真）
        """
        print("📝 纯对话模式（无工具调用）")

        stream_kwargs = dict(kwargs)
        # 纯对话链路同样请求 usage；厂商不支持时自动降级（见下方 except）
        if getattr(self.llm, "_stream_usage_enabled", False):
            stream_kwargs["stream_options"] = {"include_usage": True}

        collected: List[str] = []

        async def _consume() -> AsyncGenerator[StreamEvent, None]:
            async for chunk in self.llm.astream_invoke(messages, **stream_kwargs):
                collected.append(chunk)
                yield StreamEvent.create(
                    StreamEventType.LLM_CHUNK,
                    self.name,
                    chunk=chunk
                )
                print(chunk, end="", flush=True)

        try:
            async for event in _consume():
                yield event
        except Exception as stream_err:
            if "stream_options" not in stream_kwargs:
                raise
            from .enhanced_llm import _is_stream_options_unsupported
            if not _is_stream_options_unsupported(stream_err):
                raise
            stream_kwargs.pop("stream_options", None)
            if hasattr(self.llm, "_stream_usage_enabled"):
                self.llm._stream_usage_enabled = False
            collected.clear()  # 400 发生在首块之前，不会产生重复输出
            async for event in _consume():
                yield event

        full_response = "".join(collected)
        print()

        # 记录用量：hello_agents 会把 usage 暂存在 last_call_stats
        stats = getattr(self.llm, "last_call_stats", None)
        stats_usage = getattr(stats, "usage", None) if stats else None
        self._log_llm_usage(
            normalize_usage(stats_usage),
            call_site="chat_no_tools",
            duration_ms=float(getattr(stats, "latency_ms", 0) or 0),
            stream=True,
        )

        # 保存历史（用原始 input_text，不用 messages 末条，压缩后末条可能不是用户原文）
        self.add_message(Message(input_text, "user"))
        self.add_message(Message(full_response, "assistant"))

        print(f"💬 回复完成")

        yield StreamEvent.create(
            StreamEventType.AGENT_FINISH,
            self.name,
            result=full_response
        )
