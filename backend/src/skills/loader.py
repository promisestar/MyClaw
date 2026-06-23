"""技能加载器

实现渐进式披露机制：
- Layer 1: Metadata（启动时加载，~100 tokens/skill）
- Layer 2: SKILL.md body（按需加载，~2000+ tokens）
- Layer 3: Resources（可选，按需）

支持本地目录复制和 Git 仓库克隆导入。
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from dataclasses import dataclass

from .state_manager import SkillStateManager


@dataclass
class Skill:
    """技能数据类"""
    name: str
    description: str
    body: str
    path: Path
    dir: Path
    enabled: bool = True

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
        """扫描 skills/ 目录，加载元数据"""
        for skill_dir in self.skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            if skill_dir.name.startswith("."):
                continue
            if skill_dir.name == "__pycache__":
                continue

            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue

            metadata = self._parse_frontmatter_only(skill_md)
            if not metadata:
                continue

            name = metadata.get("name", skill_dir.name)
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
            解析后的元数据字典，解析失败返回 None
        """
        try:
            content = path.read_text(encoding='utf-8')
        except Exception:
            return None

        match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
        if not match:
            return None

        try:
            metadata = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
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

        skill = Skill(
            name=parsed_metadata.get("name", name),
            description=parsed_metadata.get("description", ""),
            body=body.strip(),
            path=path,
            dir=Path(metadata["dir"]),
            enabled=True,
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
            技能信息字典列表，包含 name, description, enabled, dir
        """
        result = []
        for name, meta in self.metadata_cache.items():
            result.append({
                "name": name,
                "description": meta["description"],
                "enabled": self._state_manager.is_enabled(name),
                "dir": meta["dir"],
            })
        return result

    def get_skill_content(self, name: str) -> Optional[str]:
        """获取 SKILL.md 的原始内容（用于编辑）

        Args:
            name: 技能名称

        Returns:
            SKILL.md 原始文本内容
        """
        if name not in self.metadata_cache:
            return None
        path = Path(self.metadata_cache[name]["path"])
        try:
            return path.read_text(encoding='utf-8')
        except Exception:
            return None

    def set_skill_content(self, name: str, content: str) -> bool:
        """更新 SKILL.md 内容

        Args:
            name: 技能名称
            content: 新的 Markdown 内容

        Returns:
            是否成功
        """
        if name not in self.metadata_cache:
            return False
        path = Path(self.metadata_cache[name]["path"])
        try:
            path.write_text(content, encoding='utf-8')
        except Exception:
            return False

        # 清除缓存，下次按需重新加载
        self.skills_cache.pop(name, None)

        # 重新解析 frontmatter 更新元数据
        metadata = self._parse_frontmatter_only(path)
        if metadata:
            self.metadata_cache[name] = {
                "name": metadata.get("name", name),
                "description": metadata.get("description", ""),
                "path": str(path),
                "dir": str(path.parent),
            }

        return True

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """设置技能启用状态

        Args:
            name: 技能名称
            enabled: 是否启用

        Returns:
            是否成功
        """
        if name not in self.metadata_cache:
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
        """删除技能目录

        Args:
            name: 技能名称

        Returns:
            是否成功
        """
        if name not in self.metadata_cache:
            return False
        skill_dir = Path(self.metadata_cache[name]["dir"])
        try:
            shutil.rmtree(skill_dir)
        except Exception:
            return False

        self.metadata_cache.pop(name, None)
        self.skills_cache.pop(name, None)
        self._state_manager.remove_state(name)
        return True

    def import_from_path(self, source: str) -> Optional[Skill]:
        """从本地目录导入技能

        将 source 目录复制到 skills_dir 下（使用目录名）。

        Args:
            source: 源目录路径

        Returns:
            导入的 Skill 对象，失败返回 None
        """
        source_path = Path(source).resolve()
        if not source_path.exists() or not source_path.is_dir():
            return None

        skill_md = source_path / "SKILL.md"
        if not skill_md.exists():
            return None

        # 先解析 frontmatter 获取名称
        metadata = self._parse_frontmatter_only(skill_md)
        if not metadata:
            return None

        name = metadata.get("name", source_path.name)
        dest_dir = self.skills_dir / name

        # 如果已存在同名技能且在同一目录则不重复导入
        if dest_dir.exists() and dest_dir.samefile(source_path):
            return None

        try:
            shutil.copytree(source_path, dest_dir, dirs_exist_ok=True)
        except Exception:
            return None

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

        return self.get_skill(name)

    def import_from_git(self, repo_url: str) -> Optional[Skill]:
        """从 Git 仓库导入技能

        Clone 仓库到 skills_dir 下，自动检测子目录中的 SKILL.md。

        Args:
            repo_url: Git 仓库 URL

        Returns:
            导入的 Skill 对象，失败返回 None
        """
        # 检查 git 是否可用
        try:
            subprocess.run(
                ["git", "--version"],
                capture_output=True, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

        # 使用仓库名作为临时目录名
        repo_name = repo_url.rstrip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]

        temp_dir = self.skills_dir / ("_" + repo_name)
        if temp_dir.exists():
            shutil.rmtree(temp_dir)

        try:
            result = subprocess.run(
                ["git", "clone", repo_url, str(temp_dir)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                return None
        except Exception:
            return None

        # 查找 SKILL.md（可能是根目录或子目录）
        skill_dirs = []
        for skill_md in sorted(temp_dir.rglob("SKILL.md")):
            if ".git" in skill_md.parts:
                continue
            skill_dirs.append(skill_md.parent)

        if not skill_dirs:
            shutil.rmtree(temp_dir)
            return None

        # 对于单目录仓库，检测 SKILL.md 中的 name 来决定目标目录
        imported_skills = []
        for skill_dir in skill_dirs:
            skill_md_path = skill_dir / "SKILL.md"
            metadata = self._parse_frontmatter_only(skill_md_path)
            if not metadata:
                continue
            name = metadata.get("name", skill_dir.name)
            dest_dir = self.skills_dir / name

            try:
                if dest_dir.exists():
                    shutil.rmtree(dest_dir)
                shutil.copytree(skill_dir, dest_dir)
            except Exception:
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
            imported_skills.append(name)

        # 清理临时目录
        shutil.rmtree(temp_dir)

        if imported_skills:
            return self.get_skill(imported_skills[0])
        return None

    def reload(self):
        """重新扫描技能目录（热重载）"""
        self.skills_cache.clear()
        self.metadata_cache.clear()
        self._scan_skills()
