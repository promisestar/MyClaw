"""图片处理：把本地图片转为 OpenAI 兼容的 image_url part。

两种模式：
- base64：把图片读出后用 Pillow 压缩到 ≤ max_mb，data URL 内联（默认）
- url：拼接公网可访问的 URL（依赖 main.py 挂载的 /files 静态目录）
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
from io import BytesIO
from pathlib import Path
from typing import Literal, Optional


logger = logging.getLogger(__name__)


ImageMode = Literal["base64", "url"]


_SUPPORTED_IMAGE_MIMES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}


class ImagePartError(Exception):
    """图片转 part 失败时抛出，由调用方决定降级策略。"""


def _guess_mime(path: str) -> str:
    ext = (os.path.splitext(path)[1] or "").lower()
    if ext in _SUPPORTED_IMAGE_MIMES:
        return _SUPPORTED_IMAGE_MIMES[ext]
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def _compress_image_bytes(raw: bytes, mime: str, max_mb: float) -> tuple[bytes, str]:
    """如有必要，用 Pillow 把图片压缩到 ≤ max_mb；返回 (新bytes, 新mime)。

    策略：
    - ≤ 阈值：原样返回
    - > 阈值：先尝试转 JPEG quality=85 → 75 → 60 → 等比缩放 0.75 → 0.5
    - 失败：原样返回（由调用方决定是否截断）
    """
    max_bytes = int(max_mb * 1024 * 1024)
    if len(raw) <= max_bytes:
        return raw, mime

    try:
        from PIL import Image  # 延迟导入，避免模块加载开销
    except Exception as exc:
        logger.warning("Pillow 不可用，无法压缩图片: %s", exc)
        return raw, mime

    try:
        img = Image.open(BytesIO(raw))
        # 透明通道：RGBA → RGB（jpeg 不支持 alpha）
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")

        # 多策略递进压缩
        attempts = [
            ("JPEG", {"quality": 85}, 1.0),
            ("JPEG", {"quality": 75}, 1.0),
            ("JPEG", {"quality": 60}, 1.0),
            ("JPEG", {"quality": 70}, 0.75),
            ("JPEG", {"quality": 70}, 0.5),
        ]
        last_buf: Optional[BytesIO] = None
        for fmt, params, scale in attempts:
            buf = BytesIO()
            work = img
            if scale != 1.0:
                w = max(1, int(img.width * scale))
                h = max(1, int(img.height * scale))
                work = img.resize((w, h), Image.Resampling.LANCZOS)
            work.save(buf, format=fmt, **params)
            last_buf = buf
            if buf.tell() <= max_bytes:
                return buf.getvalue(), "image/jpeg"

        # 全部尝试均超限：返回最后一次（最小尺寸）的结果
        if last_buf is not None:
            logger.warning("图片压缩后仍超过 %.1fMB (size=%d)", max_mb, last_buf.tell())
            return last_buf.getvalue(), "image/jpeg"
        return raw, mime
    except Exception as exc:
        logger.warning("图片压缩失败 mime=%s err=%s", mime, exc)
        return raw, mime


def build_image_part(
    path: str,
    mode: ImageMode = "base64",
    public_base_url: Optional[str] = None,
    workspace_uploads_root: Optional[str] = None,
    max_mb: float = 5.0,
    detail: Optional[str] = None,
) -> dict:
    """把本地图片转为 OpenAI image_url part。

    Args:
        path: 图片文件绝对路径（必须真实存在）
        mode: ``base64`` 内联或 ``url`` 模式
        public_base_url: URL 模式下的公网前缀（如 http://host:8000/files）
        workspace_uploads_root: URL 模式下用于计算相对路径的根目录（防越权）
        max_mb: base64 模式下最终大小上限（MB）
        detail: OpenAI 视觉 ``detail`` 字段，可选（auto/low/high）

    Returns:
        OpenAI 多模态 image_url part：
        ``{"type": "image_url", "image_url": {"url": "..."}}``

    Raises:
        ImagePartError: 文件不存在 / URL 模式下路径越权 / public_base_url 未配置
    """
    p = Path(path).resolve()
    if not p.is_file():
        raise ImagePartError(f"图片文件不存在: {path}")

    mime = _guess_mime(str(p))

    if mode == "url":
        if not public_base_url:
            raise ImagePartError("URL 模式需要配置 MULTIMODAL_PUBLIC_BASE_URL")
        if workspace_uploads_root:
            uploads_root = Path(workspace_uploads_root).resolve()
            try:
                rel = p.relative_to(uploads_root)
            except ValueError as exc:
                raise ImagePartError(
                    f"URL 模式下图片必须位于 uploads 目录内: {p}"
                ) from exc
            rel_posix = str(rel).replace("\\", "/")
        else:
            rel_posix = p.name
        url = public_base_url.rstrip("/") + "/" + rel_posix
        part = {"type": "image_url", "image_url": {"url": url}}
        if detail:
            part["image_url"]["detail"] = detail
        return part

    # base64 模式
    try:
        raw = p.read_bytes()
    except Exception as exc:
        raise ImagePartError(f"读取图片失败: {exc}") from exc

    raw, mime = _compress_image_bytes(raw, mime, max_mb)
    b64 = base64.b64encode(raw).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    part: dict = {"type": "image_url", "image_url": {"url": data_url}}
    if detail:
        part["image_url"]["detail"] = detail
    # 私有字段：记录源文件绝对路径，供 multimodal_bridge 在编码进历史前
    # 把 base64 替换为 @FILE: 占位，避免会话历史/token 计数被 base64 撑爆。
    # 该字段以下划线开头，OpenAI/兼容协议会忽略未知字段，但稳妥起见在 _build_messages
    # patch 中也会在送出前剥离。
    part["_local_path"] = str(p)
    return part


def load_image_as_data_url(path: str, max_mb: float = 5.0) -> str:
    """从本地路径即时读取并构造 data URL。

    供 multimodal_bridge 在调用 LLM 前把 @FILE: 占位还原为真实图片 data URL；
    与 build_image_part 共享同一份压缩策略。

    Raises:
        ImagePartError: 文件不存在或读取失败
    """
    p = Path(path).resolve()
    if not p.is_file():
        raise ImagePartError(f"图片文件不存在: {path}")
    mime = _guess_mime(str(p))
    try:
        raw = p.read_bytes()
    except Exception as exc:
        raise ImagePartError(f"读取图片失败: {exc}") from exc
    raw, mime = _compress_image_bytes(raw, mime, max_mb)
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"
