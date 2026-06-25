"""技能加载器

实现渐进式披露机制：
- Layer 1: Metadata（启动时加载，~100 tokens/skill）
- Layer 2: SKILL.md body（按需加载，~2000+ tokens）
- Layer 3: Resources（可选，按需）

支持本地目录复制和 Git 仓库克隆导入。
"""

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from dataclasses import dataclass, field

from .state_manager import SkillStateManager
from . import env_manager
from .exceptions import (
    SkillImportError,
    SkillLoadError,
    SkillConflictError,
    SkillNotFoundError,
)
from .validators import ensure_valid_skill_name, validate_skill_name

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    """技能数据类"""
    name: str
    description: str
    body: str
    path: Path
    dir: Path
    enabled: bool = True
    # 专属 venv 的 Python 解释器路径（无则为 None，使用主 python）
    python_path: Optional[Path] = field(default=None)
    # 是否声明了依赖（用于前端展示状态）
    has_dependencies: bool = False

    @property
    def scripts(self) -> List[Path]:
        """获取 scripts/ 目录下的所有文件"""
        scripts_dir = self.dir / "scripts"
        if not scripts_dir.exists():
            return []
        return [f for f in scripts_dir.rglob("*") if f.is_file()]

    @property
    def examples(self) -> List[Path]:
        """获取 examples/ 目录下的所有文件"""
        examples_dir = self.dir / "examples"
        if not examples_dir.exists():
            return []
        return [f for f in examples_dir.rglob("*") if f.is_file()]

    @property
    def references(self) -> List[Path]:
        """获取 references/ 目录下的所有文件"""
        references_dir = self.dir / "references"
        if not references_dir.exists():
            return []
        return [f for f in references_dir.rglob("*") if f.is_file()]


class SkillLoader:
    """技能加载器

    特性：
    - 启动时仅加载元数据
    - 按需加载完整技能
    - 扫描 skills/ 目录下的子目录
    - 支持热重载
    - 支持本地路径和 Git 仓库导入
    - 支持启用/禁用管理
    """

    def __init__(self, skills_dir: Path):
        """初始化技能加载器

        Args:
            skills_dir: 技能目录路径
        """
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)

        # 完整技能缓存
        self.skills_cache: Dict[str, Skill] = {}

        # 仅元数据缓存（启动时加载）
        self.metadata_cache: Dict[str, Dict] = {}

        # 状态管理器
        self._state_manager = SkillStateManager(
            self.skills_dir / "skill_states.json"
        )

        # 启动时扫描并加载元数据
        self._scan_skills()

    @property
    def enabled_count(self) -> int:
        """已启用的技能数量"""
        return sum(
            1 for name in self.metadata_cache
            if self._state_manager.is_enabled(name)
        )

    @property
    def total_count(self) -> int:
        """总技能数量"""
        return len(self.metadata_cache)

    def _scan_skills(self):
        """扫描 skills/ 目录，加载元数据

        对 frontmatter.name 与目录名不一致的旧数据，会记录警告但仍保留加载，
        以 frontmatter.name 为准作为 cache key。新导入的技能则强制目录名 = name。
        """
        # 排除非技能目录（如 venv、缓存等）
        SKIP_NAMES = {"__pycache__", env_manager.VENV_DIR_NAME, "node_modules"}

        for skill_dir in self.skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            if skill_dir.name.startswith("."):
                continue
            if skill_dir.name in SKIP_NAMES:
                continue

            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue

            try:
                metadata = self._parse_frontmatter_only(skill_md)
            except SkillLoadError as e:
                logger.warning(
                    "扫描技能 %s 失败：%s（detail=%s）",
                    skill_dir.name, e.message, e.detail,
                )
                continue
            except Exception:
                logger.exception("扫描技能 %s 时出现未预期错误", skill_dir.name)
                continue

            if not metadata:
                logger.debug("跳过技能目录 %s：未解析到合法 frontmatter", skill_dir.name)
                continue

            name = metadata.get("name", skill_dir.name)

            # 校验 name 合法性
            name_err = validate_skill_name(name)
            if name_err is not None:
                logger.warning(
                    "技能 %s 的 name='%s' 不合法（%s），跳过加载",
                    skill_dir.name, name, name_err,
                )
                continue

            # 检测目录名 != name 的旧数据
            if skill_dir.name != name:
                logger.warning(
                    "技能目录名 '%s' 与 frontmatter.name '%s' 不一致。"
                    "新版本要求二者一致；当前以 name 为准加载，建议手动修复或重新导入。",
                    skill_dir.name, name,
                )

            # 处理重名（不同目录的 frontmatter 写了相同 name）
            if name in self.metadata_cache:
                existing_dir = self.metadata_cache[name]["dir"]
                logger.warning(
                    "技能名 '%s' 重复：目录 '%s' 已加载，跳过 '%s'",
                    name, existing_dir, skill_dir,
                )
                continue

            self.metadata_cache[name] = {
                "name": name,
                "description": metadata.get("description", ""),
                "path": str(skill_md),
                "dir": str(skill_dir),
            }

    def _parse_frontmatter_only(self, path: Path) -> Optional[Dict]:
        """仅解析 YAML frontmatter

        Args:
            path: SKILL.md 文件路径

        Returns:
            解析后的元数据字典；frontmatter 缺失或缺少必需字段时返回 None

        Raises:
            SkillLoadError: 文件读取失败或 YAML 语法错误
        """
        try:
            content = path.read_text(encoding='utf-8')
        except OSError as e:
            logger.warning("读取 %s 失败：%s", path, e)
            raise SkillLoadError(
                f"无法读取 SKILL.md 文件：{e}",
                code="READ_FAILED",
                detail=str(path),
            )

        match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
        if not match:
            # frontmatter 缺失不是错误，只是不是合法的技能目录
            return None

        try:
            metadata = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as e:
            logger.warning("解析 %s 的 YAML frontmatter 失败：%s", path, e)
            raise SkillLoadError(
                f"SKILL.md frontmatter YAML 语法错误：{e}",
                code="YAML_ERROR",
                detail=str(path),
            )

        if not isinstance(metadata, dict):
            logger.warning("%s 的 frontmatter 不是 mapping 类型", path)
            return None

        if "name" not in metadata or "description" not in metadata:
            return None

        return metadata

    def get_descriptions(self, only_enabled: bool = True) -> str:
        """获取技能的元数据描述（用于系统提示词）

        Args:
            only_enabled: 是否仅返回启用的技能

        Returns:
            格式化的技能描述列表
        """
        if not self.metadata_cache:
            return "（暂无可用技能）"

        lines = []
        for name, meta in self.metadata_cache.items():
            if only_enabled and not self._state_manager.is_enabled(name):
                continue
            lines.append(f"- {name}: {meta['description']}")

        if not lines:
            return "（暂无可用技能）"

        return "\n".join(lines)

    def get_skill(self, name: str) -> Optional[Skill]:
        """按需加载完整技能

        Args:
            name: 技能名称

        Returns:
            Skill 对象，不存在或已禁用则返回 None
        """
        # 检查是否禁用
        if not self._state_manager.is_enabled(name):
            return None

        # 检查缓存
        if name in self.skills_cache:
            cached = self.skills_cache[name]
            if self._state_manager.is_enabled(name):
                cached.enabled = True
                return cached
            return None

        # 检查元数据
        if name not in self.metadata_cache:
            return None

        metadata = self.metadata_cache[name]
        path = Path(metadata["path"])

        # 读取完整内容
        try:
            content = path.read_text(encoding='utf-8')
        except Exception:
            return None

        # 提取 frontmatter 和 body
        match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', content, re.DOTALL)
        if not match:
            return None

        frontmatter, body = match.groups()

        try:
            parsed_metadata = yaml.safe_load(frontmatter) or {}
        except yaml.YAMLError:
            return None

        skill_dir = Path(metadata["dir"])
        skill = Skill(
            name=parsed_metadata.get("name", name),
            description=parsed_metadata.get("description", ""),
            body=body.strip(),
            path=path,
            dir=skill_dir,
            enabled=True,
            python_path=env_manager.get_venv_python(skill_dir),
            has_dependencies=env_manager.has_dependencies(skill_dir),
        )

        self.skills_cache[name] = skill
        return skill

    def list_skills(self, only_enabled: bool = False) -> List[str]:
        """列出所有技能名称

        Args:
            only_enabled: 是否仅列出启用的技能

        Returns:
            技能名称列表
        """
        if only_enabled:
            return [
                name for name in self.metadata_cache
                if self._state_manager.is_enabled(name)
            ]
        return list(self.metadata_cache.keys())

    def list_skill_infos(self) -> List[Dict]:
        """获取所有技能的完整信息（用于前端展示）

        Returns:
            技能信息字典列表，包含 name, description, enabled, dir, has_venv, has_dependencies
        """
        result = []
        for name, meta in self.metadata_cache.items():
            skill_dir = Path(meta["dir"])
            venv_python = env_manager.get_venv_python(skill_dir)
            result.append({
                "name": name,
                "description": meta["description"],
                "enabled": self._state_manager.is_enabled(name),
                "dir": meta["dir"],
                "has_venv": venv_python is not None,
                "has_dependencies": env_manager.has_dependencies(skill_dir),
                "python_path": str(venv_python) if venv_python else None,
            })
        return result

    def install_skill_env(self, name: str) -> tuple:
        """为指定技能创建/重建 venv 并安装依赖

        Args:
            name: 技能名

        Returns:
            (是否成功, 详细输出信息, Python 解释器路径或 None)

        Raises:
            SkillNotFoundError: 技能不存在
        """
        if name not in self.metadata_cache:
            raise SkillNotFoundError(f"技能 '{name}' 不存在")
        skill_dir = Path(self.metadata_cache[name]["dir"])
        try:
            ok, output, python_path = env_manager.setup_skill_env(skill_dir)
        except Exception as e:
            logger.exception("为技能 '%s' 创建环境时出现异常", name)
            return False, f"环境创建异常：{e}", None
        # 清空缓存以强制重新加载 python_path
        self.skills_cache.pop(name, None)
        return ok, output, python_path

    def get_skill_content(self, name: str) -> str:
        """获取 SKILL.md 的原始内容（用于编辑）

        Args:
            name: 技能名称

        Returns:
            SKILL.md 原始文本内容

        Raises:
            SkillNotFoundError: 技能不存在
            SkillLoadError: 文件读取失败
        """
        if name not in self.metadata_cache:
            raise SkillNotFoundError(f"技能 '{name}' 不存在")
        path = Path(self.metadata_cache[name]["path"])
        try:
            return path.read_text(encoding='utf-8')
        except OSError as e:
            logger.exception("读取 %s 失败", path)
            raise SkillLoadError(
                f"读取 SKILL.md 失败：{e}",
                code="READ_FAILED",
                detail=str(path),
            )

    def set_skill_content(self, name: str, content: str) -> str:
        """更新 SKILL.md 内容

        如果新内容中 frontmatter.name 与当前 name 不同，会触发目录重命名（P3）。

        Args:
            name: 当前技能名称
            content: 新的 Markdown 内容

        Returns:
            更新后的技能名（若改名，则返回新 name；否则返回原 name）

        Raises:
            SkillNotFoundError: 技能不存在
            SkillNameError: 新 name 不合法
            SkillConflictError: 新 name 已被其他技能使用
            SkillLoadError: 文件写入失败
        """
        if name not in self.metadata_cache:
            raise SkillNotFoundError(f"技能 '{name}' 不存在")

        path = Path(self.metadata_cache[name]["path"])

        # 预先校验新内容里 frontmatter.name（如果有）
        new_name = name
        new_metadata: Optional[Dict] = None

        fm_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
        if fm_match:
            try:
                parsed = yaml.safe_load(fm_match.group(1)) or {}
            except yaml.YAMLError as e:
                raise SkillLoadError(
                    f"新内容的 YAML frontmatter 语法错误：{e}",
                    code="YAML_ERROR",
                )

            if isinstance(parsed, dict):
                new_metadata = parsed
                candidate = parsed.get("name")
                if candidate is not None and candidate != name:
                    # P3：name 变化 → 校验合法性 + 检查冲突 + 重命名目录
                    new_name = ensure_valid_skill_name(candidate)
                    if new_name != name and new_name in self.metadata_cache:
                        raise SkillConflictError(
                            f"已存在同名技能 '{new_name}'，无法改名",
                            detail=self.metadata_cache[new_name]["dir"],
                        )

        # 写入新内容
        try:
            path.write_text(content, encoding='utf-8')
        except OSError as e:
            logger.exception("写入 %s 失败", path)
            raise SkillLoadError(
                f"写入 SKILL.md 失败：{e}",
                code="WRITE_FAILED",
                detail=str(path),
            )

        # 清除缓存
        self.skills_cache.pop(name, None)

        # 处理改名：重命名目录、迁移状态
        if new_name != name:
            old_dir = Path(self.metadata_cache[name]["dir"])
            new_dir = self.skills_dir / new_name
            try:
                old_dir.rename(new_dir)
            except OSError as e:
                logger.exception("重命名目录 %s -> %s 失败", old_dir, new_dir)
                # 重命名失败但内容已写入，尽量保持一致性：撤销内容写入
                raise SkillLoadError(
                    f"目录重命名失败：{e}。SKILL.md 已写入但目录未重命名，请手动检查。",
                    code="RENAME_FAILED",
                    detail=f"{old_dir} -> {new_dir}",
                )

            # 迁移状态
            old_enabled = self._state_manager.is_enabled(name)
            self._state_manager.set_enabled(new_name, old_enabled)
            self._state_manager.remove_state(name)

            # 迁移 metadata
            self.metadata_cache.pop(name, None)
            new_path = new_dir / "SKILL.md"
            self.metadata_cache[new_name] = {
                "name": new_name,
                "description": (new_metadata or {}).get("description", ""),
                "path": str(new_path),
                "dir": str(new_dir),
            }
            logger.info("技能已重命名：'%s' -> '%s'", name, new_name)
            return new_name

        # 未改名：仅更新元数据（description 可能改变）
        try:
            metadata = self._parse_frontmatter_only(path)
        except SkillLoadError:
            # 写入后解析失败比较罕见，记录但不抛
            logger.warning("写入后解析 %s 失败", path)
            return name

        if metadata:
            self.metadata_cache[name] = {
                "name": metadata.get("name", name),
                "description": metadata.get("description", ""),
                "path": str(path),
                "dir": str(path.parent),
            }

        return name

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """设置技能启用状态

        Args:
            name: 技能名称
            enabled: 是否启用

        Returns:
            True 表示成功，False 表示技能不存在
        """
        if name not in self.metadata_cache:
            logger.warning("set_enabled: 技能 '%s' 不存在", name)
            return False
        self._state_manager.set_enabled(name, enabled)
        if not enabled:
            self.skills_cache.pop(name, None)
        return True

    def is_enabled(self, name: str) -> bool:
        """检查技能是否启用"""
        if name not in self.metadata_cache:
            return False
        return self._state_manager.is_enabled(name)

    def delete_skill(self, name: str) -> bool:
        """删除技能目录（含专属 venv）

        Args:
            name: 技能名称

        Returns:
            True 表示成功，False 表示技能不存在或删除失败（详见日志）
        """
        if name not in self.metadata_cache:
            logger.warning("delete_skill: 技能 '%s' 不存在", name)
            return False
        skill_dir = Path(self.metadata_cache[name]["dir"])
        try:
            # Windows 下 venv 内有只读文件，onerror 钩子尝试改权限后重试
            def _on_rm_error(func, path, exc_info):
                try:
                    os.chmod(path, 0o777)
                    func(path)
                except OSError as e:
                    logger.warning("删除 %s 时无法改权限：%s", path, e)
            shutil.rmtree(skill_dir, onerror=_on_rm_error)
        except OSError as e:
            logger.exception("删除技能目录 %s 失败", skill_dir)
            print(f"⚠️ 删除技能 '{name}' 失败：{e}")
            return False

        self.metadata_cache.pop(name, None)
        self.skills_cache.pop(name, None)
        self._state_manager.remove_state(name)
        logger.info("技能 '%s' 已删除", name)
        return True

    def import_from_path(self, source: str, auto_install: bool = True) -> Skill:
        """从本地目录导入技能

        将 source 目录复制到 skills_dir 下（强制使用 frontmatter.name 作为目录名）。

        Args:
            source: 源目录路径
            auto_install: 是否自动为技能创建专属 venv 并安装依赖

        Returns:
            导入的 Skill 对象

        Raises:
            SkillImportError: 源目录不存在/缺 SKILL.md/复制失败
            SkillNameError: name 不合法
            SkillConflictError: 已存在同名技能（来自不同目录）
            SkillLoadError: SKILL.md 解析失败
        """
        source_path = Path(source).resolve()
        if not source_path.exists():
            raise SkillImportError(
                f"源路径不存在：{source}",
                code="SOURCE_NOT_FOUND",
                detail=str(source_path),
            )
        if not source_path.is_dir():
            raise SkillImportError(
                f"源路径不是目录：{source}",
                code="SOURCE_NOT_DIR",
                detail=str(source_path),
            )

        skill_md = source_path / "SKILL.md"
        if not skill_md.exists():
            raise SkillImportError(
                "源目录中找不到 SKILL.md 文件",
                code="MISSING_SKILL_MD",
                detail=str(source_path),
            )

        # 解析 frontmatter（异常向上抛）
        metadata = self._parse_frontmatter_only(skill_md)
        if not metadata:
            raise SkillImportError(
                "SKILL.md 缺少 frontmatter 或缺少必需字段（name / description）",
                code="INVALID_FRONTMATTER",
                detail=str(skill_md),
            )

        # 校验 name 合法性（P1：抛 SkillNameError）
        raw_name = metadata.get("name", source_path.name)
        name = ensure_valid_skill_name(raw_name)

        # P3：强制目录名 = name
        dest_dir = self.skills_dir / name

        # 同源同目标：无需重复导入
        if dest_dir.exists() and dest_dir.resolve() == source_path.resolve():
            logger.info("源路径与目标路径相同，跳过复制：%s", dest_dir)
            return self.get_skill(name)

        # 已存在但来自不同目录：冲突
        if dest_dir.exists() and not (dest_dir.resolve() == source_path.resolve()):
            # 若目标目录已有同名技能注册，提示用户先删除
            if name in self.metadata_cache:
                existing_dir = self.metadata_cache[name]["dir"]
                if Path(existing_dir).resolve() != source_path.resolve():
                    raise SkillConflictError(
                        f"已存在同名技能 '{name}'（位于 {existing_dir}）。"
                        f"请先删除或重命名后再导入。",
                        detail=existing_dir,
                    )

        try:
            shutil.copytree(source_path, dest_dir, dirs_exist_ok=True)
        except OSError as e:
            logger.exception("复制技能目录失败 %s -> %s", source_path, dest_dir)
            raise SkillImportError(
                f"复制目录失败：{e}",
                code="COPY_FAILED",
                detail=f"{source_path} -> {dest_dir}",
            )

        # 更新元数据
        new_skill_md = dest_dir / "SKILL.md"
        self.metadata_cache[name] = {
            "name": name,
            "description": metadata.get("description", ""),
            "path": str(new_skill_md),
            "dir": str(dest_dir),
        }
        self.skills_cache.pop(name, None)

        # 确保新导入的技能默认启用（覆盖之前可能存在的禁用状态）
        self._state_manager.set_enabled(name, True)

        # 自动为技能创建专属 venv 并安装依赖
        if auto_install:
            try:
                env_manager.setup_skill_env(dest_dir)
            except Exception:
                logger.exception("技能 '%s' 依赖安装失败（不影响加载）", name)
                print(f"⚠️ 技能 '{name}' 依赖安装失败（不影响加载）")

        skill = self.get_skill(name)
        if skill is None:
            raise SkillImportError(
                f"导入后无法加载技能 '{name}'",
                code="LOAD_AFTER_IMPORT",
            )
        return skill

    def import_from_git(self, repo_url: str, auto_install: bool = True) -> Skill:
        """从 Git 仓库导入技能

        Clone 仓库到 skills_dir 下临时目录，递归查找含 SKILL.md 的子目录，
        每个子目录作为一个技能导入。

        Args:
            repo_url: Git 仓库 URL
            auto_install: 是否自动为技能创建专属 venv 并安装依赖

        Returns:
            首个成功导入的 Skill 对象

        Raises:
            SkillImportError: git 不可用 / clone 失败 / 未找到合法 SKILL.md
            SkillNameError: 仓库中 SKILL.md 的 name 不合法
        """
        # 检查 git 是否可用
        try:
            subprocess.run(
                ["git", "--version"],
                capture_output=True, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.warning("Git 不可用：%s", e)
            raise SkillImportError(
                "Git 命令不可用，请确认已安装 Git 并加入 PATH",
                code="GIT_NOT_AVAILABLE",
                detail=str(e),
            )

        # 使用仓库名作为临时目录名
        repo_name = repo_url.rstrip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]

        temp_dir = self.skills_dir / ("_" + repo_name)
        if temp_dir.exists():
            try:
                shutil.rmtree(temp_dir)
            except OSError as e:
                logger.exception("清理临时目录 %s 失败", temp_dir)
                raise SkillImportError(
                    f"无法清理已存在的临时目录：{e}",
                    code="TEMP_CLEANUP_FAILED",
                    detail=str(temp_dir),
                )

        try:
            result = subprocess.run(
                ["git", "clone", repo_url, str(temp_dir)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()[-500:]
                logger.warning("git clone 失败 %s: %s", repo_url, stderr)
                raise SkillImportError(
                    f"Git clone 失败：{stderr or '未知错误'}",
                    code="CLONE_FAILED",
                    detail=stderr,
                )
        except SkillImportError:
            raise
        except Exception as e:
            logger.exception("执行 git clone 出现异常 %s", repo_url)
            raise SkillImportError(
                f"执行 git clone 出现异常：{e}",
                code="CLONE_EXCEPTION",
                detail=str(e),
            )

        # 查找 SKILL.md（可能是根目录或子目录）
        skill_dirs = []
        for skill_md in sorted(temp_dir.rglob("SKILL.md")):
            if ".git" in skill_md.parts:
                continue
            skill_dirs.append(skill_md.parent)

        if not skill_dirs:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise SkillImportError(
                "Git 仓库中未找到任何 SKILL.md 文件",
                code="NO_SKILL_FOUND",
                detail=repo_url,
            )

        # 对每个找到的技能目录单独导入
        imported_skills: List[tuple] = []
        skipped_errors: List[str] = []

        for skill_dir in skill_dirs:
            skill_md_path = skill_dir / "SKILL.md"
            try:
                metadata = self._parse_frontmatter_only(skill_md_path)
            except SkillLoadError as e:
                logger.warning("跳过 %s：%s", skill_md_path, e.message)
                skipped_errors.append(f"{skill_dir.name}: {e.message}")
                continue

            if not metadata:
                skipped_errors.append(f"{skill_dir.name}: 缺少合法 frontmatter")
                continue

            raw_name = metadata.get("name", skill_dir.name)
            name_err = validate_skill_name(raw_name)
            if name_err is not None:
                logger.warning("技能 %s 名称非法：%s", skill_md_path, name_err)
                skipped_errors.append(f"{raw_name or skill_dir.name}: {name_err}")
                continue

            name = raw_name.strip()
            dest_dir = self.skills_dir / name

            try:
                if dest_dir.exists():
                    shutil.rmtree(dest_dir)
                shutil.copytree(skill_dir, dest_dir)
            except OSError as e:
                logger.exception("复制技能 %s 失败", skill_dir)
                skipped_errors.append(f"{name}: 复制失败 - {e}")
                continue

            new_skill_md = dest_dir / "SKILL.md"
            self.metadata_cache[name] = {
                "name": name,
                "description": metadata.get("description", ""),
                "path": str(new_skill_md),
                "dir": str(dest_dir),
            }
            self.skills_cache.pop(name, None)
            self._state_manager.set_enabled(name, True)
            imported_skills.append((name, dest_dir))

        # 清理临时目录
        shutil.rmtree(temp_dir, ignore_errors=True)

        if not imported_skills:
            raise SkillImportError(
                "Git 仓库中没有可导入的合法技能",
                code="NO_VALID_SKILL",
                detail="; ".join(skipped_errors) if skipped_errors else None,
            )

        # 为每个导入的技能创建专属 venv 并安装依赖
        if auto_install:
            for name, dest_dir in imported_skills:
                try:
                    env_manager.setup_skill_env(dest_dir)
                except Exception:
                    logger.exception("技能 '%s' 依赖安装失败", name)
                    print(f"⚠️ 技能 '{name}' 依赖安装失败（不影响加载）")

        skill = self.get_skill(imported_skills[0][0])
        if skill is None:
            raise SkillImportError(
                f"导入后无法加载技能 '{imported_skills[0][0]}'",
                code="LOAD_AFTER_IMPORT",
            )
        return skill

    def reload(self):
        """重新扫描技能目录（热重载）"""
        self.skills_cache.clear()
        self.metadata_cache.clear()
        self._scan_skills()
