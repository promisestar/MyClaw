"""技能状态管理器

管理技能的启用/禁用状态，持久化到 JSON 文件。
与 SKILL.md 分离存储，避免修改原始技能文件。
"""

import json
from pathlib import Path
from typing import Dict


class SkillStateManager:
    """技能状态管理器

    使用独立的 skill_states.json 文件存储各技能的启用状态。
    """

    def __init__(self, state_file: Path):
        """初始化状态管理器

        Args:
            state_file: 状态文件路径（通常是 skills_dir/skill_states.json）
        """
        self.state_file = Path(state_file)
        self._states: Dict[str, bool] = {}
        self._load()

    def _load(self):
        """从文件加载状态"""
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    self._states = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._states = {}

    def _save(self):
        """将状态保存到文件"""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self._states, f, indent=2, ensure_ascii=False)

    def is_enabled(self, name: str) -> bool:
        """检查技能是否启用

        默认启用（技能不在状态文件中视为启用）。

        Args:
            name: 技能名称

        Returns:
            是否启用
        """
        return self._states.get(name, True)

    def set_enabled(self, name: str, enabled: bool):
        """设置技能启用状态

        Args:
            name: 技能名称
            enabled: 是否启用
        """
        self._states[name] = enabled
        self._save()

    def remove_state(self, name: str):
        """移除技能状态记录（技能被删除时调用）

        Args:
            name: 技能名称
        """
        if name in self._states:
            del self._states[name]
            self._save()

    def list_disabled(self) -> set:
        """获取所有被禁用的技能名称集合"""
        return {name for name, enabled in self._states.items() if not enabled}
