"""Curator 确定性状态机单测。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.skills.loader import SkillLoader  # noqa: E402
from src.skills import curator as curator_mod  # noqa: E402
from src.skills.curator import SkillCurator  # noqa: E402
from src.skills.usage import STATE_STALE, STATE_ACTIVE  # noqa: E402


def _skill_md(name: str) -> str:
    return f"""---
name: {name}
description: Curator test skill.
---

# {name}

Body.
"""


class TestCuratorTransitions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root / "workspace_skills"
        self.gl = self.root / "global_skills"
        self.ws.mkdir()
        self.gl.mkdir()
        self.loader = SkillLoader(self.ws, global_dir=self.gl)
        self.cfg = {
            "curator": {
                "enabled": True,
                "interval_hours": 1,
                "min_idle_hours": 0,
                "stale_after_days": 30,
                "archive_after_days": 90,
                "consolidate": False,
                "backup": {"enabled": False, "keep": 2},
            }
        }
        self.curator = SkillCurator(
            self.loader,
            config_getter=lambda: self.cfg,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _make_agent_skill(self, name: str):
        self.loader.create_skill(name, _skill_md(name))
        store = self.loader.usage_store_for(name)
        store.record_created(name, agent_created=True)
        return store

    def test_first_run_defers(self):
        result = self.curator.run(force=False)
        self.assertTrue(result["scopes"]["workspace"].get("deferred"))

    def test_stale_and_archive(self):
        store = self._make_agent_skill("oldie")
        now = datetime.now(timezone.utc)
        # 直接改 sidecar 时间戳
        with store._file_lock():
            data = store.load()
            rec = data["oldie"]
            rec["created_at"] = (now - timedelta(days=100)).isoformat()
            rec["last_used_at"] = (now - timedelta(days=100)).isoformat()
            rec["use_count"] = 1
            store.save(data)

        counts = self.curator.apply_automatic_transitions(now=now)
        self.assertGreaterEqual(counts["archived"], 1)
        self.assertNotIn("oldie", self.loader.metadata_cache)
        self.assertIn("oldie", self.loader.list_archived())

    def test_pin_skips(self):
        store = self._make_agent_skill("pinned")
        store.set_pinned("pinned", True)
        now = datetime.now(timezone.utc)
        with store._file_lock():
            data = store.load()
            rec = data["pinned"]
            rec["created_at"] = (now - timedelta(days=100)).isoformat()
            rec["last_used_at"] = (now - timedelta(days=100)).isoformat()
            rec["use_count"] = 1
            store.save(data)

        counts = self.curator.apply_automatic_transitions(now=now)
        self.assertEqual(counts["skipped_pinned"], 1)
        self.assertIn("pinned", self.loader.metadata_cache)

    def test_reactivate_from_stale(self):
        store = self._make_agent_skill("fresh")
        store.set_state("fresh", STATE_STALE)
        now = datetime.now(timezone.utc)
        with store._file_lock():
            data = store.load()
            rec = data["fresh"]
            rec["last_used_at"] = now.isoformat()
            rec["use_count"] = 2
            store.save(data)

        counts = self.curator.apply_automatic_transitions(now=now)
        self.assertGreaterEqual(counts["reactivated"], 1)
        self.assertEqual(store.get_record("fresh")["state"], STATE_ACTIVE)

    def test_null_created_at_backfill_defers(self):
        store = self._make_agent_skill("broken")
        now = datetime.now(timezone.utc)
        with store._file_lock():
            data = store.load()
            data["broken"]["created_at"] = None
            data["broken"]["last_used_at"] = (now - timedelta(days=100)).isoformat()
            data["broken"]["use_count"] = 1
            store.save(data)

        counts = self.curator.apply_automatic_transitions(now=now)
        self.assertGreaterEqual(counts["seeded"], 1)
        self.assertEqual(counts["archived"], 0)
        self.assertIsNotNone(store.get_record("broken").get("created_at"))
        # 仍在磁盘
        self.assertIn("broken", self.loader.metadata_cache)

    def test_curator_archive_respects_scope_cover(self):
        self.loader.create_skill("shared", _skill_md("shared"), scope="global")
        ws_dir = self.ws / "shared"
        ws_dir.mkdir()
        (ws_dir / "SKILL.md").write_text(_skill_md("shared"), encoding="utf-8")
        self.loader.reload()

        gstore = self.loader.usage_store_for(scope="global")
        gstore.record_created("shared", agent_created=True)
        now = datetime.now(timezone.utc)
        with gstore._file_lock():
            data = gstore.load()
            data["shared"]["created_at"] = (now - timedelta(days=100)).isoformat()
            data["shared"]["last_used_at"] = (now - timedelta(days=100)).isoformat()
            data["shared"]["use_count"] = 1
            gstore.save(data)

        counts = self.curator.apply_automatic_transitions(now=now, scope="global")
        self.assertGreaterEqual(counts["archived"], 1)
        self.assertEqual(self.loader.metadata_cache["shared"]["source"], "workspace")
        self.assertTrue((self.ws / "shared" / "SKILL.md").exists())

    def test_status_json_safe_when_never_chatted(self):
        import json

        curator_mod._last_chat_activity_at = None
        payload = self.curator.status()
        self.assertIsNone(payload["idle_for_seconds"])
        json.dumps(payload)  # 不得含 Infinity / NaN


if __name__ == "__main__":
    unittest.main()
