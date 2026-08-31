"""从环境变量解析 Qdrant 连接参数（与线上 RAG/Memory 一致）。"""

from __future__ import annotations

import os
from typing import Optional, Tuple


def resolve_qdrant_env() -> Tuple[Optional[str], Optional[str]]:
    """返回 (url, api_key)。url/api_key 可为 None（表示走客户端默认本地）。"""
    url = (os.getenv("QDRANT_URL") or "").strip() or None
    api_key = (os.getenv("QDRANT_API_KEY") or "").strip() or None
    return url, api_key
