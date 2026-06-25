"""Skills 知识外化系统

自实现的 Skill 系统，替代 hello_agents 依赖。

特性：
- 渐进式披露：启动时仅加载元数据，按需加载完整内容
- 状态管理：支持启用/禁用控制
- 导入功能：本地目录复制 + Git 仓库克隆
- 缓存友好：作为 tool_result 注入，不修改 system_prompt
"""

from .loader import SkillLoader, Skill
from .state_manager import SkillStateManager
from .exceptions import (
    SkillError,
    SkillImportError,
    SkillLoadError,
    SkillNameError,
    SkillConflictError,
    SkillNotFoundError,
)
from .validators import validate_skill_name, ensure_valid_skill_name

__all__ = [
    "SkillLoader",
    "Skill",
    "SkillStateManager",
    # 异常
    "SkillError",
    "SkillImportError",
    "SkillLoadError",
    "SkillNameError",
    "SkillConflictError",
    "SkillNotFoundError",
    # 校验
    "validate_skill_name",
    "ensure_valid_skill_name",
]
