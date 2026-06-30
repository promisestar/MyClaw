"""多模态消息桥接器。

让上游 ``MyClawAgent`` 可以把 OpenAI 多模态 list-content 注入对话，
而**完全不修改** ``hello_agents`` 的 ``Message`` Pydantic 模型与
``EnhancedSimpleAgent.run / arun_stream_with_tools`` 中已有的
``self.add_message(Message(input_text, "user"))`` 调用。

实现思路：
- 把 list-content 通过 ``encode_multimodal_content`` 编码为带魔术前缀
  + base64-json 的字符串，让 Pydantic ``content: str`` 校验通过。
- 编码前会**剥离掉 image_url 中的真实 base64 数据**，替换为 ``@FILE:<abs_path>``
  占位符。这样落盘到 sessions/*.json 与内存 history 里的只是一个短路径引用，
  不会被 token 计数器误算为几十万 tokens，也不会让会话文件膨胀到几十 MB。
- ``EnhancedSimpleAgent._build_messages`` 中在把 ``msg.content`` 塞进
  发给 OpenAI 的 ``messages`` 字典前调用 ``decode_multimodal_content``
  自动解码回原始 list-content，并把 ``@FILE:<path>`` 即时重新读盘构造为
  真实 data URL；同时剥离 ``_local_path`` 等私有字段。
- ``token_counter.count_text`` / ``context_manager._format_history_for_summary``
  等只看到 ``__MM_V1__:<短payload>::TEXT::<原始 text>`` 形式的字符串，
  token 估算与真实 text 长度接近，**不再被 base64 撑爆**。
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import os
from typing import Any, List

logger = logging.getLogger(__name__)

_MM_PREFIX = "__MM_V1__:"
_TEXT_HINT = "::TEXT::"
_FILE_REF_PREFIX = "@FILE:"

# image_url 真实加载时的最大体积上限（与默认 MULTIMODAL_MAX_IMAGE_MB 保持一致）。
# 这里只是 build_image_part / load_image_as_data_url 的压缩目标，与上传期硬上限无关。
_DEFAULT_IMAGE_MAX_MB = 5.0


def _strip_image_url_payload(content: list) -> list:
    """编码前的预处理：把 image_url.url 中的 ``data:...;base64,...`` 替换为
    ``@FILE:<abs_path>`` 占位符；URL 模式下保持 http(s) 原样不变。

    同时移除 part 中以下划线开头的私有字段（如 ``_local_path``）——这些字段
    本来就不该发给 LLM，剥离后字段更纯净。
    """
    cleaned: list = []
    for part in content:
        if not isinstance(part, dict):
            cleaned.append(part)
            continue
        if part.get("type") != "image_url":
            cleaned.append(part)
            continue
        # 深拷贝单 part，避免污染调用方传入的原对象（同一次请求中 LLM 仍要看到真实 base64）
        new_part = copy.deepcopy(part)
        image_url = new_part.get("image_url") or {}
        url = image_url.get("url") or ""
        local_path = new_part.pop("_local_path", None)

        if url.startswith("data:") and local_path:
            # 用文件路径引用替换 base64
            image_url["url"] = f"{_FILE_REF_PREFIX}{local_path}"
            new_part["image_url"] = image_url
        # 其它情况：
        # - url 是 http/https：原样保留（很短，不会撑爆历史）
        # - url 是 data: 但没有 _local_path：兜底保留原 url（极少见，比如用户手动传入 data url）
        cleaned.append(new_part)
    return cleaned


def _restore_image_url_payload(content: list) -> list:
    """解码后的后处理（仅在 ``_build_messages`` 调用前使用）：把 ``@FILE:<path>``
    占位符即时读盘转换为真实 data URL，让 LLM 能正常识别图片。

    任何 ``_xxx`` 私有字段也会被剥离，保证发给 OpenAI 的 part 字段干净。
    """
    # 延迟导入，避免循环依赖（image.py 已经被 multimodal/__init__ 暴露）
    from ..multimodal.image import ImagePartError, load_image_as_data_url

    restored: list = []
    for part in content:
        if not isinstance(part, dict):
            restored.append(part)
            continue
        # 剥离所有私有字段
        new_part = {k: v for k, v in part.items() if not (isinstance(k, str) and k.startswith("_"))}

        if new_part.get("type") == "image_url":
            image_url = dict(new_part.get("image_url") or {})
            url = image_url.get("url") or ""
            if isinstance(url, str) and url.startswith(_FILE_REF_PREFIX):
                local_path = url[len(_FILE_REF_PREFIX):]
                try:
                    image_url["url"] = load_image_as_data_url(
                        local_path, max_mb=_DEFAULT_IMAGE_MAX_MB
                    )
                except ImagePartError as exc:
                    # 文件丢失（用户删了 uploads/*）：降级为占位文本 part，
                    # 不阻断对话；保留原 ref 便于排查
                    logger.warning("图片即时加载失败: %s (%s)", local_path, exc)
                    restored.append({
                        "type": "text",
                        "text": f"[图片读取失败: {os.path.basename(local_path)} - {exc}]",
                    })
                    continue
                except Exception as exc:  # pragma: no cover - 兜底
                    logger.exception("图片即时加载异常: %s", local_path)
                    restored.append({
                        "type": "text",
                        "text": f"[图片处理异常: {os.path.basename(local_path)} - {exc}]",
                    })
                    continue
            new_part["image_url"] = image_url
        restored.append(new_part)
    return restored


def encode_multimodal_content(content: Any) -> str:
    """把 list-content 编码为字符串，使其能放入 hello_agents.Message。

    - str / None：原样返回
    - list[dict]：先把 image_url 中的 base64 替换为 ``@FILE:<path>`` 引用，
      再序列化为 ``{prefix}{base64-json}{TEXT_HINT}{原始文本拼接}``
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    # 关键：在序列化前先把 image_url 里的真实 base64 替换为路径引用，
    # 避免 sessions/*.json 文件膨胀 + token 计数被撑爆。
    persisted = _strip_image_url_payload(content)

    payload = base64.b64encode(
        json.dumps(persisted, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")

    # 拼接所有 text part 作为 token 估算与日志友好的尾巴
    text_pieces: List[str] = []
    for part in persisted:
        if isinstance(part, dict) and part.get("type") == "text":
            text_pieces.append(part.get("text") or "")
    text_hint = "".join(text_pieces)
    return f"{_MM_PREFIX}{payload}{_TEXT_HINT}{text_hint}"


def is_encoded_multimodal(content: Any) -> bool:
    return isinstance(content, str) and content.startswith(_MM_PREFIX)


def decode_multimodal_content(content: Any) -> Any:
    """解码 ``encode_multimodal_content`` 的产物；非编码内容原样返回。

    返回的 list-content 中 image_url 仍是 ``@FILE:<path>`` 引用形式——这是
    持久化层的中间表示。若需要真正发给 LLM 的 data URL，请使用
    ``decode_and_materialize_for_llm``（已在 ``_build_messages`` patch 中应用）。
    """
    if not is_encoded_multimodal(content):
        return content
    rest = content[len(_MM_PREFIX):]
    payload, _, _ = rest.partition(_TEXT_HINT)
    try:
        raw = base64.b64decode(payload.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        # 解码失败：回退为去掉前缀后的字符串，避免影响主流程
        return rest


def decode_and_materialize_for_llm(content: Any) -> Any:
    """专供 LLM 调用前使用：解码 + 把 @FILE: 占位即时还原为真实 data URL。

    Returns:
        - 编码字符串 → list[dict]（已还原 image_url 真实 URL）
        - 普通 str / list / None → 原样返回
    """
    decoded = decode_multimodal_content(content)
    if isinstance(decoded, list):
        return _restore_image_url_payload(decoded)
    return decoded


def install_simple_agent_multimodal_patch() -> None:
    """对 ``EnhancedSimpleAgent._build_messages`` 打补丁，让发给 LLM 的 messages
    中已被编码的多模态 content 自动解码回 list-content（并即时加载图片）。

    幂等：重复调用安全。
    """
    from . import enhanced_simple_agent as esa

    cls = esa.EnhancedSimpleAgent
    if getattr(cls, "_mm_patch_applied", False):
        return

    original_build_messages = cls._build_messages

    def patched_build_messages(self, input_text):  # type: ignore[no-untyped-def]
        # 在调用原方法前不能改 input_text（要保持 add_message 仍写入编码字符串），
        # 但原方法返回的 messages 列表中可能包含编码后的 content，统一在这里解码并即时加载图片。
        messages = original_build_messages(self, input_text)
        for item in messages:
            content = item.get("content")
            if is_encoded_multimodal(content):
                item["content"] = decode_and_materialize_for_llm(content)
        return messages

    cls._build_messages = patched_build_messages  # type: ignore[assignment]
    cls._mm_patch_applied = True
