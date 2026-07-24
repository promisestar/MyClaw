"""定时任务自动化模块 — 调度器 + 执行器 + 持久化 + Agent 工具 + REST API。

自研轻量 asyncio 调度器（与项目"自实现"哲学一致），dateutil.rrule 计算，
httpx webhook 投递，JSON 文件持久化。跨平台：Windows/Linux/macOS 通用。
"""

from .models import AutomationTask, AutomationRun
from .store import AutomationStore
from .scheduler import AutomationScheduler
from .executor import AutomationExecutor

__all__ = [
    "AutomationTask",
    "AutomationRun",
    "AutomationStore",
    "AutomationScheduler",
    "AutomationExecutor",
]
