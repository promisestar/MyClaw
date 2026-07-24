"""定时任务执行器 — agent_lock 串行化 + asyncio.to_thread 非阻塞 + webhook 投递。

执行流程：
1. acquire agent_lock（与 chat API 共享串行化）
2. bind_workspace（切换到任务配置的工作区）+ 独立 session（保护用户会话现场）
3. asyncio.to_thread(agent.chat, prompt, session_id=...)（非阻塞执行）
4. 落盘 run record
5. httpx POST webhook（fire-and-forget）
6. finally 恢复用户原工作区与会话（H1+H2）

store_getter 动态取值：工作区切换后 store 重建，执行器始终引用当前 store（C2）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Set

from .models import AutomationRun, AutomationTask
from .store import AutomationStore

logger = logging.getLogger(__name__)


class AutomationExecutor:
    """定时任务执行器。

    通过回调获取 agent / agent_lock / store（避免循环依赖 + 动态引用）。
    """

    def __init__(
        self,
        agent_getter: Callable[[], Any],
        agent_lock_getter: Callable[[], Optional[asyncio.Lock]],
        store_getter: Callable[[], AutomationStore],
    ):
        """初始化执行器。

        Args:
            agent_getter: 返回 MyClawAgent 实例的回调函数
            agent_lock_getter: 返回全局 agent_lock 的回调函数
            store_getter: 返回 AutomationStore 实例的回调（动态取值，
                         工作区切换后自动跟随，C2）
        """
        self._agent_getter = agent_getter
        self._agent_lock_getter = agent_lock_getter
        self._store_getter = store_getter
        # 后台 webhook 任务引用集合（防止被 GC 回收，L4）
        self._background_tasks: Set[asyncio.Task] = set()

    async def execute(self, task: AutomationTask) -> AutomationRun:
        """执行一个定时任务。

        Args:
            task: 要执行的任务

        Returns:
            执行记录 AutomationRun
        """
        started_at = datetime.now(timezone.utc).isoformat()
        run = AutomationRun(
            task_id=task.id,
            prompt=task.prompt,
            started_at=started_at,
        )

        agent = self._agent_getter()
        store = self._store_getter()

        if agent is None:
            run.success = False
            run.error = "Agent 未初始化"
            run.finished_at = datetime.now(timezone.utc).isoformat()
            store.save_run(run)
            return run

        lock = self._agent_lock_getter()
        result_text = ""
        error_msg: Optional[str] = None

        try:
            # 获取锁（与 chat API 共享串行化）
            if lock:
                async with lock:
                    result_text, error_msg = await self._run_agent(agent, task, run)
            else:
                result_text, error_msg = await self._run_agent(agent, task, run)
        except Exception as e:
            error_msg = f"执行异常: {e}"
            logger.exception(f"定时任务执行异常 task_id={task.id}")

        # 填充运行记录
        run.result = result_text[:10000] if result_text else ""  # 截断过长结果
        run.success = error_msg is None
        run.error = error_msg
        run.finished_at = datetime.now(timezone.utc).isoformat()

        # 落盘
        store.save_run(run)

        # 更新任务状态
        task.last_run_at = run.started_at
        task.run_count += 1
        if task.schedule_type == "once":
            task.enabled = False  # 一次性任务执行后禁用
        # 重新计算下次运行时间 — next_run_at 必须存 ISO 字符串（C1）
        next_run = task.compute_next_run()
        task.next_run_at = next_run.isoformat() if next_run else None
        store.update_task(task)

        # webhook 投递（fire-and-forget）
        if task.webhook_url:
            bg_task = asyncio.create_task(self._deliver_webhook(task, run))
            self._background_tasks.add(bg_task)
            bg_task.add_done_callback(self._background_tasks.discard)

        logger.info(
            f"定时任务完成 task_id={task.id} run_id={run.run_id} "
            f"success={run.success}"
        )
        return run

    async def _run_agent(
        self,
        agent: Any,
        task: AutomationTask,
        run: AutomationRun,
    ) -> tuple[str, Optional[str]]:
        """在 agent_lock 内执行 agent.chat。

        保护用户现场（H1+H2）：执行前保存原工作区/会话，执行后恢复。
        使用独立 session_id 避免污染用户会话历史与列表。

        Returns:
            (结果文本, 错误信息)；成功时错误为 None
        """
        # 保存用户现场（H1 工作区 + H2 会话）
        prev_workspace = getattr(agent, "current_workspace", None)
        prev_session_id = getattr(agent, "_current_session_id", None)
        switched_workspace = False

        # 独立 session_id，避免清空/污染用户当前会话（H2）
        automation_session_id = f"automation_{task.id}_{run.run_id}"

        try:
            # 切换工作区（如果配置了且与当前不同）
            if task.workspace_path and task.workspace_path != prev_workspace:
                try:
                    agent.bind_workspace(task.workspace_path)
                    switched_workspace = True
                except Exception as e:
                    logger.warning(f"切换工作区失败: {e}")

            # 非阻塞执行 agent.chat（用独立 session，activate_session 对不存在会话会 clear_history）
            try:
                result = await asyncio.to_thread(
                    agent.chat,
                    task.prompt,
                    automation_session_id,
                )
                return result, None
            except Exception as e:
                return "", f"Agent 执行失败: {e}"
        finally:
            # 恢复用户工作区（H1）— 仅在确实切换过时恢复
            if switched_workspace and prev_workspace:
                try:
                    agent.bind_workspace(prev_workspace)
                except Exception as e:
                    logger.warning(f"恢复用户工作区失败: {e}")
            # 恢复用户会话现场（H2）
            if prev_session_id:
                try:
                    agent.activate_session(prev_session_id)
                except Exception as e:
                    logger.warning(f"恢复用户会话失败: {e}")

    async def _deliver_webhook(
        self,
        task: AutomationTask,
        run: AutomationRun,
    ) -> None:
        """投递 webhook（fire-and-forget，失败仅记日志）。

        Args:
            task: 任务定义
            run: 执行记录
        """
        if not task.webhook_url:
            return

        # 验证 webhook URL scheme
        if not task.webhook_url.startswith(("http://", "https://")):
            logger.warning(
                f"webhook URL scheme 非法: {task.webhook_url}，已跳过"
            )
            return

        payload = {
            "task_id": task.id,
            "task_name": task.name,
            "run_id": run.run_id,
            "prompt": task.prompt,
            "result": run.result,
            "success": run.success,
            "error": run.error,
            "timestamp": run.finished_at,
        }

        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    task.webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                if response.status_code >= 400:
                    logger.warning(
                        f"webhook 投递返回非 2xx: {response.status_code} "
                        f"task_id={task.id}"
                    )
        except Exception as e:
            logger.warning(
                f"webhook 投递失败 task_id={task.id}: {e}"
            )
