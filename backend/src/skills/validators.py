"""技能名称校验

严格拒绝非法字符，避免目录创建失败或路径错乱。

允许的字符：
- 字母（a-z, A-Z）
- 数字（0-9）
- 中文（\u4e00-\u9fff）
- 连字符 -、下划线 _、点 .
长度限制：1-64 字符。

禁止规则：
- 不以 . 或 _ 开头（避免与隐藏目录/Python 私有模块冲突）
- 不以 . 结尾（Windows 不允许）
- 不为保留名（CON / PRN / AUX / NUL / __pycache__ / .venv 等）
- 不含路径分隔符或控制字符
"""

import re
from typing import Optional

from .exceptions import SkillNameError


# 允许的字符集
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_\-.\u4e00-\u9fff]+$")

# 长度限制
_MIN_LENGTH = 1
_MAX_LENGTH = 64

# 保留名（不区分大小写）
_RESERVED_NAMES = {
    # Python 相关
    "__pycache__",
    ".venv",
    "venv",
    "env",
    # Windows 保留设备名
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
    # 项目内部专用
    "skill_states.json",
}


def validate_skill_name(name: str) -> Optional[str]:
    """校验技能名是否合法

    Args:
        name: 技能名

    Returns:
        合法返回 None；非法返回具体错误描述
    """
    if name is None:
        return "技能名不能为空"

    if not isinstance(name, str):
        return f"技能名必须是字符串，得到 {type(name).__name__}"

    name = name.strip()

    if not name:
        return "技能名不能为空或仅含空白"

    if len(name) < _MIN_LENGTH:
        return f"技能名长度不能少于 {_MIN_LENGTH} 字符"

    if len(name) > _MAX_LENGTH:
        return f"技能名长度不能超过 {_MAX_LENGTH} 字符（当前 {len(name)}）"

    # 起始字符限制
    if name.startswith("."):
        return "技能名不能以点（.）开头"

    if name.startswith("_"):
        return "技能名不能以下划线（_）开头"

    # 结尾字符限制（Windows 兼容）
    if name.endswith("."):
        return "技能名不能以点（.）结尾"

    if name.endswith(" "):
        return "技能名不能以空格结尾"

    # 保留名检查
    if name.lower() in _RESERVED_NAMES:
        return f"'{name}' 是系统保留名，不能用作技能名"

    # 字符集检查
    if not _NAME_PATTERN.match(name):
        # 找出具体的非法字符
        illegal_chars = sorted(set(c for c in name if not _NAME_PATTERN.match(c)))
        chars_str = ", ".join(repr(c) for c in illegal_chars[:5])
        return (
            f"技能名包含非法字符 {chars_str}。"
            f"仅允许字母、数字、中文、连字符（-）、下划线（_）、点（.）"
        )

    # 防止 . 和 .. 这类路径段
    if name in (".", ".."):
        return f"技能名不能为 '{name}'"

    return None


def ensure_valid_skill_name(name: str) -> str:
    """校验技能名，非法时抛出 SkillNameError

    Args:
        name: 技能名

    Returns:
        清理后的技能名（已 strip）

    Raises:
        SkillNameError: 技能名不合法
    """
    err = validate_skill_name(name)
    if err is not None:
        raise SkillNameError(err, detail=f"输入 name='{name}'")
    return name.strip()
