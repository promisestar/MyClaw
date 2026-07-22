"""统一超时配置管理。

借鉴 WorkBuddy 设计：不设单一"对话超时"，而是按操作类型分层设置超时。
每层独立配置，单点超时互不影响。

配置来源优先级（env > config.json > 默认值）：
- `CODEBUDDY_FIRST_TOKEN_TIMEOUT_MS` → llm_first_token_ms
- `CODEBUDDY_STREAM_TIMEOUT_MS` → llm_stream_idle_ms
- `BASH_DEFAULT_TIMEOUT_MS` / `BASH_MAX_TIMEOUT_MS`
- `MCP_TIMEOUT` / `MCP_TOOL_TIMEOUT`
- `gateway.runTimeoutMs`（config.json 顶层，非 env）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class TimeoutConfig:
    """模块级超时配置（单位：毫秒）。

    每项超时独立控制，互不影响：
    - 首 token 慢是长上下文 prefill 的正常现象，不误杀
    - 流式中断是网络异常的信号，快速检测
    - bash 命令有明确的时间上限
    - MCP 调用是外部依赖，容忍度高
    - gateway 是整次对话的上限，可被 header 覆盖
    """

    # ── LLM 模块 ──
    llm_first_token_ms: int = 1_200_000    # 20 分钟：长上下文 prefill
    llm_stream_idle_ms: int = 300_000      # 5 分钟：流式输出静默

    # ── LLM 重试 ──
    llm_retry_max: int = 2                  # LLM 调用最大重试次数
    llm_retry_base_delay: float = 1.0       # 基础延迟（秒）
    llm_retry_max_delay: float = 15.0       # 最大延迟（秒）
    llm_retry_backoff: float = 2.0          # 指数退避因子
    llm_retry_jitter: float = 0.2           # 随机抖动（±20%）

    # ── Bash 模块 ──
    bash_default_ms: int = 120_000          # 2 分钟：默认命令超时
    bash_max_ms: int = 600_000              # 10 分钟：命令超时上限

    # ── MCP 模块 ──
    mcp_connect_ms: int = 30_000            # 30 秒：连接超时
    mcp_tool_ms: int = 60_000               # 60 秒：工具调用超时

    # ── Gateway 整次任务 ──
    gateway_run_ms: int = 1_800_000         # 30 分钟：整次对话上限

    # ── 工具元数据（ContextGuard 动态路由） ──
    subagent_base_ms: int = 30_000          # 30 秒：子代理基础超时
    subagent_per_tool_ms: int = 15_000      # 15 秒：每增加一个工具
    subagent_per_iteration_ms: int = 10_000 # 10 秒：每增加一轮迭代
    subagent_max_ms: int = 300_000          # 5 分钟：子代理超时上限
    subagent_min_ms: int = 30_000           # 30 秒：子代理超时下限


# ──────────────────────────────────────────────────────────────────
# 配置加载
# ──────────────────────────────────────────────────────────────────

_DEFAULT_CONFIG_PATH = os.path.expanduser("~/.helloclaw/config.json")

_instance: Optional[TimeoutConfig] = None


def _load_global_config() -> Dict[str, Any]:
    """读取 ~/.helloclaw/config.json（与 WorkspaceManager 解耦，避免循环导入）。"""
    if not os.path.exists(_DEFAULT_CONFIG_PATH):
        return {}
    try:
        with open(_DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _env_int(key: str, default: int) -> int:
    """读取整数环境变量，返回默认值如果不存在或非法。"""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    """读取浮点环境变量，返回默认值如果不存在或非法。"""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def load_timeout_config(force_reload: bool = False) -> TimeoutConfig:
    """加载超时配置（单例模式）。

    优先级：env > config.json > 默认值。

    Args:
        force_reload: 强制重新加载（用于配置热更新）

    Returns:
        TimeoutConfig 实例
    """
    global _instance
    if _instance is not None and not force_reload:
        return _instance

    global_cfg = _load_global_config()
    timeouts_cfg = global_cfg.get("timeouts", {})
    llm_retry_cfg = global_cfg.get("llm_retry", {})
    gateway_cfg = global_cfg.get("gateway", {})

    cfg = TimeoutConfig()

    # ── 从 config.json 读取 ──
    cfg.llm_first_token_ms = timeouts_cfg.get("llm_first_token_ms", cfg.llm_first_token_ms)
    cfg.llm_stream_idle_ms = timeouts_cfg.get("llm_stream_idle_ms", cfg.llm_stream_idle_ms)
    cfg.bash_default_ms = timeouts_cfg.get("bash_default_ms", cfg.bash_default_ms)
    cfg.bash_max_ms = timeouts_cfg.get("bash_max_ms", cfg.bash_max_ms)
    cfg.mcp_connect_ms = timeouts_cfg.get("mcp_connect_ms", cfg.mcp_connect_ms)
    cfg.mcp_tool_ms = timeouts_cfg.get("mcp_tool_ms", cfg.mcp_tool_ms)
    cfg.gateway_run_ms = gateway_cfg.get("runTimeoutMs", cfg.gateway_run_ms)

    cfg.llm_retry_max = llm_retry_cfg.get("max_retries", cfg.llm_retry_max)
    cfg.llm_retry_base_delay = llm_retry_cfg.get("base_delay", cfg.llm_retry_base_delay)
    cfg.llm_retry_max_delay = llm_retry_cfg.get("max_delay", cfg.llm_retry_max_delay)
    cfg.llm_retry_backoff = llm_retry_cfg.get("backoff", cfg.llm_retry_backoff)
    cfg.llm_retry_jitter = llm_retry_cfg.get("jitter", cfg.llm_retry_jitter)

    # ── env 覆盖 ──
    cfg.llm_first_token_ms = _env_int("CODEBUDDY_FIRST_TOKEN_TIMEOUT_MS", cfg.llm_first_token_ms)
    cfg.llm_stream_idle_ms = _env_int("CODEBUDDY_STREAM_TIMEOUT_MS", cfg.llm_stream_idle_ms)
    cfg.bash_default_ms = _env_int("BASH_DEFAULT_TIMEOUT_MS", cfg.bash_default_ms)
    cfg.bash_max_ms = _env_int("BASH_MAX_TIMEOUT_MS", cfg.bash_max_ms)
    cfg.mcp_connect_ms = _env_int("MCP_TIMEOUT", cfg.mcp_connect_ms)
    cfg.mcp_tool_ms = _env_int("MCP_TOOL_TIMEOUT", cfg.mcp_tool_ms)

    cfg.llm_retry_max = _env_int("LLM_RETRY_MAX", cfg.llm_retry_max)
    cfg.llm_retry_base_delay = _env_float("LLM_RETRY_BASE_DELAY", cfg.llm_retry_base_delay)
    cfg.llm_retry_max_delay = _env_float("LLM_RETRY_MAX_DELAY", cfg.llm_retry_max_delay)
    cfg.llm_retry_backoff = _env_float("LLM_RETRY_BACKOFF", cfg.llm_retry_backoff)
    cfg.llm_retry_jitter = _env_float("LLM_RETRY_JITTER", cfg.llm_retry_jitter)

    _instance = cfg
    return cfg


def get_timeout_config() -> TimeoutConfig:
    """获取超时配置单例（首次调用时加载）。"""
    return load_timeout_config()


def reset_timeout_config() -> None:
    """重置超时配置单例（用于测试或配置热更新）。"""
    global _instance
    _instance = None
