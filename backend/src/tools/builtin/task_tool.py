"""任务管理工具 —— 将 TaskTracker 暴露给 Agent 的 ReAct 循环。

让 Agent 可以通过工具调用来创建、推进、完成结构化任务。
"""

from __future__ import annotations

from typing import Any, Dict, List, TYPE_CHECKING

from hello_agents.tools import Tool, ToolParameter, ToolResponse

if TYPE_CHECKING:
    from ...agent.task_tracker import TaskTracker


class TaskTool(Tool):
    """任务管理工具。

    主 Agent 通过以下动作管理结构化任务：
    - task_create: 创建新任务
    - task_start: 标记任务为进行中
    - task_complete: 标记任务为已完成
    - task_list: 列出所有任务
    - task_progress: 查看进度摘要
    """

    def __init__(self, tracker: "TaskTracker"):
        super().__init__(
            name="task",
            description=(
                "结构化任务管理工具。用于分解复杂请求为可追踪的步骤。\n\n"
                "**何时使用（必须）**：\n"
                "- 用户请求包含 3 个以上独立步骤时\n"
                "- 需要按特定顺序完成多个步骤时\n"
                "- 多个步骤之间存在依赖关系时（如 A 的结果决定 B）\n\n"
                "**可用动作**：\n"
                "- task_create: 创建新任务\n"
                "- task_start: 开始一个任务\n"
                "- task_complete: 完成任务\n"
                "- task_fail: 标记任务失败\n"
                "- task_cancel: 取消任务\n"
                "- task_list: 列出所有任务\n"
                "- task_progress: 查看进度摘要"
            ),
            expandable=True,
        )
        self.tracker = tracker
        # 工具元数据（供 ContextGuard 动态路由）
        self.output_size_hint = 300
        self.has_side_effects = True  # 元工具，不可委托

    # ------------------------------------------------------------------ #
    # 参数定义
    # ------------------------------------------------------------------ #

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description=(
                    "操作类型。必须为以下之一: "
                    "task_create, task_start, task_complete, tool_fail, task_cancel, "
                    "task_list, task_progress"
                ),
                required=True,
            ),
            ToolParameter(
                name="subject",
                type="string",
                description="任务标题（task_create 时必填），简短描述，如 '修复登录认证bug'",
                required=False,
            ),
            ToolParameter(
                name="description",
                type="string",
                description="任务详细描述（task_create 时可选）",
                required=False,
            ),
            ToolParameter(
                name="task_id",
                type="string",
                description="任务 ID（task_start/task_complete/task_fail/task_cancel 时必填）",
                required=False,
            ),
            ToolParameter(
                name="depends_on",
                type="string",
                description=(
                    "依赖的任务 ID 列表，逗号分隔（task_create 时可选）。"
                    '例如: "task_1, task_2" 表示这些任务必须先完成'
                ),
                required=False,
            ),
            ToolParameter(
                name="notes",
                type="string",
                description="备注（task_complete/task_fail/task_cancel 时可选），如 '已修复并通过测试'",
                required=False,
            ),
        ]

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        action = (parameters.get("action") or "").strip()

        handlers = {
            "task_create": self._handle_create,
            "task_start": self._handle_start,
            "task_complete": self._handle_complete,
            "task_fail": self._handle_fail,
            "task_cancel": self._handle_cancel,
            "task_list": self._handle_list,
            "task_progress": self._handle_progress,
        }

        handler = handlers.get(action)
        if not handler:
            return ToolResponse.error(
                "INVALID_ACTION",
                f"不支持的操作: {action}。可用: {', '.join(handlers.keys())}",
            )

        try:
            return handler(parameters)
        except ValueError as exc:
            return ToolResponse.error("TASK_ERROR", str(exc))

    # ------------------------------------------------------------------ #
    # 动作处理
    # ------------------------------------------------------------------ #

    def _handle_create(self, params: Dict[str, Any]) -> ToolResponse:
        subject = (params.get("subject") or "").strip()
        if not subject:
            return ToolResponse.error("MISSING_SUBJECT", "task_create 缺少 subject")

        description = (params.get("description") or "").strip()
        depends_on = self._parse_id_list(params.get("depends_on", ""))

        try:
            task = self.tracker.create(
                subject=subject,
                description=description,
                blocked_by=depends_on,
            )
            deps_info = ""
            if depends_on:
                deps_info = f"（依赖: {', '.join(depends_on)}）"

            return ToolResponse.success(
                text=f"创建任务 [{task.id}] {task.subject}{deps_info}",
                data={
                    "task_id": task.id,
                    "subject": task.subject,
                },
            )
        except ValueError as exc:
            return ToolResponse.error("CREATE_ERROR", str(exc))

    def _handle_start(self, params: Dict[str, Any]) -> ToolResponse:
        task_id = (params.get("task_id") or "").strip()
        if not task_id:
            return ToolResponse.error("MISSING_TASK_ID", "缺少 task_id")

        try:
            task = self.tracker.start(task_id)
            return ToolResponse.success(
                text=f"开始任务 [{task.id}] {task.subject}",
                data={"task_id": task.id, "status": "in_progress"},
            )
        except ValueError as exc:
            return ToolResponse.error("START_ERROR", str(exc))

    def _handle_complete(self, params: Dict[str, Any]) -> ToolResponse:
        task_id = (params.get("task_id") or "").strip()
        if not task_id:
            return ToolResponse.error("MISSING_TASK_ID", "缺少 task_id")

        notes = (params.get("notes") or "").strip()

        try:
            task = self.tracker.complete(task_id, notes=notes)

            # 检查是否有被解除阻塞的任务
            next_task = self.tracker.get_next_available()
            next_hint = ""
            if next_task:
                next_hint = f"\n下一个可开始的任务: [{next_task.id}] {next_task.subject}"

            return ToolResponse.success(
                text=f"完成 [{task.id}] {task.subject}{next_hint}",
                data={
                    "task_id": task.id,
                    "status": "completed",
                    "next_available": next_task.id if next_task else None,
                },
            )
        except ValueError as exc:
            return ToolResponse.error("COMPLETE_ERROR", str(exc))

    def _handle_fail(self, params: Dict[str, Any]) -> ToolResponse:
        task_id = (params.get("task_id") or "").strip()
        if not task_id:
            return ToolResponse.error("MISSING_TASK_ID", "缺少 task_id")

        reason = (params.get("notes") or "执行失败").strip()

        try:
            self.tracker.fail(task_id, reason=reason)
            return ToolResponse.success(
                text=f"任务 [{task_id}] 标记为失败: {reason}",
                data={"task_id": task_id, "status": "failed"},
            )
        except ValueError as exc:
            return ToolResponse.error("FAIL_ERROR", str(exc))

    def _handle_cancel(self, params: Dict[str, Any]) -> ToolResponse:
        task_id = (params.get("task_id") or "").strip()
        if not task_id:
            return ToolResponse.error("MISSING_TASK_ID", "缺少 task_id")

        reason = (params.get("notes") or "").strip()

        try:
            self.tracker.cancel(task_id, reason=reason)
            return ToolResponse.success(
                text=f"任务 [{task_id}] 已取消" + (f": {reason}" if reason else ""),
                data={"task_id": task_id, "status": "cancelled"},
            )
        except ValueError as exc:
            return ToolResponse.error("CANCEL_ERROR", str(exc))

    def _handle_list(self, _params: Dict[str, Any]) -> ToolResponse:
        all_tasks = self.tracker.list_all()

        if not all_tasks:
            return ToolResponse.success(
                text="当前无任务",
                data={"tasks": [], "count": 0},
            )

        status_icons = {
            "pending": "⏳",
            "in_progress": "🔄",
            "completed": "✅",
            "failed": "❌",
            "cancelled": "🚫",
        }

        lines = [f"共 {len(all_tasks)} 个任务：", ""]
        for t in all_tasks:
            icon = status_icons.get(t.status.value, "❓")
            deps = f" ← 等待: {', '.join(t.blocked_by)}" if t.blocked_by else ""
            lines.append(f"{icon} [{t.id}] {t.subject} ({t.status.value}){deps}")

        return ToolResponse.success(
            text="\n".join(lines),
            data={
                "tasks": [
                    {
                        "id": t.id,
                        "subject": t.subject,
                        "status": t.status.value,
                        "blocked_by": t.blocked_by,
                    }
                    for t in all_tasks
                ],
                "count": len(all_tasks),
            },
        )

    def _handle_progress(self, _params: Dict[str, Any]) -> ToolResponse:
        summary = self.tracker.get_progress_summary()
        if not summary:
            return ToolResponse.success(
                text="当前无任务",
                data={"has_tasks": False},
            )

        return ToolResponse.success(
            text=summary,
            data={"has_tasks": True},
        )

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_id_list(raw: Any) -> List[str]:
        """解析 ID 列表（支持逗号分隔字符串或列表）。"""
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        if isinstance(raw, str):
            return [x.strip() for x in raw.split(",") if x.strip()]
        return []
