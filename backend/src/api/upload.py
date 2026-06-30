"""文件上传 API：将文件保存到工作空间 uploads 目录，供助手通过路径读取、走 RAG，或作为多模态附件随对话发送。"""

import mimetypes
import os
import re
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from ..multimodal.extractor import DocumentExtractor, classify_kind

router = APIRouter(prefix="/upload", tags=["upload"])

# 默认单文件上限 10MB，可通过环境变量调整（其他/未知类型）
_MAX_BYTES = int(os.getenv("UPLOAD_MAX_BYTES", str(10 * 1024 * 1024)))

# 文档硬上限：单文档 ≤ 10MB（用户明确要求）
_DOC_MAX_BYTES = int(os.getenv("MULTIMODAL_DOC_MAX_BYTES", str(10 * 1024 * 1024)))

# 图片硬上限（MB），用于在上传期拦截过大的图片（实际送给 VLM 前还会再做 Pillow 压缩）
_IMAGE_MAX_MB = float(os.getenv("MULTIMODAL_MAX_IMAGE_MB", "10"))
_IMAGE_MAX_BYTES = int(_IMAGE_MAX_MB * 1024 * 1024)

_CHUNK = 1024 * 1024

AttachmentKind = Literal["image", "doc", "other"]


def _get_agent():
    from ..main import get_agent as _get_agent

    return _get_agent()


def _safe_segment(s: str, max_len: int = 80) -> str:
    s = (s or "").strip()
    if not s:
        return "general"
    s = re.sub(r"[^\w\-]", "_", s)
    return s[:max_len] if len(s) > max_len else s


def _safe_filename(name: str) -> str:
    base = Path(name or "").name
    if not base or base in (".", ".."):
        return "unnamed"
    base = base.replace("\x00", "")
    if len(base) > 200:
        stem, suf = base[:180], Path(base).suffix
        base = stem + suf
    return base


def _resolve_size_limit(kind: AttachmentKind) -> tuple[int, str]:
    """根据 kind 选择字节上限及其名称（用于错误提示）。"""
    if kind == "doc":
        return _DOC_MAX_BYTES, "MULTIMODAL_DOC_MAX_BYTES"
    if kind == "image":
        return _IMAGE_MAX_BYTES, "MULTIMODAL_MAX_IMAGE_MB"
    return _MAX_BYTES, "UPLOAD_MAX_BYTES"


def _resolve_attachment_path(agent, stored_path: str) -> Path:
    """把相对 stored_path 还原为绝对路径，并校验位于 uploads 子目录内。"""
    if not stored_path:
        raise HTTPException(status_code=400, detail="stored_path 不能为空")

    ws = Path(agent.workspace.workspace_path).resolve()
    uploads_root = (ws / "uploads").resolve()

    raw = Path(stored_path)
    target = raw if raw.is_absolute() else (ws / raw)
    try:
        target = target.resolve()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"非法路径: {exc}") from exc

    try:
        target.relative_to(uploads_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="附件必须位于 uploads 目录内") from exc

    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {stored_path}")

    return target


class UploadResponse(BaseModel):
    """上传成功后的元数据（路径相对于工作空间根目录，便于在对话里引用）。"""

    filename: str = Field(description="原始文件名")
    stored_path: str = Field(description="相对于工作空间根的路径，POSIX 风格")
    size: int = Field(description="字节数")
    mime_type: str = Field(description="MIME 类型（猜测）")
    kind: AttachmentKind = Field(description="附件大类：image | doc | other")
    extracted_chars: Optional[int] = Field(
        default=None,
        description="若 kind=doc，已抽取的纯文本字符数；其它情况为空",
    )


class ExtractResponse(BaseModel):
    """文档预览抽取结果"""

    stored_path: str
    kind: str
    chars: int
    truncated: bool = Field(description="预览是否被截断（仅影响返回给前端的预览，不影响后续 Agent 注入）")
    preview: str = Field(description="预览文本（最多 ~2000 字符）")
    error: Optional[str] = None


@router.post("/file", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(None),
):
    """multipart/form-data：字段 `file` 为文件，可选 `session_id` 用于分子目录存放。

    根据文件扩展名分流大小上限：
    - 文档（pdf/docx/xlsx/txt 等）：``MULTIMODAL_DOC_MAX_BYTES``（默认 10MB）
    - 图片：``MULTIMODAL_MAX_IMAGE_MB``（默认 10MB）
    - 其它：``UPLOAD_MAX_BYTES``（默认 10MB）
    超限直接返回 413，不落盘。
    """
    agent = _get_agent()
    if not agent:
        raise HTTPException(status_code=500, detail="Agent not initialized")

    ws = Path(agent.workspace.workspace_path).resolve()
    sub = _safe_segment(session_id or "")
    dest_dir = ws / "uploads" / sub
    dest_dir.mkdir(parents=True, exist_ok=True)

    safe_name = _safe_filename(file.filename or "file")
    dest = dest_dir / safe_name
    if dest.exists():
        stem, suf = dest.stem, dest.suffix
        n = 1
        while True:
            cand = dest_dir / f"{stem}_{n}{suf}"
            if not cand.exists():
                dest = cand
                break
            n += 1

    # 按 kind 选定字节上限
    kind: AttachmentKind = classify_kind(str(dest))
    limit_bytes, limit_name = _resolve_size_limit(kind)

    mime_type = (file.content_type
                 or mimetypes.guess_type(str(dest))[0]
                 or "application/octet-stream")

    total = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"{kind} 类型文件超过上限 "
                            f"{limit_bytes / 1024 / 1024:.1f}MB（{limit_name}）"
                        ),
                    )
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except OSError as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"写入失败: {e}") from e

    # 若是文档，顺便抽取一次得到字符数（不返回正文，仅作元数据）
    extracted_chars: Optional[int] = None
    if kind == "doc":
        try:
            res = DocumentExtractor().extract_text(str(dest))
            extracted_chars = res.chars
        except Exception:
            extracted_chars = None

    rel = dest.relative_to(ws)
    return UploadResponse(
        filename=file.filename or safe_name,
        stored_path=str(rel).replace("\\", "/"),
        size=total,
        mime_type=mime_type,
        kind=kind,
        extracted_chars=extracted_chars,
    )


@router.get("/extract", response_model=ExtractResponse)
async def extract_preview(
    path: str = Query(..., description="相对于工作空间根的 stored_path"),
    preview_chars: int = Query(2000, ge=0, le=20000, description="返回给前端的预览长度"),
):
    """按 stored_path 抽取文档纯文本，仅用于前端预览。

    - 不写入会话历史
    - 完整文本仍在发送对话时由 Agent 端再次抽取并注入（避免预览/真实注入二次同步）
    """
    agent = _get_agent()
    if not agent:
        raise HTTPException(status_code=500, detail="Agent not initialized")

    target = _resolve_attachment_path(agent, path)
    if classify_kind(str(target)) != "doc":
        raise HTTPException(status_code=400, detail="extract 仅适用于文档类附件")

    res = DocumentExtractor().extract_text(str(target))
    full_text = res.text or ""
    truncated = len(full_text) > preview_chars
    preview = full_text[:preview_chars]
    return ExtractResponse(
        stored_path=path,
        kind=res.kind,
        chars=res.chars,
        truncated=truncated,
        preview=preview,
        error=res.error,
    )
