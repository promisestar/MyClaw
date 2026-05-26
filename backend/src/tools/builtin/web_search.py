"""网页搜索工具 - 支持多后端与结果时效过滤"""

import os
import re
import json
from datetime import datetime
from typing import List, Dict, Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from hello_agents.tools import Tool, ToolParameter, ToolResponse, tool_action

try:
    from tavily import TavilyClient  # type: ignore
except Exception:  # pragma: no cover
    TavilyClient = None  # type: ignore

try:
    from serpapi.google_search import GoogleSearch  # type: ignore
except Exception:  # pragma: no cover
    try:
        from serpapi import GoogleSearch  # type: ignore
    except Exception:  # pragma: no cover
        GoogleSearch = None  # type: ignore

# Agent 传入的 freshness → 各后端统一码（Brave: pd/pw/pm/py）
_FRESHNESS_ALIASES: Dict[str, str] = {
    "day": "pd",
    "pd": "pd",
    "24h": "pd",
    "week": "pw",
    "pw": "pw",
    "7d": "pw",
    "month": "pm",
    "pm": "pm",
    "31d": "pm",
    "year": "py",
    "py": "py",
    "365d": "py",
}

# Tavily days、SerpAPI tbs 映射
_TAVILY_DAYS = {"pd": 1, "pw": 7, "pm": 31, "py": 365}
_SERPAPI_TBS = {"pd": "qdr:d", "pw": "qdr:w", "pm": "qdr:m", "py": "qdr:y"}

# 用户问题含这些词时，隐含「要近期结果」
_RECENCY_QUERY_RE = re.compile(
    r"最新|近期|最近|今年|当前|近况|进展|动态|刚刚|近日|"
    r"latest|recent|current|breaking|news|update",
    re.IGNORECASE,
)


def _normalize_freshness(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    key = str(value).strip().lower()
    if key in ("none", "any", "all", ""):
        return None
    return _FRESHNESS_ALIASES.get(key)


def _infer_freshness_from_query(query: str) -> Optional[str]:
    """从查询词推断时效：「最新进展」类默认近 31 天。"""
    if _RECENCY_QUERY_RE.search(query):
        return "pm"
    return None


def _augment_query_for_recency(query: str) -> str:
    """在隐含时效的查询中补上当前年份，减少只命中旧闻。"""
    if not _RECENCY_QUERY_RE.search(query):
        return query
    year = datetime.now().year
    if str(year) in query or str(year - 1) in query:
        return query
    return f"{query} {year}"


class WebSearchTool(Tool):
    """网页搜索：Brave / Tavily / SerpApi，支持 freshness 时效过滤。"""

    def __init__(
        self,
        api_key: str = None,
        tavily_key: str | None = None,
        serpapi_key: str | None = None,
        max_results: int = 5,
        timeout: int = 10,
        default_freshness: Optional[str] = None,
        auto_recency: bool = True,
    ):
        super().__init__(
            name="web_search",
            description=(
                "按关键词搜索公开网页，返回标题、URL 与摘要。"
                "用于：查新闻、行业动态、文档入口、尚未确定 URL 的资料。"
                "用户问「最新/近期/今年/进展」时：① freshness 用 month 或 week；"
                "② query 中应包含当前年份（如 2026）；③ 拿到 URL 后用 web_fetch 读全文。"
                "不要用本工具抓取已知 URL（用 web_fetch）；不要代替 rag 查用户已入库私有文档。"
            ),
            expandable=True,
        )

        self.api_key = api_key or os.getenv("BRAVE_API_KEY")
        self.max_results = max_results
        self.timeout = timeout
        self._base_url = "https://api.search.brave.com/res/v1/web/search"
        self.tavily_key = tavily_key or os.getenv("TAVILY_API_KEY")
        self.serpapi_key = serpapi_key or os.getenv("SERPAPI_API_KEY")
        self.default_freshness = _normalize_freshness(default_freshness)
        self.auto_recency = auto_recency
        self.available_backends: list[str] = []
        self._setup_backends()

    def _setup_backends(self) -> None:
        if self.api_key:
            self.available_backends.append("brave")
        if self.tavily_key and TavilyClient is not None:
            try:
                self.tavily_client = TavilyClient(api_key=self.tavily_key)
                self.available_backends.append("tavily")
            except Exception as exc:  # pragma: no cover
                print(f"⚠️ Tavily 初始化失败: {exc}")
        elif self.tavily_key:
            print("⚠️ 未安装 tavily-python，无法使用 Tavily 搜索")
        if self.serpapi_key and GoogleSearch is not None:
            self.available_backends.append("serpapi")

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        query = parameters.get("query", "")
        count = parameters.get("count", self.max_results)
        freshness = parameters.get("freshness")
        return self._search(query, count, freshness=freshness)

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="query",
                type="string",
                description=(
                    "搜索关键词；若用户关心「最新/近期」，请在 query 中加入当前年份与具体主题，"
                    "例如「华为 芯片 制造 2026 最新」"
                ),
                required=True,
            ),
            ToolParameter(
                name="count",
                type="integer",
                description=f"返回条数，默认 {self.max_results}",
                required=False,
            ),
            ToolParameter(
                name="freshness",
                type="string",
                description=(
                    "结果时效：day(24h) | week(7天) | month(31天，默认用于「最新/近期」类问题) | "
                    "year(一年) | none(不限)。未传时，若 query 含「最新/近期」等词会自动用 month"
                ),
                required=False,
            ),
        ]

    def _resolve_search_options(
        self,
        query: str,
        freshness: Optional[str],
    ) -> tuple[str, Optional[str], bool]:
        """返回 (effective_query, freshness_code, auto_applied)。"""
        code = _normalize_freshness(freshness) or self.default_freshness
        auto_applied = False
        if code is None and self.auto_recency:
            code = _infer_freshness_from_query(query)
            auto_applied = code is not None
        effective_query = _augment_query_for_recency(query) if self.auto_recency else query
        if effective_query != query and auto_applied:
            pass  # 年份与 freshness 同时自动应用
        elif effective_query != query:
            auto_applied = True
        return effective_query, code, auto_applied

    def _search(
        self,
        query: str,
        count: int = None,
        freshness: Optional[str] = None,
    ) -> ToolResponse:
        if not query:
            return ToolResponse.error(
                code="INVALID_INPUT",
                message="搜索查询不能为空",
            )

        if not self.available_backends:
            return ToolResponse.error(
                code="MISSING_API_KEY",
                message=(
                    "未配置搜索 API。请设置 BRAVE_API_KEY、TAVILY_API_KEY 或 SERPAPI_API_KEY 之一"
                ),
            )

        effective_query, freshness_code, auto_applied = self._resolve_search_options(
            query, freshness
        )
        limit = count or self.max_results

        try:
            backend = self.available_backends[0]
            if "brave" in self.available_backends:
                results = self._search_with_brave(effective_query, limit, freshness_code)
                backend = "brave"
            elif "tavily" in self.available_backends:
                results = self._search_with_tavily(effective_query, limit, freshness_code)
                backend = "tavily"
            else:
                results = self._search_with_serpapi(effective_query, limit, freshness_code)
                backend = "serpapi"

            if not results:
                hint = ""
                if freshness_code:
                    hint = "（可尝试放宽 freshness 为 year 或 none）"
                return ToolResponse.success(
                    text=f"未找到与 '{effective_query}' 相关的结果{hint}",
                    data={
                        "query": query,
                        "effective_query": effective_query,
                        "freshness": freshness_code,
                        "results": [],
                    },
                )

            formatted = self._format_results(
                results,
                effective_query=effective_query,
                freshness_code=freshness_code,
                auto_recency=auto_applied,
                backend=backend,
            )

            return ToolResponse.success(
                text=formatted,
                data={
                    "query": query,
                    "effective_query": effective_query,
                    "freshness": freshness_code,
                    "auto_recency": auto_applied,
                    "backend": backend,
                    "results": results,
                    "count": len(results),
                },
            )

        except HTTPError as e:
            if e.code == 401:
                return ToolResponse.error(
                    code="AUTH_ERROR", message="API Key 无效或已过期"
                )
            if e.code == 429:
                return ToolResponse.error(
                    code="RATE_LIMIT", message="API 请求频率超限，请稍后再试"
                )
            return ToolResponse.error(
                code="HTTP_ERROR",
                message=f"搜索请求失败 (HTTP {e.code}): {e.reason}",
            )
        except URLError as e:
            return ToolResponse.error(
                code="NETWORK_ERROR", message=f"网络错误: {e}"
            )
        except Exception as e:
            return ToolResponse.error(
                code="SEARCH_ERROR", message=f"搜索失败: {e}"
            )

    def _search_with_brave(
        self,
        query: str,
        count: int,
        freshness_code: Optional[str],
    ) -> List[dict]:
        params: Dict[str, Any] = {"q": query, "count": count}
        if freshness_code:
            params["freshness"] = freshness_code
        url = f"{self._base_url}?{urlencode(params)}"
        request = Request(url)
        request.add_header("Accept", "application/json")
        request.add_header("Accept-Encoding", "gzip")
        request.add_header("X-Subscription-Token", self.api_key)
        with urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return self._parse_brave_results(data)

    def _search_with_tavily(
        self,
        query: str,
        count: int,
        freshness_code: Optional[str],
    ) -> List[dict]:
        if not self.tavily_key or self.tavily_client is None:
            raise RuntimeError("Tavily 未配置或未初始化")
        kwargs: Dict[str, Any] = {
            "query": query,
            "max_results": count,
            "include_raw_content": False,
        }
        if freshness_code:
            kwargs["days"] = _TAVILY_DAYS.get(freshness_code, 31)
        response = self.tavily_client.search(**kwargs)  # type: ignore[call-arg]
        results: List[dict] = []
        for item in (response.get("results", []) or [])[:count]:
            results.append({
                "title": item.get("title", "") or "",
                "url": item.get("url") or item.get("link") or "",
                "description": (
                    item.get("content")
                    or item.get("description")
                    or item.get("snippet")
                    or ""
                ),
                "published": item.get("published_date") or "",
            })
        return results

    def _search_with_serpapi(
        self,
        query: str,
        count: int,
        freshness_code: Optional[str],
    ) -> List[dict]:
        if not self.serpapi_key or GoogleSearch is None:
            raise RuntimeError("SerpApi 未配置或未安装 google-search-results")
        params: Dict[str, Any] = {
            "engine": "google",
            "q": query,
            "api_key": self.serpapi_key,
            "gl": "cn",
            "hl": "zh-cn",
            "num": count,
        }
        if freshness_code:
            params["tbs"] = _SERPAPI_TBS.get(freshness_code, "qdr:m")
        response = GoogleSearch(params).get_dict()
        results: List[dict] = []
        for item in response.get("organic_results", [])[:count]:
            results.append({
                "title": item.get("title") or item.get("link", "") or "",
                "url": item.get("link") or "",
                "description": item.get("snippet") or item.get("description") or "",
                "published": item.get("date") or "",
            })
        return results

    def _parse_brave_results(self, data: dict) -> List[dict]:
        results = []
        for item in data.get("web", {}).get("results", []):
            page_age = item.get("page_age") or item.get("age") or ""
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("description", ""),
                "published": page_age,
            })
        return results

    def _format_results(
        self,
        results: List[dict],
        *,
        effective_query: str,
        freshness_code: Optional[str],
        auto_recency: bool,
        backend: str,
    ) -> str:
        lines = [f"找到 {len(results)} 个结果（后端: {backend}）"]
        if effective_query:
            lines.append(f"实际查询: {effective_query}")
        if freshness_code:
            label = {"pd": "24小时内", "pw": "7天内", "pm": "31天内", "py": "一年内"}.get(
                freshness_code, freshness_code
            )
            suffix = "（已自动应用时效过滤）" if auto_recency else ""
            lines.append(f"时效过滤: {label}{suffix}")
        lines.append(
            f"检索时间: {datetime.now().strftime('%Y-%m-%d %H:%M')} — "
            "若摘要日期偏旧，可缩小 freshness 或改写 query 后重搜，并用 web_fetch 核对正文"
        )
        lines.append("")

        for i, result in enumerate(results, 1):
            lines.append(f"{i}. **{result['title']}**")
            lines.append(f"   URL: {result['url']}")
            pub = result.get("published") or ""
            if pub:
                lines.append(f"   时间: {pub}")
            if result.get("description"):
                lines.append(f"   {result['description'][:200]}")
            lines.append("")

        return "\n".join(lines)

    @tool_action("search_web", "搜索网络信息")
    def _search_action(
        self,
        query: str,
        count: int = None,
        freshness: str = None,
    ) -> str:
        response = self._search(query, count, freshness=freshness)
        return response.text
