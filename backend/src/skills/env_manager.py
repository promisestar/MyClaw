"""技能环境管理器

为每个 Skill 创建独立的虚拟环境，自动安装依赖。

特性：
- 每个 Skill 一个独立 venv（位于 skills/<name>/.venv）
- 优先使用 uv（极快），降级到 python -m venv + pip
- 支持多种依赖声明来源：requirements.txt / pyproject.toml / SKILL.md frontmatter
- 安装失败不影响 Skill 加载（仅记录错误，回退到主 python）
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import yaml


# 虚拟环境目录名（位于每个 Skill 目录下）
VENV_DIR_NAME = ".venv"

# 依赖文件候选名（按优先级）
DEPS_FILE_CANDIDATES = ["requirements.txt", "requirements-skill.txt"]


def get_venv_python(skill_dir: Path) -> Optional[Path]:
    """获取 Skill 专属 venv 的 Python 解释器路径

    Args:
        skill_dir: 技能目录

    Returns:
        Python 解释器路径（存在则返回），不存在返回 None
    """
    venv_dir = skill_dir / VENV_DIR_NAME
    if not venv_dir.exists():
        return None

    if os.name == "nt":
        python_exe = venv_dir / "Scripts" / "python.exe"
    else:
        python_exe = venv_dir / "bin" / "python"

    return python_exe if python_exe.exists() else None


def has_dependencies(skill_dir: Path) -> bool:
    """检查 Skill 是否声明了依赖

    检查顺序：
    1. requirements.txt / requirements-skill.txt
    2. pyproject.toml（含 [project.dependencies]）
    3. SKILL.md frontmatter 中的 dependencies 字段

    Args:
        skill_dir: 技能目录

    Returns:
        是否存在依赖声明
    """
    for fname in DEPS_FILE_CANDIDATES:
        if (skill_dir / fname).exists():
            return True

    pyproject = skill_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text(encoding="utf-8")
            if "dependencies" in content:
                return True
        except Exception:
            pass

    deps = _parse_skill_md_dependencies(skill_dir / "SKILL.md")
    return bool(deps)


def _parse_skill_md_dependencies(skill_md: Path) -> List[str]:
    """从 SKILL.md frontmatter 中提取依赖列表

    支持格式：
    ```yaml
    ---
    name: ...
    dependencies:
      - akshare
      - pandas>=2.0
    ---
    ```

    Args:
        skill_md: SKILL.md 路径

    Returns:
        依赖列表（无则返回空列表）
    """
    if not skill_md.exists():
        return []

    try:
        content = skill_md.read_text(encoding="utf-8")
    except Exception:
        return []

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return []

    try:
        metadata = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return []

    deps = metadata.get("dependencies", [])
    if isinstance(deps, list):
        return [str(d).strip() for d in deps if str(d).strip()]
    return []


def _collect_dependencies(skill_dir: Path) -> Tuple[List[str], Optional[Path]]:
    """收集 Skill 的所有依赖

    Returns:
        (依赖列表, 主依赖文件路径或 None)
        如果主要走 requirements.txt，则返回该文件路径；否则返回 None。
    """
    # 优先使用 requirements.txt
    for fname in DEPS_FILE_CANDIDATES:
        req_file = skill_dir / fname
        if req_file.exists():
            try:
                lines = req_file.read_text(encoding="utf-8").splitlines()
                deps = [
                    line.strip()
                    for line in lines
                    if line.strip() and not line.strip().startswith("#")
                ]
                return deps, req_file
            except Exception:
                pass

    # 回退到 SKILL.md frontmatter
    deps = _parse_skill_md_dependencies(skill_dir / "SKILL.md")
    return deps, None


def _detect_uv() -> Optional[str]:
    """检测系统是否安装了 uv（推荐的快速包管理器）

    Returns:
        uv 可执行文件路径，未安装返回 None
    """
    uv_path = shutil.which("uv")
    return uv_path


def _run_command(
    cmd: List[str],
    cwd: Optional[Path] = None,
    timeout: int = 300,
) -> Tuple[bool, str]:
    """执行命令并返回结果

    Args:
        cmd: 命令列表
        cwd: 工作目录
        timeout: 超时时间（秒），默认 5 分钟

    Returns:
        (是否成功, 输出信息)
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output[-4000:]  # 限制输出长度
    except subprocess.TimeoutExpired:
        return False, f"命令执行超时（{timeout}秒）"
    except FileNotFoundError as e:
        return False, f"命令未找到: {e}"
    except Exception as e:
        return False, f"执行失败: {e}"


def create_venv(skill_dir: Path) -> Tuple[bool, str]:
    """为 Skill 创建虚拟环境

    优先使用 uv（如可用），降级到 python -m venv。

    Args:
        skill_dir: 技能目录

    Returns:
        (是否成功, 输出信息)
    """
    venv_dir = skill_dir / VENV_DIR_NAME

    # 如果已存在，先删除（确保干净）
    if venv_dir.exists():
        try:
            shutil.rmtree(venv_dir)
        except Exception as e:
            return False, f"清理旧 venv 失败: {e}"

    # 优先 uv（更快）
    uv = _detect_uv()
    if uv:
        ok, output = _run_command(
            [uv, "venv", str(venv_dir), "--python", sys.executable],
            timeout=120,
        )
        if ok:
            return True, f"✅ 使用 uv 创建 venv 成功\n{output}"
        # uv 失败则降级

    # 降级到标准 venv
    ok, output = _run_command(
        [sys.executable, "-m", "venv", str(venv_dir)],
        timeout=180,
    )
    if ok:
        return True, f"✅ 使用 python -m venv 创建成功\n{output}"
    return False, f"❌ 创建 venv 失败\n{output}"


def install_dependencies(skill_dir: Path) -> Tuple[bool, str]:
    """安装 Skill 的依赖到其专属 venv

    依赖来源优先级：requirements.txt > SKILL.md frontmatter

    Args:
        skill_dir: 技能目录

    Returns:
        (是否成功, 输出信息)
    """
    venv_python = get_venv_python(skill_dir)
    if not venv_python:
        return False, "venv 不存在，请先调用 create_venv"

    deps, req_file = _collect_dependencies(skill_dir)
    if not deps:
        return True, "（无依赖声明，跳过安装）"

    uv = _detect_uv()

    # 升级 pip（仅在使用 pip 时；uv 自带）
    if not uv:
        _run_command(
            [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"],
            timeout=120,
        )

    # 安装依赖
    if req_file is not None:
        # 使用依赖文件
        if uv:
            cmd = [uv, "pip", "install", "--python", str(venv_python), "-r", str(req_file)]
        else:
            cmd = [str(venv_python), "-m", "pip", "install", "-r", str(req_file)]
    else:
        # 使用依赖列表
        if uv:
            cmd = [uv, "pip", "install", "--python", str(venv_python)] + deps
        else:
            cmd = [str(venv_python), "-m", "pip", "install"] + deps

    ok, output = _run_command(cmd, timeout=600)  # 安装超时 10 分钟
    if ok:
        return True, f"✅ 依赖安装成功（{len(deps)} 个包）\n{output}"
    return False, f"❌ 依赖安装失败\n{output}"


def setup_skill_env(skill_dir: Path) -> Tuple[bool, str, Optional[Path]]:
    """一站式：为 Skill 创建 venv 并安装依赖

    Args:
        skill_dir: 技能目录

    Returns:
        (是否成功, 输出信息, Python 解释器路径或 None)
    """
    # 无依赖时跳过 venv 创建
    if not has_dependencies(skill_dir):
        return True, "（无依赖声明，跳过 venv 创建）", None

    # 创建 venv
    ok, output = create_venv(skill_dir)
    if not ok:
        return False, output, None

    # 安装依赖
    ok, install_output = install_dependencies(skill_dir)
    output += "\n" + install_output
    if not ok:
        return False, output, None

    return True, output, get_venv_python(skill_dir)


def remove_venv(skill_dir: Path) -> bool:
    """删除 Skill 的 venv

    Args:
        skill_dir: 技能目录

    Returns:
        是否成功
    """
    venv_dir = skill_dir / VENV_DIR_NAME
    if not venv_dir.exists():
        return True
    try:
        shutil.rmtree(venv_dir)
        return True
    except Exception:
        return False
