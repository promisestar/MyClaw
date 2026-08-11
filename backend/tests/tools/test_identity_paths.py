"""identity 路径别名解析单测。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# 直接加载模块，避免经 tools/__init__ 拉起 hello_agents
import importlib.util  # noqa: E402

_mod_path = BACKEND_ROOT / "src" / "tools" / "builtin" / "identity_paths.py"
_spec = importlib.util.spec_from_file_location("identity_paths_standalone", _mod_path)
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)
resolve_identity_alias = _mod.resolve_identity_alias
install_identity_path_resolver = _mod.install_identity_path_resolver


class TestIdentityAlias(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.id_dir = os.path.join(self.tmp.name, "identity")
        os.makedirs(self.id_dir)
        Path(self.id_dir, "IDENTITY.md").write_text("# id\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_bare_name(self):
        p = resolve_identity_alias("IDENTITY.md", self.id_dir)
        self.assertEqual(p, Path(self.id_dir) / "IDENTITY.md")

    def test_tilde_path(self):
        # expanduser 后若不是 identity 结构则靠 basename+identity 段
        p = resolve_identity_alias(
            os.path.join("~", ".helloclaw", "identity", "USER.md"),
            self.id_dir,
        )
        self.assertEqual(p, Path(self.id_dir) / "USER.md")

    def test_non_identity(self):
        self.assertIsNone(resolve_identity_alias("README.md", self.id_dir))
        self.assertIsNone(resolve_identity_alias("src/main.py", self.id_dir))

    def test_install_on_mock_tool(self):
        class T:
            name = "Read"
            working_dir = Path(self.tmp.name)

            def _resolve_path(self, path: str):
                return self.working_dir / path

        t = T()
        install_identity_path_resolver(t, self.id_dir)
        resolved = t._resolve_path("IDENTITY.md")
        self.assertEqual(resolved, Path(self.id_dir) / "IDENTITY.md")
        # 工作区根下的误写路径也应重定向
        at_root = t._resolve_path(str(Path(self.tmp.name) / "USER.md"))
        self.assertEqual(at_root, Path(self.id_dir) / "USER.md")
        other = t._resolve_path("foo.txt")
        self.assertEqual(other, Path(self.tmp.name) / "foo.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
