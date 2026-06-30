"""聊天 API 路由"""
import json
from typing import List, Literal, Optional
from fastapi import APIRouter
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from ..logging.tool_logger import set_trace_id, generate_trace_id

router = APIRouter(prefix="/chat", tags=["chat"])


class Attachment(BaseModel):
    """对话附件元数据（与 /upload/file 返回的 UploadResponse 对齐）。"""

    stored_path: str = Field(description="相对于工作空间根的 POSIX 路径")
    filename: str = Field(description="原始文件名（用于在 LLM 提示中标注）")
    mime_type: str = Field(default="application/octet-stream")
    kind: Literal["image", "doc", "other"] = Field(default="other")
    size: int = Field(default=0, description="字节数")


class ChatRequest(BaseModel):
    """聊天请求"""
    message: str
    session_id: Optional[str] = None
    # 从第几条用户消息（0 起）替换该轮助手回复；该轮之后的用户/助手对话会保留
    user_turn_index: Optional[int] = None
    # True：重新生成该轮回复；False：使用编辑后的 message
    regenerate: bool = False
    # 用户通过 /技能名 方式指定的技能，后端会预加载技能内容注入上下文
    skill: Optional[str] = None
    # 多模态附件列表（图片走 VLM image_url；文档抽取为 text 注入）
    attachments: List[Attachment] = Field(default_factory=list)


class ChatResponse(BaseModel):
    """聊天响应"""
    content: str
    session_id: Optional[str] = None


def get_agent():
    """获取全局 Agent 实例"""
    from ..main import get_agent as _get_agent
    return _get_agent()

def get_agent_lock():
    """获取全局 Agent 锁（避免并发调用导致会话错乱）"""
    from ..main import get_agent_lock as _get_agent_lock
    return _get_agent_lock()


def _inject_skill_context(message: str, skill_name: Optional[str]) -> str:
    """如果指定了技能名，注入调用提示而不注入 body（避免技能全文出现在会话历史中）"""
    if not skill_name:
        return message
    try:
        from .skills import get_skill_loader
        loader = get_skill_loader()
        skill = loader.get_skill(skill_name)
        if skill:
            return (
                f'用户通过 /{skill_name} 选择了技能「{skill.name}」（{skill.description}）。\n'
                f'请先调用 Skill 工具加载技能 "{skill_name}"，'
                f'然后严格遵循技能说明完成以下任务：\n\n{message}'
            )
    except Exception:
        pass
    return message


@router.post("/send/sync", response_model=ChatResponse)
async def send_message_sync(request: ChatRequest):
    """发送消息并获取同步响应"""
    agent = get_agent()
    if not agent:
        return ChatResponse(content="Agent not initialized", session_id=request.session_id)

    message = _inject_skill_context(request.message, request.skill)
    attachments = [att.model_dump() for att in request.attachments]
    lock = get_agent_lock()
    if lock:
        async with lock:
            response = agent.chat(
                message,
                request.session_id,
                user_turn_index=request.user_turn_index,
                regenerate=request.regenerate,
                attachments=attachments,
            )
    else:
        response = agent.chat(
            message,
            request.session_id,
            user_turn_index=request.user_turn_index,
            regenerate=request.regenerate,
            attachments=attachments,
        )
    return ChatResponse(content=response, session_id=request.session_id)


@router.post("/send/stream")
async def send_message_stream(request: ChatRequest):
    """发送消息并获取流式响应 (SSE)

    事件类型：
    - session: 会话信息（包含 session_id）
    - step_start: 步骤开始
    - chunk: LLM 文本块
    - tool_start: 工具调用开始
    - tool_finish: 工具调用结束
    - step_finish: 步骤结束
    - done: 完成（含 context_usage 上下文用量）
    - error: 错误
    """

    async def event_generator():
        agent = get_agent()
        if not agent:
            yield {
                "event": "error",
                "data": json.dumps({"error": "Agent not initialized"}, ensure_ascii=False)
            }
            return

        lock = get_agent_lock()
        try:
            # 为本次请求生成 trace_id，贯穿所有工具调用日志
            set_trace_id(generate_trace_id())

            message = _inject_skill_context(request.message, request.skill)
            attachments = [att.model_dump() for att in request.attachments]

            async def _run_stream():
                async for event in agent.achat(
                    message,
                    request.session_id,
                    user_turn_index=request.user_turn_index,
                    regenerate=request.regenerate,
                    attachments=attachments,
                ):
                    event_type = event.type.value
                    event_data = event.data

                    # 处理不同类型的事件
                    if event_type == "agent_start":
                        # 发送会话信息
                        session_id = getattr(agent, '_current_session_id', None)
                        yield {
                            "event": "session",
                            "data": json.dumps({"session_id": session_id}, ensure_ascii=False)
                        }

                    elif event_type == "step_start":
                        # 步骤开始
                        yield {
                            "event": "step_start",
                            "data": json.dumps({
                                "step": event_data.get("step", 1),
                                "max_steps": event_data.get("max_steps", 10)
                            }, ensure_ascii=False)
                        }

                    elif event_type == "llm_chunk":
                        # LLM 文本块
                        chunk = event_data.get("chunk", "")
                        yield {
                            "event": "chunk",
                            "data": json.dumps({"content": chunk}, ensure_ascii=False)
                        }

                    elif event_type == "tool_call_start":
                        # 工具调用开始
                        yield {
                            "event": "tool_start",
                            "data": json.dumps({
                                "tool": event_data.get("tool_name", ""),
                                "args": event_data.get("args", {})
                            }, ensure_ascii=False)
                        }

                    elif event_type == "tool_call_finish":
                        # 工具调用结束
                        yield {
                            "event": "tool_finish",
                            "data": json.dumps({
                                "tool": event_data.get("tool_name", ""),
                                "result": event_data.get("result", "")
                            }, ensure_ascii=False)
                        }

                    elif event_type == "step_finish":
                        # 步骤结束
                        yield {
                            "event": "step_finish",
                            "data": json.dumps({
                                "step": event_data.get("step", 1)
                            }, ensure_ascii=False)
                        }

                    elif event_type == "agent_finish":
                        # Agent 完成，保存会话并推送上下文用量
                        session_id = agent.save_current_session()
                        final_content = event_data.get("result", "")
                        context_usage = agent.get_context_usage(session_id)

                        yield {
                            "event": "done",
                            "data": json.dumps({
                                "content": final_content,
                                "session_id": session_id,
                                "context_usage": context_usage,
                            }, ensure_ascii=False)
                        }

                    elif event_type == "error":
                        yield {
                            "event": "error",
                            "data": json.dumps({"error": event_data.get("error", "Unknown error")}, ensure_ascii=False)
                        }

            if lock:
                async with lock:
                    async for item in _run_stream():
                        yield item
            else:
                async for item in _run_stream():
                    yield item

        except Exception as e:
            import traceback
            traceback.print_exc()
            yield {
                "event": "error",
                "data": json.dumps({"error": str(e)}, ensure_ascii=False)
            }

    return EventSourceResponse(event_generator())


@router.post("/send")
async def send_message(request: ChatRequest):
    """发送消息（暂返回同步响应）"""
    return await send_message_sync(request)
