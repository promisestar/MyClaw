"""文档感知的文件读取工具。

继承 hello_agents 的 ReadTool，在读取文件前检测扩展名：
- 纯文本格式（.py/.ts/.md/.json 等）→ 走原版 ReadTool 逻辑（行号、offset、limit）
- 文档格式（.pdf/.docx/.xlsx/.pptx 等）→ 委托 DocumentExtractor 提取纯文本
- 目录 → 走原版 ReadTool 的目录列表逻辑

解决场景：Agent 对二进制文档调用 Read 时不再 UnicodeDecodeError，
也不会因为读不了就转去 RAG 入库——直接返回提取后的文本。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from hello_agents.tools import ToolResponse
from hello_agents.tools.builtin.file_tools import ReadTool
from hello_agents.tools.errors import ToolErrorCode

from ...multimodal.extractor import DocumentExtractor, _DOC_EXTENSIONS, _is_plain_text_safe


class DocAwareReadTool(ReadTool):
    """支持文档格式的 ReadTool。

    与原版 ReadTool 的唯一差异：遇到二进制文档格式（PDF/DOCX/XLSX/PPTX 等）
    时，委托 DocumentExtractor 提取文本，而非直接 open(encoding='utf-8') 导致
    UnicodeDecodeError。

    纯文本格式和目录列表行为与原版完全一致，不影响 offset/limit/元数据缓存。
    """

    # 覆盖父类描述，明确声明支持的文档格式，避免 LLM 误选 rag 入库
    _read_description: str = (
        "读取文件或列出目录内容。支持纯文本格式（.py/.ts/.md/.json/.vue/.html/.css 等）"
        "和文档格式（.pdf/.docx/.xlsx/.pptx — 自动提取为纯文本）。"
        "JSON/CSV 等结构化文件可直接读取。"
        "读取文档格式时无需先用 rag 入库，直接 Read 即可获取文本内容。"
        "参数: path (必需) — 文件路径或目录路径。"
        "对于文件: offset (起始行, 默认0), limit (最大行数, 默认2000)。"
        "目录参数: depth (递归深度, 默认1), limit (最大条目数, 默认200)。"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._doc_extractor = DocumentExtractor()
        # 覆盖父类 Init 设置的通用描述，显式声明文档格式支持
        self.description = self._read_description

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        path = parameters.get("path")
        if not path:
            return super().run(parameters)

        # 解析路径（复用父类逻辑）
        full_path = self._resolve_path(path)

        # 目录 → 原版逻辑
        if full_path.is_dir():
            return super().run(parameters)

        # 纯文本格式 → 原版逻辑（保留 offset/limit/行号/元数据缓存）
        if _is_plain_text_safe(str(full_path)):
            return super().run(parameters)

        # 二进制文档格式 → 委托 DocumentExtractor
        ext = (os.path.splitext(str(full_path))[1] or "").lower()
        if ext not in _DOC_EXTENSIONS:
            # 未知的二进制格式（.zip/.exe 等）→ 返回友好错误
            return ToolResponse.error(
                code=ToolErrorCode.INTERNAL_ERROR,
                message=(
                    f"不支持读取此文件格式: {ext}。"
                    f"Read 工具支持纯文本和文档格式"
                    f"（PDF/DOCX/XLSX/PPTX/TXT/MD/CSV/JSON 等）。"
                ),
            )

        # 文档格式 → 提取文本
        result = self._doc_extractor.extract_text(str(full_path))
        if result.error and not result.text:
            return ToolResponse.error(
                code=ToolErrorCode.INTERNAL_ERROR,
                message=f"文档解析失败 ({result.kind}): {result.error}",
            )

        # 构造与原版 ReadTool 兼容的返回结构
        text = result.text
        lines = text.splitlines()
        total_lines = len(lines)

        # 支持 offset/limit（对提取后的文本行做截断）
        offset = parameters.get("offset", 0)
        limit = parameters.get("limit", 2000)
        if offset > 0:
            lines = lines[offset:]
        if limit > 0:
            lines = lines[:limit]

        content = "\n".join(lines)

        # 元数据缓存（与原版一致，供乐观锁使用）
        try:
            mtime = os.path.getmtime(full_path)
            size = os.path.getsize(full_path)
            if self.registry:
                self.registry.cache_read_metadata(path, {
                    "file_mtime_ms": int(mtime * 1000),
                    "file_size_bytes": size,
                })
        except Exception:
            pass

        warning = f"\n\n[文档已自动提取为文本，原始格式: {result.kind}]"
        if result.error:
            warning += f"\n[提取提示: {result.error}]"

        return ToolResponse.success(
            text=(
                f"读取文档 {full_path.name}（{result.kind} 格式，"
                f"已提取为纯文本，当前显示 {len(lines)}/{total_lines} 行）"
            ),
            data={
                "content": content + warning,
                "lines": len(lines),
                "total_lines": total_lines,
                "file_mtime_ms": int(os.path.getmtime(full_path) * 1000),
                "file_size_bytes": os.path.getsize(full_path),
                "offset": offset,
                "limit": limit,
                "doc_kind": result.kind,
                "extracted": True,
            },
        )
