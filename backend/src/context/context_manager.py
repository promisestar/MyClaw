"""多粒度上下文压缩管理器。

参考 CoreCoder 的分层策略，在达到不同 token 阈值时依次采用更激进的压缩手段：
  Layer 1 (tool_snip)      - 截断冗长的 tool 输出
  Layer 2 (summarize)        - 将旧对话轮次压缩为摘要并保留最近轮次
  Layer 3 (hard_collapse)    - 紧急压缩：更短摘要 + 更少保留轮次
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from hello_agents.core.config import Config
from hello_agents.core.message import Message
from hello_agents.context.history import HistoryManager
from hello_agents.context.token_counter import TokenCounter

from .tokenizer import count_tokens, count_messages

if TYPE_CHECKING:
    from hello_agents.core.llm import HelloAgentsLLM


def _approx_tokens(text: str) -> int:
    """粗略 token 估算（回退，仅在无 tokenizer 信息时使用）。

    改进版区分中英文：
    - 中文 ~0.5 token/字     英文 ~0.25 token/字符
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)
    latin = len(text) - cjk
    return max(1, int(cjk * 0.5 + latin * 0.25))


def estimate_dict_messages_tokens(messages: List[Dict[str, Any]]) -> int:
    """估算 API 消息列表的 token 数。"""
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if content:
            total += _approx_tokens(str(content))
        if m.get("tool_calls"):
            total += _approx_tokens(str(m["tool_calls"]))
    return total


class ContextManager:
    """上下文管理：token 追踪 + 多粒度压缩。"""

    def __init__(
        self,
        config: Config,
        history_manager: HistoryManager,
        token_counter: TokenCounter,
        llm: Optional["HelloAgentsLLM"] = None,
        *,
        snip_ratio: float = 0.50,
        summarize_ratio: float = 0.70,
        collapse_ratio: float = 0.90,
        hard_retain_rounds: int = 4,
        tool_snip_chars: int = 1500,
    ):
        self.config = config
        self.history_manager = history_manager
        self.token_counter = token_counter
        self.llm = llm
        self.hard_retain_rounds = hard_retain_rounds
        self.tool_snip_chars = tool_snip_chars

        # 压缩比例（保存以便 update_context_window 重算阈值时使用）
        self._snip_ratio = snip_ratio
        self._summarize_ratio = summarize_ratio
        self._collapse_ratio = collapse_ratio

        self.max_tokens = config.context_window
        self._snip_at = int(self.max_tokens * snip_ratio)
        self._summarize_at = int(self.max_tokens * summarize_ratio)
        self._collapse_at = int(self.max_tokens * collapse_ratio)

        self._history_token_count = 0
        self._summary_llm: Optional["HelloAgentsLLM"] = None

        # 精确 token 统计所需的模型信息
        self._token_model: Optional[str] = None
        self._token_base_url: Optional[str] = None

    def set_model_context(self, model: str, base_url: Optional[str] = None):
        """注入当前模型信息以启用精确 token 统计。

        由 MyClawAgent 在初始化 LLM 后调用。

        Args:
            model: 模型名称（如 "glm-4", "gpt-4o"）
            base_url: API 服务地址
        """
        self._token_model = model
        self._token_base_url = base_url

    def update_context_window(self, new_max_tokens: int):
        """动态更新上下文窗口大小及所有压缩阈值。

        当 LLM 模型切换导致上下文窗口变化时调用。

        Args:
            new_max_tokens: 新的上下文窗口大小（token 数）
        """
        if new_max_tokens <= 0:
            return

        old = self.max_tokens
        self.max_tokens = new_max_tokens
        self._snip_at = int(self.max_tokens * self._snip_ratio)
        self._summarize_at = int(self.max_tokens * self._summarize_ratio)
        self._collapse_at = int(self.max_tokens * self._collapse_ratio)

        if old != new_max_tokens:
            print(
                f"📐 上下文窗口已更新: {old:,} → {new_max_tokens:,} tokens "
                f"(snip={self._snip_at:,}, summarize={self._summarize_at:,}, "
                f"collapse={self._collapse_at:,})"
            )

    def count_tokens(self, text: str) -> int:
        """精确 token 计数（优先 tiktoken，降级估算）"""
        if self._token_model is not None:
            return count_tokens(text, self._token_model, self._token_base_url)
        return _approx_tokens(text)

    # ------------------------------------------------------------------ #
    # Token 追踪
    # ------------------------------------------------------------------ #

    @property
    def history_token_count(self) -> int:
        return self._history_token_count

    def reset(self) -> None:
        """清空历史 token 计数（配合 clear_history）。"""
        self._history_token_count = 0
        self.token_counter.clear_cache()

    def recalculate_history_tokens(self) -> int:
        """全量重算历史 token（压缩后调用）。优先精确计数。"""
        if self._token_model:
            history = self.history_manager.get_history()
            # 转换为 dict 格式以使用精确计数
            messages = []
            for msg in history:
                item = {"role": msg.role, "content": msg.content or ""}
                metadata = getattr(msg, "metadata", None) or {}
                if msg.role == "assistant" and metadata.get("tool_calls"):
                    item["tool_calls"] = metadata["tool_calls"]
                messages.append(item)
            self._history_token_count = count_messages(
                messages, self._token_model, self._token_base_url
            )
        else:
            history = self.history_manager.get_history()
            self._history_token_count = self.token_counter.count_messages(history)
        return self._history_token_count

    def on_message_added(self, message: Message) -> bool:
        """历史新增一条消息后：更新 token 并视情况压缩历史。"""
        self._history_token_count += self.token_counter.count_message(message)
        return self.maybe_compress_history()

    def estimate_turn_tokens(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
    ) -> int:
        """估算本轮 LLM 调用的总 token（含 system）。优先精确计数。"""
        if self._token_model:
            total = count_messages(messages, self._token_model, self._token_base_url)
        else:
            total = estimate_dict_messages_tokens(messages)
        if system_prompt and not any(m.get("role") == "system" for m in messages):
            total += self.count_tokens(system_prompt)
        return total

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #

    def maybe_compress_history(self) -> bool:
        """按 token 阈值对持久化历史执行分层压缩。返回是否发生压缩。"""
        current = self._history_token_count
        compressed = False

        if current > self._snip_at:
            if self._snip_history_tool_outputs():
                compressed = True
                current = self.recalculate_history_tokens()

        if current > self._summarize_at:
            if self._summarize_history(aggressive=False):
                compressed = True
                current = self.recalculate_history_tokens()

        if current > self._collapse_at:
            if self._summarize_history(aggressive=True):
                compressed = True
                self.recalculate_history_tokens()

        return compressed

    def maybe_compress_messages(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
    ) -> bool:
        """对即将送入 LLM 的消息列表执行分层压缩（原地修改）。返回是否发生压缩。"""
        current = self.estimate_turn_tokens(messages, system_prompt)
        compressed = False

        if current > self._snip_at:
            if self._snip_dict_tool_outputs(messages):
                compressed = True
                current = self.estimate_turn_tokens(messages, system_prompt)

        if current > self._summarize_at and len(messages) > 10:
            if self._summarize_dict_messages(messages, keep_recent=8):
                compressed = True
                current = self.estimate_turn_tokens(messages, system_prompt)

        if current > self._collapse_at and len(messages) > 4:
            self._hard_collapse_dict_messages(messages, keep_recent=4)
            compressed = True

        return compressed

    def prepare_turn(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
    ) -> bool:
        """一轮对话开始：先压缩历史，再压缩当前消息列表。"""
        history_changed = self.maybe_compress_history()
        messages_changed = self.maybe_compress_messages(messages, system_prompt)
        return history_changed or messages_changed

    # ------------------------------------------------------------------ #
    # Layer 1: 截断 tool 输出
    # ------------------------------------------------------------------ #

    def _snip_history_tool_outputs(self) -> bool:
        """原地截断 HistoryManager 中的 tool 消息（需直接访问内部列表）。"""
        changed = False
        for msg in self.history_manager._history:
            if msg.role != "tool":
                continue
            if self._snip_text_content(msg):
                changed = True
        return changed

    def _snip_dict_tool_outputs(self, messages: List[Dict[str, Any]]) -> bool:
        changed = False
        for m in messages:
            if m.get("role") != "tool":
                continue
            content = m.get("content", "")
            if not isinstance(content, str) or len(content) <= self.tool_snip_chars:
                continue
            snipped = self._snip_content_text(content)
            if snipped != content:
                m["content"] = snipped
                changed = True
        return changed

    def _snip_text_content(self, message: Message) -> bool:
        content = message.content or ""
        snipped = self._snip_content_text(content)
        if snipped != content:
            message.content = snipped
            return True
        return False

    def _snip_content_text(self, content: str) -> str:
        if len(content) <= self.tool_snip_chars:
            return content
        lines = content.splitlines()
        if len(lines) <= 6:
            return content
        return (
            "\n".join(lines[:3])
            + f"\n... ({len(lines)} lines, snipped to save context) ...\n"
            + "\n".join(lines[-3:])
        )

    # ------------------------------------------------------------------ #
    # Layer 2/3: 历史摘要压缩（HistoryManager）
    # ------------------------------------------------------------------ #

    def _summarize_history(self, aggressive: bool) -> bool:
        history = self.history_manager.get_history()
        if not history:
            return False

        rounds = self.history_manager.estimate_rounds()
        retain = (
            min(self.config.min_retain_rounds, self.hard_retain_rounds)
            if aggressive
            else self.config.min_retain_rounds
        )
        if rounds <= retain:
            return False

        if self.config.enable_smart_compression:
            summary = self._generate_smart_summary(history, retain_rounds=retain)
        else:
            summary = self._generate_simple_summary(history, retain_rounds=retain)

        before_len = len(history)
        original_min_retain = self.history_manager.min_retain_rounds
        try:
            if aggressive:
                self.history_manager.min_retain_rounds = retain
            self.history_manager.compress(summary)
        finally:
            self.history_manager.min_retain_rounds = original_min_retain

        after_len = len(self.history_manager.get_history())
        if after_len < before_len:
            label = "硬压缩" if aggressive else "摘要压缩"
            print(f"📦 上下文{label}：{before_len} 条 → {after_len} 条")
            return True
        return False

    def _generate_simple_summary(
        self, history: List[Message], retain_rounds: int
    ) -> str:
        rounds = self.history_manager.estimate_rounds()
        user_msgs = sum(1 for msg in history if msg.role == "user")
        assistant_msgs = sum(1 for msg in history if msg.role == "assistant")

        return f"""此会话包含 {rounds} 轮对话：
- 用户消息：{user_msgs} 条
- 助手消息：{assistant_msgs} 条
- 总消息数：{len(history)} 条

（历史已压缩，保留最近 {retain_rounds} 轮完整对话）"""

    def _generate_smart_summary(
        self, history: List[Message], retain_rounds: int
    ) -> str:
        boundaries = self.history_manager.find_round_boundaries()
        if len(boundaries) <= retain_rounds:
            return self._generate_simple_summary(history, retain_rounds)

        keep_from_index = boundaries[-retain_rounds]
        to_compress = history[:keep_from_index]
        if not to_compress:
            return self._generate_simple_summary(history, retain_rounds)

        history_text = self._format_history_for_summary(to_compress)
        summary_prompt = f"""请将以下对话历史压缩为结构化摘要，保留关键信息：

## 对话历史
{history_text}

## 摘要要求
1. **任务目标**：用户想要完成什么？
2. **关键决策**：做了哪些重要决定？
3. **已完成工作**：完成了哪些任务？（列表形式）
4. **待处理事项**：还有什么未完成？
5. **重要发现**：有哪些关键信息或问题？

请用简洁的中文输出，每部分不超过 3 行。"""

        try:
            summary_llm = self._get_summary_llm()
            summary = summary_llm.invoke(
                [
                    {
                        "role": "system",
                        "content": "你是一个专业的对话摘要助手，擅长提取关键信息。",
                    },
                    {"role": "user", "content": summary_prompt},
                ],
                temperature=self.config.summary_temperature,
                max_tokens=self.config.summary_max_tokens,
            )
            return f"""## 历史摘要（{len(to_compress)} 条消息）
{summary}

---
（已压缩，保留最近 {retain_rounds} 轮完整对话）"""
        except Exception as exc:
            print(f"⚠️ 智能摘要生成失败: {exc}，使用简单摘要")
            return self._generate_simple_summary(history, retain_rounds)

    @staticmethod
    def _format_history_for_summary(history: List[Message]) -> str:
        # 延迟导入，避免循环依赖
        from ..agent.multimodal_bridge import (
            decode_multimodal_content,
            is_encoded_multimodal,
        )
        from ..multimodal import flatten_content_to_text

        lines = []
        for msg in history:
            raw = msg.content
            if is_encoded_multimodal(raw):
                raw = decode_multimodal_content(raw)
            text = flatten_content_to_text(raw) if not isinstance(raw, str) else raw
            content = text[:500] if len(text) > 500 else text
            lines.append(f"[{msg.role}]: {content}")
        return "\n\n".join(lines)

    def _get_summary_llm(self) -> "HelloAgentsLLM":
        if self._summary_llm is None:
            from hello_agents.core.llm import HelloAgentsLLM

            self._summary_llm = HelloAgentsLLM(
                provider=self.config.summary_llm_provider,
                model=self.config.summary_llm_model,
                temperature=self.config.summary_temperature,
                max_tokens=self.config.summary_max_tokens,
            )
        return self._summary_llm

    # ------------------------------------------------------------------ #
    # Layer 2/3: 运行时消息列表压缩（dict 格式，用于单次 LLM 调用）
    # ------------------------------------------------------------------ #

    def _summarize_dict_messages(
        self, messages: List[Dict[str, Any]], keep_recent: int = 8
    ) -> bool:
        if len(messages) <= keep_recent:
            return False

        old = messages[:-keep_recent]
        tail = messages[-keep_recent:]
        summary = self._get_dict_summary(old)

        messages.clear()
        messages.append(
            {
                "role": "user",
                "content": f"[Context compressed - conversation summary]\n{summary}",
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": "Got it, I have the context from our earlier conversation.",
            }
        )
        messages.extend(tail)
        print(f"📦 运行时上下文摘要压缩（保留最近 {keep_recent} 条消息）")
        return True

    def _hard_collapse_dict_messages(
        self, messages: List[Dict[str, Any]], keep_recent: int = 4
    ) -> None:
        tail = messages[-keep_recent:] if len(messages) > keep_recent else messages[-2:]
        summary = self._get_dict_summary(messages[: -len(tail)])

        messages.clear()
        messages.append(
            {
                "role": "user",
                "content": f"[Hard context reset]\n{summary}",
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": "Context restored. Continuing from where we left off.",
            }
        )
        messages.extend(tail)
        print(f"📦 运行时上下文硬压缩（保留最近 {len(tail)} 条消息）")

    def _get_dict_summary(self, messages: List[Dict[str, Any]]) -> str:
        flat = self._flatten_dict_messages(messages)
        if self.llm:
            try:
                resp = self.llm.invoke(
                    [
                        {
                            "role": "system",
                            "content": (
                                "Compress this conversation into a brief summary. "
                                "Preserve: file paths edited, key decisions made, "
                                "errors encountered, current task state. "
                                "Drop: verbose command output, code listings, "
                                "redundant back-and-forth. Reply in Chinese."
                            ),
                        },
                        {"role": "user", "content": flat[:15000]},
                    ],
                    temperature=self.config.summary_temperature,
                    max_tokens=self.config.summary_max_tokens,
                )
                return resp if isinstance(resp, str) else str(resp)
            except Exception:
                pass
        return self._extract_key_info(messages)

    @staticmethod
    def _flatten_dict_messages(messages: List[Dict[str, Any]]) -> str:
        parts = []
        for m in messages:
            role = m.get("role", "?")
            text = m.get("content", "") or ""
            if text:
                parts.append(f"[{role}] {str(text)[:400]}")
        return "\n".join(parts)

    @staticmethod
    def _extract_key_info(messages: List[Dict[str, Any]]) -> str:
        files_seen: set[str] = set()
        errors: List[str] = []

        for m in messages:
            text = str(m.get("content", "") or "")
            for match in re.finditer(r"[\w./\-]+\.\w{1,5}", text):
                files_seen.add(match.group())
            for line in text.splitlines():
                if "error" in line.lower():
                    errors.append(line.strip()[:150])

        parts = []
        if files_seen:
            parts.append(f"Files touched: {', '.join(sorted(files_seen)[:20])}")
        if errors:
            parts.append(f"Errors seen: {'; '.join(errors[:5])}")
        return "\n".join(parts) or "(no extractable context)"
