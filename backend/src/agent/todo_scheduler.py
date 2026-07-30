"""DAG 调度器 — Plan 模式的结构化 TODO 执行调度。

在 Plan 模式下，LLM 在规划期生成结构化 TODO（含依赖关系和所需工具），
用户确认后进入执行期。TodoScheduler 负责按 DAG 依赖顺序调度任务执行。

核心能力：
- 从结构化 JSON 加载 TODO 列表
- 依赖解析：仅当所有依赖项 completed 时任务才进入 ready 队列
- 失败阻塞：任务失败时阻塞所有下游依赖任务
- 偏离检测：检测当前行动是否偏离计划

设计参考 Claude Code v2 的 TODO 系统：
    {id, description, status, dependencies[], tools_required[]}
构建 DAG 并按拓扑序执行。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# 数据模型
# ============================================================================


@dataclass
class TodoItem:
    """Plan 模式的结构化 TODO 项。

    与 TaskTracker.Task 的区别：
    - TodoItem 是 Plan 生成的计划项，含 tools_required
    - Task 是通用任务追踪，TodoItem 确认后可同步到 TaskTracker
    """

    id: str
    description: str
    status: Literal["pending", "ready", "in_progress", "completed", "failed"] = "pending"
    dependencies: List[str] = field(default_factory=list)
    tools_required: List[str] = field(default_factory=list)

    @property
    def is_ready(self) -> bool:
        """任务是否可执行（仅检查无依赖场景）。

        注意：此属性无法感知依赖完成状态，仅供简单场景使用。
        完整的就绪判断请使用 TodoScheduler.get_ready_tasks()。
        """
        return self.status in ("pending", "ready") and len(self.dependencies) == 0

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。"""
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "dependencies": self.dependencies,
            "tools_required": self.tools_required,
        }


# ============================================================================
# DAG 调度器
# ============================================================================


class TodoScheduler:
    """DAG 调度器 — 管理 TodoItem 列表，按依赖顺序调度执行。

    Usage::

        scheduler = TodoScheduler()
        scheduler.load_from_plan(todo_json)

        # 获取可执行的任务
        ready = scheduler.get_ready_tasks()

        # 标记任务完成
        scheduler.mark_completed("todo_1")

        # 任务失败 → 阻塞下游
        scheduler.fail_downstream("todo_2")
    """

    def __init__(self):
        self._todos: Dict[str, TodoItem] = {}
        self._plan_text: str = ""

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #

    def load_from_plan(self, plan_data: Any) -> List[TodoItem]:
        """从结构化 TODO JSON 加载计划。

        Args:
            plan_data: 可以是 list[dict] 或 JSON 字符串。
                        每项格式：{id?, description, dependencies?, tools_required?}

        Returns:
            加载的 TodoItem 列表
        """
        if isinstance(plan_data, str):
            plan_data = json.loads(plan_data)

        if not isinstance(plan_data, list):
            raise ValueError(f"期望 list 格式的计划数据，得到 {type(plan_data)}")

        self._todos.clear()

        for i, item in enumerate(plan_data):
            if not isinstance(item, dict):
                continue

            todo_id = item.get("id") or f"todo_{i+1}"
            description = item.get("description", "").strip()
            if not description:
                continue

            dependencies = item.get("dependencies", [])
            tools_required = item.get("tools_required", [])

            todo = TodoItem(
                id=todo_id,
                description=description,
                dependencies=list(dependencies),
                tools_required=list(tools_required),
            )
            self._todos[todo_id] = todo

        # 初始化 ready 状态
        self._update_ready_status()

        logger.info("TodoScheduler 加载 %d 个 TODO 项", len(self._todos))
        return list(self._todos.values())

    def set_plan_text(self, text: str) -> None:
        """设置计划的文本表示（用于注入系统提示词）。"""
        self._plan_text = text

    @property
    def plan_text(self) -> str:
        return self._plan_text

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def get_ready_tasks(self) -> List[TodoItem]:
        """返回所有依赖已满足的 pending/ready 任务。"""
        completed = {
            tid for tid, t in self._todos.items()
            if t.status == "completed"
        }
        ready: List[TodoItem] = []
        for todo in self._todos.values():
            if todo.status in ("pending", "ready"):
                if all(dep in completed for dep in todo.dependencies):
                    ready.append(todo)
        return ready

    def get_all(self) -> List[TodoItem]:
        """获取所有 TODO 项。"""
        return list(self._todos.values())

    def get_by_id(self, todo_id: str) -> Optional[TodoItem]:
        """按 ID 获取 TODO 项。"""
        return self._todos.get(todo_id)

    def get_progress_summary(self) -> str:
        """生成可供注入系统提示词的进度摘要。"""
        if not self._todos:
            return ""

        all_todos = list(self._todos.values())
        completed = sum(1 for t in all_todos if t.status == "completed")
        total = len(all_todos)

        lines = [
            f"## 计划执行进度 ({completed}/{total})",
            "",
        ]

        in_progress = [t for t in all_todos if t.status == "in_progress"]
        ready = self.get_ready_tasks()
        failed = [t for t in all_todos if t.status == "failed"]
        pending = [
            t for t in all_todos
            if t.status == "pending" and t not in ready
        ]

        if self._plan_text:
            lines.append(f"**原始计划：**\n{self._plan_text}\n")

        if in_progress:
            lines.append("**进行中：**")
            for t in in_progress:
                lines.append(f"- 🔄 [{t.id}] {t.description}")
            lines.append("")

        if ready:
            lines.append("**可执行：**")
            for t in ready:
                tools = f" [工具: {', '.join(t.tools_required)}]" if t.tools_required else ""
                lines.append(f"- ▶️ [{t.id}] {t.description}{tools}")
            lines.append("")

        if pending:
            lines.append("**等待依赖：**")
            for t in pending:
                deps = ", ".join(t.dependencies)
                lines.append(f"- ⏳ [{t.id}] {t.description} ← 等待: {deps}")
            lines.append("")

        if failed:
            lines.append("**失败：**")
            for t in failed:
                lines.append(f"- ❌ [{t.id}] {t.description}")
            lines.append("")

        if completed > 0:
            lines.append(f"已完成 {completed}/{total} 个任务。")

        # 执行指导
        if ready:
            next_task = ready[0]
            lines.append(
                f"\n**下一步：** 执行 [{next_task.id}] {next_task.description}"
            )

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 状态变更
    # ------------------------------------------------------------------ #

    def mark_in_progress(self, todo_id: str) -> Optional[TodoItem]:
        """标记任务为进行中。"""
        todo = self._todos.get(todo_id)
        if not todo:
            return None
        todo.status = "in_progress"
        return todo

    def mark_completed(self, todo_id: str) -> Optional[TodoItem]:
        """标记任务为已完成。"""
        todo = self._todos.get(todo_id)
        if not todo:
            return None
        todo.status = "completed"
        # 更新下游任务的 ready 状态
        self._update_ready_status()
        return todo

    def mark_failed(self, todo_id: str, reason: str = "") -> None:
        """标记任务为失败，并阻塞所有下游依赖。"""
        todo = self._todos.get(todo_id)
        if not todo:
            return
        todo.status = "failed"
        # 阻塞下游
        self.fail_downstream(todo_id)
        logger.warning("TODO %s 失败: %s — 下游已阻塞", todo_id, reason)

    def fail_downstream(self, todo_id: str) -> None:
        """任务失败 → 阻塞所有下游依赖任务（迭代式 BFS，避免栈溢出）。

        将所有直接或间接依赖 todo_id 的任务标记为 failed。
        """
        queue: List[str] = [todo_id]
        visited: set = set()

        while queue:
            current_id = queue.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)

            for tid, todo in self._todos.items():
                if current_id in todo.dependencies and todo.status in ("pending", "ready"):
                    todo.status = "failed"
                    queue.append(tid)

    # ------------------------------------------------------------------ #
    # 偏离检测
    # ------------------------------------------------------------------ #

    def check_plan_divergence(self, current_action: str) -> bool:
        """[实验性] 检测当前行动是否偏离计划。

        当前实现基于简单关键词匹配，存在局限性：
        - 中文描述无法正确分词
        - 工具名为子字符串匹配，可能误判
        后续版本将改用语义相似度检测。

        Args:
            current_action: 当前 LLM 正在执行的动作描述（工具调用名+参数摘要）

        Returns:
            True 表示偏离计划
        """
        ready = self.get_ready_tasks()
        if not ready:
            # 没有 ready 任务但还有 pending → 可能是还在执行当前任务
            pending = [t for t in self._todos.values() if t.status in ("pending", "ready", "in_progress")]
            if not pending:
                # 所有任务已完成，不算偏离
                return False
            return False  # 有正在执行的任务，不算偏离

        # 检查 current_action 是否匹配某个 ready 任务
        action_lower = current_action.lower()
        for task in ready:
            # 简单关键词匹配：任务描述中的关键词出现在 current_action 中
            keywords = [w for w in task.description.lower().split() if len(w) > 2]
            if any(kw in action_lower for kw in keywords):
                return False

        # 也检查工具名是否匹配
        for task in ready:
            for tool in task.tools_required:
                if tool.lower() in action_lower:
                    return False

        return True

    # ------------------------------------------------------------------ #
    # 序列化
    # ------------------------------------------------------------------ #

    def to_plan_json(self) -> List[Dict[str, Any]]:
        """序列化为 JSON 可传输的格式。"""
        return [todo.to_dict() for todo in self._todos.values()]

    def is_all_completed(self) -> bool:
        """所有任务是否已完成。"""
        if not self._todos:
            return True
        return all(t.status == "completed" for t in self._todos.values())

    def has_failed(self) -> bool:
        """是否有任务失败。"""
        return any(t.status == "failed" for t in self._todos.values())

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _update_ready_status(self) -> None:
        """更新所有 pending 任务的 ready 状态。

        依赖已满足的 pending 任务标记为 ready。
        """
        completed = {
            tid for tid, t in self._todos.items()
            if t.status == "completed"
        }
        for todo in self._todos.values():
            if todo.status == "pending":
                if all(dep in completed for dep in todo.dependencies):
                    todo.status = "ready"

    def clear(self) -> None:
        """清空所有 TODO。"""
        self._todos.clear()
        self._plan_text = ""
