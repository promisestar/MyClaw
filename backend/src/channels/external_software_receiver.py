"""
外部软件消息接收器（仿照 nanobot 的“桥接 websocket -> 内部处理 -> 回写”模式）

当前实现对齐 nanobot 的 WhatsApp bridge 协议：
- 通过 websocket 连接到桥接服务
- 监听来自桥接服务的消息事件（type="message"）
- 解析 sender/chat_id/content/media
- 调用 HelloClawAgent.achat 生成回复，并将回复回写到桥接服务（type="send"）

桥接服务：ws://127.0.0.1:3001（默认）
配置：
- EXTERNAL_BRIDGE_URL: websocket 连接地址
- EXTERNAL_BRIDGE_TOKEN: 桥接鉴权 token（可选）
- EXTERNAL_BRIDGE_ALLOW_FROM: 逗号分隔允许的 sender_id 列表，默认 "*"（允许所有）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

import logging


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExternalBridgeConfig:
    url: str
    token: Optional[str]
    allow_from: list[str]
    connect_timeout_s: float
    handle_timeout_s: float


def _get_env_list(key: str, default: str) -> list[str]:
    import os

    raw = (os.getenv(key, default) or "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def load_external_bridge_config() -> ExternalBridgeConfig:
    import os

    url = os.getenv("EXTERNAL_BRIDGE_URL", "ws://127.0.0.1:3001").strip()
    token = os.getenv("EXTERNAL_BRIDGE_TOKEN")
    allow_from = _get_env_list("EXTERNAL_BRIDGE_ALLOW_FROM", "*")

    connect_timeout_s = float(os.getenv("EXTERNAL_BRIDGE_CONNECT_TIMEOUT_S", "10"))
    handle_timeout_s = float(os.getenv("EXTERNAL_BRIDGE_HANDLE_TIMEOUT_S", "120"))

    return ExternalBridgeConfig(
        url=url,
        token=token if token and token.strip() else None,
        allow_from=allow_from,
        connect_timeout_s=connect_timeout_s,
        handle_timeout_s=handle_timeout_s,
    )


class ExternalSoftwareReceiver:
    """
    后台常驻任务：
    - 连接桥接 websocket
    - 处理外部入站消息
    - 把输出回写给桥接服务
    """

    def __init__(self, agent: Any, agent_lock: asyncio.Lock | None = None):
        self.agent = agent
        self._running = False
        self._ws = None
        self._lock = agent_lock or asyncio.Lock()
        self._processed_message_ids: "OrderedDict[str, None]" = OrderedDict()
        self._cfg = load_external_bridge_config()

    def _is_allowed(self, sender_id: str) -> bool:
        if not self._cfg.allow_from:
            # 和 nanobot 一致：空列表拒绝全部
            return False
        if "*" in self._cfg.allow_from:
            return True
        return str(sender_id) in self._cfg.allow_from

    @staticmethod
    def _stable_session_id(chat_id: str) -> str:
        digest = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()
        return digest[:8]

    @staticmethod
    def _extract_sender_and_chat_id(data: dict[str, Any]) -> tuple[str, str]:
        """
        对齐 nanobot whatsapp 通道：
        - sender_id: 用于权限判断（pn 或 sender 的 user 部分）
        - chat_id: 用于回复（sender 的完整 LID）
        """
        sender = str(data.get("sender") or "")
        pn = str(data.get("pn") or "")
        user_id = pn if pn else sender
        sender_id = user_id.split("@")[0] if "@" in user_id else user_id
        return sender_id, sender

    async def _handle_bridge_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"外部桥接返回了非 JSON：{raw[:200]}")
            return

        msg_type = data.get("type")
        if msg_type != "message":
            # 忽略 status/qr/error 等
            return

        message_id = str(data.get("id") or "")
        if message_id:
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

        sender_id, chat_id = self._extract_sender_and_chat_id(data)
        if not chat_id:
            logger.warning(f"外部消息缺少 sender/chat_id：{data}")
            return

        if not self._is_allowed(sender_id):
            logger.warning(f"外部消息权限不足 sender_id={sender_id} chat_id={chat_id}，已丢弃")
            return

        content = str(data.get("content") or "")
        media_paths: list[str] = list(data.get("media") or [])

        # 对齐 nanobot：处理 voice 占位文本（bridge 侧为 "[Voice Message] "）
        if content == "[Voice Message]":
            content = "[Voice Message: Transcription not available]"

        # 对齐 nanobot：给图片/文件附加标签，帮助模型理解
        if media_paths:
            for p in media_paths:
                mime, _ = mimetypes.guess_type(p)
                media_type = "image" if mime and mime.startswith("image/") else "file"
                media_tag = f"[{media_type}: {p}]"
                content = f"{content}\n{media_tag}" if content else media_tag

        # chat_id -> session_id（保证同一会话持续对话）
        session_id = self._stable_session_id(chat_id)

        async with self._lock:
            reply_text = ""
            try:
                async def _run_agent() -> str:
                    final_text = ""
                    async for event in self.agent.achat(content, session_id=session_id):
                        if getattr(event.type, "value", None) == "agent_finish":
                            final_text = (event.data or {}).get("result", "") or ""
                    # 关键：补上保存会话（HTTP/SSE 路径是 chat.py 在保存，这里需要我们自己做）
                    try:
                        self.agent.save_current_session()
                    except Exception as e:
                        logger.warning(f"保存会话失败：{e}")
                    return final_text

                reply_text = await asyncio.wait_for(_run_agent(), timeout=self._cfg.handle_timeout_s)
            except asyncio.TimeoutError:
                reply_text = "抱歉，我的处理超时了。请稍后再试。"
            except Exception as e:
                logger.exception(f"处理外部消息失败：{e}")
                reply_text = "抱歉，我处理消息时发生错误。"

            if not reply_text:
                reply_text = "抱歉，我这边暂时无法生成回复。"

            # 将回复回写给桥接服务
            if self._ws is not None:
                payload = {
                    "type": "send",
                    "to": chat_id,
                    "text": reply_text,
                }
                await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def run(self) -> None:
        import websockets

        self._running = True
        backoff_s = 1.0

        while self._running:
            try:
                logger.info(f"连接外部桥接：{self._cfg.url}")
                self._ws = await asyncio.wait_for(websockets.connect(self._cfg.url), timeout=self._cfg.connect_timeout_s)

                # 可选鉴权握手（对齐 bridge/server.ts：收到第一条 auth token）
                if self._cfg.token:
                    await self._ws.send(json.dumps({"type": "auth", "token": self._cfg.token}, ensure_ascii=False))

                backoff_s = 1.0
                async for raw in self._ws:
                    await self._handle_bridge_message(raw)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"外部桥接连接/运行失败：{e}（{backoff_s} 秒后重连）")
                self._ws = None
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, 30.0)

            finally:
                if self._ws is not None:
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                    self._ws = None

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None


__all__ = ["ExternalSoftwareReceiver"]

