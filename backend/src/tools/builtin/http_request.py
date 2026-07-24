"""结构化 HTTP 请求工具 — httpx 实现，支持任意 method/headers/body/auth。

跨平台：httpx 三端通用，无平台特化需求。
安全：scheme 白名单（仅 http/https），拦截 file/ftp/javascript 等危险协议。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

from hello_agents.tools import Tool, ToolParameter, ToolResponse

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

_MAX_RESPONSE_SIZE = 50_000  # 50KB
_TRUNCATE_HEAD = 20_000
_TRUNCATE_TAIL = 10_000
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


class HttpRequestTool(Tool):
    """发送结构化 HTTP 请求，支持任意 method/headers/body/auth。

    用于调用 REST API、JSON 接口、webhook 等。
    自动处理 JSON 序列化、认证头注入、响应格式化。
    跨平台：Windows/Linux/macOS 通用。
    """

    def __init__(
        self,
        max_response_size: int = _MAX_RESPONSE_SIZE,
        default_timeout: float = 30.0,
    ):
        super().__init__(
            name="http_request",
            description=(
                "发送结构化 HTTP 请求（支持 GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS）。"
                "可自定义 headers、body、认证方式（basic/bearer）。"
                "body 为 dict 时自动 JSON 序列化。"
                "用于调用 REST API、JSON 接口、webhook 等。跨平台通用。"
            ),
            expandable=False,
        )
        self.max_response_size = max_response_size
        self.default_timeout = default_timeout
        # ContextGuard 元数据
        self.output_size_hint = 4000
        self.has_side_effects = False  # 外部调用，可委托

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="url",
                type="string",
                description="请求 URL（必须以 http:// 或 https:// 开头）",
                required=True,
            ),
            ToolParameter(
                name="method",
                type="string",
                description="HTTP 方法（GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS，默认 GET）",
                required=False,
            ),
            ToolParameter(
                name="headers",
                type="string",
                description="请求头 JSON 对象（如 {\"Content-Type\":\"application/json\",\"Authorization\":\"Bearer xxx\"}；可选）",
                required=False,
            ),
            ToolParameter(
                name="body",
                type="string",
                description="请求体（字符串或 JSON 对象；为 dict 时自动设置 Content-Type: application/json）",
                required=False,
            ),
            ToolParameter(
                name="auth_type",
                type="string",
                description="认证类型：none（默认）| basic | bearer",
                required=False,
            ),
            ToolParameter(
                name="auth_value",
                type="string",
                description="认证值（basic 时为 \"user:pass\"，bearer 时为 token）",
                required=False,
            ),
            ToolParameter(
                name="timeout",
                type="number",
                description="请求超时秒数（默认 30）",
                required=False,
            ),
            ToolParameter(
                name="return_format",
                type="string",
                description="响应格式：auto（默认，JSON 自动美化）| text（原样文本）| json（强制 JSON 解析）| headers_only（仅返回响应头）",
                required=False,
            ),
            ToolParameter(
                name="follow_redirects",
                type="boolean",
                description="是否跟随重定向（默认 true）",
                required=False,
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        if httpx is None:
            return ToolResponse.error(
                code="DEPENDENCY_MISSING",
                message="httpx 未安装，请运行 pip install httpx",
            )

        url = (parameters.get("url") or "").strip()
        if not url:
            return ToolResponse.error(code="INVALID_INPUT", message="url 不能为空")

        # scheme 白名单安全检查
        parsed = urlparse(url)
        if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
            return ToolResponse.error(
                code="UNSUPPORTED_SCHEME",
                message=f"仅支持 http/https 协议， got: {parsed.scheme or '(空)'}",
            )

        method = (parameters.get("method") or "GET").strip().upper()
        if method not in _ALLOWED_METHODS:
            return ToolResponse.error(
                code="INVALID_METHOD",
                message=f"不支持的 HTTP 方法: {method}，允许: {', '.join(sorted(_ALLOWED_METHODS))}",
            )

        # 解析 headers
        headers = self._parse_headers(parameters.get("headers"))

        # 解析 body
        body, json_body = self._parse_body(parameters.get("body"), headers)

        # 认证
        auth = self._build_auth(
            parameters.get("auth_type"),
            parameters.get("auth_value"),
            headers,
        )

        # 超时钳制：接入 TimeoutConfig（http_request_default_ms / http_request_max_ms），
        # 防止 LLM 传入超大 timeout 长挂 agent 循环（M4）
        try:
            from ...core.timeouts import get_timeout_config
            _tc = get_timeout_config()
            default_timeout = _tc.http_request_default_ms / 1000
            max_timeout = _tc.http_request_max_ms / 1000
        except Exception:
            default_timeout = self.default_timeout
            max_timeout = 120.0

        try:
            raw_timeout = float(parameters.get("timeout") or default_timeout)
        except (TypeError, ValueError):
            raw_timeout = default_timeout
        # 钳制到 [1s, max_timeout]
        timeout = min(max(raw_timeout, 1.0), max_timeout)

        return_format = (parameters.get("return_format") or "auto").strip().lower()
        follow_redirects = bool(parameters.get("follow_redirects", True))

        # 硬性响应体读取上限（字节），防止超大/无限流式响应导致内存无界膨胀（M4）
        hard_byte_cap = self.max_response_size * 5

        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=follow_redirects,
            ) as client:
                request_kwargs: Dict[str, Any] = {
                    "method": method,
                    "url": url,
                    "headers": headers,
                }
                if json_body is not None:
                    request_kwargs["json"] = json_body
                elif body is not None:
                    request_kwargs["content"] = body
                if auth is not None:
                    request_kwargs["auth"] = auth

                # 流式读取响应体，累计到硬上限即中断，避免全量物化（M4）
                with client.stream(**request_kwargs) as response:
                    status_code = response.status_code
                    resp_headers = dict(response.headers)
                    content_type = resp_headers.get("content-type", "")
                    reason_phrase = response.reason_phrase

                    body_bytes = bytearray()
                    truncated_at_cap = False
                    for chunk in response.iter_bytes():
                        body_bytes.extend(chunk)
                        if len(body_bytes) >= hard_byte_cap:
                            truncated_at_cap = True
                            break
                    raw_body = body_bytes.decode("utf-8", errors="replace")

        except httpx.TimeoutException:
            return ToolResponse.error(
                code="TIMEOUT",
                message=f"请求超时（{timeout} 秒）",
            )
        except httpx.ConnectError as e:
            return ToolResponse.error(
                code="CONNECT_ERROR",
                message=f"连接失败: {e}",
            )
        except Exception as e:
            return ToolResponse.error(
                code="REQUEST_ERROR",
                message=f"请求失败: {e}",
            )

        # 格式化响应
        text, data = self._format_response(
            status_code, resp_headers, content_type, reason_phrase,
            raw_body, return_format, truncated_at_cap,
        )

        return ToolResponse.success(
            text=text,
            data=data,
        )

    def _parse_headers(self, raw: Any) -> Dict[str, str]:
        """解析 headers 参数（接受 dict 或 JSON 字符串）。"""
        if not raw:
            return {}
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError:
                pass
        return {}

    def _parse_body(
        self,
        raw: Any,
        headers: Dict[str, str],
    ) -> tuple[Optional[Union[str, bytes]], Optional[Any]]:
        """解析 body 参数。

        Returns:
            (raw_body, json_body)：json_body 非 None 时用 json= 参数，
            否则用 content= 参数。
        """
        if raw is None:
            return None, None

        if isinstance(raw, dict):
            # dict → 自动 JSON 序列化
            if "Content-Type" not in headers and "content-type" not in headers:
                headers["Content-Type"] = "application/json"
            return None, raw

        if isinstance(raw, str):
            # 尝试解析为 JSON
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, (dict, list)):
                    if "Content-Type" not in headers and "content-type" not in headers:
                        headers["Content-Type"] = "application/json"
                    return None, parsed
            except json.JSONDecodeError:
                pass
            return raw, None

        return str(raw), None

    def _build_auth(
        self,
        auth_type: Any,
        auth_value: Any,
        headers: Dict[str, str],
    ) -> Optional[httpx.Auth]:
        """构建认证对象或注入认证头。

        - basic: 返回 httpx.BasicAuth 对象
        - bearer: 直接注入 Authorization 头（httpx 无 BearerTokenAuth 类）
        """
        if not auth_type or str(auth_type).lower() == "none":
            return None

        auth_type_str = str(auth_type).strip().lower()
        value = str(auth_value or "").strip()

        if auth_type_str == "bearer":
            if not value:
                return None
            # 直接注入 Authorization 头
            headers["Authorization"] = f"Bearer {value}"
            return None

        if auth_type_str == "basic":
            if not value or ":" not in value:
                return None
            user, _, password = value.partition(":")
            return httpx.BasicAuth(user, password)

        return None

    def _format_response(
        self,
        status_code: int,
        resp_headers: Dict[str, str],
        content_type: str,
        reason_phrase: str,
        raw_body: str,
        return_format: str,
        truncated_at_cap: bool,
    ) -> tuple[str, dict]:
        """格式化 HTTP 响应。"""
        if return_format == "headers_only":
            header_lines = [f"{k}: {v}" for k, v in resp_headers.items()]
            text = f"HTTP {status_code}\n" + "\n".join(header_lines)
            return text, {
                "status_code": status_code,
                "headers": resp_headers,
                "content_length": 0,
            }

        # 归一化换行符
        raw_body = raw_body.replace("\r\n", "\n").replace("\r", "\n")

        is_json = "json" in content_type.lower()
        formatted_body = raw_body

        if return_format == "json" or (return_format == "auto" and is_json):
            try:
                parsed = json.loads(raw_body)
                formatted_body = json.dumps(parsed, ensure_ascii=False, indent=2)
            except (json.JSONDecodeError, ValueError):
                if return_format == "json":
                    formatted_body = f"(JSON 解析失败，原样输出)\n{raw_body}"

        # 截断（展示层）
        truncated = truncated_at_cap
        if len(formatted_body) > self.max_response_size:
            formatted_body = (
                formatted_body[:_TRUNCATE_HEAD]
                + f"\n\n... 已截断（共 {len(formatted_body)} 字符）...\n\n"
                + formatted_body[-_TRUNCATE_TAIL:]
            )
            truncated = True
        elif truncated_at_cap:
            formatted_body += "\n\n... (响应体达到硬上限已停止读取) ..."

        # 构建输出文本
        parts = [
            f"HTTP {status_code} {reason_phrase}",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(raw_body)}",
            "",
            formatted_body,
        ]

        text = "\n".join(parts)
        data = {
            "status_code": status_code,
            "headers": resp_headers,
            "content_type": content_type,
            "content_length": len(raw_body),
            "truncated": truncated,
            "truncated_at_cap": truncated_at_cap,
        }

        return text, data

    def close(self) -> None:
        """清理资源（agent shutdown 时调用）。"""
        # httpx.Client 使用 with 语句自动清理，无需额外操作
        pass
