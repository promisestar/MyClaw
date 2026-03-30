"""命令执行工具 - 安全地执行 shell 命令（兼容 Windows 与 Unix）"""

import subprocess
import re
import os
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional, Set

from hello_agents.tools import Tool, ToolParameter, ToolResponse, tool_action


_IS_WINDOWS = sys.platform == "win32"

# Unix / Git Bash 等环境下常见命令
ALLOWED_COMMANDS_UNIX = [
    "ls", "cat", "echo", "pwd", "git", "npm", "pnpm", "uv", "python",
    "python3", "node", "yarn", "pip", "pip3", "mkdir", "touch", "cp",
    "mv", "grep", "find", "head", "tail", "wc", "sort", "uniq",
]

# Windows cmd 下常用等价/补充（pwd/ls 等在 cmd 中不存在）
ALLOWED_COMMANDS_WINDOWS = [
    "dir", "type", "more", "findstr", "where", "cd", "copy", "move",
    "mkdir", "md", "rmdir", "rd",
]

# 合并白名单：两端都能用各自系统命令；模型也可优先用跨平台命令（python / uv / git）
ALLOWED_COMMANDS: List[str] = sorted(
    set(ALLOWED_COMMANDS_UNIX) | set(ALLOWED_COMMANDS_WINDOWS)
)

# 危险命令模式（正则表达式）
DANGEROUS_PATTERNS = [
    r"rm\s+-rf",           # 递归强制删除
    r"rm\s+-fr",           # 递归强制删除（变体）
    r"sudo",               # 提权命令
    r"chmod\s+777",        # 危险权限设置
    r">\s*/dev/",          # 写入设备文件
    r"mkfs",               # 格式化命令
    r"dd\s+if=",           # 磁盘复制
    r">\s*/etc/",          # 写入系统配置
    r"shutdown",           # 关机命令
    r"reboot",             # 重启命令
    r"init\s+[06]",        # 切换运行级别
    r"kill\s+-9\s+1",      # 杀死 init 进程
    r":\(\)\{ :\|:& \};:",  # Fork 炸弹
    r">\s*\$HOME",         # 覆盖用户目录
    r">\s*~",              # 覆盖用户目录
]

# Windows 额外危险模式（cmd）
if _IS_WINDOWS:
    DANGEROUS_PATTERNS.extend([
        r"(?i)\bdel(?:ete)?\s+.*/[sq]",   # del /s /q 等批量删
        r"(?i)\brd\s+/s",
        r"(?i)\brmdir\s+/s",
        r"(?i)\bformat\s+",
        r"(?i)\bdiskpart\b",
    ])


def _normalize_base_command(first_token: str) -> str:
    """从命令行首段得到白名单用的命令名（Windows: 去掉路径与 .exe/.cmd）。"""
    t = first_token.strip().strip('"').strip("'")
    base = os.path.basename(t)
    lower = base.lower()
    for suf in (".exe", ".cmd", ".bat", ".com"):
        if lower.endswith(suf):
            lower = lower[: -len(suf)]
            break
    return lower


class ExecuteCommandTool(Tool):
    """命令执行工具

    提供安全的 shell 命令执行能力，包括：
    - 命令白名单机制
    - 危险命令拦截
    - 工作目录限制
    - 执行超时控制
    - Windows：识别 python.exe、支持 dir/type/cd 等 cmd 内建命令
    """

    def __init__(
        self,
        allowed_commands: List[str] = None,
        dangerous_patterns: List[str] = None,
        max_output_size: int = 10000,
        timeout: int = 30,
        allowed_directories: List[str] = None,
        default_workdir: Optional[str] = None,
    ):
        """初始化命令执行工具

        Args:
            allowed_commands: 允许的命令列表，默认使用合并后的 ALLOWED_COMMANDS
            dangerous_patterns: 危险命令模式列表，默认使用 DANGEROUS_PATTERNS
            max_output_size: 最大输出大小（字符），默认 10000
            timeout: 命令执行超时时间（秒），默认 30
            allowed_directories: 允许的工作目录列表，None 表示不限制
            default_workdir: 未传 workdir 时使用的默认 cwd（例如 Agent 工作空间根，与 Read/Write 一致）
        """
        super().__init__(
            name="execute_command",
            description="安全地执行 shell 命令，支持命令白名单和危险命令拦截（兼容 Windows）",
            expandable=True
        )

        raw = allowed_commands or ALLOWED_COMMANDS
        self.allowed_commands: Set[str] = {c.lower() for c in raw}
        self.dangerous_patterns = dangerous_patterns or DANGEROUS_PATTERNS
        self.max_output_size = max_output_size
        self.timeout = timeout
        self.allowed_directories = allowed_directories
        self.default_workdir = (
            os.path.abspath(os.path.expanduser(default_workdir))
            if default_workdir
            else None
        )

        self._dangerous_regex = [
            re.compile(pattern, re.IGNORECASE) for pattern in self.dangerous_patterns
        ]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        """执行命令（默认行为）"""
        command = parameters.get("command", "")
        workdir = parameters.get("workdir")
        timeout = parameters.get("timeout")
        if timeout is not None:
            try:
                timeout = int(timeout)
            except (TypeError, ValueError):
                timeout = None
        return self._execute_command(command, workdir, timeout=timeout)

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="command",
                type="string",
                description="要执行的 shell 命令（Windows 下为 cmd / PowerShell 可用语法；列表见 exec_allowed_commands）",
                required=True
            ),
            ToolParameter(
                name="workdir",
                type="string",
                description="工作目录（可选；不传则使用工具配置的默认目录，与 Read/Write 根目录一致）",
                required=False
            ),
            ToolParameter(
                name="timeout",
                type="integer",
                description="超时时间（秒，可选；覆盖默认工具超时）",
                required=False
            ),
        ]

    def _validate_command(self, command: str) -> tuple[bool, str]:
        """验证命令是否安全"""
        for pattern in self._dangerous_regex:
            if pattern.search(command):
                return False, f"命令包含危险模式: {pattern.pattern}"

        command_parts = command.strip().split()
        if not command_parts:
            return False, "命令为空"

        base_cmd = _normalize_base_command(command_parts[0])

        if base_cmd not in self.allowed_commands:
            hint = "dir/type/cd/where（Windows）或 ls/cat/pwd（Unix）等"
            return False, (
                f"命令 '{base_cmd}' 不在白名单中。"
                f"允许的命令示例: {', '.join(sorted(self.allowed_commands)[:15])}...；"
                f"Windows 可使用 {hint}。"
            )

        return True, ""

    def _validate_workdir(self, workdir: str) -> tuple[bool, str]:
        """验证工作目录"""
        if not self.allowed_directories:
            return True, ""

        abs_workdir = os.path.abspath(workdir)
        for allowed_dir in self.allowed_directories:
            abs_allowed = os.path.abspath(allowed_dir)
            if abs_workdir.startswith(abs_allowed):
                return True, ""

        return False, f"工作目录 '{workdir}' 不在允许的目录列表中"

    def _execute_command(
        self,
        command: str,
        workdir: str = None,
        timeout: int = None,
    ) -> ToolResponse:
        """执行命令的核心实现"""
        if not command:
            return ToolResponse.error(
                code="INVALID_INPUT",
                message="命令不能为空"
            )

        is_safe, reason = self._validate_command(command)
        if not is_safe:
            return ToolResponse.error(
                code="COMMAND_BLOCKED",
                message=f"命令被拦截: {reason}"
            )

        effective_workdir = workdir
        if effective_workdir is None and self.default_workdir:
            effective_workdir = self.default_workdir

        if effective_workdir:
            is_valid, wd_reason = self._validate_workdir(effective_workdir)
            if not is_valid:
                return ToolResponse.error(
                    code="DIRECTORY_NOT_ALLOWED",
                    message=f"工作目录无效: {wd_reason}"
                )

        env = {**os.environ, "PYTHONUNBUFFERED": "1"}

        # Windows 控制台常为系统编码；用 errors=replace 避免解码失败导致工具崩溃
        sub_kw: Dict[str, Any] = dict(
            shell=True,
            capture_output=True,
            cwd=effective_workdir,
            timeout=timeout or self.timeout,
            env=env,
        )
        if _IS_WINDOWS:
            sub_kw["encoding"] = os.device_encoding(2) or "utf-8"
            sub_kw["errors"] = "replace"
        else:
            sub_kw["text"] = True

        try:
            result = subprocess.run(command, **sub_kw)

            if _IS_WINDOWS:
                stdout = result.stdout or ""
                stderr = result.stderr or ""
            else:
                stdout = result.stdout
                stderr = result.stderr

            if len(stdout) > self.max_output_size:
                stdout = stdout[:self.max_output_size] + f"\n... (输出已截断，共 {len(result.stdout or '')} 字符)"
            if len(stderr) > self.max_output_size:
                stderr = stderr[:self.max_output_size] + f"\n... (错误输出已截断，共 {len(result.stderr or '')} 字符)"

            output_parts = []
            if stdout:
                output_parts.append(f"输出:\n{stdout}")
            if stderr:
                output_parts.append(f"错误:\n{stderr}")

            if not output_parts and result.returncode != 0:
                output_text = f"命令结束，返回码: {result.returncode}（无标准输出/错误流）"
            else:
                output_text = "\n\n".join(output_parts) if output_parts else "命令执行完成（无输出）"

            return ToolResponse.success(
                text=output_text,
                data={
                    "return_code": result.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                    "command": command,
                    "workdir": effective_workdir,
                    "platform": "windows" if _IS_WINDOWS else "posix",
                }
            )

        except subprocess.TimeoutExpired:
            return ToolResponse.error(
                code="TIMEOUT",
                message=f"命令执行超时（{timeout or self.timeout}秒）"
            )
        except Exception as e:
            return ToolResponse.error(
                code="EXECUTION_ERROR",
                message=f"命令执行失败: {str(e)}"
            )

    def _resolve_path_in_workspace(self, raw_path: str) -> Optional[Path]:
        """将路径解析到默认工作目录（或传入路径）下，防止越界删除。"""
        if not raw_path:
            return None

        p = Path(raw_path)
        if p.is_absolute():
            resolved = p.resolve()
        else:
            base = Path(self.default_workdir or os.getcwd()).resolve()
            resolved = (base / p).resolve()

        # 如果配置了 allowed_directories，必须落在其中之一
        if self.allowed_directories:
            for allowed in self.allowed_directories:
                allowed_path = Path(allowed).resolve()
                try:
                    resolved.relative_to(allowed_path)
                    return resolved
                except ValueError:
                    continue
            return None

        return resolved

    def _is_allowed_temp_cleanup_target(self, path_obj: Path) -> bool:
        """只允许清理临时产物，避免误删正式文件。"""
        lower_name = path_obj.name.lower()
        if lower_name.startswith(("tmp_", "temp_", "extract_")):
            return True
        if lower_name.endswith((".tmp", ".temp", ".log")):
            return True
        return any(part.lower() in ("tmp", "temp", ".tmp") for part in path_obj.parts)

    @tool_action("exec_run", "执行 shell 命令")
    def _run_command(
        self,
        command: str,
        workdir: str = None,
        timeout: int = None,
    ) -> str:
        """执行 shell 命令"""
        response = self._execute_command(command, workdir, timeout)
        return response.text

    @tool_action("exec_allowed_commands", "列出允许的命令")
    def _list_allowed_commands(self) -> str:
        """列出所有允许执行的命令"""
        lines = ["允许的命令（小写比较，Windows 下 python.exe 视为 python）:", ""]
        for cmd in sorted(self.allowed_commands):
            lines.append(f"- {cmd}")
        if _IS_WINDOWS:
            lines.append("")
            lines.append("Windows 提示: 查看当前目录可用 `cd`，列目录用 `dir`，读文件用 `type`，查找命令路径用 `where`。")
        lines.append("")
        lines.append("删除策略: shell 删除命令（rm/del/rd 等）会被拦截；如需清理临时文件，请用 `exec_cleanup_temp_files`。")
        return "\n".join(lines)

    @tool_action("exec_dangerous_patterns", "列出危险命令模式")
    def _list_dangerous_patterns(self) -> str:
        """列出所有会被拦截的危险命令模式"""
        return "危险命令模式:\n" + "\n".join(f"- {pattern}" for pattern in self.dangerous_patterns)

    @tool_action("exec_cleanup_temp_files", "安全清理临时文件")
    def _cleanup_temp_files(self, paths: str = "") -> str:
        """清理临时文件（受限删除）

        Args:
            paths: 逗号或换行分隔的相对路径列表；为空时默认尝试清理常见临时文件名
        """
        candidates: List[str]
        if paths and paths.strip():
            normalized = paths.replace("\n", ",")
            candidates = [p.strip() for p in normalized.split(",") if p.strip()]
        else:
            candidates = ["extract_pdf.py", "pdf_content.txt", "tmp_output.txt"]

        deleted: List[str] = []
        skipped: List[str] = []
        failed: List[str] = []

        for raw in candidates:
            target = self._resolve_path_in_workspace(raw)
            if not target:
                skipped.append(f"{raw}（不在允许目录内）")
                continue
            if not self._is_allowed_temp_cleanup_target(target):
                skipped.append(f"{raw}（不符合临时文件规则）")
                continue
            if not target.exists():
                skipped.append(f"{raw}（文件不存在）")
                continue
            if not target.is_file():
                skipped.append(f"{raw}（仅支持文件，不支持目录）")
                continue

            try:
                target.unlink()
                deleted.append(raw)
            except Exception as e:
                failed.append(f"{raw}（{e}）")

        lines = ["临时文件清理结果:"]
        lines.append(f"- 已删除: {len(deleted)}")
        if deleted:
            lines.extend([f"  - {p}" for p in deleted])
        lines.append(f"- 已跳过: {len(skipped)}")
        if skipped:
            lines.extend([f"  - {p}" for p in skipped[:20]])
        lines.append(f"- 失败: {len(failed)}")
        if failed:
            lines.extend([f"  - {p}" for p in failed[:20]])
        return "\n".join(lines)
