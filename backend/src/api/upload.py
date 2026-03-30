"""文件上传 API：将文件保存到工作空间 uploads 目录，供助手通过路径读取或 RAG 处理。"""

import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

router = APIRouter(prefix="/upload", tags=["upload"])

# 默认单文件上限 10MB，可通过环境变量调整
_MAX_BYTES = int(os.getenv("UPLOAD_MAX_BYTES", str(10 * 1024 * 1024)))
_CHUNK = 1024 * 1024


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


class UploadResponse(BaseModel):
    """上传成功后的元数据（路径相对于工作空间根目录，便于在对话里引用）。"""

    filename: str = Field(description="原始文件名")
    stored_path: str = Field(description="相对于工作空间根的路径，POSIX 风格")
    size: int = Field(description="字节数")


@router.post("/file", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(None),
):
    """multipart/form-data：字段 `file` 为文件，可选 `session_id` 用于分子目录存放。"""
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

    total = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过上限 {_MAX_BYTES} 字节（UPLOAD_MAX_BYTES）",
                    )
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except OSError as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"写入失败: {e}") from e

    rel = dest.relative_to(ws)
    return UploadResponse(
        filename=file.filename or safe_name,
        stored_path=str(rel).replace("\\", "/"),
        size=total,
    )
