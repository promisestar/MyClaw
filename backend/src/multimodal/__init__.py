"""MyClaw 多模态处理子包。

职责：
- 文档抽取（PDF/DOCX/XLSX/TXT/MD 等）：复用项目已集成的 markitdown
- 图片处理：根据配置（base64 / url 模式）构造 OpenAI 兼容的 image_url part
- content_builder：把用户文本 + 附件列表拼装成 OpenAI 多模态 list-content
"""

from .extractor import DocumentExtractor, ExtractResult
from .image import build_image_part, ImagePartError
from .content_builder import (
    MultimodalConfig,
    build_user_content,
    flatten_content_to_text,
)

__all__ = [
    "DocumentExtractor",
    "ExtractResult",
    "build_image_part",
    "ImagePartError",
    "MultimodalConfig",
    "build_user_content",
    "flatten_content_to_text",
]
