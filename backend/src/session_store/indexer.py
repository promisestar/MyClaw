"""从 session JSON 增量索引 / 全量重建。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .db import SessionIndexDB

logger = logging.getLogger(__name__)

# 单条索引文本上限（原文仍以 JSON 为准）
MAX_INDEX_CHARS = 8 * 1024


def stable_message_id(session_id: str, seq: int) -> int:
    """由 (session_id, seq) 派生稳定正整数 rowid。

    删除重插后 ID 不变，discover → scroll 跨次保存/rebuild 仍可用。
    """
    digest = hashlib.blake2b(
        f"{session_id}:{seq}".encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF


def _flatten_message_content(msg: Dict[str, Any]) -> Tuple[str, bool]:
    """拍平消息文本；优先 metadata.original_content。

    Returns:
        (text, is_compacted)
    """
    meta = msg.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}

    original = meta.get("original_content")
    content = original if original is not None else msg.get("content", "")
    is_compacted = False

    # 启发式：摘要/压缩标记
    if isinstance(content, str):
        low = content.lower()
        if "snipped to save context" in low or "[context compaction" in low:
            is_compacted = True
        if meta.get("is_compacted") or meta.get("compacted"):
            is_compacted = True

    # 多模态 list / 编码字符串
    try:
        from ..agent.multimodal_bridge import (
            decode_multimodal_content,
            is_encoded_multimodal,
        )
        from ..multimodal import flatten_content_to_text

        if isinstance(content, str) and is_encoded_multimodal(content):
            content = decode_multimodal_content(content)
        if not isinstance(content, str):
            content = flatten_content_to_text(content)
    except Exception:  # noqa: BLE001
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text") or "")
                elif isinstance(part, str):
                    parts.append(part)
            content = "\n".join(parts)
        else:
            content = str(content or "")

    text = content if isinstance(content, str) else str(content or "")
    if len(text) > MAX_INDEX_CHARS:
        text = text[:MAX_INDEX_CHARS]
    return text, is_compacted


def _file_content_hash(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class SessionIndexer:
    """会话索引写入器。"""

    def __init__(
        self,
        db: SessionIndexDB,
        sessions_dir: str,
        *,
        index_tool_messages: bool = False,
        workspace_id_getter: Optional[Callable[[], Optional[str]]] = None,
    ):
        self.db = db
        self.sessions_dir = sessions_dir
        self.index_tool_messages = index_tool_messages
        self.workspace_id_getter = workspace_id_getter

    def upsert_session(
        self,
        session_id: str,
        *,
        workspace_id: Optional[str] = None,
        source: str = "chat",
        force: bool = False,
    ) -> bool:
        """从 JSON 增量 upsert；未变则跳过。失败只打日志，返回 False。"""
        try:
            filepath = os.path.join(self.sessions_dir, f"{session_id}.json")
            if not os.path.isfile(filepath):
                self.delete_session(session_id)
                return False

            stat = os.stat(filepath)
            mtime = float(stat.st_mtime)
            content_hash = _file_content_hash(filepath)

            with self.db.lock:
                conn = self.db.connect()
                row = conn.execute(
                    "SELECT content_hash, file_mtime FROM sessions WHERE id=?",
                    (session_id,),
                ).fetchone()
                if not force and row and row["content_hash"] == content_hash:
                    return False  # 未变

                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)

                history = data.get("history") or []
                if not isinstance(history, list):
                    history = []

                ws = workspace_id
                if ws is None and self.workspace_id_getter:
                    try:
                        ws = self.workspace_id_getter()
                    except Exception:  # noqa: BLE001
                        ws = None

                preview = None
                created_at = mtime
                messages_rows: List[Tuple] = []
                seq = 0
                now = time.time()

                for msg in history:
                    if not isinstance(msg, dict):
                        continue
                    role = msg.get("role") or ""
                    if role not in ("user", "assistant", "tool"):
                        continue
                    text, is_compacted = _flatten_message_content(msg)
                    # tool 消息：默认不入表（除非配置开启）
                    if role == "tool" and not self.index_tool_messages:
                        continue
                    if preview is None and role == "user" and text.strip():
                        preview = text.strip()[:80]
                    ch = hashlib.sha256(
                        text.encode("utf-8", errors="replace")
                    ).hexdigest()[:16]
                    msg_id = stable_message_id(session_id, seq)
                    messages_rows.append(
                        (
                            msg_id,
                            session_id,
                            seq,
                            role,
                            text,
                            ch,
                            1 if is_compacted else 0,
                            now,
                        )
                    )
                    seq += 1

                title = data.get("title")
                if isinstance(title, str) and not title.strip():
                    title = None

                conn.execute("BEGIN IMMEDIATE")
                try:
                    # 先删后插：显式稳定 id，重插后 rowid 不变
                    conn.execute(
                        "DELETE FROM messages WHERE session_id=?", (session_id,)
                    )
                    conn.execute(
                        """
                        INSERT INTO sessions(
                          id, title, workspace_id, source, created_at, updated_at,
                          message_count, preview, content_hash, file_mtime
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET
                          title=excluded.title,
                          workspace_id=COALESCE(excluded.workspace_id, sessions.workspace_id),
                          source=excluded.source,
                          updated_at=excluded.updated_at,
                          message_count=excluded.message_count,
                          preview=excluded.preview,
                          content_hash=excluded.content_hash,
                          file_mtime=excluded.file_mtime
                        """,
                        (
                            session_id,
                            title,
                            ws,
                            source,
                            created_at,
                            mtime,
                            len(messages_rows),
                            preview,
                            content_hash,
                            mtime,
                        ),
                    )
                    if messages_rows:
                        conn.executemany(
                            """
                            INSERT INTO messages(
                              id, session_id, seq, role, content, content_hash,
                              is_compacted, created_at
                            ) VALUES(?,?,?,?,?,?,?,?)
                            """,
                            messages_rows,
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                return True
        except Exception as e:  # noqa: BLE001
            logger.warning("upsert_session(%s) 失败: %s", session_id, e)
            return False

    def delete_session(self, session_id: str) -> None:
        try:
            with self.db.lock:
                conn = self.db.connect()
                conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
                conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("delete_session(%s) 失败: %s", session_id, e)

    def purge_orphan_sessions(self, keep_ids: Set[str]) -> int:
        """删除索引中不在 keep_ids 内的会话（幽灵行）。返回删除数量。"""
        with self.db.lock:
            conn = self.db.connect()
            rows = conn.execute("SELECT id FROM sessions").fetchall()
            orphans = [r["id"] for r in rows if r["id"] not in keep_ids]
            for oid in orphans:
                conn.execute("DELETE FROM sessions WHERE id=?", (oid,))
            if orphans:
                conn.commit()
            return len(orphans)

    def rebuild_all(self, *, limit_concurrency_sleep: float = 0.0) -> Dict[str, Any]:
        """扫描 sessions_dir 全量重建，并清理磁盘已不存在的幽灵会话。"""
        with self.db.lock:
            conn = self.db.connect()
            self.db._set_meta_unlocked("rebuild_status", "running")
            self.db._set_meta_unlocked("rebuild_percent", "0")
            conn.commit()

        files: List[str] = []
        if os.path.isdir(self.sessions_dir):
            for name in os.listdir(self.sessions_dir):
                if name.endswith(".json"):
                    files.append(name[:-5])
        files.sort()
        keep_ids = set(files)

        # 先清幽灵，再 upsert（避免重建中途 browse 到已删会话）
        purged = self.purge_orphan_sessions(keep_ids)

        total = len(files) or 1
        ok = 0
        fail = 0
        for i, sid in enumerate(files):
            filepath = os.path.join(self.sessions_dir, f"{sid}.json")
            if self.upsert_session(sid, force=True):
                ok += 1
            elif os.path.isfile(filepath):
                fail += 1
            pct = int((i + 1) * 100 / total)
            with self.db.lock:
                self.db._set_meta_unlocked("rebuild_percent", str(pct))
                self.db.connect().commit()
            if limit_concurrency_sleep > 0:
                time.sleep(limit_concurrency_sleep)

        with self.db.lock:
            self.db._set_meta_unlocked("rebuild_status", "idle")
            self.db._set_meta_unlocked("rebuild_percent", "100")
            ver = self.db._get_meta_unlocked("schema_version") or "1"
            self.db._set_meta_unlocked("schema_version", str(ver))
            self.db.connect().commit()
        return {
            "total": len(files),
            "indexed": ok,
            "failed": fail,
            "purged": purged,
        }

    def rebuild_if_needed(self) -> Optional[Dict[str, Any]]:
        if not self.db.needs_rebuild():
            return None
        return self.rebuild_all()
