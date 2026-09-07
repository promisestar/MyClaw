"""聊天 API 路由"""
import json
import asyncio
from typing import List, Literal, Optional
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from ..logging.tool_logger import set_trace_id, generate_trace_id
from ..logging.llm_usage_logger import set_session_id
from ..agent.cancel_token import CancellationToken

router = APIRouter(prefix="/chat", tags=["chat"])

# ══════════════════════════════════════════════════════════
# 全局取消令牌管理
# agent_lock 串行化保证同一时间只有一个活跃请求，
# 因此只需维护一个"当前活跃令牌"即可。
# ══════════════════════════════════════════════════════════
_current_cancel_token: Optional[CancellationToken] = None


def get_current_cancel_token() -> Optional[CancellationToken]:
    """获取当前活跃的取消令牌（供外部模块使用）。"""
    return _current_cancel_token


def _set_current_cancel_token(token: Optional[CancellationToken]) -> None:
    """设置当前活跃的取消令牌。"""
    global _current_cancel_token
    _current_cancel_token = token


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
    # 工作区路径（可选，指定后切换到该工作区再处理消息）
    workspace_path: Optional[str] = None
    # Agent 模式：ask（只读）| plan（规划→确认→执行）| craft（全自动），默认 craft
    mode: Optional[str] = "craft"
    # Plan 模式：用户确认计划后传 true，后端加载已生成的 TODO 进入执行阶段
    plan_confirmed: bool = False


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
            # 切换工作区（在锁内执行，避免与正在进行的对话竞态）
            if request.workspace_path:
                try:
                    agent.bind_workspace(request.workspace_path)
                except ValueError as e:
                    return ChatResponse(content=f"工作区切换失败: {e}", session_id=request.session_id)
            response = agent.chat(
                message,
                request.session_id,
                user_turn_index=request.user_turn_index,
                regenerate=request.regenerate,
                attachments=attachments,
            )
    else:
        if request.workspace_path:
            try:
                agent.bind_workspace(request.workspace_path)
            except ValueError as e:
                return ChatResponse(content=f"工作区切换失败: {e}", session_id=request.session_id)
        response = agent.chat(
            message,
            request.session_id,
            user_turn_index=request.user_turn_index,
            regenerate=request.regenerate,
            attachments=attachments,
        )
    return ChatResponse(content=response, session_id=request.session_id)


@router.post("/send/stream")
async def send_message_stream(request: ChatRequest, http_request: Request):
    """发送消息并获取流式响应 (SSE)

    事件类型：
    - session: 会话信息（包含 session_id）
    - step_start: 步骤开始
    - chunk: LLM 文本块
    - tool_start: 工具调用开始
    - tool_finish: 工具调用结束
    - step_finish: 步骤结束
    - done: 完成（含 context_usage 上下文用量）
    - cancelled: 用户取消
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

        # 创建取消令牌并注册为当前活跃令牌
        cancel_token = CancellationToken()
        _set_current_cancel_token(cancel_token)

        lock = get_agent_lock()
        try:
            # 为本次请求生成 trace_id，贯穿所有工具调用日志
            set_trace_id(generate_trace_id())
            # 尽早写入 session 上下文（新建会话时会在 agent_start 再覆盖为实际 ID）
            if request.session_id:
                set_session_id(request.session_id)

            message = _inject_skill_context(request.message, request.skill)
            attachments = [att.model_dump() for att in request.attachments]

            async def _run_stream():
                async for event in agent.achat(
                    message,
                    request.session_id,
                    user_turn_index=request.user_turn_index,
                    regenerate=request.regenerate,
                    attachments=attachments,
                    cancel_token=cancel_token,
                    mode=request.mode,
                    plan_confirmed=request.plan_confirmed,
                ):
                    event_type = event.type.value
                    event_data = event.data

                    # 处理不同类型的事件
                    if event_type == "agent_start":
                        # 发送会话信息
                        session_id = getattr(agent, '_current_session_id', None)
                        # 把 session 写进上下文，供工具日志 / LLM 用量日志关联会话
                        set_session_id(session_id)
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

                    elif event_type == "plan_generated":
                        # Plan 模式：LLM 生成的结构化 TODO 计划
                        yield {
                            "event": "plan_generated",
                            "data": json.dumps({
                                "plan": event_data.get("plan", []),
                                "content": event_data.get("content", ""),
                            }, ensure_ascii=False)
                        }

                    elif event_type == "agent_finish":
                        # Agent 完成，保存会话并推送上下文用量
                        session_id = agent.save_current_session()
                        final_content = event_data.get("result", "")
                        context_usage = agent.get_context_usage(session_id)

                        # 本轮对话的 token 用量（主循环累计，含缓存命中）
                        turn_usage = event_data.get("usage") or None
                        yield {
                            "event": "done",
                            "data": json.dumps({
                                "content": final_content,
                                "session_id": session_id,
                                "context_usage": context_usage,
                                "usage": turn_usage,
                                "llm_calls": event_data.get("llm_calls", 0),
                            }, ensure_ascii=False)
                        }

                    elif event_type == "error":
                        yield {
                            "event": "error",
                            "data": json.dumps({"error": event_data.get("error", "Unknown error")}, ensure_ascii=False)
                        }

            async def _consume_stream():
                """消费 Agent 事件流，同时检测客户端断开和取消信号。"""
                async for item in _run_stream():
                    yield item

                    # 检测客户端断开（SSE 连接关闭）→ 触发后端取消
                    if await http_request.is_disconnected():
                        cancel_token.cancel('client_disconnected')
                        print('🔌 检测到客户端断开，已触发后端取消')
                        break

                    # 检测取消信号（前端通过 /chat/cancel 触发）
                    if cancel_token.is_cancelled:
                        yield {
                            'event': 'cancelled',
                            'data': json.dumps(
                                {'reason': cancel_token.reason or 'cancelled'},
                                ensure_ascii=False
                            )
                        }
                        break

            async def _do_stream():
                # 切换工作区（在锁内执行，避免与正在进行的对话竞态）
                if request.workspace_path:
                    try:
                        agent.bind_workspace(request.workspace_path)
                    except ValueError as e:
                        yield {
                            "event": "error",
                            "data": json.dumps({"error": f"工作区切换失败: {e}"}, ensure_ascii=False)
                        }
                        return
                async for item in _consume_stream():
                    yield item

            if lock:
                async with lock:
                    async for item in _do_stream():
                        yield item
            else:
                async for item in _do_stream():
                    yield item

        except Exception as e:
            import traceback
            traceback.print_exc()
            yield {
                "event": "error",
                "data": json.dumps({"error": str(e)}, ensure_ascii=False)
            }
        finally:
            # 清理全局取消令牌
            _set_current_cancel_token(None)

    return EventSourceResponse(event_generator())


@router.post("/send")
async def send_message(request: ChatRequest):
    """发送消息（暂返回同步响应）"""
    return await send_message_sync(request)


class CancelResponse(BaseModel):
    """取消响应"""
    success: bool
    message: str = ""


@router.post("/cancel", response_model=CancelResponse)
async def cancel_generation():
    """取消当前正在执行的 Agent 生成

    前端"停止"按钮调用此端点，后端通过 CancellationToken 中断 Agent 循环。
    依赖 agent_lock 串行化特性，同一时间只有一个活跃请求可被取消。
    """
    token = get_current_cancel_token()
    if token and not token.is_cancelled:
        token.cancel('user_requested')
        print('⏹️ 收到取消请求，已触发 Agent 中断')
        return CancelResponse(success=True, message='cancel signal sent')
    return CancelResponse(success=False, message='no active generation to cancel')
