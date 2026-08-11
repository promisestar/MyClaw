"""session_recall 配置默认值与加载。"""

from __future__ import annotations

import os
from typing import Any, Dict


DEFAULT_SESSION_RECALL: Dict[str, Any] = {
    "enabled": True,
    "db_path": None,
    "discover_limit": 3,
    "fts_scan_limit": 100,
    "default_window": 5,
    "index_tool_messages": False,
    "exclude_current_session": True,
    "cjk_like_fallback": True,
    "auto_hint_in_prompt": False,
    "force_like": False,
}


def resolve_session_recall_config(global_config: Dict[str, Any] | None) -> Dict[str, Any]:
    """合并全局 config.json 中的 session_recall 段。"""
    cfg = dict(DEFAULT_SESSION_RECALL)
    raw = (global_config or {}).get("session_recall")
    if isinstance(raw, dict):
        cfg.update(raw)
    return cfg


def resolve_index_db_path(cfg: Dict[str, Any], sessions_dir: str) -> str:
    custom = cfg.get("db_path")
    if custom:
        return os.path.expanduser(str(custom))
    return os.path.join(sessions_dir, "index.db")
