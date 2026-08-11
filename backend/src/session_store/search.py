"""FTS / LIKE 查询、锚定窗口、browse。"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .db import SessionIndexDB
from .models import BrowseItem, DiscoverResult, MessageHit

logger = logging.getLogger(__name__)

MAX_QUERY_CHARS = 200
DEFAULT_WINDOW = 5
DEFAULT_BOOKEND = 3
DEFAULT_DISCOVER_LIMIT = 3
DEFAULT_FTS_SCAN = 100
CONTENT_TRUNCATE = 4000
BOOKEND_TRUNCATE = 1200

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def sanitize_fts5_query(raw: str, *, max_chars: int = MAX_QUERY_CHARS) -> str:
    """消毒用户/模型输入，避免直接丢进 MATCH 炸语法。"""
    if not raw:
        return ""
    q = raw.strip()[:max_chars]
    # 保留引号短语；剥离未配对的特殊字符
    # FTS5 特殊: " * ( ) : ^
    out: List[str] = []
    in_quote = False
    i = 0
    while i < len(q):
        ch = q[i]
        if ch == '"':
            in_quote = not in_quote
            out.append(ch)
            i += 1
            continue
        if in_quote:
            out.append(ch)
            i += 1
            continue
        if ch in "*()^":
            i += 1
            continue
        if ch == ":":
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    s = "".join(out).strip()
    if in_quote:
        s = s.replace('"', " ")
    # 对含连字符/点的词加引号（在词边界简化处理）
    tokens = []
    for tok in s.split():
        if re.search(r"[-.]", tok) and not tok.startswith('"'):
            tokens.append(f'"{tok}"')
        else:
            tokens.append(tok)
    return " ".join(tokens).strip()


def _format_when(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return None


def _truncate(text: str, limit: int) -> Tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "…", True


def _row_to_hit(row: sqlite3.Row, *, truncate_to: int, anchor: bool = False) -> MessageHit:
    content, truncated = _truncate(row["content"] or "", truncate_to)
    return MessageHit(
        id=int(row["id"]),
        role=row["role"],
        content=content,
        seq=int(row["seq"]),
        anchor=anchor,
        content_truncated=truncated,
        is_compacted=bool(row["is_compacted"]),
    )


def _make_snippet(content: str, query: str, radius: int = 40) -> str:
    if not content:
        return ""
    # 取 query 中第一个非特殊词做高亮
    terms = [t.strip('"') for t in query.split() if t.strip('"')]
    lower = content.lower()
    pos = -1
    hit_term = ""
    for t in terms:
        if not t:
            continue
        p = lower.find(t.lower())
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
            hit_term = content[p : p + len(t)]
    if pos < 0:
        snippet, _ = _truncate(content, 120)
        return snippet
    start = max(0, pos - radius)
    end = min(len(content), pos + len(hit_term) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return f"{prefix}{content[start:pos]}>>>{hit_term}<<<{content[pos + len(hit_term):end]}{suffix}"


class SessionSearch:
    """会话原文检索。"""

    def __init__(
        self,
        db: SessionIndexDB,
        sessions_dir: str,
        *,
        discover_limit: int = DEFAULT_DISCOVER_LIMIT,
        fts_scan_limit: int = DEFAULT_FTS_SCAN,
        default_window: int = DEFAULT_WINDOW,
        cjk_like_fallback: bool = True,
        exclude_current_session: bool = True,
    ):
        self.db = db
        self.sessions_dir = sessions_dir
        self.discover_limit = discover_limit
        self.fts_scan_limit = fts_scan_limit
        self.default_window = default_window
        self.cjk_like_fallback = cjk_like_fallback
        self.exclude_current_session = exclude_current_session

    def rebuild_warning(self) -> Optional[Dict[str, Any]]:
        if self.db.is_rebuilding():
            return {
                "status": "running",
                "percent": self.db.get_meta("rebuild_percent", "0"),
                "message": "索引重建中，结果可能不全",
            }
        return None

    # ------------------------------------------------------------------ #
    # Discover
    # ------------------------------------------------------------------ #

    def discover(
        self,
        query: str,
        *,
        limit: Optional[int] = None,
        window: Optional[int] = None,
        role_filter: str = "user,assistant",
        sort: str = "relevance",
        current_session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        include_current: bool = False,
    ) -> Dict[str, Any]:
        with self.db.lock:
            return self._discover_unlocked(
                query,
                limit=limit,
                window=window,
                role_filter=role_filter,
                sort=sort,
                current_session_id=current_session_id,
                workspace_id=workspace_id,
                include_current=include_current,
            )

    def _discover_unlocked(
        self,
        query: str,
        *,
        limit: Optional[int] = None,
        window: Optional[int] = None,
        role_filter: str = "user,assistant",
        sort: str = "relevance",
        current_session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        include_current: bool = False,
    ) -> Dict[str, Any]:
        limit = limit if limit is not None else self.discover_limit
        window = window if window is not None else self.default_window
        sanitized = sanitize_fts5_query(query)
        if not sanitized:
            return {
                "success": False,
                "mode": "discover",
                "error": "empty_query",
                "results": [],
                "count": 0,
                "index_mode": self.db.index_mode,
            }

        roles = [r.strip() for r in role_filter.split(",") if r.strip()]
        exclude = None
        if self.exclude_current_session and not include_current and current_session_id:
            exclude = current_session_id

        hits = self._search_messages(
            sanitized,
            roles=roles,
            sort=sort,
            scan_limit=self.fts_scan_limit,
            exclude_session_id=exclude,
            workspace_id=workspace_id,
        )

        # CJK fallback
        if (
            self.cjk_like_fallback
            and _CJK_RE.search(sanitized)
            and len(hits) < max(1, limit)
        ):
            like_hits = self._like_search(
                sanitized,
                roles=roles,
                sort=sort,
                scan_limit=self.fts_scan_limit,
                exclude_session_id=exclude,
                workspace_id=workspace_id,
            )
            seen_ids = {h["id"] for h in hits}
            for h in like_hits:
                if h["id"] not in seen_ids:
                    hits.append(h)
                    seen_ids.add(h["id"])

        # 按 session 去重（保留首次/最优命中）
        results: List[DiscoverResult] = []
        seen_sessions: Set[str] = set()
        for h in hits:
            sid = h["session_id"]
            if sid in seen_sessions:
                continue
            seen_sessions.add(sid)
            view = self._get_anchored_view_unlocked(
                sid, int(h["id"]), window=window, bookend=DEFAULT_BOOKEND
            )
            sess = self._get_session_unlocked(sid)
            snippet = _make_snippet(h["content"] or "", sanitized)
            results.append(
                DiscoverResult(
                    session_id=sid,
                    title=(sess or {}).get("title"),
                    preview=(sess or {}).get("preview"),
                    when=_format_when((sess or {}).get("updated_at")),
                    match_message_id=int(h["id"]),
                    matched_role=h["role"],
                    snippet=snippet,
                    messages=view["messages"],
                    bookend_start=view["bookend_start"],
                    bookend_end=view["bookend_end"],
                    messages_before=view["messages_before"],
                    messages_after=view["messages_after"],
                    workspace_id=(sess or {}).get("workspace_id"),
                )
            )
            if len(results) >= limit:
                break

        out: Dict[str, Any] = {
            "success": True,
            "mode": "discover",
            "query": query,
            "count": len(results),
            "results": [r.to_dict() for r in results],
            "index_mode": self.db.index_mode,
        }
        warn = self.rebuild_warning()
        if warn:
            out["index_rebuild"] = warn
        return out

    def _search_messages(
        self,
        query: str,
        *,
        roles: Sequence[str],
        sort: str,
        scan_limit: int,
        exclude_session_id: Optional[str],
        workspace_id: Optional[str],
    ) -> List[Dict[str, Any]]:
        if self.db.use_fts:
            try:
                return self._fts_search(
                    query,
                    roles=roles,
                    sort=sort,
                    scan_limit=scan_limit,
                    exclude_session_id=exclude_session_id,
                    workspace_id=workspace_id,
                )
            except sqlite3.OperationalError as e:
                logger.warning("FTS 查询失败，降级 LIKE: %s", e)
        return self._like_search(
            query,
            roles=roles,
            sort=sort,
            scan_limit=scan_limit,
            exclude_session_id=exclude_session_id,
            workspace_id=workspace_id,
        )

    def _fts_search(
        self,
        query: str,
        *,
        roles: Sequence[str],
        sort: str,
        scan_limit: int,
        exclude_session_id: Optional[str],
        workspace_id: Optional[str],
    ) -> List[Dict[str, Any]]:
        conn = self.db.connect()
        role_placeholders = ",".join("?" * len(roles)) if roles else ""
        params: List[Any] = [query]
        where = ["messages_fts MATCH ?", f"m.role IN ({role_placeholders})"]
        params.extend(roles)
        if exclude_session_id:
            where.append("m.session_id != ?")
            params.append(exclude_session_id)
        if workspace_id:
            where.append("(s.workspace_id IS NULL OR s.workspace_id = ?)")
            params.append(workspace_id)

        order = "bm25(messages_fts)"
        if sort == "newest":
            order = "m.created_at DESC, bm25(messages_fts)"
        elif sort == "oldest":
            order = "m.created_at ASC, bm25(messages_fts)"

        sql = f"""
            SELECT m.id, m.session_id, m.seq, m.role, m.content, m.is_compacted,
                   m.created_at, bm25(messages_fts) AS rank
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE {" AND ".join(where)}
            ORDER BY {order}
            LIMIT ?
        """
        params.append(scan_limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _like_search(
        self,
        query: str,
        *,
        roles: Sequence[str],
        sort: str,
        scan_limit: int,
        exclude_session_id: Optional[str],
        workspace_id: Optional[str],
    ) -> List[Dict[str, Any]]:
        conn = self.db.connect()
        # 去掉引号，用 AND 多词 LIKE；收紧到近 90 天
        terms = [t.strip('"') for t in query.split() if t.strip('"')]
        if not terms:
            return []
        role_placeholders = ",".join("?" * len(roles)) if roles else ""
        params: List[Any] = []
        where = [f"m.role IN ({role_placeholders})"]
        params.extend(roles)
        cutoff = time.time() - 90 * 86400
        where.append("(s.updated_at IS NULL OR s.updated_at >= ?)")
        params.append(cutoff)
        for t in terms[:5]:
            where.append("m.content LIKE ?")
            params.append(f"%{t}%")
        if exclude_session_id:
            where.append("m.session_id != ?")
            params.append(exclude_session_id)
        if workspace_id:
            where.append("(s.workspace_id IS NULL OR s.workspace_id = ?)")
            params.append(workspace_id)

        order = "s.updated_at DESC"
        if sort == "oldest":
            order = "s.updated_at ASC"
        elif sort == "relevance":
            order = "s.updated_at DESC"

        sql = f"""
            SELECT m.id, m.session_id, m.seq, m.role, m.content, m.is_compacted,
                   m.created_at
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE {" AND ".join(where)}
            ORDER BY {order}
            LIMIT ?
        """
        params.append(scan_limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Anchored view / scroll / read / browse
    # ------------------------------------------------------------------ #

    def get_anchored_view(
        self,
        session_id: str,
        message_id: int,
        *,
        window: int = DEFAULT_WINDOW,
        bookend: int = DEFAULT_BOOKEND,
    ) -> Dict[str, Any]:
        with self.db.lock:
            return self._get_anchored_view_unlocked(
                session_id, message_id, window=window, bookend=bookend
            )

    def _get_anchored_view_unlocked(
        self,
        session_id: str,
        message_id: int,
        *,
        window: int = DEFAULT_WINDOW,
        bookend: int = DEFAULT_BOOKEND,
    ) -> Dict[str, Any]:
        conn = self.db.connect()
        anchor = conn.execute(
            "SELECT * FROM messages WHERE id=? AND session_id=?",
            (message_id, session_id),
        ).fetchone()
        if anchor is None:
            # 索引滞后：尝试 JSON fallback（无稳定 id，返回空窗口）
            return {
                "messages": [],
                "bookend_start": [],
                "bookend_end": [],
                "messages_before": 0,
                "messages_after": 0,
            }

        seq = int(anchor["seq"])
        lo = max(0, seq - window)
        hi = seq + window
        rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE session_id=? AND seq BETWEEN ? AND ?
              AND role IN ('user','assistant')
            ORDER BY seq ASC
            """,
            (session_id, lo, hi),
        ).fetchall()
        messages = [
            _row_to_hit(r, truncate_to=CONTENT_TRUNCATE, anchor=(int(r["id"]) == message_id))
            for r in rows
        ]

        start_rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE session_id=? AND role IN ('user','assistant')
            ORDER BY seq ASC LIMIT ?
            """,
            (session_id, bookend),
        ).fetchall()
        end_rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE session_id=? AND role IN ('user','assistant')
            ORDER BY seq DESC LIMIT ?
            """,
            (session_id, bookend),
        ).fetchall()
        end_rows = list(reversed(end_rows))

        before = conn.execute(
            """
            SELECT COUNT(*) AS c FROM messages
            WHERE session_id=? AND seq < ? AND role IN ('user','assistant')
            """,
            (session_id, seq),
        ).fetchone()["c"]
        after = conn.execute(
            """
            SELECT COUNT(*) AS c FROM messages
            WHERE session_id=? AND seq > ? AND role IN ('user','assistant')
            """,
            (session_id, seq),
        ).fetchone()["c"]

        return {
            "messages": messages,
            "bookend_start": [
                _row_to_hit(r, truncate_to=BOOKEND_TRUNCATE) for r in start_rows
            ],
            "bookend_end": [
                _row_to_hit(r, truncate_to=BOOKEND_TRUNCATE) for r in end_rows
            ],
            "messages_before": int(before),
            "messages_after": int(after),
        }

    def scroll(
        self,
        session_id: str,
        around_message_id: int,
        *,
        window: Optional[int] = None,
        current_session_id: Optional[str] = None,
        include_current: bool = False,
    ) -> Dict[str, Any]:
        with self.db.lock:
            window = window if window is not None else self.default_window
            if (
                self.exclude_current_session
                and not include_current
                and current_session_id
                and session_id == current_session_id
            ):
                pass
            view = self._get_anchored_view_unlocked(
                session_id, around_message_id, window=window
            )
            out = {
                "success": True,
                "mode": "scroll",
                "session_id": session_id,
                "around_message_id": around_message_id,
                "messages": [m.to_dict() for m in view["messages"]],
                "messages_before": view["messages_before"],
                "messages_after": view["messages_after"],
                "index_mode": self.db.index_mode,
            }
            warn = self.rebuild_warning()
            if warn:
                out["index_rebuild"] = warn
            return out

    def read_session(
        self,
        session_id: str,
        *,
        head: int = 20,
        tail: int = 10,
    ) -> Dict[str, Any]:
        with self.db.lock:
            sess = self._get_session_unlocked(session_id)
            conn = self.db.connect()
            if sess is None:
                filepath = os.path.join(self.sessions_dir, f"{session_id}.json")
                if not os.path.isfile(filepath):
                    return {
                        "success": False,
                        "mode": "read",
                        "error": "session_not_found",
                        "session_id": session_id,
                    }
                return {
                    "success": True,
                    "mode": "read",
                    "session_id": session_id,
                    "title": None,
                    "preview": None,
                    "message_count": None,
                    "head": [],
                    "tail": [],
                    "note": "索引中无该会话，请触发 rebuild 或等待 upsert",
                    "index_mode": self.db.index_mode,
                }

            head_rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id=? AND role IN ('user','assistant')
                ORDER BY seq ASC LIMIT ?
                """,
                (session_id, head),
            ).fetchall()
            tail_rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id=? AND role IN ('user','assistant')
                ORDER BY seq DESC LIMIT ?
                """,
                (session_id, tail),
            ).fetchall()
            tail_rows = list(reversed(tail_rows))
            out = {
                "success": True,
                "mode": "read",
                "session_id": session_id,
                "title": sess.get("title"),
                "preview": sess.get("preview"),
                "when": _format_when(sess.get("updated_at")),
                "message_count": sess.get("message_count"),
                "head": [
                    _row_to_hit(r, truncate_to=CONTENT_TRUNCATE).to_dict()
                    for r in head_rows
                ],
                "tail": [
                    _row_to_hit(r, truncate_to=CONTENT_TRUNCATE).to_dict()
                    for r in tail_rows
                ],
                "index_mode": self.db.index_mode,
            }
            warn = self.rebuild_warning()
            if warn:
                out["index_rebuild"] = warn
            return out

    def browse(
        self,
        *,
        limit: int = 10,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.db.lock:
            conn = self.db.connect()
            params: List[Any] = []
            where = ""
            if workspace_id:
                where = "WHERE workspace_id = ? OR workspace_id IS NULL"
                params.append(workspace_id)
            params.append(limit)
            rows = conn.execute(
                f"""
                SELECT id, title, preview, updated_at, message_count, workspace_id
                FROM sessions
                {where}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            items = [
                BrowseItem(
                    session_id=r["id"],
                    title=r["title"],
                    preview=r["preview"],
                    when=_format_when(r["updated_at"]),
                    message_count=int(r["message_count"] or 0),
                    workspace_id=r["workspace_id"],
                ).to_dict()
                for r in rows
            ]
            out: Dict[str, Any] = {
                "success": True,
                "mode": "browse",
                "count": len(items),
                "results": items,
                "index_mode": self.db.index_mode,
            }
            warn = self.rebuild_warning()
            if warn:
                out["index_rebuild"] = warn
            return out

    def _get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self.db.lock:
            return self._get_session_unlocked(session_id)

    def _get_session_unlocked(self, session_id: str) -> Optional[Dict[str, Any]]:
        conn = self.db.connect()
        row = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return dict(row) if row else None
