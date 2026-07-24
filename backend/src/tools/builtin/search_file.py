"""文件名 glob 搜索工具 — pathlib + fnmatch 纯 Python 实现，跨平台。"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from hello_agents.tools import Tool, ToolParameter, ToolResponse

from ._search_utils import (
    DEFAULT_IGNORE_DIRS,
    compile_gitignore_patterns,
    normalize_line_endings,
    parse_gitignore,
    resolve_search_path,
    should_prune_dir,
    truncate_output,
)


class SearchFileTool(Tool):
    """按文件名 glob 模式搜索文件。

    使用 pathlib.Path.rglob + fnmatch 过滤，自动跳过忽略目录。
    跨平台：Windows/Linux/macOS 通用。
    """

    def __init__(
        self,
        project_root: str = ".",
        max_output_size: int = 5_000,
    ):
        super().__init__(
            name="search_file",
            description=(
                "按文件名 glob 模式搜索文件（如 \"*.py\"、\"test_*.ts\"）。"
                "递归扫描目录，自动跳过 .git/node_modules/.venv 等忽略目录。"
                "返回相对路径列表（POSIX 风格）。跨平台通用。"
            ),
            expandable=False,
        )
        self.project_root = os.path.abspath(os.path.expanduser(project_root))
        self.max_output_size = max_output_size
        # ContextGuard 元数据
        self.output_size_hint = 1000
        self.has_side_effects = False  # 只读，可委托

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="pattern",
                type="string",
                description="文件名 glob 模式（如 \"*.py\"、\"*.test.ts\"、\"config.*\"）",
                required=True,
            ),
            ToolParameter(
                name="target_directory",
                type="string",
                description="搜索目录（可选；默认工作空间根，相对路径相对工作空间）",
                required=False,
            ),
            ToolParameter(
                name="recursive",
                type="boolean",
                description="是否递归搜索子目录（默认 true）",
                required=False,
            ),
            ToolParameter(
                name="ignore_globs",
                type="string",
                description="要忽略的 glob 模式，逗号分隔（如 \"*.pyc,dist/**\"；可选）",
                required=False,
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        pattern = (parameters.get("pattern") or "").strip()
        if not pattern:
            return ToolResponse.error(code="INVALID_INPUT", message="pattern 不能为空")

        raw_dir = parameters.get("target_directory")
        # 限定搜索范围在工作区内（L2 沙箱）
        target, err = resolve_search_path(
            raw_dir, self.project_root, allowed_directories=[self.project_root]
        )
        if err:
            return ToolResponse.error(code="PATH_ERROR", message=err)

        recursive = bool(parameters.get("recursive", True))
        ignore_globs_raw = parameters.get("ignore_globs") or ""
        ignore_globs = [
            g.strip() for g in ignore_globs_raw.split(",") if g.strip()
        ] if ignore_globs_raw else []

        # 解析 .gitignore
        gitignore_path = target / ".gitignore"
        gi_patterns = compile_gitignore_patterns(parse_gitignore(gitignore_path))

        results: List[str] = []

        if recursive:
            for root, dirs, files in os.walk(target, topdown=True):
                # prune 忽略目录
                dirs[:] = [
                    d for d in dirs
                    if d not in DEFAULT_IGNORE_DIRS
                    and not should_prune_dir(d, gi_patterns)
                ]

                for filename in files:
                    if not fnmatch.fnmatch(filename, pattern):
                        continue

                    filepath = Path(root) / filename
                    try:
                        rel = filepath.relative_to(target).as_posix()
                    except ValueError:
                        rel = str(filepath)

                    # 检查 ignore_globs
                    if self._matches_any(rel, ignore_globs):
                        continue

                    # 检查 gitignore
                    from ._search_utils import should_ignore_path
                    if should_ignore_path(rel, gi_patterns):
                        continue

                    results.append(rel)
        else:
            # 非递归：仅扫描当前目录
            try:
                for entry in os.scandir(target):
                    if entry.is_file() and fnmatch.fnmatch(entry.name, pattern):
                        rel = entry.name
                        if not self._matches_any(rel, ignore_globs):
                            results.append(rel)
            except (OSError, PermissionError) as e:
                return ToolResponse.error(
                    code="SCAN_ERROR",
                    message=f"扫描目录失败: {e}",
                )

        results.sort()
        text = "\n".join(results) if results else "(无匹配文件)"
        text = truncate_output(text, self.max_output_size)

        return ToolResponse.success(
            text=text,
            data={
                "pattern": pattern,
                "count": len(results),
                "search_dir": str(target),
                "recursive": recursive,
            },
        )

    @staticmethod
    def _matches_any(path: str, patterns: List[str]) -> bool:
        """检查路径是否匹配任一 ignore glob。"""
        for p in patterns:
            if fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path.split("/")[-1], p):
                return True
        return False
