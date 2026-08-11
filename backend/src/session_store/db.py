"""SQLite 连接、schema、迁移。"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from .probe import probe_sqlite_fts_capabilities

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# content 表 + FTS5 external content
_CREATE_BASE = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id            TEXT PRIMARY KEY,
  title         TEXT,
  workspace_id  TEXT,
  source        TEXT DEFAULT 'chat',
  created_at    REAL,
  updated_at    REAL,
  message_count INTEGER DEFAULT 0,
  preview       TEXT,
  content_hash  TEXT,
  file_mtime    REAL
);

CREATE TABLE IF NOT EXISTS messages (
  id            INTEGER PRIMARY KEY,
  session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  seq           INTEGER NOT NULL,
  role          TEXT NOT NULL,
  content       TEXT NOT NULL,
  content_hash  TEXT,
  is_compacted  INTEGER DEFAULT 0,
  created_at    REAL,
  UNIQUE(session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
"""

_CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  content,
  content='messages',
  content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
  INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


class SessionIndexDB:
    """会话全文索引数据库（WAL + 进程内互斥锁）。"""

    def __init__(
        self,
        db_path: str,
        *,
        force_like: bool = False,
        probe: Optional[Dict[str, Any]] = None,
    ):
        self.db_path = db_path
        self._probe = probe or probe_sqlite_fts_capabilities()
        self.use_fts = bool(self._probe.get("fts5")) and not force_like
        self.index_mode = "fts5" if self.use_fts else "like"
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        """跨线程串行化对共享连接的访问。"""
        return self._lock

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """持锁获取连接；调用方负责 commit/rollback。"""
        with self._lock:
            yield self.connect()

    def connect(self) -> sqlite3.Connection:
        """返回共享连接。调用方应已持有 ``self.lock``（或经 ``connection()``）。"""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._conn = None

    def ensure_schema(self) -> None:
        with self._lock:
            conn = self.connect()
            conn.executescript(_CREATE_BASE)
            if self.use_fts:
                try:
                    conn.executescript(_CREATE_FTS)
                except sqlite3.OperationalError as e:
                    logger.warning("FTS5 schema 创建失败，降级 LIKE: %s", e)
                    self.use_fts = False
                    self.index_mode = "like"

            # 记录能力位与版本
            self._set_meta_unlocked("schema_version", str(SCHEMA_VERSION))
            self._set_meta_unlocked("fts5", "1" if self.use_fts else "0")
            self._set_meta_unlocked("index_mode", self.index_mode)
            self._set_meta_unlocked(
                "sqlite_version", str(self._probe.get("sqlite_version") or "")
            )
            self._set_meta_unlocked("trigram", "1" if self._probe.get("trigram") else "0")
            conn.commit()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            return self._get_meta_unlocked(key, default)

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._set_meta_unlocked(key, value)
            self.connect().commit()

    def _get_meta_unlocked(self, key: str, default: Optional[str] = None) -> Optional[str]:
        conn = self.connect()
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def _set_meta_unlocked(self, key: str, value: str) -> None:
        conn = self.connect()
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def needs_rebuild(self) -> bool:
        """空库或 schema_version 落后则需要重建。"""
        with self._lock:
            ver = self._get_meta_unlocked("schema_version")
            if ver is None or int(ver) < SCHEMA_VERSION:
                return True
            conn = self.connect()
            n = conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
            return n == 0 and self._sessions_json_exist()

    def _sessions_json_exist(self) -> bool:
        sessions_dir = os.path.dirname(self.db_path)
        if not os.path.isdir(sessions_dir):
            return False
        for name in os.listdir(sessions_dir):
            if name.endswith(".json"):
                return True
        return False

    def is_rebuilding(self) -> bool:
        status = self.get_meta("rebuild_status", "idle")
        return status == "running"
