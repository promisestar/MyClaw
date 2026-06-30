"""多模态 user content 构造器。

把「文本 + 附件列表」拼装成 OpenAI 兼容的多模态 content：

    [
      {"type": "text", "text": "用户文本 + <file name='a.pdf'>文档全文</file> ..."},
      {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}},
      ...
    ]

约束：
- 文档体积已在 upload.py 通过 MULTIMODAL_DOC_MAX_BYTES（默认 10MB）硬上限拦截，
  此处不再做字符级截断，整段全文注入；
- 图片若文件丢失/读取失败，降级为在 text 中追加 ``[图片读取失败: ...]`` 提示，
  不阻断整个对话流程。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .extractor import DocumentExtractor, classify_kind
from .image import ImageMode, ImagePartError, build_image_part


logger = logging.getLogger(__name__)


@dataclass
class MultimodalConfig:
    """多模态构造配置（从环境变量 / config.json 注入）。"""

    image_mode: ImageMode = "base64"
    public_base_url: Optional[str] = None  # URL 模式下的公网前缀
    uploads_root: Optional[str] = None     # URL 模式下用于校验越权
    max_image_mb: float = 5.0


def _attachment_abs_path(att: dict, workspace_root: str) -> str:
    """把 attachment.stored_path（相对工作空间根）转为绝对路径。"""
    stored = att.get("stored_path") or ""
    if not stored:
        return ""
    if os.path.isabs(stored):
        return stored
    return os.path.normpath(os.path.join(workspace_root, stored))


def _format_doc_snippet(filename: str, kind: str, text: str, error: Optional[str]) -> str:
    """把单个文档抽取结果格式化为 LLM 友好的片段。"""
    header = f'<file name="{filename}" kind="{kind}">'
    body = text if text else ""
    if error:
        body = (body + ("\n\n" if body else "") + f"[抽取提示] {error}").strip()
    return f"{header}\n{body}\n</file>"


def build_user_content(
    text: str,
    attachments: Iterable[dict],
    *,
    workspace_root: str,
    config: MultimodalConfig,
) -> Any:
    """构造 OpenAI 多模态 user content。

    Args:
        text: 用户输入的纯文本
        attachments: ``[{stored_path, filename, mime_type, kind, size}, ...]``
        workspace_root: 工作空间绝对路径（用来还原 stored_path）
        config: 多模态配置

    Returns:
        - 无附件时：原样返回 ``text`` 字符串（保持现有链路无侵入）
        - 有附件时：返回 ``list[dict]``，首项为 text part，后续依次为 image_url part
    """
    attachments = list(attachments or [])
    if not attachments:
        return text or ""

    text_parts: list[str] = [text or ""]
    image_parts: list[dict] = []

    extractor = DocumentExtractor()

    for att in attachments:
        abs_path = _attachment_abs_path(att, workspace_root)
        filename = att.get("filename") or os.path.basename(abs_path) or "unnamed"
        # 优先使用前端传来的 kind；为空时由扩展名兜底
        kind = att.get("kind") or classify_kind(abs_path)

        if kind == "image":
            try:
                part = build_image_part(
                    abs_path,
                    mode=config.image_mode,
                    public_base_url=config.public_base_url,
                    workspace_uploads_root=config.uploads_root,
                    max_mb=config.max_image_mb,
                )
                image_parts.append(part)
            except ImagePartError as exc:
                logger.warning("图片附件处理失败: %s (%s)", filename, exc)
                text_parts.append(f"\n\n[图片读取失败: {filename} - {exc}]")
            except Exception as exc:  # 兜底，避免阻断对话
                logger.exception("图片附件处理异常: %s", filename)
                text_parts.append(f"\n\n[图片处理异常: {filename} - {exc}]")
            continue

        if kind == "doc":
            res = extractor.extract_text(abs_path)
            snippet = _format_doc_snippet(filename, res.kind, res.text, res.error)
            text_parts.append("\n\n" + snippet)
            continue

        # other 类型：保留路径引用，由 Agent 决定是否用工具读取
        text_parts.append(
            f"\n\n[附件 {filename}（kind={kind}）保存在 {att.get('stored_path')}，"
            f"如需读取请使用 read 工具]"
        )

    final_text = "".join(text_parts).strip()
    content: list[dict] = [{"type": "text", "text": final_text}]
    content.extend(image_parts)
    return content


def flatten_content_to_text(content: Any) -> str:
    """把可能是 list-content 的消息拍平成纯文本。

    主要用途：
    - token 估算（图片 part 用占位符）
    - 历史摘要 / Memory 捕获 / 日志展示
    - 会话历史给前端兜底渲染

    Args:
        content: ``str | list[dict] | None``

    Returns:
        拍平后的字符串（保证为 str）。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    pieces: list[str] = []
    for part in content:
        if isinstance(part, str):
            pieces.append(part)
            continue
        if not isinstance(part, dict):
            pieces.append(str(part))
            continue
        ptype = part.get("type")
        if ptype == "text":
            pieces.append(part.get("text") or "")
        elif ptype == "image_url":
            # 用占位符替代实际 base64/URL，避免 token 估算/日志噪声
            url = (part.get("image_url") or {}).get("url") or ""
            if url.startswith("data:"):
                pieces.append("[image:base64]")
            elif url:
                pieces.append(f"[image:{url[:80]}]")
            else:
                pieces.append("[image]")
        else:
            # 兜底：未知 part 类型，取 text 字段或字符串化
            pieces.append(part.get("text") or "")
    return "".join(pieces)
