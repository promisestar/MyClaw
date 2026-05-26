"""在工作空间内安全执行 shell 命令（参考 CoreCoder bash 工具）。"""

import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

from hello_agents.tools import Tool, ToolParameter, ToolResponse

_IS_WINDOWS = sys.platform == "win32"
_DEFAULT_MAX_OUTPUT = 15_000
_TRUNCATE_HEAD = 6000
_TRUNCATE_TAIL = 3000

# (pattern, reason) — 命中即拦截
_DANGEROUS_PATTERNS: List[tuple[str, str]] = [
    (r"\brm\s+(-\w*)?-r\w*\s+(/|~|\$HOME)", "递归删除 home/root"),
    (r"\brm\s+(-\w*)?-rf\s", "强制递归删除"),
    (r"\bsudo\b", "提权命令"),
    (r"\bmkfs\b", "格式化文件系统"),
    (r"\bdd\s+.*of=/dev/", "裸盘写入"),
    (r"\bchmod\s+(-R\s+)?777\s+/", "根目录 chmod 777"),
    (r":\(\)\s*\{.*:\|:.*\}", "fork bomb"),
    (r"\bcurl\b.*\|\s*(sudo\s+)?bash", "curl 管道到 bash"),
    (r"\bwget\b.*\|\s*(sudo\s+)?bash", "wget 管道到 bash"),
    (r">\s*/etc/", "写入系统配置"),
    (r"\bshutdown\b|\breboot\b", "关机/重启"),
]
if _IS_WINDOWS:
    _DANGEROUS_PATTERNS.extend([
        (r"(?i)\bdel(?:ete)?\s+.*/[sq]", "批量删除"),
        (r"(?i)\brd\s+/s|\brmdir\s+/s", "递归删除目录"),
        (r"(?i)\bformat\s+|\bdiskpart\b", "格式化磁盘"),
    ])


def _check_dangerous(command: str) -> Optional[str]:
    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return reason
    return None


def _truncate_output(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return (
        text[:_TRUNCATE_HEAD]
        + f"\n\n... 已截断（共 {len(text)} 字符）...\n\n"
        + text[-_TRUNCATE_TAIL:]
    )


def _track_cd(command: str, cwd: str) -> Optional[str]:
    """从命令链中解析 cd，成功则返回新工作目录。"""
    for part in command.split("&&"):
        part = part.strip()
        if part.startswith("cd "):
            target = part[3:].strip().strip("'\"")
            if target:
                new_dir = os.path.normpath(os.path.join(cwd, os.path.expanduser(target)))
                if os.path.isdir(new_dir):
                    return new_dir
    return None


class BashTool(Tool):
    """在工作空间内执行 shell 命令，拦截破坏性操作。"""

    def __init__(
        self,
        max_output_size: int = _DEFAULT_MAX_OUTPUT,
        timeout: int = 120,
        allowed_directories: Optional[List[str]] = None,
        default_workdir: Optional[str] = None,
    ):
        super().__init__(
            name="execute_command",
            description=(
                "在工作空间内执行 shell 命令，返回 stdout、stderr 与退出码。"
                "用于运行测试、安装依赖、git、构建脚本等。"
                "破坏性命令会被拦截；可用 cd 切换目录（在同一会话内保持）。"
            ),
            expandable=False,
        )
        self.max_output_size = max_output_size
        self.timeout = timeout
        self.allowed_directories = (
            [os.path.abspath(os.path.expanduser(d)) for d in allowed_directories]
            if allowed_directories
            else None
        )
        self.default_workdir = (
            os.path.abspath(os.path.expanduser(default_workdir))
            if default_workdir
            else None
        )
        self._cwd: Optional[str] = None

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        command = (parameters.get("command") or "").strip()
        workdir = parameters.get("workdir")
        timeout = parameters.get("timeout")
        if timeout is not None:
            try:
                timeout = int(timeout)
            except (TypeError, ValueError):
                timeout = None
        return self._execute(command, workdir, timeout)

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="command",
                type="string",
                description="要执行的 shell 命令",
                required=True,
            ),
            ToolParameter(
                name="workdir",
                type="string",
                description="工作目录（可选；默认工作空间根，或与上一条 cd 一致）",
                required=False,
            ),
            ToolParameter(
                name="timeout",
                type="integer",
                description=f"超时秒数（默认 {self.timeout}）",
                required=False,
            ),
        ]

    def _is_allowed_path(self, path: str) -> bool:
        if not self.allowed_directories:
            return True
        abs_path = os.path.abspath(path)
        return any(
            abs_path == allowed or abs_path.startswith(allowed + os.sep)
            for allowed in self.allowed_directories
        )

    def _resolve_cwd(self, workdir: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        cwd = workdir or self._cwd or self.default_workdir
        if not cwd:
            return os.getcwd(), None
        cwd = os.path.abspath(os.path.expanduser(cwd))
        if not os.path.isdir(cwd):
            return None, f"工作目录不存在: {cwd}"
        if not self._is_allowed_path(cwd):
            return None, f"工作目录不在允许范围内: {cwd}"
        return cwd, None

    def _execute(
        self,
        command: str,
        workdir: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> ToolResponse:
        if not command:
            return ToolResponse.error(code="INVALID_INPUT", message="命令不能为空")

        reason = _check_dangerous(command)
        if reason:
            return ToolResponse.error(
                code="COMMAND_BLOCKED",
                message=f"命令被拦截（{reason}）。请改用更具体、安全的写法。",
            )

        cwd, err = self._resolve_cwd(workdir)
        if err:
            return ToolResponse.error(code="DIRECTORY_NOT_ALLOWED", message=err)

        run_timeout = timeout or self.timeout
        sub_kw: Dict[str, Any] = dict(
            shell=True,
            capture_output=True,
            cwd=cwd,
            timeout=run_timeout,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        if _IS_WINDOWS:
            sub_kw["encoding"] = os.device_encoding(2) or "utf-8"
            sub_kw["errors"] = "replace"
        else:
            sub_kw["text"] = True

        try:
            proc = subprocess.run(command, **sub_kw)
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""

            if proc.returncode == 0:
                new_cwd = _track_cd(command, cwd)
                if new_cwd and self._is_allowed_path(new_cwd):
                    self._cwd = new_cwd

            parts: List[str] = []
            if stdout:
                parts.append(stdout.rstrip())
            if stderr:
                parts.append(f"[stderr]\n{stderr.rstrip()}")
            if proc.returncode != 0:
                parts.append(f"[exit code: {proc.returncode}]")

            output = "\n".join(parts).strip() or "(无输出)"
            output = _truncate_output(output, self.max_output_size)

            return ToolResponse.success(
                text=output,
                data={
                    "return_code": proc.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                    "command": command,
                    "workdir": cwd,
                },
            )
        except subprocess.TimeoutExpired:
            return ToolResponse.error(
                code="TIMEOUT",
                message=f"命令执行超时（{run_timeout} 秒）",
            )
        except Exception as e:
            return ToolResponse.error(
                code="EXECUTION_ERROR",
                message=f"命令执行失败: {e}",
            )
