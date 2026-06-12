"""增强版 SimpleAgent - 支持流式工具调用"""

import json
import asyncio
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

# 导入 HelloClaw 专用 LLM（支持流式工具调用）
from .enhanced_llm import EnhancedHelloAgentsLLM, StreamToolEventType

from ..logging.tool_logger import ToolCallLogger, get_trace_id

if TYPE_CHECKING:
    from hello_agents.tools.registry import ToolRegistry


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

        # 解耦的上下文管理（替代基类 Agent 内嵌的压缩逻辑）
        self.context_manager = ContextManager(
            config=self.config,
            history_manager=self.history_manager,
            token_counter=self.token_counter,
            llm=self.llm,
        )
        self.context_manager.recalculate_history_tokens()

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

    def _build_messages(self, input_text: str) -> List[Dict[str, Any]]:
        """构建消息列表，并在对话开始前执行上下文管理。"""
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
            "content": input_text
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
        """执行单个工具调用并 yield 流式事件。"""
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

        t_start = time.perf_counter()
        exec_result = self._execute_tool_call(tool_name, arguments)
        duration_ms = (time.perf_counter() - t_start) * 1000

        # 结构化日志：记录工具调用
        tool_status = "error" if exec_result.startswith("❌") else "done"
        ToolCallLogger.log(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            args=arguments,
            result=exec_result,
            session_id=getattr(self, "_current_session_id", None),
            status=tool_status,
            duration_ms=duration_ms,
        )
        self._maybe_track_temp_file(
            tool_name=tool_name,
            arguments=arguments,
            exec_result=exec_result,
            existed_before=preexisting_file,
            tracked_files=tracked_temp_files,
        )

        result_preview = exec_result[:200] + "..." if len(exec_result) > 200 else exec_result
        if exec_result.startswith("❌"):
            print(f"❌ 工具执行失败: {result_preview}")
        else:
            print(f"👀 观察: {result_preview}")

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
            "args": arguments,
            "result": exec_result,
            "status": "error" if exec_result.startswith("❌") else "done"
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
        """若工具参数 JSON 已完整，立即执行该工具。"""
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

    def run(self, input_text: str, **kwargs) -> str:
        """同步运行；每轮工具迭代重建 tool_schemas（支持 MCP 渐进披露）。"""
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

        messages = self._build_messages(input_text)

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
            current_iteration += 1
            tool_schemas = self._build_tool_schemas()
            print(
                f"🔧 同步第 {current_iteration} 轮可用工具 "
                f"({len(tool_schemas)}): {self.tool_registry.list_tools()}"
            )

            try:
                response = self.llm.invoke_with_tools(
                    messages=messages,
                    tools=tool_schemas,
                    tool_choice="auto",
                    **kwargs,
                )
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

                result = self._execute_tool_call(tool_name, arguments)

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
        **kwargs
    ) -> AsyncGenerator[StreamEvent, None]:
        """异步流式运行（支持工具调用）

        使用 EnhancedHelloAgentsLLM 的 astream_invoke_with_tools 方法实现流式工具调用。
        每个工具调用的参数 JSON 解析完成后会立即执行，无需等待整轮流结束。

        Args:
            input_text: 用户输入
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

        print(f"\n🤖 {self.name} 开始处理问题（流式）: {input_text}")
        tracked_temp_files: Set[Path] = set()

        try:
            # 构建消息列表，并在对话开始前检查/执行上下文压缩
            messages = self._build_messages(input_text)

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
                response = self.run(input_text, **kwargs)
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

            while current_iteration < self.max_tool_iterations:
                current_iteration += 1

                tool_schemas = self._build_tool_schemas()
                tool_names = (
                    list(self.tool_registry._tools.keys())
                    if self.tool_registry
                    else []
                )
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

                # 使用 LLM 的流式工具调用方法
                try:
                    async for event in self.llm.astream_invoke_with_tools(
                        messages=messages,
                        tools=tool_schemas,
                        tool_choice="auto",
                        **kwargs
                    ):
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
                            for idx in sorted(pending_tools.keys()):
                                tc_state = pending_tools[idx]
                                if tc_state.get("executed"):
                                    continue
                                async for tool_event in self._try_execute_ready_tool(
                                    tc_state,
                                    tracked_temp_files,
                                    iteration_tool_records,
                                    tool_results_by_id,
                                    executed_ids,
                                ):
                                    yield tool_event
                                if tc_state.get("executed"):
                                    continue
                                tool_call_id = tc_state.get("id") or ""
                                tool_name = tc_state.get("name") or ""
                                if tool_call_id and tool_name:
                                    async for tool_event in self._execute_tool_call_with_error(
                                        tool_name=tool_name,
                                        tool_call_id=tool_call_id,
                                        error_message="参数 JSON 不完整或格式错误",
                                        tool_call_records=iteration_tool_records,
                                        tool_results_by_id=tool_results_by_id,
                                        executed_ids=executed_ids,
                                    ):
                                        yield tool_event
                                    tc_state["executed"] = True

                    print()  # 换行

                except Exception as e:
                    error_msg = f"LLM 调用失败: {str(e)}"
                    print(f"\n❌ {error_msg}")
                    yield StreamEvent.create(
                        StreamEventType.ERROR,
                        self.name,
                        error=error_msg
                    )
                    break

                # 获取累积结果
                result = self.llm.get_last_stream_tool_result()
                if result is None:
                    break

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

                    async for tool_event in self._yield_tool_call_execution(
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        arguments=arguments,
                        tracked_temp_files=tracked_temp_files,
                        tool_call_records=iteration_tool_records,
                        tool_results_by_id=tool_results_by_id,
                    ):
                        yield tool_event
                    executed_ids.add(tool_call_id)

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

            # 发送完成事件
            yield StreamEvent.create(
                StreamEventType.AGENT_FINISH,
                self.name,
                result=final_response
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
                result=""  # 空结果表示失败
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

        full_response = ""
        async for chunk in self.llm.astream_invoke(messages, **kwargs):
            full_response += chunk
            yield StreamEvent.create(
                StreamEventType.LLM_CHUNK,
                self.name,
                chunk=chunk
            )
            print(chunk, end="", flush=True)

        print()

        # 保存历史（用原始 input_text，不用 messages 末条，压缩后末条可能不是用户原文）
        self.add_message(Message(input_text, "user"))
        self.add_message(Message(full_response, "assistant"))

        print(f"💬 回复完成")

        yield StreamEvent.create(
            StreamEventType.AGENT_FINISH,
            self.name,
            result=full_response
        )
