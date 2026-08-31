"""L0：ToolModeFilter / Plan 解析 / TodoScheduler / Bash / ContextGuard / Cancel。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# 避免经 agent/__init__ 拉起重依赖：独立加载 tool_mode_filter
import importlib.util  # noqa: E402

_tmf_path = BACKEND_ROOT / "src" / "agent" / "tool_mode_filter.py"
_spec = importlib.util.spec_from_file_location("tool_mode_filter_eval", _tmf_path)
_tmf = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_tmf)
ToolMode = _tmf.ToolMode
ToolModeFilter = _tmf.ToolModeFilter
SIDE_EFFECT_TOOLS = _tmf.SIDE_EFFECT_TOOLS

from src.agent.cancel_token import CancellationToken  # noqa: E402
from src.agent.todo_scheduler import TodoScheduler  # noqa: E402
from src.context.context_guard import ContextGuard  # noqa: E402
from src.tools.builtin.bash import BashTool  # noqa: E402


def _load_myclaw_parse():
    """延迟导入 MyClawAgent（较重），仅用于 plan 解析测试。"""
    from src.agent.myclaw_agent import MyClawAgent

    return MyClawAgent


class TestSideEffectToolNames(unittest.TestCase):
    def test_execute_command_is_gated(self):
        self.assertIn("execute_command", SIDE_EFFECT_TOOLS)
        self.assertIn("Write", SIDE_EFFECT_TOOLS)
        self.assertIn("Edit", SIDE_EFFECT_TOOLS)
        self.assertIn("memory_add", SIDE_EFFECT_TOOLS)
        self.assertIn("automation", SIDE_EFFECT_TOOLS)


class TestToolModeFilter(unittest.TestCase):
    def setUp(self):
        tools = {
            "Read": object(),
            "Write": object(),
            "Edit": object(),
            "execute_command": object(),
            "memory_add": object(),
            "memory_search": object(),
            "automation": object(),
            "web_search": object(),
        }
        registry = MagicMock()
        registry.list_tools.return_value = list(tools.keys())
        registry.get_tool.side_effect = lambda name: tools.get(name)
        # 部分实现可能用 get_all_tools / schemas
        registry.get_all_tools.return_value = [
            SimpleNamespace(name=n) for n in tools
        ]
        self.filter = ToolModeFilter(registry)

    def test_read_only_hides_side_effects(self):
        self.filter.set_mode(ToolMode.READ_ONLY)
        names = set(self.filter.get_available_tool_names())
        self.assertIn("Read", names)
        self.assertIn("web_search", names)
        self.assertIn("memory_search", names)
        for bad in ("Write", "Edit", "execute_command", "automation", "memory_add"):
            self.assertNotIn(bad, names)
            self.assertIsNone(self.filter.get_tool(bad))

    def test_full_exposes_all(self):
        self.filter.set_mode(ToolMode.FULL)
        names = set(self.filter.get_available_tool_names())
        self.assertIn("Write", names)
        self.assertIn("execute_command", names)
        self.assertIsNotNone(self.filter.get_tool("Write"))


class TestPlanParse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        MyClawAgent = _load_myclaw_parse()
        cls.parse = MyClawAgent._parse_plan_from_response

    def test_json_fence(self):
        text = (
            "分析如下。\n"
            "```json\n"
            '[{"id":"todo_1","description":"读文件","dependencies":[],"tools_required":["Read"]},'
            '{"id":"todo_2","description":"改文件","dependencies":["todo_1"],"tools_required":["Edit"]}]\n'
            "```\n"
        )
        agent = SimpleNamespace()
        plan = type(self).parse(agent, text)
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0]["description"], "读文件")

    def test_missing_description_rejected(self):
        text = '```json\n[{"id":"todo_1","dependencies":[]}]\n```'
        agent = SimpleNamespace()
        self.assertEqual(type(self).parse(agent, text), [])

    def test_garbage_returns_empty(self):
        agent = SimpleNamespace()
        self.assertEqual(type(self).parse(agent, "没有计划"), [])


class TestTodoScheduler(unittest.TestCase):
    def test_dependency_and_downstream_fail(self):
        sched = TodoScheduler()
        sched.load_from_plan(
            [
                {"id": "a", "description": "A", "dependencies": []},
                {"id": "b", "description": "B", "dependencies": ["a"]},
                {"id": "c", "description": "C", "dependencies": ["b"]},
            ]
        )
        ready = {t.id for t in sched.get_ready_tasks()}
        self.assertEqual(ready, {"a"})

        sched.mark_completed("a")
        ready = {t.id for t in sched.get_ready_tasks()}
        self.assertEqual(ready, {"b"})

        sched.mark_failed("b", "boom")
        self.assertEqual(sched.get_by_id("b").status, "failed")
        self.assertEqual(sched.get_by_id("c").status, "failed")

        summary = sched.get_progress_summary()
        self.assertIn("计划执行进度", summary)


class TestBashSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.tool = BashTool(
            allowed_directories=[self.root],
            default_workdir=self.root,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_blocks_rm_rf_root(self):
        resp = self.tool.run({"command": "rm -rf /"})
        # ToolResponse 可能是对象或 dict-like
        code = getattr(resp, "error_code", None) or getattr(resp, "code", None)
        msg = str(getattr(resp, "message", "") or getattr(resp, "content", "") or resp)
        if code is None and hasattr(resp, "error"):
            err = resp.error
            code = getattr(err, "code", None) if err else None
            msg = str(err or msg)
        # 兼容不同 ToolResponse 形状
        blob = f"{code}\n{msg}\n{resp}"
        self.assertIn("COMMAND_BLOCKED", blob)

    def test_echo_ok(self):
        resp = self.tool.run({"command": "echo myclaw_eval_ok"})
        blob = str(getattr(resp, "content", None) or getattr(resp, "data", None) or resp)
        # 某些实现把 stdout 放在 data/result
        if hasattr(resp, "model_dump"):
            blob = str(resp.model_dump())
        elif hasattr(resp, "__dict__"):
            blob = blob + str(resp.__dict__)
        self.assertIn("myclaw_eval_ok", blob)


class TestContextGuard(unittest.TestCase):
    def test_no_delegate_side_effects(self):
        guard = ContextGuard(orchestrator=None)
        for name in ("Write", "Edit", "memory_add", "task", "subagent", "Skill", "browser", "automation"):
            self.assertIn(name, ContextGuard.NO_DELEGATE_TOOLS)
            decision = guard.decide(name)
            self.assertNotEqual(decision, "delegate", msg=f"{name} -> {decision}")

    def test_small_tool_inline(self):
        guard = ContextGuard(orchestrator=None)
        # calculator / 极小输出
        d = guard.decide("calculator")
        self.assertIn(d, ("inline", "snip"))


class TestCancellationToken(unittest.TestCase):
    def test_cancel(self):
        token = CancellationToken()
        self.assertFalse(token.is_cancelled)
        token.cancel("user_requested")
        self.assertTrue(token.is_cancelled)
        self.assertEqual(token.reason, "user_requested")


class TestEvalScorerReadonly(unittest.TestCase):
    def test_ask_gate_detects_write(self):
        from eval.harness.scorers import score_scenario
        from eval.harness.types import Expectation, Scenario, StreamTrace

        sc = Scenario(
            id="t",
            title="t",
            message="x",
            mode="ask",
            expect=Expectation(forbid_successful_tools=["Write"], min_done_chars=1),
        )
        trace = StreamTrace(
            events=[{"event": "done", "data": {}}],
            tool_finishes=[{"tool": "Write", "result": "ok written"}],
            done_content="done",
        )
        result = score_scenario(sc, trace)
        self.assertFalse(result.hard_pass)

        trace2 = StreamTrace(
            events=[{"event": "done", "data": {}}],
            tool_finishes=[{"tool": "Write", "result": "当前为只读模式，该工具被禁用"}],
            done_content="只能解释，不能改",
        )
        result2 = score_scenario(sc, trace2)
        self.assertTrue(result2.hard_pass)


if __name__ == "__main__":
    unittest.main()
