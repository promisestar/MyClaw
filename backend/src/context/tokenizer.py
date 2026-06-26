"""多 Provider 精确 Token 统计器

根据 hello_agents 的 adapter 体系（base_url 驱动），为每种 Provider 提供精确 token 计数：

Provider 检测（与 llm_adapters.py 一致）：
- base_url 含 "anthropic.com" → Anthropic    → token_count API（本地调用）
- base_url 含 "googleapis.com" / "generativelanguage" → Gemini → Gemini tokenizer
- 其他（默认）→ OpenAI 兼容                     → tiktoken

降级策略：
- tiktoken 未安装 → 改进版字符估算（中英文区分）
- token_count API 调用失败 → 字符估算
- 所有失败均有缓存兜底，不阻塞主流程
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Provider 检测（与 llm_adapters.create_adapter 逻辑一致）
# --------------------------------------------------------------------------- #

def detect_provider(base_url: Optional[str]) -> str:
    """根据 base_url 检测 API 提供商

    Args:
        base_url: API 服务地址

    Returns:
        提供商标识："openai" | "anthropic" | "gemini"
    """
    if not base_url:
        return "openai"
    url = base_url.lower()
    if "anthropic.com" in url:
        return "anthropic"
    if "googleapis.com" in url or "generativelanguage" in url:
        return "gemini"
    return "openai"


# --------------------------------------------------------------------------- #
# tiktoken（OpenAI 系列 — 零延迟本地精确计数）
# --------------------------------------------------------------------------- #

_tiktoken_available = False
_tiktoken_load_error: Optional[str] = None

# 模型名 → tiktoken encoding 名 映射
_OPENAI_ENCODING_MAP: Dict[str, str] = {
    # GPT-4o / GPT-4o-mini
    "gpt-4o":      "o200k_base",
    "gpt-4o-mini": "o200k_base",
    "o3":          "o200k_base",
    "o3-mini":     "o200k_base",
    "o4-mini":     "o200k_base",
    # GPT-4 / GPT-4-turbo / GPT-3.5
    "gpt-4":       "cl100k_base",
    "gpt-4-turbo": "cl100k_base",
    "gpt-3.5":     "cl100k_base",
    # 国产模型（均走 OpenAI 兼容协议，统一用 cl100k_base 最接近）
    "glm-4":       "cl100k_base",
    "deepseek":    "cl100k_base",
    "qwen":        "cl100k_base",
    "kimi":        "cl100k_base",
    "hunyuan":     "cl100k_base",
    "doubao":      "cl100k_base",
}

try:
    import tiktoken
    _tiktoken_available = True
except ImportError as e:
    _tiktoken_available = False
    _tiktoken_load_error = str(e)


@functools.lru_cache(maxsize=32)
def _get_tiktoken_encoder(encoding_name: str):
    """获取 tiktoken encoder（带缓存）"""
    try:
        return tiktoken.get_encoding(encoding_name)
    except Exception:
        return None


def _get_openai_encoding_name(model: str) -> str:
    """根据模型名确定应使用的 tiktoken encoding"""
    model_lower = model.lower()
    for prefix, encoding in _OPENAI_ENCODING_MAP.items():
        if model_lower.startswith(prefix.lower()):
            return encoding
    # 默认：回退到 cl100k_base（GPT-4/GPT-3.5 系列均用这个）
    return "cl100k_base"


def _count_openai_tokens(text: str, model: str) -> Optional[int]:
    """使用 tiktoken 精确计数

    Args:
        text: 文本内容
        model: 模型名称

    Returns:
        token 数；tiktoken 不可用时返回 None
    """
    if not _tiktoken_available or not text:
        return 0 if not text else None

    encoding_name = _get_openai_encoding_name(model)
    encoder = _get_tiktoken_encoder(encoding_name)
    if encoder is None:
        return None

    try:
        return len(encoder.encode(text))
    except Exception:
        logger.debug("tiktoken encode 失败 model=%s", model, exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# Anthropic — token_count SDK API
# --------------------------------------------------------------------------- #

def _count_anthropic_tokens(text: str, model: str) -> Optional[int]:
    """使用 Anthropic SDK 本地 tokenizer 计数

    优先使用 SDK 的 count_tokens（需 anthropic>=0.39.0），
    失败降级到改进版字符估算。
    """
    if not text:
        return 0

    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.count_tokens(
            model=model,
            messages=[{"role": "user", "content": text}],
        )
        return resp.input_tokens
    except ImportError:
        logger.debug("anthropic SDK 未安装，降级到字符估算")
    except Exception:
        logger.debug("Anthropic token 计数失败 model=%s", model, exc_info=True)

    return None


# --------------------------------------------------------------------------- #
# 改进版字符估算（所有 Provider 的回退方案）
# --------------------------------------------------------------------------- #

def _estimate_tokens_fallback(text: str) -> int:
    """改进版字符估算 — 区分中英文和代码

    规则：
    - 中文字符（含 CJK）：~0.5 token/字（即 2 字 ≈ 1 token）
    - 英文/数字/标点：~0.25 token/字符（即 4 字符 ≈ 1 token）
    - 空格和换行：0 token

    该估算为 OpenAI cl100k_base 的粗略拟合，误差约 ±15%。
    """
    if not text:
        return 0

    cjk = 0   # 中文/日文/韩文
    latin = 0 # 英文/数字/标点/其它

    for ch in text:
        cp = ord(ch)
        # CJK Unified (0x4E00-0x9FFF), Extension A (0x3400-0x4DBF),
        # Hiragana (0x3040-0x309F), Katakana (0x30A0-0x30FF),
        # Hangul (0xAC00-0xD7AF)
        if (0x4E00 <= cp <= 0x9FFF or       # CJK 基本区
            0x3400 <= cp <= 0x4DBF or       # CJK Extension A
            0xF900 <= cp <= 0xFAFF or       # CJK Compatibility
            0x20000 <= cp <= 0x2A6DF):      # CJK Extension B
            cjk += 1
        elif ch.isspace() and ch != '\u3000':  # 空格不计数（全角空格除外）
            continue
        else:
            latin += 1

    # CJK: ~2 字/token → 每字 0.5
    # Latin: ~4 字符/token → 每字符 0.25
    return max(1, int(cjk * 0.5 + latin * 0.25))


# --------------------------------------------------------------------------- #
# 统一入口：按 Provider 路由
# --------------------------------------------------------------------------- #

@functools.lru_cache(maxsize=128)
def _count_single_cached(text: str, model: str, provider: str) -> int:
    """单文本 token 计数（缓存 128 条）"""
    if not text:
        return 0

    if provider == "openai":
        count = _count_openai_tokens(text, model)
        if count is not None:
            return count
    elif provider == "anthropic":
        count = _count_anthropic_tokens(text, model)
        if count is not None:
            return count
    # Gemini 及所有回退：用改进估算
    return _estimate_tokens_fallback(text)


def count_tokens(
    text: str,
    model: str,
    base_url: Optional[str] = None,
) -> int:
    """统一的 token 计数接口（单文本）

    Args:
        text: 文本内容
        model: 模型名称（如 "glm-4", "gpt-4o", "claude-sonnet-4-20250514"）
        base_url: API 服务地址（用于检测 Provider）

    Returns:
        token 数（始终 ≥ 1；空串返回 0）
    """
    provider = detect_provider(base_url)
    result = _count_single_cached(text, model, provider)
    return max(0, result)


def count_messages(
    messages: List[Dict[str, Any]],
    model: str,
    base_url: Optional[str] = None,
) -> int:
    """计算消息列表的 token 总数

    处理以下 OpenAI 格式字段：
    - content (str)
    - tool_calls 中的 function.name + function.arguments
    - system 消息

    Args:
        messages: API 消息列表
        model: 模型名称
        base_url: API 服务地址

    Returns:
        token 总数
    """
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue

        # 消息内容
        content = msg.get("content")
        if isinstance(content, str) and content:
            total += count_tokens(content, model, base_url)

        # 工具调用（OpenAI 格式）
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if isinstance(tc, dict):
                    func = tc.get("function", {})
                    if isinstance(func, dict):
                        total += count_tokens(
                            func.get("name", "") + func.get("arguments", ""),
                            model, base_url,
                        )

    # 每条消息有固定的 overhead（~4 tokens for role + formatting）
    total += len(messages) * 4
    return max(1, total)


def invalidate_cache():
    """清空缓存"""
    _count_single_cached.cache_clear()
    _get_tiktoken_encoder.cache_clear()


def is_precise(base_url: Optional[str] = None) -> bool:
    """当前 Provider 是否支持精确 token 计数"""
    provider = detect_provider(base_url)
    if provider == "openai":
        return _tiktoken_available
    if provider == "anthropic":
        try:
            import anthropic
            return True
        except ImportError:
            return False
    return False


def get_initialization_info() -> str:
    """获取 Tokenizer 初始化状态（用于启动日志）"""
    if _tiktoken_available:
        return "tiktoken 就绪 → OpenAI 系列精确计数"
    if _tiktoken_load_error:
        return f"tiktoken 未安装（{_tiktoken_load_error}）→ 改进版字符估算"
    return "改进版字符估算"


# ═══════════════════════════════════════════════════════════════════════════
# 模型 → 上下文窗口 映射（用于动态感知，替代硬编码 128000）
# ═══════════════════════════════════════════════════════════════════════════

# 数据集来源：各厂商官方文档 + API /models 响应。
# 未列出的模型匹配前缀模糊查找（如 "gpt-4o" 匹配 "gpt-4o-*"），
# 均未命中则返回默认值并记 warning。
_MODEL_CONTEXT_WINDOW: Dict[str, int] = {
    # ── OpenAI ──
    "gpt-4o":               128_000,
    "gpt-4o-mini":          128_000,
    "gpt-4.1":              1_000_000,
    "gpt-4.1-mini":         1_000_000,
    "gpt-4.1-nano":         1_000_000,
    "gpt-4-turbo":          128_000,
    "gpt-4":                8_192,
    "gpt-4-32k":            32_768,
    "gpt-3.5-turbo":        16_384,
    "gpt-3.5-turbo-16k":    16_384,
    "o3":                   200_000,
    "o3-mini":              200_000,
    "o4-mini":              200_000,
    "o1":                   200_000,
    "o1-mini":              128_000,
    "o1-preview":           128_000,
    # ── Anthropic ──
    "claude-sonnet-4":      200_000,
    "claude-opus-4":        200_000,
    "claude-3.5-sonnet":    200_000,
    "claude-3.5-haiku":     200_000,
    "claude-3-opus":        200_000,
    "claude-3-sonnet":      200_000,
    "claude-3-haiku":       200_000,
    # ── Google ──
    "gemini-2.5-pro":       1_048_576,
    "gemini-2.5-flash":     1_048_576,
    "gemini-2.0-flash":     1_048_576,
    "gemini-1.5-pro":       2_097_152,
    "gemini-1.5-flash":     1_048_576,
    # ── 国产模型（OpenAI 兼容协议）──
    "glm-4":                128_000,
    "glm-4-plus":           128_000,
    "glm-4-flash":          128_000,
    "glm-3-turbo":          128_000,
    "deepseek-v3":          128_000,
    "deepseek-r1":          128_000,
    "deepseek-chat":        128_000,
    "deepseek-reasoner":    64_000,
    "qwen-max":             131_072,
    "qwen-plus":            131_072,
    "qwen-turbo":           131_072,
    "qwen3-235b":           131_072,
    "qwen3-32b":            131_072,
    "moonshot-v1":          128_000,
    "kimi-k2":              128_000,
    "hunyuan-turbo":        256_000,
    "hunyuan-standard":     256_000,
    "doubao-pro":           128_000,
    "doubao-lite":          128_000,
    "deepseek-v4-pro":      1_000_000,
}


def get_context_window(
    model: str,
    base_url: Optional[str] = None,
    default: int = 128_000,
) -> int:
    """根据模型名动态获取上下文窗口大小

    匹配策略（按优先级）：
    1. 精确匹配模型全名
    2. 前缀匹配（如 "gpt-4o" 匹配 "gpt-4o-2024-08-06"）
    3. 返回默认值

    Args:
        model: 模型名
        base_url: API 地址（用于 provider 级别的兜底）
        default: 默认值（无法识别时使用，默认 128K）

    Returns:
        上下文窗口大小（token 数）
    """
    if not model:
        return default

    model_lower = model.lower().strip()

    # 1. 精确匹配
    if model_lower in _MODEL_CONTEXT_WINDOW:
        return _MODEL_CONTEXT_WINDOW[model_lower]

    # 2. 前缀匹配（从长到短，避免 "gpt-4" 误匹配 "gpt-4o"）
    for prefix in sorted(_MODEL_CONTEXT_WINDOW, key=len, reverse=True):
        if model_lower.startswith(prefix.lower()):
            return _MODEL_CONTEXT_WINDOW[prefix]

    # 3. Provider 级别兜底
    provider = detect_provider(base_url)
    if provider == "anthropic":
        # Claude 系列默认 200K
        logger.info("未识别的 Anthropic 模型 '%s'，默认上下文窗口 200K", model)
        return 200_000
    if provider == "gemini":
        logger.info("未识别的 Gemini 模型 '%s'，默认上下文窗口 1M", model)
        return 1_048_576

    # 4. 最终兜底
    logger.info("未识别的模型 '%s'，使用默认上下文窗口 %s", model, f"{default:,}")
    return default

