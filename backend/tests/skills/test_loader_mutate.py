"""SkillLoader 变异面单测：create / patch / archive / restore。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.skills.loader import SkillLoader, _fuzzy_replace  # noqa: E402
from src.skills.exceptions import SkillConflictError, SkillError, SkillLoadError  # noqa: E402


def _skill_md(name: str, desc: str = "A test skill.") -> str:
    return f"""---
name: {name}
description: {desc}
---

# {name}

Do the thing carefully.
"""


class TestLoaderMutate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root / "workspace_skills"
        self.gl = self.root / "global_skills"
        self.ws.mkdir()
        self.gl.mkdir()
        self.loader = SkillLoader(self.ws, global_dir=self.gl)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fuzzy_replace_exact_and_ws(self):
        content = "hello   world\nnext\n"
        out, n = _fuzzy_replace(content, "hello world", "hi world")
        self.assertEqual(n, 1)
        self.assertIn("hi world", out)

    def test_create_patch_archive_restore(self):
        name, lint = self.loader.create_skill("demo", _skill_md("demo"), scope="workspace")
        self.assertEqual(name, "demo")
        self.assertIn("demo", self.loader.metadata_cache)
        self.assertTrue((self.ws / "demo" / "SKILL.md").exists())

        rel, count, _ = self.loader.patch_skill_file(
            "demo",
            "Do the thing carefully.",
            "Do the thing carefully and verify.",
        )
        self.assertEqual(rel, "SKILL.md")
        self.assertEqual(count, 1)
        body = (self.ws / "demo" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("and verify", body)

        archived = self.loader.archive_skill("demo")
        self.assertTrue(Path(archived).exists())
        self.assertNotIn("demo", self.loader.metadata_cache)

        restored = self.loader.restore_skill("demo", scope="workspace")
        self.assertTrue(Path(restored).exists())
        self.assertIn("demo", self.loader.metadata_cache)

    def test_pin_blocks_archive(self):
        self.loader.create_skill("pinned-one", _skill_md("pinned-one"))
        self.loader.usage_store_for("pinned-one").set_pinned("pinned-one", True)
        with self.assertRaises(SkillError) as ctx:
            self.loader.archive_skill("pinned-one")
        self.assertEqual(ctx.exception.code, "PINNED")

    def test_write_file_path_guard(self):
        self.loader.create_skill("demo2", _skill_md("demo2"))
        with self.assertRaises(SkillLoadError):
            self.loader.write_skill_file("demo2", "../escape.txt", "x")
        with self.assertRaises(SkillLoadError):
            self.loader.write_skill_file("demo2", "secrets/x.txt", "x")
        rel = self.loader.write_skill_file("demo2", "scripts/hello.py", "print(1)\n")
        self.assertEqual(rel, "scripts/hello.py")
        self.assertTrue((self.ws / "demo2" / "scripts" / "hello.py").exists())

    def test_create_conflict(self):
        self.loader.create_skill("dup", _skill_md("dup"))
        with self.assertRaises(SkillConflictError):
            self.loader.create_skill("dup", _skill_md("dup"))

    def test_rename_migrates_usage_pin(self):
        self.loader.create_skill("old-name", _skill_md("old-name"))
        store = self.loader.usage_store_for("old-name")
        store.record_created("old-name", agent_created=True)
        store.set_pinned("old-name", True)
        store.bump_use("old-name")

        new_content = """---
name: new-name
description: Renamed skill.
---

# new-name

Do the thing carefully.
"""
        out = self.loader.set_skill_content("old-name", new_content)
        self.assertEqual(out, "new-name")
        self.assertNotIn("old-name", store.load())
        rec = store.get_record("new-name")
        self.assertTrue(rec.get("pinned"))
        self.assertEqual(rec.get("created_by"), "agent")
        self.assertGreaterEqual(int(rec.get("use_count") or 0), 1)
        with self.assertRaises(SkillError) as ctx:
            self.loader.archive_skill("new-name")
        self.assertEqual(ctx.exception.code, "PINNED")

    def test_archive_scoped_does_not_touch_workspace_cover(self):
        """全局同名陈旧时，只归档 global 目录，不动 workspace 覆盖方。"""
        self.loader.create_skill("shared", _skill_md("shared"), scope="global")
        # create_skill 拒绝对 cache 已存在的同名；手工植入 workspace 覆盖
        ws_dir = self.ws / "shared"
        ws_dir.mkdir()
        (ws_dir / "SKILL.md").write_text(_skill_md("shared"), encoding="utf-8")
        self.loader.reload()
        self.assertEqual(self.loader.metadata_cache["shared"]["source"], "workspace")

        gstore = self.loader.usage_store_for(scope="global")
        gstore.record_created("shared", agent_created=True)

        archived = self.loader.archive_skill("shared", scope="global")
        self.assertTrue(Path(archived).exists())
        self.assertIn("shared", self.loader.metadata_cache)
        self.assertEqual(self.loader.metadata_cache["shared"]["source"], "workspace")
        self.assertTrue((self.ws / "shared" / "SKILL.md").exists())
        self.assertFalse((self.gl / "shared" / "SKILL.md").exists())

    def test_restore_timestamped_archive_uses_logical_name(self):
        self.loader.create_skill("demo", _skill_md("demo"))
        self.loader.archive_skill("demo")
        arch = self.ws / ".archive"
        stamped = arch / "demo-20260101120000"
        stamped.mkdir()
        (stamped / "SKILL.md").write_text(_skill_md("demo"), encoding="utf-8")
        restored = self.loader.restore_skill("demo-20260101120000", scope="workspace")
        self.assertTrue(
            restored.replace("\\", "/").endswith("/demo")
            or Path(restored).name == "demo"
        )
        self.assertIn("demo", self.loader.metadata_cache)


if __name__ == "__main__":
    unittest.main()
