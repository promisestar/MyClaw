"""Agent 执行取消令牌。

提供协作式取消机制，允许前端通过 /chat/cancel 端点中断正在执行的 Agent 循环。
在 Agent ReAct 循环的检查点（迭代开头、工具执行前、LLM 流式输出中）检查
token.is_cancelled，已取消时跳出循环并优雅结束。
"""

from typing import Optional


class AgentCancelledError(Exception):
    """Agent 执行被取消时抛出的异常。

    Attributes:
        reason: 取消原因（如 "user_requested"、"client_disconnected"）
    """

    def __init__(self, reason: str = 'cancelled'):
        self.reason = reason
        super().__init__(f'Agent execution cancelled: {reason}')


class CancellationToken:
    """协作式取消令牌。

    仅在单线程 asyncio 事件循环中使用，无需加锁。

    用法::

        token = CancellationToken()
        # 在 Agent 循环检查点：
        if token.is_cancelled:
            break
        # 前端请求取消：
        token.cancel('user_requested')
    """

    def __init__(self):
        self._cancelled: bool = False
        self._reason: Optional[str] = None

    def cancel(self, reason: str = 'user_requested') -> None:
        """触发取消信号。

        Args:
            reason: 取消原因，用于日志和前端提示
        """
        if not self._cancelled:
            self._cancelled = True
            self._reason = reason

    @property
    def is_cancelled(self) -> bool:
        """是否已收到取消信号。"""
        return self._cancelled

    @property
    def reason(self) -> Optional[str]:
        """取消原因（未取消时为 None）。"""
        return self._reason

    def check(self) -> None:
        """检查点：如果已取消则抛出 AgentCancelledError。

        适用于需要立即中断的场景（如同步循环）。
        异步流式循环中推荐直接检查 ``is_cancelled`` 并 ``break``。
        """
        if self._cancelled:
            raise AgentCancelledError(self._reason or 'cancelled')

    def reset(self) -> None:
        """重置令牌状态（复用场景）。"""
        self._cancelled = False
        self._reason = None
