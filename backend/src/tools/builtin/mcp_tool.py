"""
协议工具集合

提供基于协议实现的工具接口：
- MCP Tool: 基于 fastmcp 库，用于连接和调用 MCP 服务器
- A2A Tool: 基于官方 a2a 库，用于 Agent 间通信（需要安装 a2a）
- ANP Tool: 基于概念实现，用于服务发现和网络管理
"""

from typing import Dict, Any, List, Optional
from hello_agents.tools import Tool, ToolParameter, ToolResponse, ToolErrorCode
import os


# MCP服务器环境变量映射表
# 用于自动检测常见MCP服务器需要的环境变量
MCP_SERVER_ENV_MAP = {
    "server-github": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
    "server-slack": ["SLACK_BOT_TOKEN", "SLACK_TEAM_ID"],
    "server-google-drive": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"],
    "server-postgres": ["POSTGRES_CONNECTION_STRING"],
    "server-sqlite": [],  # 不需要环境变量
    "server-filesystem": [],  # 不需要环境变量
}


class MCPTool(Tool):
    """MCP (Model Context Protocol) 工具

    连接到 MCP 服务器并调用其提供的工具、资源和提示词。
    
    功能：
    - 列出服务器提供的工具
    - 调用服务器工具
    - 读取服务器资源
    - 获取提示词模板

    使用示例:
        >>> from hello_agents.tools.builtin import MCPTool
        >>>
        >>> # 方式1: 使用内置演示服务器
        >>> tool = MCPTool()  # 自动创建内置服务器
        >>> result = tool.run({"action": "list_tools"})
        >>>
        >>> # 方式2: 连接到外部 MCP 服务器
        >>> tool = MCPTool(server_command=["python", "examples/mcp_example.py"])
        >>> result = tool.run({"action": "list_tools"})
        >>>
        >>> # 方式3: 使用自定义 FastMCP 服务器
        >>> from fastmcp import FastMCP
        >>> server = FastMCP("MyServer")
        >>> tool = MCPTool(server=server)

    注意：使用 fastmcp 库，已包含在依赖中
    """
    
    def __init__(self,
                 name: str = "mcp",
                 description: Optional[str] = None,
                 server_command: Optional[List[str]] = None,
                 server_args: Optional[List[str]] = None,
                 server: Optional[Any] = None,
                 auto_expand: bool = True,
                 env: Optional[Dict[str, str]] = None,
                 env_keys: Optional[List[str]] = None):
        """
        初始化 MCP 工具

        Args:
            name: 工具名称（默认为"mcp"，建议为不同服务器指定不同名称）
            description: 工具描述（可选，默认为通用描述）
            server_command: 服务器启动命令（如 ["python", "server.py"]）
            server_args: 服务器参数列表
            server: FastMCP 服务器实例（可选，用于内存传输）
            auto_expand: 是否自动展开为独立工具（默认True）
            env: 环境变量字典（优先级最高，直接传递给MCP服务器）
            env_keys: 要从系统环境变量加载的key列表（优先级中等）

        环境变量优先级（从高到低）：
            1. 直接传递的env参数
            2. env_keys指定的环境变量
            3. 自动检测的环境变量（根据server_command）

        注意：如果所有参数都为空，将创建内置演示服务器

        示例：
            >>> # 方式1：直接传递环境变量（优先级最高）
            >>> github_tool = MCPTool(
            ...     name="github",
            ...     server_command=["npx", "-y", "@modelcontextprotocol/server-github"],
            ...     env={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_xxx"}
            ... )
            >>>
            >>> # 方式2：从.env文件加载指定的环境变量
            >>> github_tool = MCPTool(
            ...     name="github",
            ...     server_command=["npx", "-y", "@modelcontextprotocol/server-github"],
            ...     env_keys=["GITHUB_PERSONAL_ACCESS_TOKEN"]
            ... )
            >>>
            >>> # 方式3：自动检测（最简单，推荐）
            >>> github_tool = MCPTool(
            ...     name="github",
            ...     server_command=["npx", "-y", "@modelcontextprotocol/server-github"]
            ...     # 自动从环境变量加载GITHUB_PERSONAL_ACCESS_TOKEN
            ... )
        """
        self.server_command = server_command
        self.server_args = server_args or []
        self.server = server
        self._client = None
        self._available_tools = []
        self.auto_expand = auto_expand
        self.prefix = f"mcp_{name}_" if auto_expand else ""

        # 环境变量处理（优先级：env > env_keys > 自动检测）
        self.env = self._prepare_env(env, env_keys, server_command)

        # 如果没有指定任何服务器，创建内置演示服务器
        if not server_command and not server:
            self.server = self._create_builtin_server()

        # 自动发现工具
        self._discover_tools()

        # 设置默认描述或自动生成
        if description is None:
            description = self._generate_description()

        super().__init__(
            name=name,
            description=description,
            expandable=auto_expand
        )

    def _prepare_env(self,
                     env: Optional[Dict[str, str]],
                     env_keys: Optional[List[str]],
                     server_command: Optional[List[str]]) -> Dict[str, str]:
        """
        准备环境变量

        优先级：env > env_keys > 自动检测

        Args:
            env: 直接传递的环境变量字典
            env_keys: 要从系统环境变量加载的key列表
            server_command: 服务器命令（用于自动检测）

        Returns:
            合并后的环境变量字典
        """
        result_env = {}

        # 1. 自动检测（优先级最低）
        if server_command:
            # 从命令中提取服务器名称
            server_name = None
            for part in server_command:
                if "server-" in part:
                    # 提取类似 "@modelcontextprotocol/server-github" 中的 "server-github"
                    server_name = part.split("/")[-1] if "/" in part else part
                    break

            # 查找映射表
            if server_name and server_name in MCP_SERVER_ENV_MAP:
                auto_keys = MCP_SERVER_ENV_MAP[server_name]
                for key in auto_keys:
                    value = os.getenv(key)
                    if value:
                        result_env[key] = value
                        print(f"🔑 自动加载环境变量: {key}")

        # 2. env_keys指定的环境变量（优先级中等）
        if env_keys:
            for key in env_keys:
                value = os.getenv(key)
                if value:
                    result_env[key] = value
                    print(f"🔑 从env_keys加载环境变量: {key}")
                else:
                    print(f"⚠️  警告: 环境变量 {key} 未设置")

        # 3. 直接传递的env（优先级最高）
        if env:
            result_env.update(env)
            for key in env.keys():
                print(f"🔑 使用直接传递的环境变量: {key}")

        return result_env

    def _create_builtin_server(self):
        """创建内置演示服务器"""
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
                    "tools_count": 6
                }

            return server

        except ImportError:
            raise ImportError(
                "创建内置 MCP 服务器需要 fastmcp 库。请安装: pip install fastmcp"
            )

    def _discover_tools(self):
        """发现MCP服务器提供的所有工具"""
        try:
            from ...mcp.client import MCPClient
            import asyncio

            async def discover():
                client_source = self.server if self.server else self.server_command
                async with MCPClient(client_source, self.server_args, env=self.env) as client:
                    tools = await client.list_tools()
                    return tools

            # 运行异步发现
            try:
                loop = asyncio.get_running_loop()
                # 如果已有循环，在新线程中运行
                import concurrent.futures
                def run_in_thread():
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    try:
                        return new_loop.run_until_complete(discover())
                    finally:
                        new_loop.close()

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(run_in_thread)
                    self._available_tools = future.result()
            except RuntimeError:
                # 没有运行中的循环
                self._available_tools = asyncio.run(discover())

        except Exception as e:
            # 工具发现失败不影响初始化
            self._available_tools = []

    def _generate_description(self) -> str:
        """生成增强的工具描述"""
        if not self._available_tools:
            return "连接到 MCP 服务器，调用工具、读取资源和获取提示词。支持内置服务器和外部服务器。"

        if self.auto_expand:
            # 展开模式：简单描述
            return f"MCP工具服务器，包含{len(self._available_tools)}个工具。这些工具会自动展开为独立的工具供Agent使用。"
        else:
            # 非展开模式：详细描述
            desc_parts = [
                f"MCP工具服务器，提供{len(self._available_tools)}个工具："
            ]

            # 列出所有工具
            for tool in self._available_tools:
                tool_name = tool.get('name', 'unknown')
                tool_desc = tool.get('description', '无描述')
                # 简化描述，只取第一句
                short_desc = tool_desc.split('.')[0] if tool_desc else '无描述'
                desc_parts.append(f"  • {tool_name}: {short_desc}")

            # 添加调用格式说明
            desc_parts.append("\n调用格式：返回JSON格式的参数")
            desc_parts.append('{"action": "call_tool", "tool_name": "工具名", "arguments": {...}}')

            # 添加示例
            if self._available_tools:
                first_tool = self._available_tools[0]
                tool_name = first_tool.get('name', 'example')
                desc_parts.append(f'\n示例：{{"action": "call_tool", "tool_name": "{tool_name}", "arguments": {{...}}}}')

            return "\n".join(desc_parts)

    def get_expanded_tools(self) -> List['Tool']:  # type: ignore
        """
        获取展开的工具列表

        将MCP服务器的每个工具包装成独立的Tool对象

        Returns:
            Tool对象列表
        """
        if not self.auto_expand:
            return []

        from .mcp_wrapper_tool import MCPWrappedTool

        expanded_tools = []
        for tool_info in self._available_tools:
            wrapped_tool = MCPWrappedTool(
                mcp_tool=self,
                tool_info=tool_info,
                prefix=self.prefix
            )
            expanded_tools.append(wrapped_tool)

        return expanded_tools

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        """
        执行 MCP 操作

        Args:
            parameters: 包含以下参数的字典
                - action: 操作类型 (list_tools, call_tool, list_resources, read_resource, list_prompts, get_prompt)
                  如果不指定action但指定了tool_name，会自动推断为call_tool
                - tool_name: 工具名称（call_tool 需要）
                - arguments: 工具参数（call_tool 需要）
                - uri: 资源 URI（read_resource 需要）
                - prompt_name: 提示词名称（get_prompt 需要）
                - prompt_arguments: 提示词参数（get_prompt 可选）

        Returns:
            ToolResponse: 标准化工具响应（与 HelloAgents run_with_timing 协议一致）
        """
        import asyncio
        from ...mcp.client import MCPClient

        # 智能推断action：如果没有action但有tool_name，自动设置为call_tool
        action = (parameters.get("action") or "").lower()
        if not action and "tool_name" in parameters:
            action = "call_tool"
            parameters["action"] = action

        if not action:
            return ToolResponse.error(
                code=ToolErrorCode.INVALID_PARAM,
                message="必须指定 action 参数或 tool_name 参数",
                context={"params_input": parameters},
            )

        try:

            async def run_mcp_operation() -> ToolResponse:
                if self.server:
                    client_source = self.server
                else:
                    client_source = self.server_command

                async with MCPClient(client_source, self.server_args, env=self.env) as client:
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
                        future = executor.submit(run_in_thread)
                        return future.result()
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
        """获取工具参数定义"""
        return [
            ToolParameter(
                name="action",
                type="string",
                description="操作类型: list_tools, call_tool, list_resources, read_resource, list_prompts, get_prompt",
                required=True
            ),
            ToolParameter(
                name="tool_name",
                type="string",
                description="工具名称（call_tool 操作需要）",
                required=False
            ),
            ToolParameter(
                name="arguments",
                type="object",
                description="工具参数（call_tool 操作需要）",
                required=False
            ),
            ToolParameter(
                name="uri",
                type="string",
                description="资源 URI（read_resource 操作需要）",
                required=False
            ),
            ToolParameter(
                name="prompt_name",
                type="string",
                description="提示词名称（get_prompt 操作需要）",
                required=False
            ),
            ToolParameter(
                name="prompt_arguments",
                type="object",
                description="提示词参数（get_prompt 操作可选）",
                required=False
            )
        ]