"""代码检索工具共享模块 — .gitignore 解析、忽略目录过滤、二进制检测、输出截断。

被 search_content / search_file / list_dir 三者复用，避免重复实现。
全部使用 pathlib + 标准库，三端（Win/Linux/macOS）通用。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional, Set, Tuple

# ── 跨平台统一的忽略目录列表 ──
DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset({
    # VCS
    ".git", ".hg", ".svn",
    # Python
    ".venv", "venv", "env", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", "site-packages",
    # Node
    "node_modules", "bower_components", ".pnpm-store",
    # MyClaw
    ".myclaw",
    # Build artifacts
    "dist", "build", "target", "out", "bin", "obj",
    # IDE
    ".idea", ".vscode", ".vs",
    # Framework caches
    ".next", ".nuxt", ".gradle", ".turbo",
})

# 二进制文件扩展名（搜索时跳过）
_BINARY_EXTENSIONS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff",
    ".pdf", ".zip", ".gz", ".tar", ".tgz", ".bz2", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flv",
    ".pyc", ".pyo", ".class", ".jar", ".war",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".sqlite", ".db", ".mdb",
})


def parse_gitignore(gitignore_path: Path) -> List[str]:
    """解析 .gitignore 文件，返回 pattern 列表。

    处理常见语法：
    - 注释行（#）和空行 → 跳过
    - 取反（!）→ 跳过（简化实现）
    - 目录模式（结尾 /）
    - 通配符（*、**、?）

    Args:
        gitignore_path: .gitignore 文件路径

    Returns:
        pattern 字符串列表
    """
    if not gitignore_path.is_file():
        return []

    patterns: List[str] = []
    try:
        text = gitignore_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return patterns

    for raw_line in text.splitlines():
        line = raw_line.strip()
        # 跳过注释和空行
        if not line or line.startswith("#"):
            continue
        # 跳过取反（简化实现）
        if line.startswith("!"):
            continue
        patterns.append(line)

    return patterns


def _gitignore_pattern_to_regex(pattern: str) -> re.Pattern:
    """将单个 .gitignore pattern 转为正则。

    Args:
        pattern: gitignore pattern（如 "*.pyc", "node_modules/", "/build"）

    Returns:
        编译后的正则 Pattern
    """
    rooted = pattern.startswith("/")
    if rooted:
        pattern = pattern[1:]

    dir_only = pattern.endswith("/")
    if dir_only:
        pattern = pattern[:-1]

    parts: List[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                # ** 匹配任意路径段
                parts.append(".*")
                i += 2
                # 跳过后续的 /
                if i < len(pattern) and pattern[i] == "/":
                    i += 1
            else:
                # * 匹配除 / 外的任意字符
                parts.append("[^/]*")
                i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(c))
            i += 1

    regex_body = "".join(parts)

    if rooted:
        # 仅从根路径匹配
        full = f"^{regex_body}"
    else:
        # 任意层级匹配
        full = f"(^|/){regex_body}"

    if dir_only:
        full = f"{full}(/|$)"
    else:
        full = f"{full}$"

    return re.compile(full)


def compile_gitignore_patterns(patterns: List[str]) -> List[re.Pattern]:
    """批量编译 gitignore patterns。"""
    return [_gitignore_pattern_to_regex(p) for p in patterns]


def should_ignore_path(
    rel_path: str,
    compiled_patterns: List[re.Pattern],
) -> bool:
    """检查相对路径是否匹配 gitignore patterns。

    Args:
        rel_path: 相对于搜索根的 POSIX 风格路径（如 "src/main.py"）
        compiled_patterns: 编译后的正则列表

    Returns:
        True 表示应忽略
    """
    for pat in compiled_patterns:
        if pat.search(rel_path):
            return True
    return False


def should_prune_dir(
    dirname: str,
    ignore_patterns: List[re.Pattern],
) -> bool:
    """检查目录是否应被 prune（在 os.walk 中跳过）。

    同时检查 DEFAULT_IGNORE_DIRS 和 gitignore patterns。

    Args:
        dirname: 目录名（不含路径）
        ignore_patterns: 编译后的 gitignore patterns

    Returns:
        True 表示应跳过该目录
    """
    if dirname in DEFAULT_IGNORE_DIRS:
        return True
    # gitignore 目录模式匹配
    for pat in ignore_patterns:
        if pat.search(dirname) or pat.search(f"{dirname}/"):
            return True
    return False


def is_binary_file(filepath: Path, sample_size: int = 8192) -> bool:
    """快速检测二进制文件。

    通过扩展名和内容检测（NUL 字节）判断。

    Args:
        filepath: 文件路径
        sample_size: 采样字节数

    Returns:
        True 表示是二进制文件
    """
    # 扩展名快速判断
    if filepath.suffix.lower() in _BINARY_EXTENSIONS:
        return True

    try:
        with open(filepath, "rb") as f:
            chunk = f.read(sample_size)
        # NUL 字节是二进制文件的强信号
        return b"\x00" in chunk
    except (OSError, PermissionError):
        return True  # 无法读取，视为二进制跳过


def resolve_search_path(
    raw_path: Optional[str],
    project_root: str,
    allowed_directories: Optional[List[str]] = None,
) -> Tuple[Optional[Path], Optional[str]]:
    """解析搜索路径参数，返回绝对路径和错误信息。

    Args:
        raw_path: 用户传入的路径（可能为相对/绝对）
        project_root: 工作区根目录
        allowed_directories: 允许的目录白名单（None 表示不限制）

    Returns:
        (绝对 Path, 错误信息)；成功时错误为 None
    """
    if not raw_path:
        target = Path(project_root).resolve()
    else:
        p = Path(raw_path)
        if p.is_absolute():
            target = p.resolve()
        else:
            target = (Path(project_root) / p).resolve()

    if not target.exists():
        return None, f"路径不存在: {target}"

    # 白名单检查
    if allowed_directories:
        target_str = str(target)
        allowed = False
        for ad in allowed_directories:
            ad_abs = os.path.abspath(os.path.expanduser(ad))
            if target_str == ad_abs or target_str.startswith(ad_abs + os.sep):
                allowed = True
                break
        if not allowed:
            return None, f"路径不在允许范围内: {target}"

    return target, None


def truncate_output(text: str, limit: int, head: int = 6000, tail: int = 3000) -> str:
    """截断过长输出（head + tail 模式）。

    与 bash.py 的 _truncate_output 一致。

    Args:
        text: 原始文本
        limit: 截断阈值（字符数）
        head: 保留头部字符数
        tail: 保留尾部字符数

    Returns:
        截断后的文本
    """
    if len(text) <= limit:
        return text
    return (
        text[:head]
        + f"\n\n... 已截断（共 {len(text)} 字符）...\n\n"
        + text[-tail:]
    )


def normalize_line_endings(text: str) -> str:
    """归一化换行符（CRLF/CR → LF），跨平台一致性。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")
