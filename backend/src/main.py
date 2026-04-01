"""
HelloClaw Backend - FastAPI 入口
"""
import os
import asyncio

# 禁用 PYTHONSTARTUP 以避免 I/O 问题
os.environ.pop("PYTHONSTARTUP", None)

from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import chat, session, config, memory, upload
from .workspace.manager import WorkspaceManager
from .agent.helloclaw_agent import HelloClawAgent
from .channels.external_software_receiver import ExternalSoftwareReceiver

# 加载环境变量
load_dotenv()

# 全局 Agent 实例
_agent: HelloClawAgent = None
_agent_lock: asyncio.Lock | None = None


def get_agent() -> HelloClawAgent:
    """获取全局 Agent 实例"""
    global _agent
    return _agent


def get_agent_lock() -> asyncio.Lock | None:
    """获取全局 Agent 锁（用于避免并发调用导致会话错乱）"""
    global _agent_lock
    return _agent_lock


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global _agent

    # 启动时初始化
    print("HelloClaw Backend starting...")

    # 初始化工作空间
    workspace_path = os.getenv("WORKSPACE_PATH", "~/.helloclaw/workspace")
    workspace = WorkspaceManager(workspace_path)
    workspace.ensure_workspace_exists()
    print(f"Workspace initialized at: {workspace.workspace_path}")

    # 设置全局 workspace 实例
    config.set_workspace(workspace)
    memory.set_workspace(workspace)

    # 初始化全局 Agent 实例
    _agent = HelloClawAgent(workspace_path=workspace_path)
    print("HelloClawAgent initialized")

    # 防并发：所有对同一进程内 agent 的调用共享同一把锁
    _agent_lock = asyncio.Lock()

    # 启动外部软件消息接收器（后台常驻）
    # 默认依赖环境变量 EXTERNAL_BRIDGE_URL；若不需要可不设置/设置为不可用地址仍会重连
    receiver: ExternalSoftwareReceiver | None = None
    receiver_task: asyncio.Task | None = None
    try:
        ext_enabled = os.getenv("EXTERNAL_BRIDGE_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
        ext_url = os.getenv("EXTERNAL_BRIDGE_URL", "").strip()

        if ext_enabled or ext_url:
            receiver = ExternalSoftwareReceiver(agent=_agent, agent_lock=_agent_lock)
            receiver_task = asyncio.create_task(receiver.run())
            print("ExternalSoftwareReceiver started (background)")
        else:
            print("ExternalSoftwareReceiver disabled (set EXTERNAL_BRIDGE_URL or EXTERNAL_BRIDGE_ENABLED=true)")
    except Exception as e:
        print(f"⚠️ 启动 ExternalSoftwareReceiver 失败: {e}")

    try:
        yield
    finally:
        # 关闭时清理
        print("HelloClaw Backend shutting down...")
        try:
            if _agent is not None and hasattr(_agent, "shutdown"):
                _agent.shutdown()
        except Exception as e:
            print(f"⚠️ Agent 资源清理失败: {e}")
        finally:
            _agent = None
            # 停止外部接收器
            if receiver_task is not None:
                receiver_task.cancel()
                try:
                    await receiver_task
                except asyncio.CancelledError:
                    pass
            if receiver is not None:
                try:
                    await receiver.stop()
                except Exception:
                    pass


app = FastAPI(
    title="HelloClaw API",
    description="AI Agent powered by HelloAgents",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:5173").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 健康检查
@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "helloclaw-backend"}


# 注册 API 路由
app.include_router(chat.router, prefix="/api")
app.include_router(session.router, prefix="/api")
app.include_router(config.router, prefix="/api")
app.include_router(memory.router, prefix="/api")
app.include_router(upload.router, prefix="/api")


@app.get("/api")
async def api_root():
    return {"message": "HelloClaw API v0.1.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=True,
    )
