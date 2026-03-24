"""网页搜索工具 - 使用 Brave Search API 进行网络搜索"""

import os
import json
from typing import List, Dict, Any
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from hello_agents.tools import Tool, ToolParameter, ToolResponse, tool_action

try:
    from tavily import TavilyClient  # type: ignore
except Exception:  # pragma: no cover - 可选依赖
    TavilyClient = None  # type: ignore

try:
    # serpapi 包中 GoogleSearch 位于 serpapi.google_search 模块中，
    # 顶层 serpapi 不一定导出该符号。
    from serpapi.google_search import GoogleSearch  # type: ignore
except Exception:  # pragma: no cover - 可选依赖
    try:  # 兼容旧版本/不同封装
        from serpapi import GoogleSearch  # type: ignore
    except Exception:  # pragma: no cover - 可选依赖
        GoogleSearch = None  # type: ignore

class WebSearchTool(Tool):
    """网页搜索工具

    使用 Brave Search API 进行网络搜索。
    需要配置环境变量 BRAVE_API_KEY 或在初始化时传入 API key。
    """

    def __init__(
        self,
        api_key: str = None,
        tavily_key: str | None = None, # 扩展搜索功能
        serpapi_key: str | None = None, # 扩展搜索功能
        max_results: int = 5,
        timeout: int = 10,
    ):
        """初始化网页搜索工具

        Args:
            api_key: Brave Search API key，如未提供则从环境变量 BRAVE_API_KEY 读取
            max_results: 最大返回结果数，默认 5
            timeout: 请求超时时间（秒），默认 10
        """
        super().__init__(
            name="web_search",
            description="使用搜索引擎搜索网络信息, 支持 Tavily、SerpApi、BRAVE等后端",
            expandable=True
        )

        self.api_key = api_key or os.getenv("BRAVE_API_KEY")
        self.max_results = max_results
        self.timeout = timeout
        self._base_url = "https://api.search.brave.com/res/v1/web/search"

        self.tavily_key = tavily_key or os.getenv("TAVILY_API_KEY")
        self.serpapi_key = serpapi_key or os.getenv("SERPAPI_API_KEY")

        self.available_backends: list[str] = []
        self._setup_backends()


    def _setup_backends(self) -> None:
        if self.api_key:
            self.available_backends.append("brave")
        else:
            print("⚠️ BRAVE_API_KEY 未设置")
        if self.tavily_key and TavilyClient is not None:
            try:
                self.tavily_client = TavilyClient(api_key=self.tavily_key)
                self.available_backends.append("tavily")
                print("✅ Tavily 搜索引擎已初始化")
            except Exception as exc:  # pragma: no cover - 第三方库初始化失败
                print(f"⚠️ Tavily 初始化失败: {exc}")
        elif self.tavily_key:
            print("⚠️ 未安装 tavily-python，无法使用 Tavily 搜索")
        else:
            print("⚠️ TAVILY_API_KEY 未设置")

        if self.serpapi_key:
            if GoogleSearch is not None:
                self.available_backends.append("serpapi")
                print("✅ SerpApi 搜索引擎已初始化")
            else:
                print("⚠️ 未安装 google-search-results，无法使用 SerpApi 搜索")
        else:
            print("⚠️ SERPAPI_API_KEY 未设置")

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        """执行搜索（默认行为）"""
        query = parameters.get("query", "")
        count = parameters.get("count", self.max_results)
        return self._search(query, count)

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="query",
                type="string",
                description="搜索查询词",
                required=True
            ),
            ToolParameter(
                name="count",
                type="integer",
                description=f"返回结果数量，默认 {self.max_results}",
                required=False
            ),
        ]

    def _search(self, query: str, count: int = None) -> ToolResponse:
        """执行搜索的核心实现

        Args:
            query: 搜索查询
            count: 返回结果数量

        Returns:
            ToolResponse: 搜索结果
        """
        if not query:
            return ToolResponse.error(
                code="INVALID_INPUT",
                message="搜索查询不能为空"
            )

        if self.available_backends == []:
            return ToolResponse.error(
                code="MISSING_API_KEY",
                message="未配置任何搜索后端。请设置环境变量 TAVILY_API_KEY 或 SERPAPI_API_KEY 或在初始化时传入 tavily_key 或 serpapi_key 参数"
            )

        try:
            if "brave" in self.available_backends:
                results = self._search_with_brave(query, count)
            elif "tavily" in self.available_backends:
                results = self._search_with_tavily(query, count)
            elif "serpapi" in self.available_backends:
                results = self._search_with_serpapi(query, count)

            if not results:
                return ToolResponse.success(
                    text=f"未找到与 '{query}' 相关的结果",
                    data={"query": query, "results": []}
                )

            # 格式化输出
            formatted = self._format_results(results)

            return ToolResponse.success(
                text=formatted,
                data={
                    "query": query,
                    "results": results,
                    "count": len(results),
                }
            )

        except HTTPError as e:
            if e.code == 401:
                return ToolResponse.error(
                    code="AUTH_ERROR",
                    message="API Key 无效或已过期"
                )
            elif e.code == 429:
                return ToolResponse.error(
                    code="RATE_LIMIT",
                    message="API 请求频率超限，请稍后再试"
                )
            else:
                return ToolResponse.error(
                    code="HTTP_ERROR",
                    message=f"搜索请求失败 (HTTP {e.code}): {e.reason}"
                )
        except URLError as e:
            return ToolResponse.error(
                code="NETWORK_ERROR",
                message=f"网络错误: {str(e)}"
            )
        except Exception as e:
            return ToolResponse.error(
                code="SEARCH_ERROR",
                message=f"搜索失败: {str(e)}"
            )

    def _search_with_brave(self, query: str, count: int = None) -> List[dict]:
        """使用 Brave Search API 进行搜索"""
        # 构建请求
        params = {
            "q": query,
            "count": count or self.max_results,
        }

        url = f"{self._base_url}?q={query}&count={params['count']}"
        request = Request(url)
        request.add_header("Accept", "application/json")
        request.add_header("Accept-Encoding", "gzip")
        request.add_header("X-Subscription-Token", self.api_key)

        # 发送请求
        with urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))

        # 解析结果
        results = self._parse_search_results(data)
        return results

    def _search_with_tavily(self, query: str, count: int = None) -> List[dict]:
        """使用 Tavily API 进行搜索"""
        if not self.tavily_key:
            raise RuntimeError("TAVILY_API_KEY 未配置，无法使用 Tavily 搜索")
        if self.tavily_client is None:
            raise RuntimeError("Tavily 客户端未初始化，无法使用 Tavily 搜索")

        limit = count or self.max_results
        response = self.tavily_client.search(  # type: ignore[call-arg]
            query=query,
            max_results=limit,
            include_raw_content=False,
        )

        raw_results = response.get("results", []) or []
        results: List[dict] = []
        for item in raw_results[:limit]:
            results.append({
                "title": item.get("title", "") or "",
                "url": item.get("url") or item.get("link") or "",
                "description": (
                    item.get("content")
                    or item.get("description")
                    or item.get("snippet")
                    or ""
                ),
            })
        return results

    def _search_with_serpapi(self, query: str, count: int = None) -> List[dict]:
        """使用 SerpApi API 进行搜索"""
        if not self.serpapi_key:
            raise RuntimeError("SERPAPI_API_KEY 未配置，无法使用 SerpApi 搜索")
        if GoogleSearch is None:
            raise RuntimeError("未安装 google-search-results，无法使用 SerpApi")

        limit = count or self.max_results
        params = {
            "engine": "google",
            "q": query,
            "api_key": self.serpapi_key,
            "gl": "cn",
            "hl": "zh-cn",
            "num": limit,
        }

        response = GoogleSearch(params).get_dict()
        results: List[dict] = []

        # 为了与 Brave 返回结构一致，这里只使用 organic_results，并统一为 title/url/description。
        for item in response.get("organic_results", [])[:limit]:
            results.append({
                "title": item.get("title") or item.get("link", "") or "",
                "url": item.get("link") or "",
                "description": item.get("snippet") or item.get("description") or "",
            })

        return results
    def _parse_search_results(self, data: dict) -> List[dict]:
        """解析 Brave Search API 响应

        Args:
            data: API 响应数据

        Returns:
            搜索结果列表
        """
        results = []

        # 提取 web 搜索结果
        web_results = data.get("web", {}).get("results", [])

        for item in web_results:
            result = {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("description", ""),
            }
            results.append(result)

        return results

    def _format_results(self, results: List[dict]) -> str:
        """格式化搜索结果

        Args:
            results: 搜索结果列表

        Returns:
            格式化的文本
        """
        lines = [f"找到 {len(results)} 个结果:\n"]

        for i, result in enumerate(results, 1):
            lines.append(f"{i}. **{result['title']}**")
            lines.append(f"   URL: {result['url']}")
            if result['description']:
                lines.append(f"   {result['description'][:200]}")
            lines.append("")

        return "\n".join(lines)

    @tool_action("search_web", "搜索网络信息")
    def _search_action(self, query: str, count: int = None) -> str:
        """搜索网络

        Args:
            query: 搜索查询词
            count: 返回结果数量（可选）
        """
        response = self._search(query, count)
        return response.text
