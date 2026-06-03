"""
增强的 MCP 客户端实现

支持多种传输方式的 MCP 客户端，用于教学和实际应用。

支持的传输方式：
1. Memory: 内存传输（用于测试，直接传递 FastMCP 实例）
2. Stdio: 标准输入输出传输（本地进程，Python/Node.js/npx 脚本）
3. HTTP: Streamable HTTP 传输（远程服务器，如 GitHub 托管 MCP）
4. SSE: Server-Sent Events 传输（实时通信）
"""

from __future__ import annotations

from typing import Dict, Any, List, Optional, Union, Tuple
import logging
import os
import shutil

logger = logging.getLogger(__name__)

try:
    from fastmcp import Client, FastMCP
    from fastmcp.client.transports import (
        PythonStdioTransport,
        SSETransport,
        StreamableHttpTransport,
        NpxStdioTransport,
        StdioTransport,
    )
    from fastmcp.client.transports.inference import infer_transport
    FASTMCP_AVAILABLE = True
except ImportError:
    FASTMCP_AVAILABLE = False
    Client = None
    FastMCP = None
    PythonStdioTransport = None
    SSETransport = None
    StreamableHttpTransport = None
    NpxStdioTransport = None
    StdioTransport = None
    infer_transport = None


def _resolve_stdio_command(command: str) -> str:
    """Windows 上 npx/node 等常需 .cmd 后缀。"""
    if os.name != "nt" or os.path.isabs(command) or command.endswith(".cmd"):
        return command
    if shutil.which(command):
        return command
    cmd_path = shutil.which(f"{command}.cmd")
    return cmd_path or command


def _parse_npx_command(
    command: List[str], extra_args: Optional[List[str]] = None
) -> Optional[Tuple[str, List[str]]]:
    """解析 ['npx','-y','@scope/pkg', ...] → (package, package_args)。"""
    if not command:
        return None
    exe = command[0].lower().removesuffix(".cmd")
    if exe != "npx" or len(command) < 2:
        return None

    args = list(command[1:])
    package: Optional[str] = None
    package_args: List[str] = []
    i = 0
    while i < len(args):
        flag = args[i]
        if flag in ("-y", "--yes"):
            i += 1
            continue
        if flag in ("--prefer-offline", "--no-install"):
            i += 1
            continue
        if package is None:
            package = args[i]
            i += 1
            package_args = args[i:]
            break
        i += 1

    if not package:
        return None
    if extra_args:
        package_args = list(extra_args) + package_args
    return package, package_args


def _normalize_tools(result: Any) -> List[Any]:
    if isinstance(result, list):
        return result
    if hasattr(result, "tools"):
        return result.tools or []
    return []


def _normalize_resources(result: Any) -> List[Any]:
    if isinstance(result, list):
        return result
    if hasattr(result, "resources"):
        return result.resources or []
    return []


def _normalize_prompts(result: Any) -> List[Any]:
    if isinstance(result, list):
        return result
    if hasattr(result, "prompts"):
        return result.prompts or []
    return []


class MCPClient:
    """MCP 客户端，支持多种传输方式"""

    def __init__(
        self,
        server_source: Union[str, List[str], FastMCP, Dict[str, Any]],
        server_args: Optional[List[str]] = None,
        transport_type: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        auth: Optional[str] = None,
        **transport_kwargs,
    ):
        """
        初始化 MCP 客户端

        Args:
            server_source: 服务器源（FastMCP 实例 / HTTP URL / 命令列表 / 配置字典）
            server_args: 附加命令行参数（stdio）
            transport_type: 强制传输类型 ("stdio", "http", "sse")
            env: 传给子进程的环境变量（stdio）
            headers: HTTP/SSE 请求头（远程 MCP 鉴权等）
            auth: HTTP Bearer token 或 httpx.Auth（远程 MCP）
            **transport_kwargs: 其他传输参数
        """
        if not FASTMCP_AVAILABLE:
            raise ImportError(
                "Enhanced MCP client requires the 'fastmcp' library (version 2.0+). "
                "Install it with: pip install fastmcp>=2.0.0"
            )

        self.server_args = server_args or []
        self.transport_type = transport_type
        self.env = env or {}
        self.headers = headers or {}
        self.auth = auth
        self.transport_kwargs = transport_kwargs
        self.server_source = self._prepare_server_source(server_source)
        self.client: Optional[Client] = None
        self._context_manager = None

    def _http_transport_kwargs(self) -> Dict[str, Any]:
        kw = dict(self.transport_kwargs)
        if self.headers:
            kw["headers"] = {**kw.get("headers", {}), **self.headers}
        if self.auth is not None and "auth" not in kw:
            kw["auth"] = self.auth
        return kw

    def _prepare_server_source(
        self, server_source: Union[str, List[str], FastMCP, Dict[str, Any]]
    ):
        if isinstance(server_source, FastMCP):
            logger.debug("MCP transport: memory (%s)", server_source.name)
            return server_source

        if isinstance(server_source, dict):
            logger.debug(
                "MCP transport: config (%s)",
                server_source.get("transport", "stdio"),
            )
            return self._create_transport_from_config(server_source)

        if isinstance(server_source, str) and server_source.startswith(("http://", "https://")):
            ttype = (self.transport_type or "http").lower()
            http_kw = self._http_transport_kwargs()
            logger.debug("MCP transport: %s %s", ttype, server_source)
            if ttype == "sse":
                return SSETransport(url=server_source, **http_kw)
            return StreamableHttpTransport(url=server_source, **http_kw)

        if isinstance(server_source, str) and server_source.endswith(".py"):
            logger.debug("MCP transport: stdio python %s", server_source)
            return PythonStdioTransport(
                script_path=server_source,
                args=self.server_args,
                env=self.env or None,
                **self.transport_kwargs,
            )

        if isinstance(server_source, list) and server_source:
            npx_parsed = _parse_npx_command(server_source, self.server_args)
            if npx_parsed and NpxStdioTransport is not None:
                package, pkg_args = npx_parsed
                logger.debug("MCP transport: npx %s", package)
                return NpxStdioTransport(
                    package=package,
                    args=pkg_args or None,
                    env_vars=self.env or None,
                    **self.transport_kwargs,
                )

            cmd = _resolve_stdio_command(server_source[0])
            args = server_source[1:] + self.server_args
            if (
                cmd.lower().removesuffix(".cmd") == "python"
                and args
                and args[0].endswith(".py")
            ):
                logger.debug("MCP transport: stdio python %s", args[0])
                return PythonStdioTransport(
                    script_path=args[0],
                    args=args[1:],
                    env=self.env or None,
                    **self.transport_kwargs,
                )

            logger.debug("MCP transport: stdio %s %s", cmd, " ".join(args))
            return StdioTransport(
                command=cmd,
                args=args,
                env=self.env or None,
                **self.transport_kwargs,
            )

        if infer_transport is not None:
            try:
                inferred = infer_transport(server_source)
                logger.debug("MCP transport: inferred %s", type(inferred).__name__)
                return inferred
            except (ValueError, TypeError):
                pass

        logger.debug("MCP transport: passthrough %r", server_source)
        return server_source

    def _create_transport_from_config(self, config: Dict[str, Any]):
        transport_type = config.get("transport", "stdio")

        if transport_type == "stdio":
            args = list(config.get("args", [])) + self.server_args
            env = config.get("env") or self.env or None
            cwd = config.get("cwd")
            command = config.get("command", "python")

            npx_cmd = [command] + args if command == "npx" else None
            if command == "npx" or (args and _parse_npx_command([command] + args)):
                parsed = _parse_npx_command([command] + args, self.server_args)
                if parsed and NpxStdioTransport is not None:
                    package, pkg_args = parsed
                    return NpxStdioTransport(
                        package=package,
                        args=pkg_args or None,
                        env_vars=env,
                        cwd=cwd,
                        **self.transport_kwargs,
                    )

            if args and args[0].endswith(".py"):
                return PythonStdioTransport(
                    script_path=args[0],
                    args=args[1:],
                    env=env,
                    cwd=cwd,
                    **self.transport_kwargs,
                )
            return StdioTransport(
                command=_resolve_stdio_command(command),
                args=args,
                env=env,
                cwd=cwd,
                **self.transport_kwargs,
            )

        if transport_type == "sse":
            http_kw = self._http_transport_kwargs()
            return SSETransport(
                url=config["url"],
                headers=config.get("headers"),
                auth=config.get("auth"),
                **http_kw,
            )

        if transport_type == "http":
            http_kw = self._http_transport_kwargs()
            return StreamableHttpTransport(
                url=config["url"],
                headers=config.get("headers"),
                auth=config.get("auth"),
                **http_kw,
            )

        raise ValueError(f"Unsupported transport type: {transport_type}")

    async def __aenter__(self):
        logger.debug("Connecting to MCP server...")
        self.client = Client(self.server_source)
        self._context_manager = self.client
        await self._context_manager.__aenter__()
        logger.debug("MCP connected")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._context_manager:
            await self._context_manager.__aexit__(exc_type, exc_val, exc_tb)
            self.client = None
            self._context_manager = None
        logger.debug("MCP disconnected")

    async def list_tools(self) -> List[Dict[str, Any]]:
        if not self.client:
            raise RuntimeError("Client not connected. Use 'async with client:' context manager.")

        result = await self.client.list_tools()
        tools = _normalize_tools(result)

        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": getattr(tool, "inputSchema", None) or {},
            }
            for tool in tools
        ]

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        if not self.client:
            raise RuntimeError("Client not connected. Use 'async with client:' context manager.")

        result = await self.client.call_tool(tool_name, arguments)

        if hasattr(result, "content") and result.content:
            if len(result.content) == 1:
                content = result.content[0]
                if hasattr(content, "text"):
                    return content.text
                if hasattr(content, "data"):
                    return content.data
            return [
                getattr(c, "text", getattr(c, "data", str(c)))
                for c in result.content
            ]
        if hasattr(result, "data") and result.data is not None:
            return result.data
        return None

    async def list_resources(self) -> List[Dict[str, Any]]:
        if not self.client:
            raise RuntimeError("Client not connected. Use 'async with client:' context manager.")

        result = await self.client.list_resources()
        resources = _normalize_resources(result)
        return [
            {
                "uri": resource.uri,
                "name": resource.name or "",
                "description": resource.description or "",
                "mime_type": getattr(resource, "mimeType", None),
            }
            for resource in resources
        ]

    async def read_resource(self, uri: str) -> Any:
        if not self.client:
            raise RuntimeError("Client not connected. Use 'async with client:' context manager.")

        result = await self.client.read_resource(uri)

        if hasattr(result, "contents") and result.contents:
            if len(result.contents) == 1:
                content = result.contents[0]
                if hasattr(content, "text"):
                    return content.text
                if hasattr(content, "blob"):
                    return content.blob
            return [
                getattr(c, "text", getattr(c, "blob", str(c)))
                for c in result.contents
            ]
        return None

    async def list_prompts(self) -> List[Dict[str, Any]]:
        if not self.client:
            raise RuntimeError("Client not connected. Use 'async with client:' context manager.")

        result = await self.client.list_prompts()
        prompts = _normalize_prompts(result)
        return [
            {
                "name": prompt.name,
                "description": prompt.description or "",
                "arguments": getattr(prompt, "arguments", []),
            }
            for prompt in prompts
        ]

    async def get_prompt(
        self, prompt_name: str, arguments: Optional[Dict[str, str]] = None
    ) -> List[Dict[str, Any]]:
        if not self.client:
            raise RuntimeError("Client not connected. Use 'async with client:' context manager.")

        result = await self.client.get_prompt(prompt_name, arguments or {})

        if hasattr(result, "messages") and result.messages:
            return [
                {
                    "role": msg.role,
                    "content": (
                        getattr(msg.content, "text", str(msg.content))
                        if hasattr(msg.content, "text")
                        else str(msg.content)
                    ),
                }
                for msg in result.messages
            ]
        return []

    async def ping(self) -> bool:
        if not self.client:
            raise RuntimeError("Client not connected. Use 'async with client:' context manager.")
        try:
            await self.client.ping()
            return True
        except Exception:
            return False

    def get_transport_info(self) -> Dict[str, Any]:
        if not self.client:
            return {"status": "not_connected"}

        transport = getattr(self.client, "transport", None)
        if transport:
            return {
                "status": "connected",
                "transport_type": type(transport).__name__,
                "transport_info": str(transport),
            }
        return {"status": "unknown"}
