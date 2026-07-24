"""Agent 身份管理器 — 从基座目录加载身份/人格文件，并负责首次部署。

将 Agent 的"灵魂"（IDENTITY/SOUL/USER/BOOTSTRAP）从工作区解耦到固定基座目录
`~/.helloclaw/identity/`，切工作区时人格不丢失。负责首次部署时从模板复制 identity
文件到基座目录，并支持从旧 workspace 目录的 V1→V2 迁移。
"""

import os
import re
import shutil
from pathlib import Path
from typing import Optional


# 身份/人格文件列表（不含 BOOTSTRAP — 引导完成后会被删除）
IDENTITY_FILES = ["IDENTITY", "SOUL", "USER"]

# 引导文件
BOOTSTRAP_FILE = "BOOTSTRAP"

# 模板目录（相对于当前文件：workspace/templates/）
TEMPLATES_DIR = Path(__file__).parent / "templates"
IDENTITY_TEMPLATES_DIR = TEMPLATES_DIR / "identity"


class IdentityManager:
    """管理 Agent 的身份、人格、用户画像文件。

    所有文件从固定的 AGENT_HOME/identity/ 下读取，不随工作区切换而改变。
    负责首次部署时从模板复制 identity 文件到基座目录，并支持 V1→V2 迁移。
    """

    def __init__(self, home_dir: str = "~/.helloclaw"):
        self.home_dir = os.path.expanduser(home_dir)
        self.identity_dir = os.path.join(self.home_dir, "identity")
        self._migration_marker = os.path.join(self.home_dir, ".v2_migration_done")

    # ==================== 部署与迁移 ====================

    def _deploy_from_template(self, filename: str):
        """从模板部署单个文件到 identity 目录（不覆盖已有）。"""
        target = os.path.join(self.identity_dir, filename)
        if os.path.exists(target):
            return
        src = IDENTITY_TEMPLATES_DIR / filename
        os.makedirs(self.identity_dir, exist_ok=True)
        if src.exists():
            shutil.copy2(src, target)
            print(f"📝 已从模板部署: {target}")
        else:
            # 模板缺失时写入基础占位内容
            name = filename.replace(".md", "")
            self.save_file(filename, f"# {name}\n\n（待配置）\n")

    def _migrate_from_workspace(self, filename: str, old_workspace: str):
        """从旧 workspace 目录迁移文件到 identity 目录（不覆盖已有）。"""
        target = os.path.join(self.identity_dir, f"{filename}.md")
        if os.path.exists(target):
            return
        old = os.path.join(old_workspace, f"{filename}.md")
        if os.path.exists(old):
            os.makedirs(self.identity_dir, exist_ok=True)
            shutil.copy2(old, target)
            print(f"📦 已从旧工作区迁移: {target}")

    def ensure_exists(self, old_workspace: Optional[str] = None):
        """Phase 1 部署：确保 identity 文件存在。

        1. 如果 .v2_migration_done 标记不存在 → 尝试从旧 workspace 迁移
        2. 迁移后或新安装 → 从 templates/identity/ 复制缺失文件
        3. 确保 BOOTSTRAP.md 存在（仅新装/引导未完成时）
        4. 写入迁移完成标记

        Args:
            old_workspace: 旧 workspace 目录路径（V1 兼容迁移用），None 则跳过迁移
        """
        os.makedirs(self.identity_dir, exist_ok=True)

        # Step 1: 从旧 workspace 迁移（仅首次）
        if old_workspace and not os.path.exists(self._migration_marker):
            migrated = False
            for name in IDENTITY_FILES:
                before = os.path.exists(os.path.join(self.identity_dir, f"{name}.md"))
                self._migrate_from_workspace(name, old_workspace)
                if not before and os.path.exists(os.path.join(self.identity_dir, f"{name}.md")):
                    migrated = True
            if migrated:
                print("📦 V1 → V2 身份文件迁移完成")

        # Step 2: 从模板填充缺失文件
        for name in IDENTITY_FILES:
            self._deploy_from_template(f"{name}.md")

        # Step 3: 确保 BOOTSTRAP 存在（引导完成后会自动删除）
        bootstrap_path = os.path.join(self.identity_dir, f"{BOOTSTRAP_FILE}.md")
        if not os.path.exists(bootstrap_path):
            self._deploy_from_template(f"{BOOTSTRAP_FILE}.md")

        # Step 4: 标记迁移完成
        if not os.path.exists(self._migration_marker):
            with open(self._migration_marker, "w", encoding="utf-8") as f:
                f.write("done")

    # ==================== 入职状态 ====================

    def is_onboarding_completed(self) -> bool:
        """入职是否完成 = BOOTSTRAP.md 已删除。

        同时会检查身份是否已确定，如果是则自动删除 BOOTSTRAP.md。
        """
        self.try_complete_onboarding()
        bootstrap_path = os.path.join(self.identity_dir, f"{BOOTSTRAP_FILE}.md")
        return not os.path.exists(bootstrap_path)

    def _is_identity_established(self) -> bool:
        """检查身份是否已确定（名称字段有实际内容）。"""
        identity = self.identity
        if not identity:
            return False
        # 匹配格式: - **名称：** xxx 或 - **名称:** xxx
        match = re.search(r'\*\*名称[：:]\*\*\s*(.+?)(?:\n|$)', identity)
        if match:
            name = match.group(1).strip()
            # 占位符特征：以下划线开头、包含"选一个"、包含"（"
            if name and not name.startswith('_') and '选一个' not in name and '（' not in name:
                return True
        return False

    def try_complete_onboarding(self):
        """如果身份已确定，删除 BOOTSTRAP.md 完成入职。"""
        bootstrap_path = os.path.join(self.identity_dir, f"{BOOTSTRAP_FILE}.md")
        if not os.path.exists(bootstrap_path):
            return
        if self._is_identity_established():
            os.remove(bootstrap_path)
            print("✅ 入职完成，已删除 BOOTSTRAP.md")

    # ==================== 读写 ====================

    def save_file(self, filename: str, content: str):
        """保存 identity 文件。

        Args:
            filename: 文件名（含 .md 后缀）
            content: 文件内容
        """
        os.makedirs(self.identity_dir, exist_ok=True)
        path = os.path.join(self.identity_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def _load_raw(self, filename: str) -> Optional[str]:
        """读取 identity 文件原始内容。"""
        path = os.path.join(self.identity_dir, filename)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def load_name(self, name: str) -> Optional[str]:
        """按配置名（不含后缀）读取文件内容。"""
        return self._load_raw(f"{name}.md")

    def list_configs(self) -> list:
        """列出 identity 目录下已存在的配置文件名（不含后缀）。"""
        result = []
        for name in IDENTITY_FILES + [BOOTSTRAP_FILE]:
            if os.path.exists(os.path.join(self.identity_dir, f"{name}.md")):
                result.append(name)
        return result

    # ==================== 便捷属性 ====================

    @property
    def identity(self) -> str:
        return self._load_raw("IDENTITY.md") or ""

    @property
    def soul(self) -> str:
        return self._load_raw("SOUL.md") or ""

    @property
    def user(self) -> str:
        return self._load_raw("USER.md") or ""

    @property
    def bootstrap(self) -> Optional[str]:
        return self._load_raw("BOOTSTRAP.md")

    def read_name(self) -> Optional[str]:
        """从 IDENTITY.md 读取助手名称（占位符返回 None）。"""
        identity = self.identity
        if not identity:
            return None
        match = re.search(r'\*\*名称[：:]\*\*\s*(.+?)(?:\n|$)', identity)
        if match:
            name = match.group(1).strip()
            if name and not name.startswith('_') and '选一个' not in name and '（' not in name:
                return name
        return None
