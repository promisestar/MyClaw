"""会话 API 路由"""
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Literal

router = APIRouter(prefix="/session", tags=["session"])


class SessionInfo(BaseModel):
    """会话信息"""
    id: str
    created_at: float
    updated_at: float


class SessionListResponse(BaseModel):
    """会话列表响应"""
    sessions: List[SessionInfo]


class SessionCreateRequest(BaseModel):
    """创建会话请求"""
    pass


class SessionCreateResponse(BaseModel):
    """创建会话响应"""
    session_id: str
    message: str = "Session created successfully"


# ==================== OpenAI 标准消息格式 ====================

class ToolCallFunction(BaseModel):
    """工具调用函数"""
    name: str
    arguments: str  # JSON 字符串


class ToolCall(BaseModel):
    """工具调用"""
    id: str
    type: Literal["function"] = "function"
    function: ToolCallFunction


class HistoryAttachmentMeta(BaseModel):
    """历史消息附件元数据（由 MyClawAgent.get_session_history 从 list-content 中提取）。

    当前仅图片需要在前端回显缩略图（文档已在发送时抽文本注入到 user 文本，回放时不再单独渲染）。
    """
    kind: Literal["image"] = "image"
    url: str  # data:image/...;base64,xxx 或 http(s)://...


class ChatMessage(BaseModel):
    """聊天消息（OpenAI 标准格式 + 多模态附件）"""
    role: Literal["user", "assistant", "tool"]
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None  # assistant 消息中的工具调用
    tool_call_id: Optional[str] = None  # tool 消息中的调用 ID
    attachments: Optional[List[HistoryAttachmentMeta]] = None  # 多模态附件（仅 user 消息可能存在）


class SessionHistoryResponse(BaseModel):
    """会话历史响应"""
    session_id: str
    messages: List[ChatMessage]


class ContextUsageResponse(BaseModel):
    """上下文窗口使用情况"""
    session_id: Optional[str] = None
    context_window: int
    used_tokens: int
    system_tokens: int
    history_tokens: int
    used_percent: float


def get_agent():
    """获取全局 Agent 实例"""
    from ..main import get_agent as _get_agent
    return _get_agent()


def get_agent_lock():
    """获取全局 Agent 锁（与 chat 路由共用，避免并发切换会话）"""
    from ..main import get_agent_lock as _get_agent_lock
    return _get_agent_lock()


@router.get("/list", response_model=SessionListResponse)
async def list_sessions():
    """获取会话列表

    返回所有会话，按更新时间倒序排列
    """
    agent = get_agent()
    if not agent:
        return SessionListResponse(sessions=[])

    sessions = agent.list_sessions()
    return SessionListResponse(sessions=[
        SessionInfo(
            id=s["id"],
            created_at=s["created_at"],
            updated_at=s["updated_at"]
        )
        for s in sessions
    ])


@router.post("/create", response_model=SessionCreateResponse)
async def create_session():
    """创建新会话，返回新会话的 ID"""
    agent = get_agent()
    if not agent:
        raise HTTPException(status_code=500, detail="Agent not initialized")

    # 创建新会话并激活到 Agent 内存
    session_id = agent.create_session()
    lock = get_agent_lock()
    if lock:
        async with lock:
            agent.activate_session(session_id)
    else:
        agent.activate_session(session_id)

    return SessionCreateResponse(
        session_id=session_id,
        message="Session created successfully",
    )


@router.get("/{session_id}")
async def get_session(session_id: str):
    """获取会话详情

    返回会话的基本信息
    """
    agent = get_agent()
    if not agent:
        raise HTTPException(status_code=500, detail="Agent not initialized")

    sessions = agent.list_sessions()
    for s in sessions:
        if s["id"] == session_id:
            return SessionInfo(
                id=s["id"],
                created_at=s["created_at"],
                updated_at=s["updated_at"]
            )

    raise HTTPException(status_code=404, detail="Session not found")


@router.get("/{session_id}/context-usage", response_model=ContextUsageResponse)
async def get_session_context_usage(session_id: str):
    """获取指定会话的上下文窗口使用情况（token 估算）。"""
    agent = get_agent()
    if not agent:
        raise HTTPException(status_code=500, detail="Agent not initialized")

    lock = get_agent_lock()
    if lock:
        async with lock:
            agent.activate_session(session_id)
            usage = agent.get_context_usage(session_id)
    else:
        agent.activate_session(session_id)
        usage = agent.get_context_usage(session_id)
    return ContextUsageResponse(**usage)


@router.get("/{session_id}/history", response_model=SessionHistoryResponse)
async def get_session_history(session_id: str):
    """获取会话历史消息

    返回会话的所有聊天记录，按照 OpenAI 标准格式
    """
    agent = get_agent()
    if not agent:
        raise HTTPException(status_code=500, detail="Agent not initialized")

    lock = get_agent_lock()
    if lock:
        async with lock:
            agent.activate_session(session_id)
            raw_messages = agent.get_session_history(session_id)
    else:
        agent.activate_session(session_id)
        raw_messages = agent.get_session_history(session_id)
    if raw_messages is None:
        raw_messages = []

    # 转换为 OpenAI 标准格式
    chat_messages: List[ChatMessage] = []

    for m in raw_messages:
        role = m.get("role", "")
        content = m.get("content", "")
        metadata = m.get("metadata", {})

        if role == "user":
            # 透传多模态附件元数据（图片缩略图）
            raw_atts = m.get("attachments") or []
            atts: Optional[List[HistoryAttachmentMeta]] = None
            if raw_atts:
                atts = []
                for a in raw_atts:
                    if not isinstance(a, dict):
                        continue
                    if a.get("kind") == "image" and a.get("url"):
                        atts.append(HistoryAttachmentMeta(kind="image", url=a["url"]))
                if not atts:
                    atts = None
            chat_messages.append(ChatMessage(role="user", content=content, attachments=atts))

        elif role == "assistant":
            tool_calls_data = metadata.get("tool_calls")
            if tool_calls_data:
                # 包含工具调用的 assistant 消息
                tool_calls = [
                    ToolCall(
                        id=tc.get("id", ""),
                        type="function",
                        function=ToolCallFunction(
                            name=tc.get("function", {}).get("name", ""),
                            arguments=tc.get("function", {}).get("arguments", "{}")
                        )
                    )
                    for tc in tool_calls_data
                ]
                chat_messages.append(ChatMessage(
                    role="assistant",
                    content=content if content else None,
                    tool_calls=tool_calls
                ))
            elif content:
                # 普通的 assistant 文本消息
                chat_messages.append(ChatMessage(role="assistant", content=content))

        elif role == "tool":
            # tool 消息
            tool_call_id = metadata.get("tool_call_id")
            chat_messages.append(ChatMessage(
                role="tool",
                content=content,
                tool_call_id=tool_call_id
            ))

    return SessionHistoryResponse(
        session_id=session_id,
        messages=chat_messages
    )


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    """删除会话

    删除指定会话及其历史记录
    """
    agent = get_agent()
    if not agent:
        raise HTTPException(status_code=500, detail="Agent not initialized")

    success = agent.delete_session(session_id)
    if success:
        return {"message": "Session deleted successfully", "session_id": session_id}

    raise HTTPException(status_code=404, detail="Session not found")

