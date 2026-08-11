"""session_store 单元测试。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

# 保证可从 backend 根目录导入 src.*
BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.session_store.probe import (  # noqa: E402
    clear_probe_cache,
    probe_sqlite_fts_capabilities,
)
from src.session_store.db import SessionIndexDB  # noqa: E402
from src.session_store.indexer import SessionIndexer, stable_message_id  # noqa: E402
from src.session_store.search import SessionSearch, sanitize_fts5_query  # noqa: E402

# 直接加载 tool_mode_filter，避免经 agent/__init__ 拉起 hello_agents
import importlib.util  # noqa: E402

_tmf_path = BACKEND_ROOT / "src" / "agent" / "tool_mode_filter.py"
_spec = importlib.util.spec_from_file_location("tool_mode_filter_standalone", _tmf_path)
_tmf = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_tmf)
SIDE_EFFECT_TOOLS = _tmf.SIDE_EFFECT_TOOLS


def _write_session(sessions_dir: str, sid: str, history: list) -> str:
    path = os.path.join(sessions_dir, f"{sid}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"history": history}, f, ensure_ascii=False)
    return path


class TestProbe(unittest.TestCase):
    def test_probe_structure_no_temp_files(self):
        clear_probe_cache()
        before = set(os.listdir(tempfile.gettempdir()))
        result = probe_sqlite_fts_capabilities(force=True)
        after = set(os.listdir(tempfile.gettempdir()))
        self.assertIn("sqlite_version", result)
        self.assertIn("fts5", result)
        self.assertIn("trigram", result)
        self.assertIn("error", result)
        # :memory: 不应留下新临时文件（允许无关并发噪声，只断言无 index.db 类产物）
        new = after - before
        self.assertFalse(any("index.db" in n for n in new))
        print("PROBE_RESULT", result)


class TestSanitize(unittest.TestCase):
    def test_sanitize_strips_specials(self):
        q = sanitize_fts5_query('Redis (config)^ *foo:bar')
        self.assertNotIn("(", q)
        self.assertNotIn(")", q)
        self.assertNotIn("^", q)
        self.assertNotIn("*", q)

    def test_sanitize_length(self):
        q = sanitize_fts5_query("x" * 500)
        self.assertLessEqual(len(q), 200)


class _BaseIndexTest(unittest.TestCase):
    force_like = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sessions_dir = os.path.join(self.tmp.name, "sessions")
        os.makedirs(self.sessions_dir)
        self.db_path = os.path.join(self.sessions_dir, "index.db")
        clear_probe_cache()
        self.db = SessionIndexDB(
            self.db_path,
            force_like=self.force_like,
            probe=probe_sqlite_fts_capabilities(force=True),
        )
        self.db.ensure_schema()
        self.indexer = SessionIndexer(self.db, self.sessions_dir)
        self.search = SessionSearch(self.db, self.sessions_dir)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()


class TestFTSOrLikeIndex(_BaseIndexTest):
    force_like = False

    def test_two_sessions_isolation(self):
        _write_session(
            self.sessions_dir,
            "sess_redis",
            [
                {"role": "user", "content": "帮我配一下本地 Redis 端口 6379"},
                {"role": "assistant", "content": "好的，Redis 配置如下…"},
            ],
        )
        _write_session(
            self.sessions_dir,
            "sess_pg",
            [
                {"role": "user", "content": "Postgres 连接串怎么写"},
                {"role": "assistant", "content": "postgresql://…"},
            ],
        )
        self.assertTrue(self.indexer.upsert_session("sess_redis", force=True))
        self.assertTrue(self.indexer.upsert_session("sess_pg", force=True))

        out = self.search.discover("Redis", limit=5, current_session_id="current")
        self.assertTrue(out["success"])
        ids = [r["session_id"] for r in out["results"]]
        self.assertIn("sess_redis", ids)
        self.assertNotIn("sess_pg", ids)
        # 锚定窗口应含原文
        hit = out["results"][0]
        blob = json.dumps(hit, ensure_ascii=False)
        self.assertIn("Redis", blob)

    def test_exclude_current_session(self):
        _write_session(
            self.sessions_dir,
            "cur",
            [{"role": "user", "content": "UniqueTokenXYZ current only"}],
        )
        _write_session(
            self.sessions_dir,
            "old",
            [{"role": "user", "content": "UniqueTokenXYZ in old session"}],
        )
        self.indexer.upsert_session("cur", force=True)
        self.indexer.upsert_session("old", force=True)

        out = self.search.discover(
            "UniqueTokenXYZ", limit=5, current_session_id="cur", include_current=False
        )
        ids = [r["session_id"] for r in out["results"]]
        self.assertNotIn("cur", ids)
        self.assertIn("old", ids)

    def test_delete_session_removes_hits(self):
        _write_session(
            self.sessions_dir,
            "gone",
            [{"role": "user", "content": "DeleteMePhrase123"}],
        )
        self.indexer.upsert_session("gone", force=True)
        self.indexer.delete_session("gone")
        out = self.search.discover("DeleteMePhrase123", current_session_id=None)
        self.assertEqual(out["count"], 0)

    def test_update_session_replaces_content(self):
        _write_session(
            self.sessions_dir,
            "upd",
            [{"role": "user", "content": "OldSentenceAlpha"}],
        )
        self.indexer.upsert_session("upd", force=True)
        time.sleep(0.05)
        _write_session(
            self.sessions_dir,
            "upd",
            [{"role": "user", "content": "NewSentenceBeta"}],
        )
        self.indexer.upsert_session("upd", force=True)
        old = self.search.discover("OldSentenceAlpha", current_session_id=None)
        new = self.search.discover("NewSentenceBeta", current_session_id=None)
        self.assertEqual(old["count"], 0)
        self.assertEqual(new["count"], 1)

    def test_rebuild_after_db_delete(self):
        _write_session(
            self.sessions_dir,
            "rb",
            [{"role": "user", "content": "RebuildKeyword999"}],
        )
        self.indexer.upsert_session("rb", force=True)
        self.db.close()
        os.remove(self.db_path)
        # 重建
        self.db = SessionIndexDB(
            self.db_path,
            force_like=self.force_like,
            probe=probe_sqlite_fts_capabilities(force=True),
        )
        self.db.ensure_schema()
        self.indexer = SessionIndexer(self.db, self.sessions_dir)
        self.search = SessionSearch(self.db, self.sessions_dir)
        result = self.indexer.rebuild_all()
        self.assertGreaterEqual(result["indexed"], 1)
        out = self.search.discover("RebuildKeyword999", current_session_id=None)
        self.assertEqual(out["count"], 1)

    def test_original_content_preferred(self):
        _write_session(
            self.sessions_dir,
            "comp",
            [
                {
                    "role": "assistant",
                    "content": "line1\n... (10 lines, snipped to save context) ...\nline10",
                    "metadata": {
                        "original_content": "完整原文含 SecretOriginalPhrase"
                    },
                }
            ],
        )
        self.indexer.upsert_session("comp", force=True)
        out = self.search.discover("SecretOriginalPhrase", current_session_id=None)
        self.assertEqual(out["count"], 1)

    def test_browse(self):
        _write_session(
            self.sessions_dir,
            "b1",
            [{"role": "user", "content": "hello browse"}],
        )
        self.indexer.upsert_session("b1", force=True)
        out = self.search.browse(limit=5)
        self.assertTrue(out["success"])
        self.assertGreaterEqual(out["count"], 1)

    def test_ask_mode_tool_not_blocked(self):
        self.assertNotIn("session_search", SIDE_EFFECT_TOOLS)

    def test_message_id_stable_across_reindex(self):
        _write_session(
            self.sessions_dir,
            "stable",
            [
                {"role": "user", "content": "AnchorPhraseStable"},
                {"role": "assistant", "content": "reply"},
            ],
        )
        self.indexer.upsert_session("stable", force=True)
        out1 = self.search.discover("AnchorPhraseStable", current_session_id=None)
        self.assertEqual(out1["count"], 1)
        mid = out1["results"][0]["match_message_id"]
        expected = stable_message_id("stable", 0)
        self.assertEqual(mid, expected)

        # 再次 force upsert（模拟每轮保存）
        time.sleep(0.02)
        _write_session(
            self.sessions_dir,
            "stable",
            [
                {"role": "user", "content": "AnchorPhraseStable"},
                {"role": "assistant", "content": "reply updated"},
            ],
        )
        self.indexer.upsert_session("stable", force=True)
        out2 = self.search.discover("AnchorPhraseStable", current_session_id=None)
        self.assertEqual(out2["results"][0]["match_message_id"], mid)

        scrolled = self.search.scroll("stable", mid, window=2)
        self.assertTrue(scrolled["success"])
        self.assertGreater(len(scrolled["messages"]), 0)
        self.assertTrue(any(m.get("anchor") for m in scrolled["messages"]))

    def test_rebuild_purges_orphan_sessions(self):
        _write_session(
            self.sessions_dir,
            "keep",
            [{"role": "user", "content": "KeepMePhrase"}],
        )
        _write_session(
            self.sessions_dir,
            "gone",
            [{"role": "user", "content": "GhostPhrase"}],
        )
        self.indexer.upsert_session("keep", force=True)
        self.indexer.upsert_session("gone", force=True)
        # 只删磁盘 JSON，不走 delete_session
        os.remove(os.path.join(self.sessions_dir, "gone.json"))
        result = self.indexer.rebuild_all()
        self.assertGreaterEqual(result.get("purged", 0), 1)
        ghost = self.search.discover("GhostPhrase", current_session_id=None)
        self.assertEqual(ghost["count"], 0)
        keep = self.search.discover("KeepMePhrase", current_session_id=None)
        self.assertEqual(keep["count"], 1)


class TestLikeOnlyIndex(_BaseIndexTest):
    force_like = True

    def test_like_mode_discover(self):
        self.assertEqual(self.db.index_mode, "like")
        _write_session(
            self.sessions_dir,
            "like1",
            [{"role": "user", "content": "中文短查询Redis配置"}],
        )
        self.indexer.upsert_session("like1", force=True)
        out = self.search.discover("Redis", current_session_id=None)
        self.assertTrue(out["success"])
        self.assertEqual(out["index_mode"], "like")
        self.assertEqual(out["count"], 1)

    def test_cjk_like_fallback_path(self):
        _write_session(
            self.sessions_dir,
            "cjk",
            [{"role": "user", "content": "上次部署失败的日志我们怎么排查的"}],
        )
        self.indexer.upsert_session("cjk", force=True)
        out = self.search.discover("部署失败", current_session_id=None)
        self.assertEqual(out["count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
