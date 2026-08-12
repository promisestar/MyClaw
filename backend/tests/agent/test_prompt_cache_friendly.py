"""系统提示词缓存友好：turn_context 与会话内冻结 system。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.agent.enhanced_simple_agent import (  # noqa: E402
    compose_turn_user_content,
)
from src.agent.myclaw_agent import MyClawAgent  # noqa: E402


class TestComposeTurnUserContent(unittest.TestCase):
    def test_no_context_returns_input(self):
        self.assertEqual(compose_turn_user_content("hello"), "hello")
        self.assertEqual(compose_turn_user_content("hello", None), "hello")
        self.assertEqual(compose_turn_user_content("hello", "  "), "hello")

    def test_context_prefixed_with_stable_template(self):
        out = compose_turn_user_content("用户问题", "## 相关记忆（自动注入）\n1. [pref] 喜欢简洁")
        self.assertIn("## 本轮上下文", out)
        self.assertIn("## 相关记忆（自动注入）", out)
        self.assertIn("---", out)
        self.assertTrue(out.endswith("用户问题"))
        # 分隔后才是干净原文
        self.assertEqual(out.split("---\n\n", 1)[1], "用户问题")


class TestComposeTurnContext(unittest.TestCase):
    def test_joins_non_empty(self):
        joined = MyClawAgent._compose_turn_context(
            "",
            "  mem  ",
            None,
            MyClawAgent._plan_planning_instruction(),
        )
        self.assertIsNotNone(joined)
        assert joined is not None
        self.assertIn("mem", joined)
        self.assertIn("规划模式指令", joined)
        self.assertNotIn("相关记忆", joined)

    def test_all_empty_returns_none(self):
        self.assertIsNone(MyClawAgent._compose_turn_context("", None, "   "))


class TestEnsureSessionSystemPrompt(unittest.TestCase):
    def _stub(self) -> SimpleNamespace:
        identity = SimpleNamespace(is_onboarding_completed=lambda: True)
        builds = []

        def _build():
            builds.append(1)
            return f"BASE-{len(builds)}"

        stub = SimpleNamespace(
            _agent=SimpleNamespace(system_prompt="init"),
            _current_session_id="sess-a",
            _current_workspace="/ws",
            identity=identity,
            _build_system_prompt=_build,
            _system_prompt_frozen=False,
            _system_prompt_fingerprint=None,
            _onboarding_was_incomplete=False,
            _builds=builds,
        )
        return stub

    def test_force_rebuilds_once_then_stable(self):
        stub = self._stub()
        MyClawAgent.ensure_session_system_prompt(stub, force=True)
        first = stub._agent.system_prompt
        MyClawAgent.ensure_session_system_prompt(stub)
        MyClawAgent.ensure_session_system_prompt(stub)
        self.assertEqual(stub._agent.system_prompt, first)
        self.assertEqual(len(stub._builds), 1)

    def test_session_change_forces_rebuild(self):
        stub = self._stub()
        MyClawAgent.ensure_session_system_prompt(stub, force=True)
        stub._current_session_id = "sess-b"
        MyClawAgent.ensure_session_system_prompt(stub)
        self.assertEqual(len(stub._builds), 2)
        self.assertEqual(stub._agent.system_prompt, "BASE-2")

    def test_onboarding_complete_forces_rebuild(self):
        stub = self._stub()
        stub._onboarding_was_incomplete = True
        done = {"v": False}
        stub.identity = SimpleNamespace(
            is_onboarding_completed=lambda: done["v"],
        )
        MyClawAgent.ensure_session_system_prompt(stub, force=True)
        self.assertEqual(len(stub._builds), 1)
        done["v"] = True
        MyClawAgent.ensure_session_system_prompt(stub)
        self.assertEqual(len(stub._builds), 2)


class TestComposeMultimodal(unittest.TestCase):
    def test_compose_multimodal_with_context_stays_encoded(self):
        from src.agent.multimodal_bridge import (
            decode_multimodal_content,
            encode_multimodal_content,
            is_encoded_multimodal,
        )

        original = encode_multimodal_content(
            [
                {"type": "text", "text": "看这张图"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/a.png"},
                },
            ]
        )
        self.assertTrue(is_encoded_multimodal(original))
        merged = compose_turn_user_content(original, "## 相关记忆（自动注入）\nx")
        self.assertTrue(is_encoded_multimodal(merged))
        decoded = decode_multimodal_content(merged)
        self.assertIsInstance(decoded, list)
        self.assertEqual(decoded[0]["type"], "text")
        self.assertIn("本轮上下文", decoded[0]["text"])
        self.assertIn("相关记忆", decoded[0]["text"])
        # 原文本与图片仍在
        texts = [p.get("text", "") for p in decoded if p.get("type") == "text"]
        self.assertTrue(any("看这张图" in t for t in texts))
        self.assertTrue(any(p.get("type") == "image_url" for p in decoded))

    def test_compose_multimodal_exception_keeps_encoding(self):
        from src.agent import enhanced_simple_agent as esa
        from src.agent.multimodal_bridge import encode_multimodal_content, is_encoded_multimodal
        from unittest.mock import patch

        original = encode_multimodal_content(
            [{"type": "text", "text": "hi"}]
        )
        with patch(
            "src.agent.multimodal_bridge.decode_multimodal_content",
            side_effect=RuntimeError("boom"),
        ):
            out = esa.compose_turn_user_content(original, "ctx")
        self.assertEqual(out, original)
        self.assertTrue(is_encoded_multimodal(out))
        self.assertNotIn("## 本轮上下文", out)


class TestRebindFingerprint(unittest.TestCase):
    def test_rebind_updates_session_without_rebuild(self):
        builds = []

        def _build():
            builds.append(1)
            return "BASE"

        stub = SimpleNamespace(
            _agent=SimpleNamespace(system_prompt="BASE"),
            _current_session_id="s1",
            _current_workspace="/ws",
            identity=SimpleNamespace(is_onboarding_completed=lambda: True),
            _build_system_prompt=_build,
            _system_prompt_frozen=True,
            _system_prompt_fingerprint=("s1", "/ws", True),
            _onboarding_was_incomplete=False,
        )
        stub._current_session_id = "s2"
        MyClawAgent._rebind_system_prompt_fingerprint(stub)
        self.assertEqual(stub._system_prompt_fingerprint, ("s2", "/ws", True))
        MyClawAgent.ensure_session_system_prompt(stub)
        self.assertEqual(len(builds), 0)


class TestBuildMessagesKeepsHistoryClean(unittest.TestCase):
    """_build_messages 末条含上下文；run 写入历史用干净原文。"""

    def test_api_user_contains_context(self):
        agent = MagicMock()
        agent.system_prompt = "SYS"
        agent._history = []
        agent.context_manager = MagicMock()
        agent.context_manager.maybe_compress_history.return_value = False
        agent.context_manager.maybe_compress_messages.return_value = False

        from src.agent.enhanced_simple_agent import EnhancedSimpleAgent

        messages = EnhancedSimpleAgent._build_messages(
            agent,
            "干净用户原文",
            turn_context="## 相关记忆（自动注入）\nx",
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], "SYS")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn("相关记忆（自动注入）", messages[-1]["content"])
        self.assertIn("干净用户原文", messages[-1]["content"])
        clean = compose_turn_user_content("干净用户原文", None)
        self.assertEqual(clean, "干净用户原文")
        self.assertNotIn("相关记忆", clean)

    def test_run_add_message_excludes_turn_context(self):
        """run() 必须把干净 input_text 写入历史，而非合并后的 API content。"""
        from hello_agents.core.message import Message
        from src.agent.enhanced_simple_agent import EnhancedSimpleAgent

        agent = MagicMock(spec=EnhancedSimpleAgent)
        agent.system_prompt = "SYS"
        agent._history = []
        agent.config = SimpleNamespace(trace_enabled=False)
        agent.enable_tool_calling = False
        agent.tool_registry = None
        agent.context_manager = MagicMock()
        agent.context_manager.maybe_compress_history.return_value = False
        agent.context_manager.maybe_compress_messages.return_value = False
        agent.llm = MagicMock()
        agent.llm.invoke.return_value = SimpleNamespace(content="ok", usage={}, latency_ms=0)
        saved = []

        def _add(msg):
            saved.append(msg)

        agent.add_message = _add
        agent._build_messages = lambda text, turn_context=None: [
            {"role": "system", "content": "SYS"},
            {
                "role": "user",
                "content": compose_turn_user_content(text, turn_context),
            },
        ]

        result = EnhancedSimpleAgent.run(
            agent,
            "干净用户原文",
            turn_context="## 相关记忆（自动注入）\n1. x",
        )
        self.assertEqual(result, "ok")
        self.assertGreaterEqual(len(saved), 1)
        user_msg = saved[0]
        self.assertIsInstance(user_msg, Message)
        self.assertEqual(user_msg.role, "user")
        self.assertEqual(user_msg.content, "干净用户原文")
        self.assertNotIn("相关记忆", user_msg.content)
        self.assertNotIn("本轮上下文", user_msg.content)


if __name__ == "__main__":
    unittest.main()
