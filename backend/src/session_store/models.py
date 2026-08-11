"""跨会话回忆索引结果 DTO。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MessageHit:
    id: int
    role: str
    content: str
    seq: int = 0
    anchor: bool = False
    content_truncated: bool = False
    is_compacted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if not self.anchor:
            d.pop("anchor", None)
        if not self.content_truncated:
            d.pop("content_truncated", None)
        if not self.is_compacted:
            d.pop("is_compacted", None)
        return d


@dataclass
class DiscoverResult:
    session_id: str
    title: Optional[str]
    preview: Optional[str]
    when: Optional[str]
    match_message_id: int
    matched_role: str
    snippet: str
    messages: List[MessageHit] = field(default_factory=list)
    bookend_start: List[MessageHit] = field(default_factory=list)
    bookend_end: List[MessageHit] = field(default_factory=list)
    messages_before: int = 0
    messages_after: int = 0
    link_hint: str = ""
    workspace_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "preview": self.preview,
            "when": self.when,
            "match_message_id": self.match_message_id,
            "matched_role": self.matched_role,
            "snippet": self.snippet,
            "messages": [m.to_dict() for m in self.messages],
            "bookend_start": [m.to_dict() for m in self.bookend_start],
            "bookend_end": [m.to_dict() for m in self.bookend_end],
            "messages_before": self.messages_before,
            "messages_after": self.messages_after,
            "link_hint": self.link_hint or f"可请用户在会话列表打开 {self.session_id}",
            "workspace_id": self.workspace_id,
        }


@dataclass
class BrowseItem:
    session_id: str
    title: Optional[str]
    preview: Optional[str]
    when: Optional[str]
    message_count: int = 0
    workspace_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
