"""代码内容搜索工具 — ripgrep 优先 + Python 兜底。

跨平台实现：ripgrep 用 subprocess list 参数（shell=False），未检测到 rg 时
降级为纯 Python（re + os.walk prune）。全部使用 pathlib，三端通用。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from hello_agents.tools import Tool, ToolParameter, ToolResponse

from ._search_utils import (
    DEFAULT_IGNORE_DIRS,
    compile_gitignore_patterns,
    is_binary_file,
    normalize_line_endings,
    parse_gitignore,
    resolve_search_path,
    should_prune_dir,
    truncate_output,
)

_IS_WINDOWS = sys.platform == "win32"
_MAX_OUTPUT = 15_000

# ripgrep 二进制缓存探测结果（进程级缓存）
_RG_PATH: Optional[str] = None
_RG_CHECKED = False


def _find_ripgrep() -> Optional[str]:
    """探测系统 ripgrep 二进制路径（三端通用）。"""
    global _RG_PATH, _RG_CHECKED
    if _RG_CHECKED:
        return _RG_PATH
    _RG_CHECKED = True
    _RG_PATH = shutil.which("rg")
    return _RG_PATH


class SearchContentTool(Tool):
    """在文件内容中搜索正则匹配。

    优先使用 ripgrep（系统二进制，高性能），未检测到时降级为纯 Python 实现。
    支持 glob 文件过滤、上下文行、多种输出模式。
    """

    def __init__(
        self,
        project_root: str = ".",
        max_output_size: int = _MAX_OUTPUT,
    ):
        super().__init__(
            name="search_content",
            description=(
                "在文件内容中搜索正则匹配（类似 ripgrep/grep）。"
                "支持 glob 文件过滤、上下文行、三种输出模式（content/files_with_matches/count）。"
                "优先使用 ripgrep 加速，未安装时自动降级为纯 Python。"
                "跨平台：Windows/Linux/macOS 通用。"
            ),
            expandable=False,
        )
        self.project_root = os.path.abspath(os.path.expanduser(project_root))
        self.max_output_size = max_output_size
        # ContextGuard 元数据
        self.output_size_hint = 3000
        self.has_side_effects = False  # 只读搜索，可委托

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="pattern",
                type="string",
                description="正则表达式（支持 Python re 语法）",
                required=True,
            ),
            ToolParameter(
                name="path",
                type="string",
                description="搜索目录（可选；默认工作空间根，相对路径相对工作空间）",
                required=False,
            ),
            ToolParameter(
                name="glob",
                type="string",
                description="文件名过滤 glob（如 \"*.py\"；可选）",
                required=False,
            ),
            ToolParameter(
                name="case_sensitive",
                type="boolean",
                description="是否区分大小写（默认 false）",
                required=False,
            ),
            ToolParameter(
                name="context_before",
                type="integer",
                description="匹配行前显示的上下文行数（默认 0）",
                required=False,
            ),
            ToolParameter(
                name="context_after",
                type="integer",
                description="匹配行后显示的上下文行数（默认 0）",
                required=False,
            ),
            ToolParameter(
                name="output_mode",
                type="string",
                description="输出模式：content（默认，显示匹配行）| files_with_matches（仅文件名）| count（每文件匹配数）",
                required=False,
            ),
            ToolParameter(
                name="head_limit",
                type="integer",
                description="最大返回结果数（默认 100，防止输出过长）",
                required=False,
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        pattern = (parameters.get("pattern") or "").strip()
        if not pattern:
            return ToolResponse.error(code="INVALID_INPUT", message="pattern 不能为空")

        raw_path = parameters.get("path")
        # 限定搜索范围在工作区内（L2 沙箱），project_root 由 _rebind_workspace_tools 跟随工作区
        target, err = resolve_search_path(
            raw_path, self.project_root, allowed_directories=[self.project_root]
        )
        if err:
            return ToolResponse.error(code="PATH_ERROR", message=err)

        glob_filter = parameters.get("glob") or None
        case_sensitive = bool(parameters.get("case_sensitive", False))
        context_before = int(parameters.get("context_before") or 0)
        context_after = int(parameters.get("context_after") or 0)
        output_mode = (parameters.get("output_mode") or "content").strip()
        head_limit = int(parameters.get("head_limit") or 100)

        if output_mode not in ("content", "files_with_matches", "count"):
            return ToolResponse.error(
                code="INVALID_INPUT",
                message=f"output_mode 必须是 content/files_with_matches/count， got {output_mode}",
            )

        # 编译正则验证
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            re.compile(pattern, flags)
        except re.error as e:
            return ToolResponse.error(
                code="INVALID_REGEX",
                message=f"正则表达式无效: {e}",
            )

        # 优先 ripgrep
        rg = _find_ripgrep()
        if rg:
            result = self._search_with_ripgrep(
                rg, pattern, target, glob_filter, case_sensitive,
                context_before, context_after, output_mode, head_limit,
            )
        else:
            result = self._search_with_python(
                pattern, target, glob_filter, case_sensitive,
                context_before, context_after, output_mode, head_limit,
            )

        if result is None:
            return ToolResponse.error(
                code="SEARCH_ERROR",
                message="搜索执行失败",
            )

        text, data = result
        text = truncate_output(text, self.max_output_size)

        return ToolResponse.success(text=text or "(无匹配结果)", data=data)

    # ── ripgrep 实现 ──

    def _search_with_ripgrep(
        self,
        rg_path: str,
        pattern: str,
        target: Path,
        glob_filter: Optional[str],
        case_sensitive: bool,
        context_before: int,
        context_after: int,
        output_mode: str,
        head_limit: int,
    ) -> Optional[tuple]:
        """使用 ripgrep 搜索（shell=False，list 参数，跨平台安全）。"""
        args: List[str] = [
            rg_path,
            "--no-heading",
            "--color", "never",
            "-n",  # 行号
        ]

        if not case_sensitive:
            args.append("-i")

        if context_before > 0:
            args.extend(["-B", str(context_before)])
        if context_after > 0:
            args.extend(["-A", str(context_after)])

        if output_mode == "files_with_matches":
            args.append("-l")
        elif output_mode == "count":
            args.append("-c")

        if glob_filter:
            args.extend(["-g", glob_filter])

        # 尊重 .gitignore（ripgrep 默认行为）— 不加 --no-ignore-vcs，
        # 否则 .env 等敏感文件会被搜进上下文（M1）。手动追加常见忽略目录
        # 作为无 .git 目录时的兜底。
        for d in DEFAULT_IGNORE_DIRS:
            args.extend(["-g", f"!{d}/**"])
            args.extend(["-g", f"!{d}"])

        # pattern 用 -e 显式标记为正则，避免以 "-" 开头的 pattern 被当作 flag（M2）
        args.extend(["-e", pattern])
        # 显式终止参数解析，防止后续路径被误判为 flag
        args.append("--")
        args.append(str(target))

        try:
            proc = subprocess.run(
                args,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            # 超时返回 None（而非二元组），让上层走 SEARCH_ERROR 分支（M3）
            return None
        except Exception:
            return None

        stdout = normalize_line_endings(proc.stdout or "")
        stderr = proc.stderr or ""

        # ripgrep 退出码 1 = 无匹配（正常）
        # 退出码 2 = 错误
        if proc.returncode == 2 and stderr:
            return None

        # 截取 head_limit 行
        lines = stdout.splitlines()
        total_lines = len(lines)
        if output_mode == "content" and total_lines > head_limit * 3:
            # 上下文模式下行数会膨胀，按 head_limit 个匹配截断
            lines = lines[:head_limit * 3]
            stdout = "\n".join(lines) + f"\n... (已截断，共 {total_lines} 行)"
        elif total_lines > head_limit:
            lines = lines[:head_limit]
            stdout = "\n".join(lines) + f"\n... (已截断，共 {total_lines} 行)"

        match_count = total_lines if output_mode == "content" else total_lines

        return stdout, {
            "engine": "ripgrep",
            "output_mode": output_mode,
            "match_count": match_count,
            "truncated": total_lines > head_limit,
            "search_path": str(target),
        }

    # ── Python fallback 实现 ──

    def _search_with_python(
        self,
        pattern: str,
        target: Path,
        glob_filter: Optional[str],
        case_sensitive: bool,
        context_before: int,
        context_after: int,
        output_mode: str,
        head_limit: int,
    ) -> Optional[tuple]:
        """纯 Python 搜索（re + os.walk prune，跨平台）。"""
        import fnmatch

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error:
            return None

        # 解析 .gitignore
        gitignore_path = target / ".gitignore"
        gi_patterns = compile_gitignore_patterns(parse_gitignore(gitignore_path))

        results: List[str] = []
        files_matched = 0
        total_matches = 0

        for root, dirs, files in os.walk(target, topdown=True):
            # prune 忽略目录
            dirs[:] = [
                d for d in dirs
                if d not in DEFAULT_IGNORE_DIRS
                and not should_prune_dir(d, gi_patterns)
            ]

            if total_matches >= head_limit and output_mode == "content":
                break
            if files_matched >= head_limit and output_mode != "content":
                break

            for filename in files:
                if output_mode != "content" and files_matched >= head_limit:
                    break
                if total_matches >= head_limit and output_mode == "content":
                    break

                # glob 过滤
                if glob_filter and not fnmatch.fnmatch(filename, glob_filter):
                    continue

                filepath = Path(root) / filename

                # 检查 gitignore
                try:
                    rel = filepath.relative_to(target).as_posix()
                except ValueError:
                    rel = str(filepath)
                from ._search_utils import should_ignore_path
                if should_ignore_path(rel, gi_patterns):
                    continue

                if is_binary_file(filepath):
                    continue

                try:
                    content = filepath.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except (OSError, PermissionError):
                    continue

                content = normalize_line_endings(content)
                lines = content.splitlines()
                file_matches: List[str] = []
                file_match_count = 0

                for i, line in enumerate(lines):
                    if regex.search(line):
                        file_match_count += 1
                        total_matches += 1

                        if output_mode == "files_with_matches":
                            file_matches.append(rel)
                            break  # 只需知道有匹配
                        elif output_mode == "count":
                            continue  # 计数后统一输出
                        else:  # content
                            # 上下文行
                            start = max(0, i - context_before)
                            end = min(len(lines), i + context_after + 1)
                            for j in range(start, end):
                                marker = ">" if j == i else " "
                                file_matches.append(
                                    f"{rel}:{j + 1}:{marker} {lines[j]}"
                                )
                            if context_before > 0 or context_after > 0:
                                file_matches.append("")  # 分隔空行

                            if total_matches >= head_limit:
                                break

                if file_match_count > 0:
                    files_matched += 1
                    if output_mode == "files_with_matches":
                        results.append(rel)
                    elif output_mode == "count":
                        results.append(f"{rel}:{file_match_count}")
                    else:
                        results.extend(file_matches)

        text = "\n".join(results)
        return text, {
            "engine": "python",
            "output_mode": output_mode,
            "match_count": total_matches,
            "files_matched": files_matched,
            "truncated": total_matches > head_limit,
            "search_path": str(target),
        }
