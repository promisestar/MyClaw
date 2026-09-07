"""增强版 HelloAgentsLLM - 支持流式工具调用"""

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Union, Any, AsyncIterator

import os

from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.core.exceptions import HelloAgentsException

from ..core.timeouts import TimeoutConfig
from ..logging.llm_usage_logger import normalize_usage


# ==================== 流式工具调用数据结构 ====================

class StreamToolEventType(Enum):
    """流式工具调用事件类型"""
    CONTENT = "content"  # 文本内容增量
    TOOL_CALL_START = "tool_call_start"  # 工具调用开始（收到ID和名称）
    TOOL_CALL_DELTA = "tool_call_delta"  # 工具调用参数增量
    FINISH = "finish"  # 流结束


@dataclass
class StreamToolEvent:
    """流式工具调用事件

    封装流式响应中的不同类型数据，统一处理文本内容和工具调用。
    """
    event_type: StreamToolEventType
    # 文本内容
    content: Optional[str] = None
    # 工具调用
    tool_call_index: Optional[int] = None  # 工具调用索引（用于增量累积）
    tool_call_id: Optional[str] = None  # 工具调用ID
    tool_name: Optional[str] = None  # 工具名称
    tool_arguments_delta: Optional[str] = None  # 参数增量
    # 结束信息
    finish_reason: Optional[str] = None

    @property
    def is_content(self) -> bool:
        """是否为文本内容事件"""
        return self.event_type == StreamToolEventType.CONTENT

    @property
    def is_tool_call(self) -> bool:
        """是否为工具调用事件"""
        return self.event_type in (
            StreamToolEventType.TOOL_CALL_START,
            StreamToolEventType.TOOL_CALL_DELTA
        )

    @property
    def is_finish(self) -> bool:
        """是否为结束事件"""
        return self.event_type == StreamToolEventType.FINISH


@dataclass
class StreamToolCallResult:
    """流式工具调用完成后的结果

    包含累积的文本内容和工具调用列表。
    """
    content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    finish_reason: Optional[str] = None
    # 本次调用的 token 用量（归一化后的字典，见 normalize_usage）
    usage: Optional[Dict[str, int]] = None
    # 本次调用耗时（毫秒）
    duration_ms: float = 0.0
    # 是否实际拿到了 usage（False = 厂商未返回，统计时不应算作 0 消耗）
    has_usage: bool = False

    def add_content(self, delta: str):
        """添加文本内容"""
        self.content += delta

    def add_tool_call_start(self, index: int, tool_id: str, tool_name: str):
        """添加工具调用开始"""
        # 确保列表足够长
        while len(self.tool_calls) <= index:
            self.tool_calls.append({"id": "", "name": "", "arguments": ""})
        self.tool_calls[index]["id"] = tool_id
        self.tool_calls[index]["name"] = tool_name

    def add_tool_call_delta(self, index: int, arguments_delta: str):
        """添加工具调用参数增量"""
        while len(self.tool_calls) <= index:
            self.tool_calls.append({"id": "", "name": "", "arguments": ""})
        self.tool_calls[index]["arguments"] += arguments_delta

    def get_complete_tool_calls(self) -> List[Dict[str, Any]]:
        """获取完整的工具调用列表（过滤不完整的）"""
        return [
            tc for tc in self.tool_calls
            if tc["id"] and tc["name"]
        ]

    def to_assistant_message(self) -> Dict[str, Any]:
        """转换为助手消息格式（用于追加到消息历史）"""
        message: Dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"]
                    }
                }
                for tc in self.get_complete_tool_calls()
            ]
        return message


def _is_stream_options_unsupported(err: Exception) -> bool:
    """判断异常是否由 stream_options 不被支持引起。

    不同厂商的错误文案差异很大，这里做宽松匹配：
    只要错误信息里出现 stream_options / include_usage 相关字样，或返回 400，
    就认为是该参数导致的失败并降级重试。

    注意：hello_agents 适配器会把原始异常包成 HelloAgentsException，
    因此除了 err 本身，还要检查 __cause__ / __context__ 上的 status_code。
    """
    candidates = [err]
    for attr in ("__cause__", "__context__"):
        chained = getattr(err, attr, None)
        if chained is not None and chained is not err:
            candidates.append(chained)

    for c in candidates:
        text = f"{type(c).__name__}: {c}".lower()
        if "stream_options" in text or "include_usage" in text:
            return True
        status = getattr(c, "status_code", None)
        if status == 400:
            return True
        if "400" in text and "invalid_request" in text:
            return True
    return False


# ==================== 增强版 LLM 类 ====================

class EnhancedHelloAgentsLLM(HelloAgentsLLM):
    """
    增强版 HelloAgentsLLM - 添加流式工具调用支持

    继承自 HelloAgentsLLM，新增以下方法：
    - astream_invoke_with_tools: 异步流式工具调用
    - get_last_stream_tool_result: 获取最后一次流式工具调用的累积结果
    """

    def __init__(self, *args, timeout_config: Optional[TimeoutConfig] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_stream_tool_result: Optional[StreamToolCallResult] = None
        self._timeout_config = timeout_config or TimeoutConfig()
        self._async_client: Optional[Any] = None  # 实例级 AsyncOpenAI 客户端缓存
        # 是否请求 stream_options.include_usage。
        # 部分厂商/网关不支持该参数会直接 400，此时自动降级为 False 并永久关闭。
        # 可用环境变量 LLM_STREAM_INCLUDE_USAGE=0 显式关闭。
        self._stream_usage_enabled: bool = os.getenv(
            "LLM_STREAM_INCLUDE_USAGE", "1"
        ).strip().lower() not in ("0", "false", "no")

    def _get_async_client(self):
        """获取或创建实例级 AsyncOpenAI 客户端（连接池复用）。

        避免每次 astream_invoke_with_tools 调用都新建客户端导致反复建立 TCP 连接。
        """
        if self._async_client is None:
            from openai import AsyncOpenAI
            self._async_client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._async_client

    async def close_async_client(self):
        """关闭异步客户端并释放 httpx 连接池。

        在以下场景调用：
        - MyClawAgent.shutdown() 时（lifespan finally 块 / Ctrl+C）
        - _reload_llm_if_changed() 替换 LLM 实例前
        """
        if self._async_client is not None:
            try:
                await self._async_client.close()
            except Exception:
                pass
            self._async_client = None

    async def astream_invoke_with_tools(
        self,
        messages: List[Dict],
        tools: List[Dict],
        tool_choice: Union[str, Dict] = "auto",
        **kwargs
    ) -> AsyncIterator[StreamToolEvent]:
        """
        异步流式调用 LLM 并支持工具调用（Function Calling）

        这是最优雅的流式工具调用方法，封装了所有流式处理的复杂逻辑。

        Args:
            messages: 消息列表
            tools: 工具 schema 列表
            tool_choice: 工具选择策略
            **kwargs: 其他参数（temperature, max_tokens 等）

        Yields:
            StreamToolEvent: 流式事件，可能是文本内容或工具调用增量

        Example:
            async for event in llm.astream_invoke_with_tools(messages, tools):
                if event.is_content:
                    print(event.content, end="")
                elif event.event_type == StreamToolEventType.TOOL_CALL_START:
                    print(f"\\n调用工具: {event.tool_name}")

            # 获取累积结果
            result = llm.get_last_stream_tool_result()
        """
        from openai import AsyncOpenAI

        # 复用实例级客户端（避免每次调用新建 TCP 连接）
        client = self._get_async_client()

        # 构建请求参数
        request_params: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "stream": True,
        }
        if kwargs.get("temperature") is not None:
            request_params["temperature"] = kwargs["temperature"]
        if self.max_tokens:
            request_params["max_tokens"] = self.max_tokens
        if self._stream_usage_enabled:
            # 关键：不传这个参数时，绝大多数厂商的流式响应不会返回 usage，
            # token 消耗将完全不可见（实测 MiniMax-M3 即如此）。
            request_params["stream_options"] = {"include_usage": True}

        # 初始化累积结果
        result = StreamToolCallResult()

        # 模块级超时：首 token + 流式中断
        import time as _time
        start_time = _time.perf_counter()
        last_chunk_time = start_time
        first_token_received = False
        first_token_timeout_s = self._timeout_config.llm_first_token_ms / 1000
        stream_idle_timeout_s = self._timeout_config.llm_stream_idle_ms / 1000

        try:
            try:
                response = await client.chat.completions.create(**request_params)
            except Exception as create_err:
                # 厂商不支持 stream_options 时降级重试一次（否则整条链路不可用）
                if self._stream_usage_enabled and _is_stream_options_unsupported(create_err):
                    self._stream_usage_enabled = False
                    request_params.pop("stream_options", None)
                    print(
                        "⚠️ 当前模型不支持 stream_options.include_usage，"
                        "已自动降级（token 用量将无法统计）"
                    )
                    response = await client.chat.completions.create(**request_params)
                else:
                    raise

            # 手动迭代以支持首 token / 流式中断超时检测
            while True:
                if not first_token_received:
                    # 首 token 超时检查
                    elapsed = _time.perf_counter() - start_time
                    if elapsed > first_token_timeout_s:
                        raise TimeoutError(
                            f"LLM 首 token 超时 ({elapsed:.1f}s > {first_token_timeout_s:.1f}s)"
                        )
                    chunk_timeout = first_token_timeout_s - elapsed
                else:
                    # 流式中断超时检查
                    idle = _time.perf_counter() - last_chunk_time
                    if idle > stream_idle_timeout_s:
                        raise TimeoutError(
                            f"LLM 流式中断超时 ({idle:.1f}s > {stream_idle_timeout_s:.1f}s)"
                        )
                    chunk_timeout = stream_idle_timeout_s - idle

                try:
                    chunk = await asyncio.wait_for(
                        response.__anext__(), timeout=max(chunk_timeout, 0.1)
                    )
                except StopAsyncIteration:
                    break

                first_token_received = True
                last_chunk_time = _time.perf_counter()

                # usage 只出现在最后一个 chunk（此时 choices 为空数组），
                # 因此必须在 `if not chunk.choices: continue` 之前读取。
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    normalized = normalize_usage(chunk_usage)
                    if normalized:
                        result.usage = normalized
                        result.has_usage = True

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                delta = choice.delta

                # 处理文本内容
                if delta.content:
                    result.add_content(delta.content)
                    yield StreamToolEvent(
                        event_type=StreamToolEventType.CONTENT,
                        content=delta.content
                    )

                # 处理工具调用增量
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index

                        # 工具调用开始（收到 ID 或名称）
                        if tc_delta.id or (tc_delta.function and tc_delta.function.name):
                            tool_id = tc_delta.id or ""
                            tool_name = tc_delta.function.name if tc_delta.function else ""
                            if tool_id or tool_name:
                                result.add_tool_call_start(idx, tool_id, tool_name)
                                yield StreamToolEvent(
                                    event_type=StreamToolEventType.TOOL_CALL_START,
                                    tool_call_index=idx,
                                    tool_call_id=tool_id,
                                    tool_name=tool_name
                                )

                        # 工具调用参数增量
                        if tc_delta.function and tc_delta.function.arguments:
                            args_delta = tc_delta.function.arguments
                            result.add_tool_call_delta(idx, args_delta)
                            yield StreamToolEvent(
                                event_type=StreamToolEventType.TOOL_CALL_DELTA,
                                tool_call_index=idx,
                                tool_arguments_delta=args_delta
                            )

                # 处理结束原因
                if choice.finish_reason:
                    result.finish_reason = choice.finish_reason
                    yield StreamToolEvent(
                        event_type=StreamToolEventType.FINISH,
                        finish_reason=choice.finish_reason
                    )

        except Exception as e:
            raise HelloAgentsException(f"流式工具调用失败: {str(e)}")

        result.duration_ms = (_time.perf_counter() - start_time) * 1000

        # 保存累积结果供后续使用
        self._last_stream_tool_result = result

    def get_last_stream_tool_result(self) -> Optional[StreamToolCallResult]:
        """
        获取最后一次流式工具调用的累积结果

        Returns:
            StreamToolCallResult 或 None
        """
        return self._last_stream_tool_result
