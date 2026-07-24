"""定时任务调度器 — 自研轻量 asyncio 后台循环。

每 30 秒轮询一次 due 任务，提交给 executor 执行。
单 asyncio.Task，异常捕获不崩溃。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional, Set

from .executor import AutomationExecutor
from .models import AutomationTask
from .store import AutomationStore

logger = logging.getLogger(__name__)

_DEFAULT_CHECK_INTERVAL = 30  # 秒


class AutomationScheduler:
    """轻量 asyncio 调度器。

    后台循环：每 check_interval_s 秒检查一次 due 任务。
    发现 due 任务后提交给 executor 异步执行（不阻塞循环）。

    store_getter 动态取值：工作区切换后 store 重建，
    调度器始终引用当前 store，避免读到旧工作区数据。
    """

    def __init__(
        self,
        store_getter: Callable[[], AutomationStore],
        executor: AutomationExecutor,
        check_interval_s: int = _DEFAULT_CHECK_INTERVAL,
    ):
        self._store_getter = store_getter
        self._executor = executor
        self._check_interval_s = check_interval_s
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # 正在执行的任务 ID 集合（避免重复执行）
        self._executing: Set[str] = set()
        # 后台任务引用集合（防止被 GC 回收，L4）
        self._background_tasks: Set[asyncio.Task] = set()

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        """启动调度器后台循环。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"AutomationScheduler 已启动（轮询间隔 {self._check_interval_s}s）")

    async def stop(self) -> None:
        """停止调度器。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # 等待进行中的后台执行任务完成（最多 5 秒）
        if self._background_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._background_tasks, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                pass
            self._background_tasks.clear()
        logger.info("AutomationScheduler 已停止")

    async def _run_loop(self) -> None:
        """调度器主循环。"""
        # 启动时立即检查一次
        await self._check_and_dispatch()

        while self._running:
            try:
                await asyncio.sleep(self._check_interval_s)
                if not self._running:
                    break
                await self._check_and_dispatch()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"调度器循环异常: {e}", exc_info=True)
                # 异常后短暂等待再继续
                await asyncio.sleep(5)

    async def _check_and_dispatch(self) -> None:
        """检查 due 任务并提交执行。"""
        store = self._store_getter()
        try:
            tasks = store.load_tasks()
        except Exception as e:
            logger.error(f"加载任务失败: {e}", exc_info=True)
            return

        now = datetime.now(timezone.utc)
        due_count = 0

        for task in tasks:
            if task.id in self._executing:
                continue

            # 检查是否到期
            if not task.is_due(now):
                # 如果 next_run_at 未设置，尝试计算
                if not task.next_run_at and task.enabled:
                    next_run = task.compute_next_run(now)
                    if next_run:
                        task.next_run_at = next_run.isoformat()
                        store.update_task(task)
                continue

            due_count += 1
            self._executing.add(task.id)

            # 提交执行（不阻塞循环）
            bg_task = asyncio.create_task(self._execute_and_cleanup(task))
            self._background_tasks.add(bg_task)
            bg_task.add_done_callback(self._background_tasks.discard)

        if due_count > 0:
            logger.info(f"发现 {due_count} 个 due 任务，已提交执行")

    async def _execute_and_cleanup(self, task: AutomationTask) -> None:
        """执行任务并从执行集合中移除。"""
        store = self._store_getter()
        try:
            # 标记为正在执行，更新 next_run_at 防止重复触发
            # next_run_at 必须存 ISO 字符串（字段类型为 Optional[str]），
            # 直接赋 datetime 会导致 JSON 序列化崩溃（C1）
            next_run = task.compute_next_run()
            task.next_run_at = next_run.isoformat() if next_run else None
            store.update_task(task)

            await self._executor.execute(task)
        except Exception as e:
            logger.error(
                f"任务执行失败 task_id={task.id}: {e}",
                exc_info=True,
            )
        finally:
            self._executing.discard(task.id)

    def refresh_task(self, task_id: str) -> None:
        """刷新单个任务的 next_run_at（创建/更新后调用）。"""
        store = self._store_getter()
        try:
            task = store.get_task(task_id)
            if task and task.enabled:
                # next_run_at 存 ISO 字符串（C1）
                next_run = task.compute_next_run()
                task.next_run_at = next_run.isoformat() if next_run else None
                store.update_task(task)
        except Exception as e:
            logger.warning(f"刷新任务失败 task_id={task_id}: {e}")
