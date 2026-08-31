"""SSE 客户端：消费 POST /api/chat/send/stream。"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import httpx

from .types import StreamTrace


def _parse_sse_block(block: str) -> Optional[Dict[str, Any]]:
    """解析单个 SSE 事件块（event: / data: 行）。"""
    event_name = "message"
    data_lines: List[str] = []
    for line in block.splitlines():
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return None
    raw = "\n".join(data_lines)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {"raw": raw}
    return {"event": event_name, "data": payload}


async def run_chat_stream(
    base_url: str,
    *,
    message: str,
    mode: str = "craft",
    plan_confirmed: bool = False,
    session_id: Optional[str] = None,
    skill: Optional[str] = None,
    workspace_path: Optional[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    timeout_s: float = 180.0,
) -> StreamTrace:
    """调用流式聊天并汇总轨迹。"""
    url = base_url.rstrip("/") + "/api/chat/send/stream"
    body: Dict[str, Any] = {
        "message": message,
        "mode": mode,
        "plan_confirmed": plan_confirmed,
    }
    if session_id:
        body["session_id"] = session_id
    if skill:
        body["skill"] = skill
    if workspace_path:
        body["workspace_path"] = workspace_path
    if attachments:
        body["attachments"] = attachments

    trace = StreamTrace()
    t0 = time.perf_counter()

    timeout = httpx.Timeout(timeout_s, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=body) as resp:
            resp.raise_for_status()
            buffer = ""
            async for chunk in resp.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    parsed = _parse_sse_block(block.strip())
                    if not parsed:
                        continue
                    _ingest(trace, parsed)

    trace.latency_ms = (time.perf_counter() - t0) * 1000.0
    return trace


def _ingest(trace: StreamTrace, parsed: Dict[str, Any]) -> None:
    event = parsed["event"]
    data = parsed.get("data") or {}
    trace.events.append(parsed)

    if event == "session":
        trace.session_id = data.get("session_id") or trace.session_id
    elif event == "chunk":
        trace.chunks.append(data.get("content", ""))
    elif event == "tool_start":
        trace.tool_starts.append(data)
    elif event == "tool_finish":
        trace.tool_finishes.append(data)
    elif event == "plan_generated":
        plan = data.get("plan")
        if isinstance(plan, list):
            trace.plan = plan
    elif event == "done":
        trace.done_content = data.get("content", "") or ""
        trace.session_id = data.get("session_id") or trace.session_id
        usage = data.get("context_usage")
        if isinstance(usage, dict):
            trace.context_usage = usage
    elif event == "error":
        err = data.get("error") or str(data)
        trace.errors.append(str(err))
    elif event == "cancelled":
        trace.cancelled_reason = data.get("reason") or "cancelled"


async def cancel_chat(base_url: str) -> Dict[str, Any]:
    """触发 POST /api/chat/cancel。"""
    url = base_url.rstrip("/") + "/api/chat/cancel"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url)
        resp.raise_for_status()
        return resp.json() if resp.content else {"ok": True}
