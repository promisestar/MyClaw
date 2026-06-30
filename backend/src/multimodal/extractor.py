"""文档文本抽取器。

复用项目已存在的 markitdown 集成（见 src/rag/pipeline.py 的实现思路），
统一把 PDF / DOCX / XLSX / PPTX / TXT / MD / CSV / JSON 等文档转为纯文本，
用于把文档内容注入到当前用户消息的 text part 中送给 LLM。

设计原则：
- 单文件 ≤ MULTIMODAL_DOC_MAX_BYTES（默认 10MB）的大小约束已在 upload.py 强制；
  本模块不再做字符级截断（避免双重截断造成困惑）。
- 解析异常返回安全降级文本（包含错误原因），永不抛出，避免阻断对话主流程。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger(__name__)


DocKind = Literal["pdf", "docx", "xlsx", "pptx", "text", "html", "csv", "json", "unknown"]


# 与 rag/pipeline.py 中的「非纯文本」白名单保持一致，避免对二进制走 open().read()
_NON_PLAIN_TEXT_EXTENSIONS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".rar",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
})

# 文档类附件支持的扩展名（image 类不走这里）
_DOC_EXTENSIONS = frozenset({
    ".pdf",
    ".doc", ".docx",
    ".xls", ".xlsx",
    ".ppt", ".pptx",
    ".txt", ".md", ".markdown",
    ".csv", ".tsv",
    ".json", ".xml", ".yaml", ".yml", ".toml",
    ".html", ".htm",
    ".log", ".conf", ".ini", ".cfg",
})

_IMAGE_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif",
})


@dataclass
class ExtractResult:
    """文档抽取结果"""

    text: str
    kind: DocKind
    chars: int
    error: Optional[str] = None  # 抽取失败时的降级提示

    def to_dict(self) -> dict:
        d = {"text": self.text, "kind": self.kind, "chars": self.chars}
        if self.error:
            d["error"] = self.error
        return d


def classify_kind(path: str) -> Literal["image", "doc", "other"]:
    """根据扩展名判断附件大类（与前端/上传 API 共享同一份判断）。"""
    ext = (os.path.splitext(path)[1] or "").lower()
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    if ext in _DOC_EXTENSIONS:
        return "doc"
    return "other"


def _ext(path: str) -> str:
    return (os.path.splitext(path)[1] or "").lower()


def _ext_to_kind(path: str) -> DocKind:
    ext = _ext(path)
    if ext == ".pdf":
        return "pdf"
    if ext in (".doc", ".docx"):
        return "docx"
    if ext in (".xls", ".xlsx"):
        return "xlsx"
    if ext in (".ppt", ".pptx"):
        return "pptx"
    if ext in (".html", ".htm"):
        return "html"
    if ext in (".csv", ".tsv"):
        return "csv"
    if ext == ".json":
        return "json"
    if ext in (".txt", ".md", ".markdown", ".log", ".conf", ".ini", ".cfg",
               ".yaml", ".yml", ".toml", ".xml"):
        return "text"
    return "unknown"


def _is_plain_text_safe(path: str) -> bool:
    return _ext(path) not in _NON_PLAIN_TEXT_EXTENSIONS


def _fallback_plain_text(path: str) -> str:
    """UTF-8 兜底读取（仅对纯文本格式调用）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as exc:
        logger.warning("Plain-text fallback failed for %s: %s", path, exc)
        return ""


def _get_markitdown():
    try:
        from markitdown import MarkItDown
        return MarkItDown()
    except Exception as exc:
        logger.warning("markitdown 不可用: %s", exc)
        return None


def _extract_text_from_result(result) -> str:
    if result is None:
        return ""
    text = getattr(result, "text_content", None)
    if isinstance(text, str) and text.strip():
        return text
    markdown = getattr(result, "markdown", None)
    if isinstance(markdown, str) and markdown.strip():
        return markdown
    return ""


class DocumentExtractor:
    """文档抽取器（无状态，线程安全）。"""

    def is_supported_doc(self, path: str) -> bool:
        return _ext(path) in _DOC_EXTENSIONS

    def extract_text(self, path: str) -> ExtractResult:
        """抽取文档纯文本。

        Args:
            path: 绝对路径或调用方已解析后的可读路径

        Returns:
            ExtractResult：text 字段保证为 str（异常时为空 + error 提示）
        """
        kind = _ext_to_kind(path)
        p = Path(path)
        if not p.is_file():
            return ExtractResult(text="", kind=kind, chars=0,
                                 error=f"文件不存在: {path}")

        # 1) 纯文本类型：直接读，速度最快、避免引入 markitdown 的副作用
        if kind == "text":
            text = _fallback_plain_text(str(p))
            return ExtractResult(text=text, kind=kind, chars=len(text))

        # 2) 结构化文档：交给 markitdown
        md = _get_markitdown()
        if md is None:
            # markitdown 不可用：纯文本兜底（仅对安全格式）
            if _is_plain_text_safe(str(p)):
                text = _fallback_plain_text(str(p))
                return ExtractResult(text=text, kind=kind, chars=len(text),
                                     error="markitdown 不可用，已用纯文本兜底")
            return ExtractResult(text="", kind=kind, chars=0,
                                 error="markitdown 不可用，无法解析二进制文档")

        try:
            result = md.convert(str(p))
            text = _extract_text_from_result(result)
            if text:
                return ExtractResult(text=text, kind=kind, chars=len(text))
            # markitdown 返回空：纯文本兜底
            if _is_plain_text_safe(str(p)):
                text = _fallback_plain_text(str(p))
                return ExtractResult(text=text, kind=kind, chars=len(text),
                                     error="markitdown 返回空，已用纯文本兜底")
            return ExtractResult(text="", kind=kind, chars=0,
                                 error="markitdown 返回空且非纯文本格式")
        except Exception as exc:
            logger.warning("DocumentExtractor failed for %s: %s", path, exc)
            if _is_plain_text_safe(str(p)):
                text = _fallback_plain_text(str(p))
                return ExtractResult(text=text, kind=kind, chars=len(text),
                                     error=f"markitdown 解析失败({exc})，已用纯文本兜底")
            return ExtractResult(text="", kind=kind, chars=0,
                                 error=f"解析失败: {exc}")
