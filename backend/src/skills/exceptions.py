"""技能系统异常类

定义结构化的异常类型，便于上层 API 返回明确错误信息。
所有异常均携带：
- code: 机器可读的错误码（用于前端国际化或精确判断）
- message: 用户可读的错误消息
- detail: 可选的详细信息（如底层异常堆栈摘要）
"""

from typing import Optional


class SkillError(Exception):
    """技能系统基础异常"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "SKILL_ERROR",
        detail: Optional[str] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


class SkillImportError(SkillError):
    """技能导入失败"""

    def __init__(self, message: str, *, code: str = "IMPORT_FAILED", detail: Optional[str] = None):
        super().__init__(message, code=code, detail=detail)


class SkillLoadError(SkillError):
    """技能加载失败（读取/解析）"""

    def __init__(self, message: str, *, code: str = "LOAD_FAILED", detail: Optional[str] = None):
        super().__init__(message, code=code, detail=detail)


class SkillNameError(SkillError):
    """技能名称非法"""

    def __init__(self, message: str, *, code: str = "INVALID_NAME", detail: Optional[str] = None):
        super().__init__(message, code=code, detail=detail)


class SkillConflictError(SkillError):
    """技能名称冲突（已存在同名技能）"""

    def __init__(self, message: str, *, code: str = "NAME_CONFLICT", detail: Optional[str] = None):
        super().__init__(message, code=code, detail=detail)


class SkillNotFoundError(SkillError):
    """技能不存在"""

    def __init__(self, message: str, *, code: str = "NOT_FOUND", detail: Optional[str] = None):
        super().__init__(message, code=code, detail=detail)
