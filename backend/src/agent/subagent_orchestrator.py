"""SubAgent 编排器 —— 上下文隔离 + 并行执行。

核心理念（借鉴 WorkBuddy 架构）：
- 主 Agent 专注智能决策，不直接执行重型工具
- 子 Agent 在隔离上下文中消化复杂任务
- 中间数据在子代理内消化，不回传主上下文
- 多个独立子任务并行执行

架构示意：

  主 Agent（决策中枢）
    ├→ SubAgent-1（隔离上下文 + 白名单工具）→ 返回摘要
    ├→ SubAgent-2（隔离上下文 + 白名单工具）→ 返回摘要
    └→ SubAgent-3（隔离上下文 + 白名单工具）→ 返回摘要

  主上下文始终保持精简
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from hello_agents.tools import ToolRegistry

from .enhanced_simple_agent import EnhancedSimpleAgent
from ..logging.llm_usage_logger import log_response_safe

logger = logging.getLogger(__name__)


# ============================================================================
# 数据模型
# ============================================================================


class SubAgentResultMode(str, Enum):
    """子代理结果回传模式"""

    SUMMARY = "summary"   # 仅回传 LLM 生成的摘要（默认，省 Token）
    FULL = "full"         # 回传完整工具输出（仅在主 Agent 必须看原始数据时使用）
    SILENT = "silent"     # 不回传任何内容（纯副作用任务，如清理、验证）


@dataclass
class SubAgentTask:
    """子代理任务定义

    Attributes:
        task_id: 任务唯一标识
        description: 自然语言任务描述（子代理的系统提示词）
        tools: 该子代理可用的工具名称白名单（空列表 = 无工具）
        result_mode: 结果回传模式
        max_iterations: 最大工具调用迭代次数（默认 8，子代理应比主代理更保守）
        timeout_seconds: 超时时间（秒），默认 60
    """

    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    description: str = ""
    tools: List[str] = field(default_factory=list)
    result_mode: SubAgentResultMode = SubAgentResultMode.SUMMARY
    max_iterations: int = 8
    timeout_seconds: int = 60  # 默认值，__post_init__ 会自动覆盖

    def __post_init__(self):
        """自动计算超时（如果仍为默认值 60）。

        公式：base(30) + per_tool(15 * len(tools)) + per_iteration(10 * max_iterations)
        限制范围：30s - 300s（5 分钟）
        """
        if self.timeout_seconds == 60:
            base = 30
            per_tool = 15 * len(self.tools)
            per_iteration = 10 * self.max_iterations
            calculated = base + per_tool + per_iteration
            self.timeout_seconds = min(max(calculated, 30), 300)


@dataclass
class SubAgentResult:
    """子代理执行结果

    Attributes:
        task_id: 对应的任务 ID
        success: 是否执行成功
        summary: 子代理返回的摘要文本
        tool_calls_count: 子代理执行的工具调用次数
        duration_ms: 执行耗时（毫秒）
        error: 如果失败，错误信息
    """

    task_id: str
    success: bool
    summary: str = ""
    tool_calls_count: int = 0
    duration_ms: float = 0.0
    error: str = ""

    def to_agent_text(self) -> str:
        """转为注入主 Agent 上下文的文本"""
        if not self.success:
            return (
                f"[子代理 {self.task_id}] 执行失败: {self.error}\n"
                f"耗时: {self.duration_ms:.0f}ms"
            )
        return (
            f"[子代理 {self.task_id}] 任务完成\n"
            f"工具调用: {self.tool_calls_count} 次\n"
            f"耗时: {self.duration_ms:.0f}ms\n"
            f"结论: {self.summary}"
        )


# ============================================================================
# 子代理编排器
# ============================================================================


class SubAgentOrchestrator:
    """子代理编排器。

    Usage::

        orchestrator = SubAgentOrchestrator(
            llm=agent._llm,
            master_tool_registry=agent.tool_registry,
            workspace_path=agent.workspace_path,
        )
        result = await orchestrator.run_task(
            SubAgentTask(
                description="搜索项目中的所有 Python 文件并列出类定义",
                tools=["read_file", "execute_command"],
            )
        )
    """

    def __init__(
        self,
        *,
        llm,
        master_tool_registry: ToolRegistry,
        workspace_path: str,
        summary_llm=None,
    ):
        """初始化编排器。

        Args:
            llm: 主 Agent 的 LLM 实例（复用连接，子代理不新建客户端）
            master_tool_registry: 主 Agent 的工具注册表（用于按名查找工具）
            workspace_path: 工作空间根路径
            summary_llm: 可选的专用摘要模型（节省成本），默认复用 llm
        """
        self.llm = llm
        self.master_tool_registry = master_tool_registry
        self.workspace_path = workspace_path
        self._summary_llm = summary_llm or llm

        # 统计
        self.total_spawns = 0
        self.total_tool_calls = 0
        self.total_duration_ms = 0.0

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #

    async def run_task(self, task: SubAgentTask) -> SubAgentResult:
        """执行单个子代理任务（阻塞等待结果）。"""
        import time

        t_start = time.perf_counter()
        print(f"🤖 子代理 {task.task_id} 启动: tools={task.tools}, "
              f"max_iter={task.max_iterations}, timeout={task.timeout_seconds}s")

        try:
            # 1. 构建隔离的工具注册表
            isolated_tools = self._build_isolated_tools(task.tools)

            # 2. 构建子代理的系统提示词
            system_prompt = self._build_subagent_system_prompt(task)

            # 3. 创建隔离的子代理实例
            from hello_agents.core.config import Config

            sub_config = Config(
                session_enabled=False,
                compression_threshold=0.8,
                min_retain_rounds=2,
                enable_smart_compression=False,
                context_window=64000,
                subagent_enabled=False,  # 子代理不再递归创建子代理
            )

            sub_agent = EnhancedSimpleAgent(
                name=f"subagent-{task.task_id}",
                llm=self.llm,
                system_prompt=system_prompt,
                config=sub_config,
                tool_registry=isolated_tools if isolated_tools else None,
                enable_tool_calling=bool(task.tools),
                max_tool_iterations=task.max_iterations,
                workspace_root=self.workspace_path,
                auto_cleanup_temp_files=True,
                max_tool_retries=1,  # 子代理少重试，避免超时
            )

            # 4. 在隔离上下文中执行（同步 run，简化实现）
            #    用 run_in_executor 避免阻塞事件循环
            loop = asyncio.get_running_loop()
            raw_response = await asyncio.wait_for(
                loop.run_in_executor(None, sub_agent.run, task.description),
                timeout=task.timeout_seconds,
            )

            # 5. 提取工具调用统计
            tool_calls_count = self._count_tool_calls(sub_agent)

            # 6. 根据 result_mode 生成摘要
            if task.result_mode == SubAgentResultMode.SILENT:
                summary = "（静默任务完成，无输出）"
            elif task.result_mode == SubAgentResultMode.FULL:
                summary = raw_response
            else:
                # SUMMARY 模式：用 LLM 压缩子代理输出
                summary = await self._summarize(raw_response)

            self.total_spawns += 1
            self.total_tool_calls += tool_calls_count
            duration_ms = (time.perf_counter() - t_start) * 1000
            self.total_duration_ms += duration_ms

            logger.info(
                "subagent %s completed: %d tool calls, %.0fms",
                task.task_id, tool_calls_count, duration_ms,
            )

            return SubAgentResult(
                task_id=task.task_id,
                success=True,
                summary=summary,
                tool_calls_count=tool_calls_count,
                duration_ms=duration_ms,
            )

        except asyncio.TimeoutError:
            return SubAgentResult(
                task_id=task.task_id,
                success=False,
                error=f"子代理超时（{task.timeout_seconds}s）",
            )

        except Exception as exc:
            logger.exception("subagent %s failed", task.task_id)
            return SubAgentResult(
                task_id=task.task_id,
                success=False,
                error=str(exc),
            )

    async def parallel_run(self, tasks: List[SubAgentTask]) -> List[SubAgentResult]:
        """并行执行多个子代理任务。

        所有子代理共享同一个 LLM 连接（连接池复用），
        但它们各自的会话上下文完全隔离。

        Returns:
            与输入 tasks 顺序一致的结果列表（失败也包含）
        """
        results: List[Union[SubAgentResult, BaseException]] = await asyncio.gather(
            *(self.run_task(t) for t in tasks),
            return_exceptions=True,
        )

        # 展开异常
        output: List[SubAgentResult] = []
        for task, result in zip(tasks, results):
            if isinstance(result, Exception):
                output.append(SubAgentResult(
                    task_id=task.task_id,
                    success=False,
                    error=f"子代理崩溃: {result}",
                ))
            else:
                output.append(result)

        return output

    def get_stats(self) -> Dict[str, Any]:
        """获取编排器运行统计"""
        return {
            "total_spawns": self.total_spawns,
            "total_tool_calls": self.total_tool_calls,
            "total_duration_ms": self.total_duration_ms,
            "avg_duration_ms": (
                self.total_duration_ms / self.total_spawns
                if self.total_spawns > 0
                else 0
            ),
        }

    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #

    def _build_isolated_tools(self, tool_names: List[str]) -> Optional[ToolRegistry]:
        """从主工具注册表中按白名单提取工具，构建隔离的子注册表。

        - 只暴露 task.tools 中指定的工具
        - 子代理不能访问主代理的 memory/capture/flush 等内部工具
        - 如果 tool_names 为空，返回 None（纯文本子代理）
        """
        if not tool_names:
            return None

        registry = ToolRegistry()
        for name in tool_names:
            tool = self.master_tool_registry.get_tool(name)
            if tool:
                registry.register_tool(tool)
            else:
                logger.warning("subagent tool '%s' not found in master registry", name)

        return registry if registry.list_tools() else None

    @staticmethod
    def _build_subagent_system_prompt(task: SubAgentTask) -> str:
        """构建子代理的系统提示词。

        子代理的系统提示词被精心设计为"单任务、单目标"：
        - 不需要知道主对话的完整上下文
        - 不需要管理记忆
        - 不需要处理多模态
        - 只需聚焦当前任务
        """
        tools_note = ""
        if task.tools:
            tools_note = f" 可用工具: {', '.join(task.tools)}。使用这些工具完成任务。"

        return f"""你是一个专注于单一任务的子代理。你的唯一工作：

任务：{task.description}

规则：
1. 只做任务要求的事，不要偏离主题
2. 完成后输出清晰的结果摘要，不要问"还需要什么"
3. 如果任务无法完成，直接说明原因
4. 不要尝试加载技能、管理记忆、搜索网络（除非任务要求）
{tools_note}"""

    async def _summarize(self, text: str) -> str:
        """用 LLM 将子代理的完整输出压缩为摘要。

        子代理的输出可能包含大量工具返回，但主 Agent 只需要结论。
        """
        # 如果输出很短，不需要压缩
        if len(text) <= 500:
            return text

        prompt = f"""请将以下子代理的执行结果压缩为简洁的结构化摘要：

## 规则
- 输出不超过 300 字
- 只保留关键结论和最终结果
- 去掉冗长的工具输出、命令行回显、重复信息
- 如果执行失败，说明失败原因

## 子代理输出
{text[:8000]}

## 摘要（中文）："""

        try:
            loop = asyncio.get_running_loop()
            # 使用轻量 LLM 生成摘要
            resp = await loop.run_in_executor(
                None,
                lambda: self._summary_llm.invoke(
                    [
                        {"role": "system", "content": "你是专业的摘要助手。输出简洁、准确。"},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.1,
                    max_tokens=400,
                ),
            )
            # 记录子代理摘要调用的 token 用量（此前是完全的统计盲区）
            log_response_safe(
                resp,
                model=str(getattr(self._summary_llm, "model", "") or ""),
                call_site="subagent_summary",
                agent_name="subagent",
            )
            result = resp.content if hasattr(resp, "content") else str(resp)
            return result.strip()
        except Exception:
            # 压缩失败 → 截断返回
            return text[:1000] + "\n...（输出过长已截断）"

    @staticmethod
    def _count_tool_calls(sub_agent: EnhancedSimpleAgent) -> int:
        """从子代理的历史中统计工具调用次数。"""
        count = 0
        for msg in sub_agent._history:
            metadata = getattr(msg, "metadata", None) or {}
            if metadata.get("tool_calls"):
                count += 1
        return count
