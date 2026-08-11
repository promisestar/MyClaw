"""SQLite FTS5 能力探测（:memory: 临时库，不写用户 index.db）。"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

_cached_result: Optional[Dict[str, Any]] = None


def clear_probe_cache() -> None:
    """清除进程内探测缓存（测试用）。"""
    global _cached_result
    _cached_result = None


def probe_sqlite_fts_capabilities(*, force: bool = False) -> Dict[str, Any]:
    """探测当前解释器自带的 SQLite 是否支持 FTS5 / trigram。

    Returns:
        dict with keys: sqlite_version, fts5, trigram, error
    """
    global _cached_result
    if _cached_result is not None and not force:
        return dict(_cached_result)

    result: Dict[str, Any] = {
        "sqlite_version": sqlite3.sqlite_version,
        "fts5": False,
        "trigram": False,
        "error": None,
    }
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)")
            conn.execute("DROP TABLE temp._fts5_probe")
            result["fts5"] = True
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE temp._trigram_probe "
                    "USING fts5(x, tokenize='trigram')"
                )
                conn.execute("DROP TABLE temp._trigram_probe")
                result["trigram"] = True
            except sqlite3.OperationalError:
                pass  # Phase 1 不依赖 trigram
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        result["error"] = str(e)
    except Exception as e:  # noqa: BLE001 — 探测不得拖垮启动
        result["error"] = str(e)

    _cached_result = dict(result)
    return result
