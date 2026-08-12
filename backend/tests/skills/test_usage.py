"""SkillUsageStore 单测。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.skills.usage import (  # noqa: E402
    SkillUsageStore,
    STATE_ACTIVE,
    STATE_STALE,
    STATE_ARCHIVED,
    latest_activity_at,
    is_curator_managed_record,
)


class TestSkillUsageStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.skills_dir = Path(self.tmp.name) / "skills"
        self.skills_dir.mkdir()
        self.store = SkillUsageStore(self.skills_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bump_use_and_patch(self):
        self.store.bump_use("demo")
        self.store.bump_view("demo")
        self.store.bump_patch("demo")
        rec = self.store.get_record("demo")
        self.assertEqual(rec["use_count"], 1)
        self.assertEqual(rec["view_count"], 1)
        self.assertEqual(rec["patch_count"], 1)
        self.assertIsNotNone(latest_activity_at(rec))

    def test_record_created_user_vs_agent(self):
        self.store.record_created("user-skill", agent_created=False)
        self.assertFalse(self.store.is_curator_managed("user-skill"))
        self.store.record_created("agent-skill", agent_created=True)
        self.assertTrue(self.store.is_curator_managed("agent-skill"))
        self.assertTrue(is_curator_managed_record(self.store.get_record("agent-skill")))

    def test_pin_and_state(self):
        self.store.record_created("x", agent_created=True)
        self.store.set_pinned("x", True)
        self.assertTrue(self.store.is_pinned("x"))
        self.store.set_state("x", STATE_STALE)
        self.assertEqual(self.store.get_record("x")["state"], STATE_STALE)
        self.store.set_state("x", STATE_ARCHIVED)
        self.assertIsNotNone(self.store.get_record("x")["archived_at"])
        self.store.set_state("x", STATE_ACTIVE)
        self.assertIsNone(self.store.get_record("x")["archived_at"])

    def test_forget_and_curated_report(self):
        self.store.record_created("a", agent_created=True)
        self.store.record_created("b", agent_created=False)
        self.store.bump_use("a")
        report = self.store.curated_report()
        names = [r["name"] for r in report]
        self.assertIn("a", names)
        self.assertNotIn("b", names)
        self.store.forget("a")
        self.assertNotIn("a", self.store.load())

    def test_ensure_created_at_backfills_null(self):
        self.store.bump_use("x")
        with self.store._file_lock():
            data = self.store.load()
            data["x"]["created_at"] = None
            self.store.save(data)
        self.assertTrue(self.store.ensure_created_at("x"))
        self.assertIsNotNone(self.store.get_record("x").get("created_at"))
        self.assertFalse(self.store.ensure_created_at("x"))

    def test_rename_record_keeps_pin(self):
        self.store.record_created("a", agent_created=True)
        self.store.set_pinned("a", True)
        self.store.rename_record("a", "b")
        self.assertNotIn("a", self.store.load())
        self.assertTrue(self.store.is_pinned("b"))
        self.assertTrue(self.store.is_curator_managed("b"))


if __name__ == "__main__":
    unittest.main()
