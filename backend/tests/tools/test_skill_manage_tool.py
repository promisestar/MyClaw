"""SkillManageTool 单测。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.skills.loader import SkillLoader  # noqa: E402
from src.tools.builtin.skill_manage_tool import SkillManageTool  # noqa: E402


def _skill_md(name: str) -> str:
    return f"""---
name: {name}
description: Test skill for manage tool.
---

# {name}

Step one.
"""


class TestSkillManageTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "skills"
        self.ws.mkdir()
        self.loader = SkillLoader(self.ws)
        self.refresh_calls = 0

        def _on_changed():
            self.refresh_calls += 1

        self.tool = SkillManageTool(
            self.loader, on_changed=_on_changed, agent_created_default=False
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _data(self, resp):
        data = getattr(resp, "data", None)
        if isinstance(data, dict) and data:
            return data
        text = getattr(resp, "text", None) or ""
        return json.loads(text)

    def _is_error(self, resp) -> bool:
        status = getattr(resp, "status", None)
        if status is not None and str(getattr(status, "value", status)).lower() == "error":
            return True
        if getattr(resp, "error_info", None):
            return True
        return False

    def test_create_patch_view_delete(self):
        r = self.tool.run(
            {
                "action": "create",
                "name": "flow",
                "content": _skill_md("flow"),
            }
        )
        self.assertFalse(self._is_error(r), getattr(r, "text", r))
        data = self._data(r)
        self.assertTrue(data.get("success"))
        self.assertFalse(data.get("agent_created"))
        self.assertGreaterEqual(self.refresh_calls, 1)

        store = self.loader.usage_store_for("flow")
        self.assertFalse(store.is_curator_managed("flow"))

        r2 = self.tool.run(
            {
                "action": "patch",
                "name": "flow",
                "old_string": "Step one.",
                "new_string": "Step one. Step two.",
            }
        )
        self.assertFalse(self._is_error(r2), getattr(r2, "text", r2))
        data2 = self._data(r2)
        self.assertEqual(data2.get("replacements"), 1)
        self.assertEqual(store.get_record("flow")["patch_count"], 1)

        r3 = self.tool.run({"action": "view", "name": "flow"})
        self.assertFalse(self._is_error(r3), getattr(r3, "text", r3))
        data3 = self._data(r3)
        self.assertIn("Step two", data3.get("content", ""))
        self.assertEqual(store.get_record("flow")["view_count"], 1)

        r4 = self.tool.run({"action": "delete", "name": "flow"})
        self.assertFalse(self._is_error(r4), getattr(r4, "text", r4))
        data4 = self._data(r4)
        self.assertIn("archived_to", data4)
        self.assertNotIn("flow", self.loader.metadata_cache)

    def test_pin_blocks_delete(self):
        self.tool.run(
            {"action": "create", "name": "keep", "content": _skill_md("keep")}
        )
        self.loader.usage_store_for("keep").set_pinned("keep", True)
        r = self.tool.run({"action": "delete", "name": "keep"})
        self.assertTrue(self._is_error(r), "expected pin error")
        blob = json.dumps(
            {
                "text": getattr(r, "text", None),
                "message": getattr(r, "message", None),
                "context": getattr(r, "context", None),
                "data": getattr(r, "data", None),
            },
            ensure_ascii=False,
            default=str,
        )
        self.assertIn("PINNED", blob)
        self.assertIn("keep", self.loader.metadata_cache)


if __name__ == "__main__":
    unittest.main()
