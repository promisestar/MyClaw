"""SubAgent 工具 —— 让主 Agent 可以 spawn 子代理处理复杂任务。

作为 Tool 暴露给主 Agent 的 ReAct 循环。主 Agent 调用本工具时，
内部启动一个上下文隔离的子代理执行任务，只返回摘要结果。
"""

from __future__ import annotations

from typing import Any, Dict, List, TYPE_CHECKING

from hello_agents.tools import Tool, ToolParameter, ToolResponse

if TYPE_CHECKING:
    from ...agent.subagent_orchestrator import SubAgentOrchestrator


class SubAgentTool(Tool):
    """子代理启动工具。

    主 Agent 通过调用此工具，将复杂子任务委托给上下文隔离的子代理。
    子代理的结果以摘要形式注入主上下文，避免工具输出的数据污染。

    可用动作：
    - spawn: 启动一个子代理执行任务
    - parallel_spawn: 并行启动多个子代理
    """

    def __init__(self, orchestrator: "SubAgentOrchestrator"):
        super().__init__(
            name="subagent",
            description=(
                "启动子代理在隔离上下文中独立完成复杂任务。子代理的结果仅以摘要形式"
                "返回，不会把详细的工具输出塞进主对话。\n\n"
                "**何时使用（必须）**：\n"
                "- 需要搜索/读取大量文件时（缓解主上下文压力）\n"
                "- 需要执行多步数据处理管道时\n"
                "- 需要在独立空间做实验（修改文件、运行命令）\n"
                "- 多个可并行的子任务（一次调用 parallel_spawn）\n\n"
                "**何时不用**：\n"
                "- 简单的单次工具调用（如读取一个已知小文件、一次简单计算）\n"
                "- 用户明确要求实时看到中间过程的任务\n\n"
                "**动作说明**：\n"
                "- spawn: 启动单个子代理\n"
                "- parallel_spawn: 并行启动多个子代理（推荐用于无依赖的子任务）"
            ),
            expandable=True,
        )
        self.orchestrator = orchestrator
        # 工具元数据（供 ContextGuard 动态路由）
        self.output_size_hint = 500
        self.has_side_effects = True  # 递归工具，不可委托

    # ------------------------------------------------------------------ #
    # 参数定义
    # ------------------------------------------------------------------ #

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description="操作类型：spawn（单子代理）或 parallel_spawn（并行多子代理）",
                required=True,
            ),
            ToolParameter(
                name="task",
                type="string",
                description=(
                    "任务描述（action=spawn 时必填）。"
                    "用自然语言描述子代理要完成的任务，越具体越好。"
                    '例如：搜索 src/ 目录下所有包含 TODO 的 Python 文件，'
                    "列出文件名和 TODO 内容"
                ),
                required=False,
            ),
            ToolParameter(
                name="tools",
                type="string",
                description=(
                    "子代理可用的工具列表，逗号分隔。"
                    '可选值：read_file, write_file, edit_file, execute_command, '
                    "web_search, web_fetch, memory_search, calculator 等。\n"
                    "如不指定，子代理将无法使用任何工具（纯文本推理）。\n"
                    '例如："read_file, execute_command, web_search"'
                ),
                required=False,
            ),
            ToolParameter(
                name="tasks_json",
                type="string",
                description=(
                    "并行任务列表（action=parallel_spawn 时必填）。"
                    "JSON 数组格式，每个元素包含 task, tools 字段。\n"
                    '例如：[{"task":"搜索 auth 相关文件","tools":"read_file,execute_command"},'
                    '{"task":"检查前端路由","tools":"read_file"}]'
                ),
                required=False,
            ),
        ]

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        """同步入口（由 EnhancedSimpleAgent 的同步 run 路径调用）。

        通过 asyncio 桥接到异步实现。
        """
        import asyncio

        action = (parameters.get("action") or "").strip()
        if not action:
            return ToolResponse.error("MISSING_ACTION", "缺少 action 参数")

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 事件循环已在运行 → 用 run_coroutine_threadsafe 或创建新 loop
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, self._async_run(parameters))
                    return future.result(timeout=120)
            else:
                return loop.run_until_complete(self._async_run(parameters))
        except RuntimeError:
            # 没有运行中的事件循环
            return asyncio.run(self._async_run(parameters))

    async def _async_run(self, parameters: Dict[str, Any]) -> ToolResponse:
        from ...agent.subagent_orchestrator import SubAgentTask, SubAgentResultMode

        action = (parameters.get("action") or "").strip()

        if action == "spawn":
            return await self._handle_spawn(parameters)
        elif action == "parallel_spawn":
            return await self._handle_parallel_spawn(parameters)
        else:
            return ToolResponse.error(
                "INVALID_ACTION",
                f"不支持的操作: {action}。可用: spawn, parallel_spawn",
            )

    async def _handle_spawn(self, parameters: Dict[str, Any]) -> ToolResponse:
        from ...agent.subagent_orchestrator import SubAgentTask, SubAgentResultMode

        task_desc = (parameters.get("task") or "").strip()
        if not task_desc:
            return ToolResponse.error("MISSING_TASK", "spawn 动作缺少 task 参数")

        tool_names = self._parse_tool_list(parameters.get("tools", ""))

        sub_task = SubAgentTask(
            description=task_desc,
            tools=tool_names,
            result_mode=SubAgentResultMode.SUMMARY,
        )

        result = await self.orchestrator.run_task(sub_task)
        return ToolResponse.success(
            text=result.to_agent_text(),
            data={
                "task_id": result.task_id,
                "success": result.success,
                "tool_calls": result.tool_calls_count,
                "duration_ms": result.duration_ms,
            },
        )

    async def _handle_parallel_spawn(self, parameters: Dict[str, Any]) -> ToolResponse:
        import json

        from ...agent.subagent_orchestrator import SubAgentTask, SubAgentResultMode

        raw = (parameters.get("tasks_json") or "").strip()
        if not raw:
            return ToolResponse.error(
                "MISSING_TASKS", "parallel_spawn 动作缺少 tasks_json 参数"
            )

        try:
            task_specs = json.loads(raw)
        except json.JSONDecodeError as exc:
            return ToolResponse.error(
                "INVALID_JSON", f"tasks_json 格式错误: {exc}"
            )

        if not isinstance(task_specs, list) or not task_specs:
            return ToolResponse.error(
                "INVALID_TASKS", "tasks_json 必须是非空数组"
            )

        tasks: List[SubAgentTask] = []
        for i, spec in enumerate(task_specs):
            if not isinstance(spec, dict):
                return ToolResponse.error(
                    "INVALID_TASK_SPEC", f"第 {i+1} 个任务格式无效（应为对象）"
                )
            task_desc = (spec.get("task") or f"Task #{i+1}").strip()
            tool_names = self._parse_tool_list(spec.get("tools", ""))
            tasks.append(SubAgentTask(
                description=task_desc,
                tools=tool_names,
                result_mode=SubAgentResultMode.SUMMARY,
            ))

        results = await self.orchestrator.parallel_run(tasks)

        # 组装返回文本
        lines = [f"并行执行 {len(results)} 个子代理任务：", ""]
        success_count = 0
        for r in results:
            lines.append(r.to_agent_text())
            lines.append("---")
            if r.success:
                success_count += 1

        summary_line = f"\n完成: {success_count}/{len(results)} 个任务成功"
        lines.append(summary_line)

        return ToolResponse.success(
            text="\n".join(lines),
            data={
                "total": len(results),
                "success": success_count,
                "failed": len(results) - success_count,
                "results": [
                    {
                        "task_id": r.task_id,
                        "success": r.success,
                        "summary": r.summary,
                        "tool_calls": r.tool_calls_count,
                        "duration_ms": r.duration_ms,
                    }
                    for r in results
                ],
            },
        )

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_tool_list(raw: Any) -> List[str]:
        """解析工具列表（支持逗号分隔字符串或列表）。"""
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        if isinstance(raw, str):
            return [x.strip() for x in raw.split(",") if x.strip()]
        return []
