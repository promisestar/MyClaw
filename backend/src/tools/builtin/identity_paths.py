"""身份文件路径解析 — 将 IDENTITY/USER/SOUL/BOOTSTRAP 映射到基座 identity/。

解耦后身份文件在 ``~/.helloclaw/identity/``，与工作区无关。
模型常误用工作区相对路径 ``IDENTITY.md``；此模块在工具层做别名重定向。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Set

IDENTITY_FILENAMES: Set[str] = {
    "IDENTITY.md",
    "USER.md",
    "SOUL.md",
    "BOOTSTRAP.md",
}


def identity_dir_from_home(home_path: str) -> str:
    return os.path.join(os.path.expanduser(home_path), "identity")


def resolve_identity_alias(path: str, identity_dir: str) -> Optional[Path]:
    """若 path 指向身份文件别名，返回基座下的绝对 Path；否则 None。"""
    if not path or not identity_dir:
        return None

    raw = str(path).strip().replace("\\", "/")
    expanded = os.path.expanduser(raw)
    name = os.path.basename(expanded.rstrip("/"))
    if name not in IDENTITY_FILENAMES:
        return None

    id_root = Path(os.path.realpath(identity_dir))
    target = id_root / name

    # 裸文件名：IDENTITY.md
    if raw == name or expanded.replace("\\", "/") == name:
        return target

    norm = expanded.replace("\\", "/").lower()
    # identity/IDENTITY.md 或 .../identity/IDENTITY.md
    if f"/identity/{name.lower()}" in norm or norm.endswith(f"identity/{name.lower()}"):
        return target

    # 已是 identity 目录下的绝对路径
    if os.path.isabs(expanded):
        try:
            real = Path(os.path.realpath(expanded))
            if real == target or id_root in real.parents or real.parent == id_root:
                return real if real.exists() or real.parent == id_root else target
        except OSError:
            return target

    return None


def install_identity_path_resolver(tool, identity_dir: str) -> None:
    """给 Read/Write/Edit 安装 identity 别名解析。"""
    if not hasattr(tool, "_resolve_path"):
        return

    tool._identity_dir = identity_dir

    if getattr(tool, "_identity_path_resolver_installed", False):
        return

    original_resolve = tool._resolve_path

    def _resolve_path(path: str):
        redirected = resolve_identity_alias(path, tool._identity_dir)
        if redirected is not None:
            return redirected
        # 工作区根下的同名文件（历史误写 / 模型仍写相对路径）→ 基座
        name = os.path.basename(str(path or "").strip().replace("\\", "/"))
        if name in IDENTITY_FILENAMES and hasattr(tool, "working_dir"):
            expanded = os.path.expanduser(str(path).strip())
            try:
                candidate = (
                    Path(expanded)
                    if os.path.isabs(expanded)
                    else Path(tool.working_dir) / expanded
                )
                if candidate.resolve().parent == Path(tool.working_dir).resolve():
                    return Path(os.path.realpath(tool._identity_dir)) / name
            except OSError:
                pass
        return original_resolve(path)

    tool._resolve_path = _resolve_path  # type: ignore[method-assign]
    tool._identity_path_resolver_installed = True

    # Write/Edit：backup_path.relative_to(working_dir) 在写基座时会失败；
    # 写身份文件时临时把 working_dir 切到 identity_dir。
    if getattr(tool, "name", "") in ("Write", "Edit", "MultiEdit"):
        if getattr(tool, "_identity_workdir_patch_installed", False):
            return
        original_run = tool.run

        def run_with_identity_workdir(parameters):
            path = (parameters or {}).get("path") or ""
            if resolve_identity_alias(path, tool._identity_dir) is None:
                return original_run(parameters)
            old_wd = tool.working_dir
            tool.working_dir = Path(tool._identity_dir).resolve()
            try:
                return original_run(parameters)
            finally:
                tool.working_dir = old_wd

        tool.run = run_with_identity_workdir  # type: ignore[method-assign]
        tool._identity_workdir_patch_installed = True
