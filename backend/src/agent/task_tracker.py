"""任务追踪器 —— 结构化任务管理。

借鉴 WorkBuddy 的 Task 系统（pending → in_progress → completed + 依赖阻塞），
为 MyClaw 提供显式的多步任务追踪能力。

核心能力：
- 任务生命周期管理（创建/开始/完成/取消）
- 依赖关系（blocked_by / blocks）
- 进度摘要（供 Memory Flush / 系统提示词注入）
- 轻量持久化（可选，写入工作空间 JSON 文件）

设计原则：
- 纯内存状态，零外部依赖
- 可选持久化（会话关闭时保存，下次打开时恢复）
- 不替代 ReAct 循环，而是增强其可追踪性
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# 数据模型
# ============================================================================


class TaskStatus(str, Enum):
    """任务状态"""

    PENDING = "pending"       # 待开始
    IN_PROGRESS = "in_progress"  # 进行中
    COMPLETED = "completed"   # 已完成
    FAILED = "failed"         # 失败
    CANCELLED = "cancelled"   # 已取消


@dataclass
class Task:
    """单个任务"""

    id: str
    subject: str                              # 简短标题（如 "修复登录模块认证 bug"）
    description: str = ""                     # 详细描述
    status: TaskStatus = TaskStatus.PENDING
    blocked_by: List[str] = field(default_factory=list)   # 依赖的任务 ID 列表
    blocks: List[str] = field(default_factory=list)       # 被哪些任务依赖
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    owner: str = ""                           # 分派给哪个子代理（可选）
    notes: str = ""                           # 备注（完成/失败时补充）
    tools_required: List[str] = field(default_factory=list)  # 完成任务需要的工具列表（Plan 模式）

    @property
    def is_blocked(self) -> bool:
        """该任务是否被阻塞（依赖未完成）"""
        return len(self.blocked_by) > 0

    @property
    def is_ready(self) -> bool:
        """该任务是否可以开始（pending + 无阻塞依赖）"""
        return self.status == TaskStatus.PENDING and not self.blocked_by

    @property
    def duration_seconds(self) -> Optional[float]:
        """任务耗时（秒）"""
        if self.started_at is None:
            return None
        end = self.completed_at or time.time()
        return end - self.started_at


# ============================================================================
# 任务追踪器
# ============================================================================


class TaskTracker:
    """任务追踪器。

    管理当前会话的任务列表，支持 CRUD + 依赖解析 + 持久化。

    Usage::

        tracker = TaskTracker()

        # 创建任务
        tracker.create("修复登录认证 bug", "JWT token 刷新逻辑有误")
        tracker.create("重构 UserService", blocked_by=["task_1"])

        # 开始任务
        tracker.start("task_1")

        # 完成并解除阻塞
        tracker.complete("task_1", notes="已修复，通过测试")
        # → task_2 的 blocked_by 自动清除

        # 获取进度
        print(tracker.get_progress_summary())
    """

    MAX_TASKS = 20              # 单会话最大任务数
    PERSIST_DIRNAME = "tasks"   # 持久化子目录名

    def __init__(self, persist_dir: Optional[str] = None):
        """初始化追踪器。

        Args:
            persist_dir: 可选的持久化目录路径。传入后，save() 和 load() 可用。
        """
        self._tasks: Dict[str, Task] = {}
        self._persist_dir = persist_dir

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #

    def create(
        self,
        subject: str,
        description: str = "",
        *,
        blocked_by: Optional[List[str]] = None,
        task_id: Optional[str] = None,
    ) -> Task:
        """创建新任务。

        Args:
            subject: 任务标题（简短，如 "修复登录认证 bug"）
            description: 详细描述
            blocked_by: 依赖的任务 ID 列表（这些任务必须先完成）
            task_id: 自定义任务 ID（默认自动生成）

        Returns:
            创建的 Task 对象

        Raises:
            ValueError: 如果超过 MAX_TASKS 限制
        """
        if len(self._tasks) >= self.MAX_TASKS:
            raise ValueError(
                f"单会话最多 {self.MAX_TASKS} 个任务。请先完成或取消旧任务。"
            )

        tid = task_id or f"task_{str(uuid.uuid4())[:8]}"
        if tid in self._tasks:
            raise ValueError(f"任务 ID '{tid}' 已存在")

        task = Task(
            id=tid,
            subject=subject.strip(),
            description=description.strip(),
            blocked_by=list(blocked_by or []),
        )

        # 反向建立 blocks 关系
        for dep_id in task.blocked_by:
            dep = self._tasks.get(dep_id)
            if dep:
                dep.blocks.append(tid)

        self._tasks[tid] = task
        logger.info("task created: %s — %s", tid, subject)
        return task

    def create_batch(
        self,
        tasks: List[Dict[str, Any]],
    ) -> List[Task]:
        """批量创建任务（Plan 模式确认后加载 TODO 用）。

        Args:
            tasks: 任务列表，每项含 subject, description?, blocked_by?, tools_required?

        Returns:
            创建的 Task 对象列表
        """
        created: List[Task] = []
        for item in tasks:
            task = self.create(
                subject=item.get("subject", item.get("description", "")),
                description=item.get("description", ""),
                blocked_by=item.get("blocked_by") or item.get("dependencies"),
            )
            task.tools_required = item.get("tools_required", [])
            created.append(task)
        return created

    def start(self, task_id: str) -> Task:
        """将任务标记为进行中。

        自动检查：如果任务有未完成的依赖，拒绝开始。
        """
        task = self._get(task_id)
        if task.status == TaskStatus.IN_PROGRESS:
            return task  # 幂等

        if task.status != TaskStatus.PENDING:
            raise ValueError(f"任务 '{task_id}' 状态为 {task.status.value}，无法开始")

        # 检查依赖
        for dep_id in task.blocked_by:
            dep = self._tasks.get(dep_id)
            if dep and dep.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
                raise ValueError(
                    f"任务 '{task_id}' 依赖 '{dep_id}'（状态: {dep.status.value}），"
                    f"请先完成依赖任务"
                )

        task.status = TaskStatus.IN_PROGRESS
        task.started_at = time.time()
        logger.info("task started: %s", task_id)
        return task

    def complete(self, task_id: str, *, notes: str = "") -> Task:
        """将任务标记为已完成。

        副作用：自动解除所有依赖此任务的其他任务的阻塞。
        """
        task = self._get(task_id)
        if task.status == TaskStatus.COMPLETED:
            return task  # 幂等

        task.status = TaskStatus.COMPLETED
        task.completed_at = time.time()
        if notes:
            task.notes = notes

        # 解除所有被阻塞的任务
        unblocked_count = 0
        for blocked_id in task.blocks:
            blocked = self._tasks.get(blocked_id)
            if blocked and task_id in blocked.blocked_by:
                blocked.blocked_by.remove(task_id)
                unblocked_count += 1

        task.blocks.clear()
        logger.info(
            "task completed: %s (unblocked %d tasks)", task_id, unblocked_count,
        )
        return task

    def fail(self, task_id: str, *, reason: str = ""):
        """将任务标记为失败。"""
        task = self._get(task_id)
        task.status = TaskStatus.FAILED
        task.completed_at = time.time()
        task.notes = reason or "任务执行失败"
        logger.info("task failed: %s — %s", task_id, reason)

    def cancel(self, task_id: str, *, reason: str = ""):
        """取消任务。"""
        task = self._get(task_id)
        task.status = TaskStatus.CANCELLED
        task.completed_at = time.time()
        task.notes = reason or "任务已取消"

        # 同时解除阻塞
        for blocked_id in task.blocks:
            blocked = self._tasks.get(blocked_id)
            if blocked and task_id in blocked.blocked_by:
                blocked.blocked_by.remove(task_id)
        task.blocks.clear()

        logger.info("task cancelled: %s — %s", task_id, reason)

    def get(self, task_id: str) -> Optional[Task]:
        """按 ID 获取任务。"""
        return self._tasks.get(task_id)

    def list_all(self) -> List[Task]:
        """列出所有任务（按创建时间排序）。"""
        return sorted(self._tasks.values(), key=lambda t: t.created_at)

    def list_ready(self) -> List[Task]:
        """列出所有可开始的任务（pending + 无阻塞）。"""
        return [t for t in self._tasks.values() if t.is_ready]

    def list_blocked(self) -> List[Task]:
        """列出所有被阻塞的任务。"""
        return [t for t in self._tasks.values() if t.is_blocked]

    def list_active(self) -> List[Task]:
        """列出活跃任务（pending + in_progress）。"""
        return [
            t for t in self._tasks.values()
            if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)
        ]

    # ------------------------------------------------------------------ #
    # 进度摘要
    # ------------------------------------------------------------------ #

    def get_progress_summary(self) -> str:
        """生成可供注入系统提示词的进度摘要。

        Returns:
            格式化的进度文本，如果没有任务则返回空字符串。
        """
        if not self._tasks:
            return ""

        all_tasks = self.list_all()
        completed = sum(1 for t in all_tasks if t.status == TaskStatus.COMPLETED)
        total = len(all_tasks)

        lines = [
            f"## 任务进度 ({completed}/{total})",
            "",
        ]

        # 按状态分组
        in_progress = [t for t in all_tasks if t.status == TaskStatus.IN_PROGRESS]
        pending = [t for t in all_tasks if t.status == TaskStatus.PENDING]
        failed = [t for t in all_tasks if t.status == TaskStatus.FAILED]

        if in_progress:
            lines.append("**进行中：**")
            for t in in_progress:
                lines.append(f"- 🔄 [{t.id}] {t.subject}")
            lines.append("")

        ready = [t for t in pending if not t.is_blocked]
        blocked = [t for t in pending if t.is_blocked]

        if ready:
            lines.append("**待开始：**")
            for t in ready:
                lines.append(f"- ⏳ [{t.id}] {t.subject}")
            lines.append("")

        if blocked:
            lines.append("**被阻塞：**")
            for t in blocked:
                deps = ", ".join(t.blocked_by)
                lines.append(f"- 🚫 [{t.id}] {t.subject} ← 等待: {deps}")
            lines.append("")

        if failed:
            lines.append("**失败：**")
            for t in failed:
                lines.append(f"- ❌ [{t.id}] {t.subject}: {t.notes[:80]}")
            lines.append("")

        if completed > 0:
            lines.append(f"已完成 {completed} 个任务。继续推进剩余任务。")

        return "\n".join(lines)

    def get_next_available(self) -> Optional[Task]:
        """获取下一个可以开始的任务（pending + 无阻塞 + 最早创建）。"""
        ready = self.list_ready()
        return ready[0] if ready else None

    # ------------------------------------------------------------------ #
    # 持久化（可选）
    # ------------------------------------------------------------------ #

    def save(self, session_id: str) -> bool:
        """将当前任务列表持久化到磁盘。

        Args:
            session_id: 会话 ID，用于生成文件名

        Returns:
            是否成功保存
        """
        if not self._persist_dir:
            return False

        try:
            os.makedirs(self._persist_dir, exist_ok=True)

            filepath = os.path.join(self._persist_dir, f"{session_id}.json")
            data = {
                "session_id": session_id,
                "saved_at": datetime.now().isoformat(),
                "tasks": [
                    {
                        "id": t.id,
                        "subject": t.subject,
                        "description": t.description,
                        "status": t.status.value,
                        "blocked_by": t.blocked_by,
                        "blocks": t.blocks,
                        "created_at": t.created_at,
                        "started_at": t.started_at,
                        "completed_at": t.completed_at,
                        "owner": t.owner,
                        "notes": t.notes,
                        "tools_required": t.tools_required,
                    }
                    for t in self._tasks.values()
                ],
            }

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            logger.info("task tracker saved: %d tasks → %s", len(self._tasks), filepath)
            return True

        except Exception as exc:
            logger.exception("task tracker save failed: %s", exc)
            return False

    def load(self, session_id: str) -> bool:
        """从磁盘恢复任务列表。

        Args:
            session_id: 会话 ID

        Returns:
            是否成功加载
        """
        if not self._persist_dir:
            return False

        filepath = os.path.join(self._persist_dir, f"{session_id}.json")
        if not os.path.exists(filepath):
            return False

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            self._tasks.clear()

            for raw in data.get("tasks", []):
                task = Task(
                    id=raw["id"],
                    subject=raw["subject"],
                    description=raw.get("description", ""),
                    status=TaskStatus(raw.get("status", "pending")),
                    blocked_by=raw.get("blocked_by", []),
                    blocks=raw.get("blocks", []),
                    created_at=raw.get("created_at", time.time()),
                    started_at=raw.get("started_at"),
                    completed_at=raw.get("completed_at"),
                    owner=raw.get("owner", ""),
                    notes=raw.get("notes", ""),
                    tools_required=raw.get("tools_required", []),
                )
                self._tasks[task.id] = task

            logger.info("task tracker loaded: %d tasks ← %s", len(self._tasks), filepath)
            return True

        except Exception as exc:
            logger.exception("task tracker load failed: %s", exc)
            return False

    def clear(self):
        """清空所有任务（切换会话时调用）。"""
        self._tasks.clear()
        logger.info("task tracker cleared")

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _get(self, task_id: str) -> Task:
        """获取任务，不存在则抛异常。"""
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f"任务 '{task_id}' 不存在")
        return task
