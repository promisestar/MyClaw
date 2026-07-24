"""工作区授权管理 — 维护用户已授权的工作区白名单。

用户须显式授权一个本地目录后才能切换为当前工作区。白名单持久化到
`~/.helloclaw/workspaces.json`，防止前端传任意路径越级访问敏感目录。
"""

import json
import os
from typing import List


# 授权白名单文件
AUTH_FILE = os.path.expanduser("~/.helloclaw/workspaces.json")


def _load() -> List[str]:
    """加载白名单（返回绝对路径列表，解析符号链接防绕过）。"""
    if not os.path.exists(AUTH_FILE):
        return []
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [os.path.realpath(os.path.expanduser(p))
                for p in data if isinstance(p, str)]
    except (json.JSONDecodeError, IOError):
        return []


def _save(paths: List[str]):
    """保存白名单。"""
    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(paths, f, indent=2, ensure_ascii=False)


def list_allowed_workspaces() -> List[str]:
    """返回所有已授权且存在的目录。"""
    return [p for p in _load() if os.path.isdir(p)]


def is_allowed(workspace_path: str) -> bool:
    """检查工作区是否已授权且存在。"""
    abs_path = os.path.realpath(os.path.expanduser(workspace_path))
    return os.path.isdir(abs_path) and abs_path in _load()


def authorize(workspace_path: str) -> bool:
    """授权一个新工作区。

    Returns:
        True 表示授权成功；False 表示路径不存在或无效
    """
    abs_path = os.path.realpath(os.path.expanduser(workspace_path))
    if not os.path.isdir(abs_path):
        return False
    current = _load()
    if abs_path not in current:
        current.append(abs_path)
        _save(current)
    return True


def revoke(workspace_path: str):
    """撤销一个工作区授权。"""
    current = _load()
    abs_path = os.path.realpath(os.path.expanduser(workspace_path))
    if abs_path in current:
        current.remove(abs_path)
        _save(current)
