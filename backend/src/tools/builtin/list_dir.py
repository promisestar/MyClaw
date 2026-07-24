"""目录列表工具 — os.scandir 单层扫描，跨平台。"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from hello_agents.tools import Tool, ToolParameter, ToolResponse

from ._search_utils import resolve_search_path, truncate_output


class ListDirTool(Tool):
    """列出目录内容（单层扫描）。

    返回条目名 + 类型标注（DIR/FILE/LINK）。
    跨平台：Windows/Linux/macOS 通用。
    """

    def __init__(
        self,
        project_root: str = ".",
        max_output_size: int = 5_000,
    ):
        super().__init__(
            name="list_dir",
            description=(
                "列出指定目录的内容（单层，不递归）。"
                "返回条目名和类型标注（[DIR]/[FILE]/[LINK]）。"
                "用于快速了解目录结构。跨平台通用。"
            ),
            expandable=False,
        )
        self.project_root = os.path.abspath(os.path.expanduser(project_root))
        self.max_output_size = max_output_size
        # ContextGuard 元数据
        self.output_size_hint = 1500
        self.has_side_effects = False  # 只读，可委托

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="target_directory",
                type="string",
                description="要列出的目录路径（可选；默认工作空间根，相对路径相对工作空间）",
                required=False,
            ),
            ToolParameter(
                name="ignore_globs",
                type="string",
                description="要忽略的 glob 模式，逗号分隔（如 \"*.pyc,.git\"；可选）",
                required=False,
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        raw_dir = parameters.get("target_directory")
        # 限定范围在工作区内（L2 沙箱）
        target, err = resolve_search_path(
            raw_dir, self.project_root, allowed_directories=[self.project_root]
        )
        if err:
            return ToolResponse.error(code="PATH_ERROR", message=err)

        if not target.is_dir():
            return ToolResponse.error(
                code="NOT_A_DIRECTORY",
                message=f"路径不是目录: {target}",
            )

        ignore_globs_raw = parameters.get("ignore_globs") or ""
        ignore_globs = [
            g.strip() for g in ignore_globs_raw.split(",") if g.strip()
        ] if ignore_globs_raw else []

        entries: List[str] = []

        try:
            scan_results = sorted(
                os.scandir(target),
                key=lambda e: (not e.is_dir(), e.name.lower()),
            )
        except (OSError, PermissionError) as e:
            return ToolResponse.error(
                code="SCAN_ERROR",
                message=f"扫描目录失败: {e}",
            )

        for entry in scan_results:
            name = entry.name

            # 检查 ignore_globs
            if ignore_globs and any(
                fnmatch.fnmatch(name, g) for g in ignore_globs
            ):
                continue

            # 类型标注
            if entry.is_symlink():
                tag = "[LINK]"
            elif entry.is_dir():
                tag = "[DIR]"
            elif entry.is_file():
                tag = "[FILE]"
            else:
                tag = "[OTHER]"

            entries.append(f"{tag} {name}")

        text = "\n".join(entries) if entries else "(空目录)"
        text = truncate_output(text, self.max_output_size)

        return ToolResponse.success(
            text=text,
            data={
                "directory": str(target),
                "entry_count": len(entries),
            },
        )
