"""
协议工具集合

提供基于协议实现的工具接口：
- MCP Tool: 基于 fastmcp 库，用于连接和调用 MCP 服务器
"""

from typing import Dict, Any, List, Optional, TYPE_CHECKING
from hello_agents.tools import Tool, ToolParameter, ToolResponse, ToolErrorCode
from hello_agents.tools.response import ToolStatus
import os

if TYPE_CHECKING:
    from hello_agents.tools.registry import ToolRegistry


# GitHub 托管 MCP（与 Cursor 一致，工具数多于已弃用的 npm server-github）
GITHUB_HOSTED_MCP_URL = "https://api.githubcopilot.com/mcp/"

# MCP服务器环境变量映射表
MCP_SERVER_ENV_MAP = {
    "server-github": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
    "github-mcp-server": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
    "server-slack": ["SLACK_BOT_TOKEN", "SLACK_TEAM_ID"],
    "server-google-drive": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"],
    "server-postgres": ["POSTGRES_CONNECTION_STRING"],
    "server-sqlite": [],
    "server-filesystem": [],
}


def reset_all_mcp_disclosed_tools(registry: "ToolRegistry") -> None:
    """清理 registry 中所有 MCP 网关已动态披露的子工具。"""
    for tool in registry.get_all_tools():
        if isinstance(tool, MCPTool) and not tool.auto_expand:
            tool.reset_disclosed_tools()


class MCPTool(Tool):
    """MCP (Model Context Protocol) 工具

    渐进披露模式（auto_expand=False，默认）：
    - 初始化时发现远端工具并生成 Skill 风格目录描述
    - 仅注册网关本身，不展开子工具
    - Agent 调用 enable_tools 后，子工具动态加入 ToolRegistry

    全量展开模式（auto_expand=True）：启动时展开全部子工具（旧行为）。
    """

    def __init__(
        self,
        name: str = "mcp",
        description: Optional[str] = None,
        server_command: Optional[List[str]] = None,
        server_url: Optional[str] = None,
        server_args: Optional[List[str]] = None,
        server: Optional[Any] = None,
        transport_type: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        auto_expand: bool = False,
        env: Optional[Dict[str, str]] = None,
        env_keys: Optional[List[str]] = None,
        tool_registry: Optional["ToolRegistry"] = None,
    ):
        self.server_command = server_command
        self.server_url = (server_url or "").strip() or None
        self.server_args = server_args or []
        self.server = server
        self.transport_type = transport_type
        self.headers = headers
        self._client = None
        self._available_tools: List[Dict[str, Any]] = []
        self._gateway_name = name
        self.auto_expand = auto_expand
        self.tool_registry = tool_registry
        self._disclosed_tools: Dict[str, "Tool"] = {}
        self._max_disclosed = int(os.getenv("MCP_MAX_DISCLOSED_TOOLS", "20"))

        # 渐进模式与全量展开模式共用前缀，便于命名一致
        self.prefix = f"mcp_{name}_"
        self._wrapped_prefix = self.prefix

        self.env = self._prepare_env(env, env_keys, server_command, self.server_url)
        self.headers = self._prepare_headers(self.headers, env_keys, self.env)

        if not server_command and not self.server_url and not server:
            self.server = self._create_builtin_server()

        if server_command and "@modelcontextprotocol/server-github" in " ".join(
            server_command
        ):
            print(
                "ℹ️ MCP: @modelcontextprotocol/server-github 已弃用且工具较少（约 26 个）。"
                f" 建议在 config.json 改用 server_url: {GITHUB_HOSTED_MCP_URL}"
            )

        self._discover_tools()

        if description is None:
            description = self._generate_description()

        super().__init__(
            name=name,
            description=description,
            expandable=auto_expand,
        )

    def _prepare_headers(
        self,
        headers: Optional[Dict[str, str]],
        env_keys: Optional[List[str]],
        env: Dict[str, str],
    ) -> Dict[str, str]:
        result: Dict[str, str] = dict(headers or {})
        if "Authorization" in result:
            return result
        token = env.get("GITHUB_PERSONAL_ACCESS_TOKEN")
        if token:
            result["Authorization"] = f"Bearer {token}"
        return result

    def _mcp_client_kwargs(self) -> Dict[str, Any]:
        kw: Dict[str, Any] = {}
        if self.transport_type:
            kw["transport_type"] = self.transport_type
        if self.headers:
            kw["headers"] = self.headers
        if self.server_url and "Authorization" not in self.headers:
            token = self.env.get("GITHUB_PERSONAL_ACCESS_TOKEN")
            if token:
                kw["auth"] = token
        return kw

    def _client_source(self):
        if self.server:
            return self.server
        if self.server_url:
            return self.server_url
        return self.server_command

    def _prepare_env(
        self,
        env: Optional[Dict[str, str]],
        env_keys: Optional[List[str]],
        server_command: Optional[List[str]],
        server_url: Optional[str] = None,
    ) -> Dict[str, str]:
        result_env: Dict[str, str] = {}

        if server_url and "githubcopilot.com" in server_url:
            key = "GITHUB_PERSONAL_ACCESS_TOKEN"
            value = os.getenv(key)
            if value:
                result_env[key] = value

        if server_command:
            server_name = None
            for part in server_command:
                if "server-" in part:
                    server_name = part.split("/")[-1] if "/" in part else part
                    break
            if server_name and server_name in MCP_SERVER_ENV_MAP:
                for key in MCP_SERVER_ENV_MAP[server_name]:
                    value = os.getenv(key)
                    if value:
                        result_env[key] = value
                        print(f"🔑 自动加载环境变量: {key}")

        if env_keys:
            for key in env_keys:
                value = os.getenv(key)
                if value:
                    result_env[key] = value
                    print(f"🔑 从env_keys加载环境变量: {key}")
                else:
                    print(f"⚠️  警告: 环境变量 {key} 未设置")

        if env:
            result_env.update(env)
            for key in env.keys():
                print(f"🔑 使用直接传递的环境变量: {key}")

        return result_env

    def _create_builtin_server(self):
        try:
            from fastmcp import FastMCP

            server = FastMCP("HelloAgents-BuiltinServer")

            @server.tool()
            def add(a: float, b: float) -> float:
                """加法计算器"""
                return a + b

            @server.tool()
            def subtract(a: float, b: float) -> float:
                """减法计算器"""
                return a - b

            @server.tool()
            def multiply(a: float, b: float) -> float:
                """乘法计算器"""
                return a * b

            @server.tool()
            def divide(a: float, b: float) -> float:
                """除法计算器"""
                if b == 0:
                    raise ValueError("除数不能为零")
                return a / b

            @server.tool()
            def greet(name: str = "World") -> str:
                """友好问候"""
                return f"Hello, {name}! 欢迎使用 HelloAgents MCP 工具！"

            @server.tool()
            def get_system_info() -> dict:
                """获取系统信息"""
                import platform
                import sys
                return {
                    "platform": platform.system(),
                    "python_version": sys.version,
                    "server_name": "HelloAgents-BuiltinServer",
                    "tools_count": 6,
                }

            return server
        except ImportError:
            raise ImportError(
                "创建内置 MCP 服务器需要 fastmcp 库。请安装: pip install fastmcp"
            )

    def _discover_tools(self) -> None:
        try:
            from ...mcp.client import MCPClient
            import asyncio

            async def discover():
                async with MCPClient(
                    self._client_source(),
                    self.server_args,
                    env=self.env,
                    **self._mcp_client_kwargs(),
                ) as client:
                    return await client.list_tools()

            try:
                asyncio.get_running_loop()
                import concurrent.futures

                def run_in_thread():
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    try:
                        return new_loop.run_until_complete(discover())
                    finally:
                        new_loop.close()

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    self._available_tools = executor.submit(run_in_thread).result()
            except RuntimeError:
                self._available_tools = asyncio.run(discover())
        except Exception:
            self._available_tools = []

    def _tool_catalog_lines(self) -> List[str]:
        lines: List[str] = []
        for tool in self._available_tools:
            remote_name = tool.get("name", "unknown")
            tool_desc = tool.get("description", "无描述")
            short_desc = tool_desc.split(".")[0] if tool_desc else "无描述"
            registered = f"{self._wrapped_prefix}{remote_name}"
            lines.append(f"  - {remote_name} → 披露后名称为 `{registered}`: {short_desc}")
        return lines

    def _generate_description(self) -> str:
        gateway = getattr(self, "name", None) or self._gateway_name
        if not self._available_tools:
            return (
                f"MCP 外部系统网关（{gateway}）。"
                "连接失败或未发现在远端工具。"
                "可尝试 action=list_tools 刷新；"
                '披露工具: {"action":"enable_tools","tool_names":["远端工具名"]}。'
            )

        if self.auto_expand:
            return (
                f"MCP 工具服务器，包含 {len(self._available_tools)} 个工具。"
                "这些工具会在启动时自动展开为独立工具。"
            )

        catalog = "\n".join(self._tool_catalog_lines())
        disclosed = ", ".join(self.list_disclosed_tools()) or "（尚无）"
        return f"""MCP 外部系统网关（{gateway}）— 按需披露远端工具（类似 Skill 加载）。

可用远端工具（共 {len(self._available_tools)} 个）：
{catalog}

何时使用：
- 任务需要此外部系统能力，且内置工具无法满足时
- 从上方目录选定远端工具名，先披露再调用

如何使用（推荐两阶段）：
1. 披露：{{"action":"enable_tools","tool_names":["远端工具名",...]}}
2. 调用：下一轮直接使用披露后的工具名（如 `{self._wrapped_prefix}工具名`）并传入参数

单次快捷（无需等待下一轮）：{{"action":"enable_and_call","tool_name":"远端工具名","arguments":{{...}}}}

兜底：list_tools / call_tool / list_resources / read_resource / list_prompts / get_prompt

本会话已披露：{disclosed}"""

    def _find_remote_tool(self, remote_name: str) -> Optional[Dict[str, Any]]:
        for tool in self._available_tools:
            if tool.get("name") == remote_name:
                return tool
        return None

    def _schema_summary(self, wrapped: "Tool") -> str:
        try:
            params = wrapped.get_parameters()
        except Exception:
            params = []
        if not params:
            return "  （无参数）"
        parts = []
        for p in params:
            req = "必填" if getattr(p, "required", True) else "可选"
            parts.append(f"    - {p.name} ({p.type}, {req}): {p.description or ''}")
        return "\n".join(parts)

    def list_disclosed_tools(self) -> List[str]:
        return sorted(self._disclosed_tools.keys())

    def reset_disclosed_tools(self) -> None:
        if not self.tool_registry:
            self._disclosed_tools.clear()
            return
        for registered_name in list(self._disclosed_tools.keys()):
            self.tool_registry.unregister(registered_name)
        self._disclosed_tools.clear()

    def enable_tools(self, tool_names: List[str]) -> ToolResponse:
        if not tool_names:
            return ToolResponse.error(
                code=ToolErrorCode.INVALID_PARAM,
                message="必须指定 tool_names（远端工具名列表）",
                context={"tool_names": tool_names},
            )
        if not self.tool_registry:
            return ToolResponse.error(
                code=ToolErrorCode.INTERNAL_ERROR,
                message="MCP 网关未绑定 ToolRegistry，无法披露子工具",
            )

        if self.auto_expand:
            return ToolResponse.error(
                code=ToolErrorCode.INVALID_PARAM,
                message="全量展开模式下无需 enable_tools，请直接调用已展开的工具",
            )

        from .mcp_wrapper_tool import MCPWrappedTool

        enabled: List[str] = []
        skipped: List[str] = []
        errors: List[str] = []
        schema_blocks: List[str] = []

        for remote_name in tool_names:
            if not isinstance(remote_name, str) or not remote_name.strip():
                errors.append(f"无效工具名: {remote_name!r}")
                continue
            remote_name = remote_name.strip()
            registered_name = f"{self._wrapped_prefix}{remote_name}"

            if registered_name in self._disclosed_tools:
                skipped.append(registered_name)
                wrapped = self._disclosed_tools[registered_name]
                schema_blocks.append(f"- {registered_name}（已披露）:\n{self._schema_summary(wrapped)}")
                continue

            if len(self._disclosed_tools) >= self._max_disclosed:
                errors.append(
                    f"已达本会话披露上限 ({self._max_disclosed})，请先切换会话或增大 MCP_MAX_DISCLOSED_TOOLS"
                )
                break

            tool_info = self._find_remote_tool(remote_name)
            if not tool_info:
                available = ", ".join(t.get("name", "?") for t in self._available_tools[:10])
                errors.append(f"远端工具 '{remote_name}' 不存在。可用示例: {available}")
                continue

            wrapped = MCPWrappedTool(
                mcp_tool=self,
                tool_info=tool_info,
                prefix=self._wrapped_prefix,
            )
            self.tool_registry.register_tool(wrapped, auto_expand=False)
            self._disclosed_tools[registered_name] = wrapped
            enabled.append(registered_name)
            schema_blocks.append(f"- {registered_name}:\n{self._schema_summary(wrapped)}")

        if not enabled and not skipped and errors:
            return ToolResponse.error(
                code=ToolErrorCode.NOT_FOUND,
                message="未能披露任何工具：" + "; ".join(errors),
                context={"tool_names": tool_names, "errors": errors},
            )

        summary_parts = [
            f"<mcp-tools-enabled server=\"{self._gateway_name}\">",
            f"新披露: {', '.join(enabled) if enabled else '无'}",
            f"已存在: {', '.join(skipped) if skipped else '无'}",
        ]
        if errors:
            summary_parts.append(f"错误: {'; '.join(errors)}")
        if schema_blocks:
            summary_parts.append("\n参数 schema 摘要：\n" + "\n".join(schema_blocks))
        summary_parts.append(
            f"\n✅ 请在下一轮直接使用披露后的工具名（如 `{self._wrapped_prefix}...`）进行调用。"
        )
        summary_parts.append("</mcp-tools-enabled>")

        return ToolResponse.success(
            text="\n".join(summary_parts),
            data={
                "enabled": enabled,
                "skipped": skipped,
                "errors": errors,
                "disclosed": self.list_disclosed_tools(),
            },
        )

    def get_expanded_tools(self) -> List["Tool"]:  # type: ignore
        if not self.auto_expand:
            return []

        from .mcp_wrapper_tool import MCPWrappedTool

        return [
            MCPWrappedTool(mcp_tool=self, tool_info=info, prefix=self.prefix)
            for info in self._available_tools
        ]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        action = (parameters.get("action") or "").lower()
        if not action and "tool_name" in parameters and "tool_names" not in parameters:
            action = "call_tool"
            parameters["action"] = action

        if action == "enable_tools":
            raw_names = parameters.get("tool_names") or parameters.get("tool_name")
            if isinstance(raw_names, str):
                tool_names = [raw_names]
            elif isinstance(raw_names, list):
                tool_names = raw_names
            else:
                tool_names = []
            return self.enable_tools(tool_names)

        if action == "enable_and_call":
            remote_name = parameters.get("tool_name") or ""
            arguments = parameters.get("arguments") or {}
            if not remote_name:
                return ToolResponse.error(
                    code=ToolErrorCode.INVALID_PARAM,
                    message="enable_and_call 需要 tool_name 和 arguments",
                    context={"params_input": parameters},
                )
            enable_result = self.enable_tools([remote_name])
            if enable_result.status != ToolStatus.SUCCESS:
                return enable_result
            return self.run({
                "action": "call_tool",
                "tool_name": remote_name,
                "arguments": arguments,
            })

        if not action:
            return ToolResponse.error(
                code=ToolErrorCode.INVALID_PARAM,
                message="必须指定 action 参数或 tool_name 参数",
                context={"params_input": parameters},
            )

        import asyncio
        from ...mcp.client import MCPClient

        try:

            async def run_mcp_operation() -> ToolResponse:
                async with MCPClient(
                    self._client_source(),
                    self.server_args,
                    env=self.env,
                    **self._mcp_client_kwargs(),
                ) as client:
                    if action == "list_tools":
                        tools = await client.list_tools()
                        if not tools:
                            return ToolResponse.success(
                                text="没有找到可用的工具",
                                data={"action": action, "tools_count": 0},
                            )
                        result = f"找到 {len(tools)} 个工具:\n"
                        for tool in tools:
                            result += f"- {tool['name']}: {tool['description']}\n"
                        return ToolResponse.success(
                            text=result,
                            data={"action": action, "tools_count": len(tools)},
                        )

                    if action == "call_tool":
                        tool_name = parameters.get("tool_name")
                        arguments = parameters.get("arguments", {})
                        if not tool_name:
                            return ToolResponse.error(
                                code=ToolErrorCode.INVALID_PARAM,
                                message="必须指定 tool_name 参数",
                                context={"params_input": parameters},
                            )
                        result = await client.call_tool(tool_name, arguments)
                        text = f"工具 '{tool_name}' 执行结果:\n{result}"
                        return ToolResponse.success(
                            text=text,
                            data={"action": action, "tool_name": tool_name, "result": result},
                        )

                    if action == "list_resources":
                        resources = await client.list_resources()
                        if not resources:
                            return ToolResponse.success(
                                text="没有找到可用的资源",
                                data={"action": action, "resources_count": 0},
                            )
                        result = f"找到 {len(resources)} 个资源:\n"
                        for resource in resources:
                            result += f"- {resource['uri']}: {resource['name']}\n"
                        return ToolResponse.success(
                            text=result,
                            data={"action": action, "resources_count": len(resources)},
                        )

                    if action == "read_resource":
                        uri = parameters.get("uri")
                        if not uri:
                            return ToolResponse.error(
                                code=ToolErrorCode.INVALID_PARAM,
                                message="必须指定 uri 参数",
                                context={"params_input": parameters},
                            )
                        content = await client.read_resource(uri)
                        return ToolResponse.success(
                            text=f"资源 '{uri}' 内容:\n{content}",
                            data={"action": action, "uri": uri},
                        )

                    if action == "list_prompts":
                        prompts = await client.list_prompts()
                        if not prompts:
                            return ToolResponse.success(
                                text="没有找到可用的提示词",
                                data={"action": action, "prompts_count": 0},
                            )
                        result = f"找到 {len(prompts)} 个提示词:\n"
                        for prompt in prompts:
                            result += f"- {prompt['name']}: {prompt['description']}\n"
                        return ToolResponse.success(
                            text=result,
                            data={"action": action, "prompts_count": len(prompts)},
                        )

                    if action == "get_prompt":
                        prompt_name = parameters.get("prompt_name")
                        prompt_arguments = parameters.get("prompt_arguments", {})
                        if not prompt_name:
                            return ToolResponse.error(
                                code=ToolErrorCode.INVALID_PARAM,
                                message="必须指定 prompt_name 参数",
                                context={"params_input": parameters},
                            )
                        messages = await client.get_prompt(prompt_name, prompt_arguments)
                        result = f"提示词 '{prompt_name}':\n"
                        for msg in messages:
                            result += f"[{msg['role']}] {msg['content']}\n"
                        return ToolResponse.success(
                            text=result,
                            data={"action": action, "prompt_name": prompt_name, "messages": messages},
                        )

                    return ToolResponse.error(
                        code=ToolErrorCode.INVALID_PARAM,
                        message=f"不支持的操作 '{action}'",
                        context={"params_input": parameters},
                    )

            try:
                try:
                    asyncio.get_running_loop()
                    import concurrent.futures

                    def run_in_thread():
                        new_loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(new_loop)
                        try:
                            return new_loop.run_until_complete(run_mcp_operation())
                        finally:
                            new_loop.close()

                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        return executor.submit(run_in_thread).result()
                except RuntimeError:
                    return asyncio.run(run_mcp_operation())
            except Exception as e:
                return ToolResponse.error(
                    code=ToolErrorCode.EXECUTION_ERROR,
                    message=f"异步操作失败: {str(e)}",
                    context={"params_input": parameters},
                )

        except Exception as e:
            return ToolResponse.error(
                code=ToolErrorCode.INTERNAL_ERROR,
                message=f"MCP 操作失败: {str(e)}",
                context={"params_input": parameters},
            )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description=(
                    "操作: enable_tools（推荐）, enable_and_call, list_tools, call_tool, "
                    "list_resources, read_resource, list_prompts, get_prompt"
                ),
                required=True,
            ),
            ToolParameter(
                name="tool_names",
                type="array",
                description="远端工具名列表（enable_tools 必填）",
                required=False,
            ),
            ToolParameter(
                name="tool_name",
                type="string",
                description="远端工具名（call_tool / enable_and_call 需要）",
                required=False,
            ),
            ToolParameter(
                name="arguments",
                type="object",
                description="工具参数（call_tool / enable_and_call 需要）",
                required=False,
            ),
            ToolParameter(
                name="uri",
                type="string",
                description="资源 URI（read_resource 需要）",
                required=False,
            ),
            ToolParameter(
                name="prompt_name",
                type="string",
                description="提示词名称（get_prompt 需要）",
                required=False,
            ),
            ToolParameter(
                name="prompt_arguments",
                type="object",
                description="提示词参数（get_prompt 可选）",
                required=False,
            ),
        ]
