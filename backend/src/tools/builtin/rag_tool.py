"""RAG工具 - 检索增强生成

为MyClaw提供简洁易用的RAG能力：
- 🔄 数据流程：用户数据 → 文档解析 → 向量化存储 → 智能检索 → LLM增强问答
- 📚 多格式支持：PDF、Word、Excel、PPT、图片、音频、网页等
- 🧠 智能问答：自动检索相关内容，注入提示词，生成准确答案
- 🏷️ 命名空间：支持多项目隔离，便于管理不同知识库

使用示例：
```python
# 1. 初始化RAG工具
rag = RAGTool()

# 2. 添加文档
rag.run({"action": "add_document", "file_path": "document.pdf"})  # 返回 ToolResponse

# 3. 智能问答
resp = rag.run({"action": "ask", "question": "什么是机器学习？"})
answer = resp.text
```
"""

from typing import Dict, Any, List, Optional, Tuple
import hashlib
import logging
import os
import re
import tempfile
import time

from hello_agents.tools import Tool, ToolParameter, tool_action, ToolResponse, ToolErrorCode
from ...rag.pipeline import create_rag_pipeline
from ...rag.qdrant_store import QdrantConnectionManager
from hello_agents.core.llm import HelloAgentsLLM


logger = logging.getLogger(__name__)


def _rag_response_from_text(msg: str) -> ToolResponse:
    """将原有字符串结果转为 ToolResponse（供 run_with_timing 使用）。"""
    if msg is None:
        return ToolResponse.success(text="")
    if msg.startswith("❌"):
        if "文件不存在" in msg:
            code = ToolErrorCode.NOT_FOUND
        elif any(
            x in msg
            for x in (
                "参数验证失败",
                "不能为空",
                "不支持的操作",
                "请提供要询问",
                "路径列表不能为空",
                "数量不匹配",
            )
        ):
            code = ToolErrorCode.INVALID_PARAM
        else:
            code = ToolErrorCode.EXECUTION_ERROR
        return ToolResponse.error(code=code, message=msg)
    if msg.startswith("⚠️"):
        return ToolResponse.partial(text=msg)
    return ToolResponse.success(text=msg)


class RAGTool(Tool):
    """RAG 知识库工具

    管理用户已入库私有文档：向量化入库、语义检索、LLM 增强问答。
    与工作区源码/配置无关的私有资料应走本工具，不用 Read 代替检索。
    """

    def __init__(
        self,
        knowledge_base_path: str = "./knowledge_base",
        qdrant_url: Optional[str] = None,
        qdrant_api_key: Optional[str] = None,
        collection_name: str = "rag_knowledge_base",
        rag_namespace: str = "default",
        expandable: bool = False,
        workspace_root: Optional[str] = None,
    ):
        super().__init__(
            name="rag",
            description=(
                "管理用户已入库私有文档的知识库：向量化入库、语义检索与 LLM 问答。"
                "此工具不具备联网能力，无法访问互联网！"
                "用于：用户上传/提供的 PDF、笔记等资料；问「资料里写了什么」、『帮我总结一下文档』类问题。"
                "用户上传了文件，并要求基于该文件进行对话时。"
                "需要确认知识库中是否有某份文件时（使用 stats）。"
                "严禁用于查询公开网页、新闻、实时信息（此类请求必须使用 web_search 工具）。"
                "严禁假设内容已在库中；如果用户提到了新话题，必须先调用 add_document 或 add_text 入库。"
            ),
            expandable=expandable
        )
        # 与 Read/上传 API 一致：相对路径相对 Agent 工作空间根（非进程 CWD）
        self._workspace_root = (
            os.path.normpath(os.path.expanduser(workspace_root))
            if workspace_root
            else None
        )

        self.knowledge_base_path = knowledge_base_path
        self.qdrant_url = qdrant_url or os.getenv("QDRANT_URL")
        self.qdrant_api_key = qdrant_api_key or os.getenv("QDRANT_API_KEY")
        self.collection_name = collection_name
        self.rag_namespace = rag_namespace
        self._pipelines: Dict[str, Dict[str, Any]] = {}
        
        # 确保知识库目录存在
        os.makedirs(knowledge_base_path, exist_ok=True)
        
        # 初始化组件
        self._init_components()
    
    def _init_components(self):
        """初始化RAG组件"""
        try:
            # 初始化默认命名空间的 RAG 管道
            default_pipeline = create_rag_pipeline(
                qdrant_url=self.qdrant_url,
                qdrant_api_key=self.qdrant_api_key,
                collection_name=self.collection_name,
                rag_namespace=self.rag_namespace
            )
            self._pipelines[self.rag_namespace] = default_pipeline

            # 初始化 LLM 用于回答生成
            self.llm = HelloAgentsLLM()

            self.initialized = True
            logger.info("RAG工具初始化成功: namespace=%s, collection=%s", self.rag_namespace, self.collection_name)
            
        except Exception as e:
            self.initialized = False
            self.init_error = str(e)
            logger.exception("RAG工具初始化失败")

    def _resolve_document_path(self, file_path: str) -> str:
        """将用户给出的路径解析为磁盘绝对路径；相对路径相对工作空间根。"""
        if not file_path or not str(file_path).strip():
            return file_path
        p = str(file_path).strip()
        if os.path.isabs(p):
            return os.path.normpath(os.path.expanduser(p))
        if self._workspace_root:
            return os.path.normpath(os.path.join(self._workspace_root, p))
        return os.path.normpath(os.path.expanduser(p))

    def _normalize_namespace(self, namespace: Optional[str] = None) -> str:
        """规范化命名空间，避免空字符串导致缓存键不一致。"""
        return (str(namespace).strip() if namespace else self.rag_namespace) or self.rag_namespace

    @staticmethod
    def _stable_text_document_id(text: str) -> str:
        """基于文本内容生成稳定文档 ID，避免 Python hash 随进程变化。"""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        return f"text_{digest}"

    @staticmethod
    def _safe_filename_stem(value: str) -> str:
        """将外部传入的 document_id 转为安全文件名前缀。"""
        stem = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value or "text")).strip("._")
        return stem[:80] or "text"

    def _write_temp_text_document(self, text: str, document_id: str) -> str:
        """写入临时 Markdown 文件，供统一文档入库流程复用。"""
        os.makedirs(self.knowledge_base_path, exist_ok=True)
        safe_id = self._safe_filename_stem(document_id)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".md",
            prefix=f"{safe_id}_",
            dir=self.knowledge_base_path,
            delete=False,
        ) as tmp:
            tmp.write(text)
            return tmp.name

    def _index_text_document(
        self,
        text: str,
        document_id: Optional[str] = None,
        namespace: str = "default",
        chunk_size: int = 800,
        chunk_overlap: int = 100,
    ) -> Tuple[int, int, str]:
        """将纯文本写临时文件并入库，返回分块数、耗时与最终文档 ID。"""
        final_document_id = document_id or self._stable_text_document_id(text)
        tmp_path = self._write_temp_text_document(text, final_document_id)
        try:
            pipeline = self._get_pipeline(namespace)
            t0 = time.time()
            chunks_added = pipeline["add_documents"](
                file_paths=[tmp_path],
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
            process_ms = int((time.time() - t0) * 1000)
            return chunks_added, process_ms, final_document_id
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                logger.warning("清理 RAG 临时文本文件失败: %s", tmp_path, exc_info=True)

    @staticmethod
    def _clean_text(text: Any) -> str:
        """安全转字符串并过滤非法 Unicode。"""
        try:
            return str(text).encode("utf-8", errors="ignore").decode("utf-8")
        except Exception:
            return str(text)

    @staticmethod
    def _truncate_text(text: str, max_chars: int, suffix: str = "...") -> str:
        """按字符上限截断文本，避免负数和重复省略号。"""
        if max_chars <= 0:
            return ""
        if len(text) <= max_chars:
            return text
        keep = max(0, max_chars - len(suffix))
        return text[:keep].rstrip() + suffix

    def _get_pipeline(self, namespace: Optional[str] = None) -> Dict[str, Any]:
        """获取指定命名空间的 RAG 管道，若不存在则自动创建。"""
        target_ns = self._normalize_namespace(namespace)
        if target_ns in self._pipelines:
            return self._pipelines[target_ns]

        pipeline = create_rag_pipeline(
            qdrant_url=self.qdrant_url,
            qdrant_api_key=self.qdrant_api_key,
            collection_name=self.collection_name,
            rag_namespace=target_ns
        )
        self._pipelines[target_ns] = pipeline
        logger.info("创建 RAG pipeline: namespace=%s, collection=%s", target_ns, self.collection_name)
        return pipeline

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        """执行工具（非展开模式）

        Args:
            parameters: 工具参数字典，必须包含action参数

        Returns:
            ToolResponse（与 HelloAgents run_with_timing 协议一致）
        """
        if not self.validate_parameters(parameters):
            return ToolResponse.error(
                code=ToolErrorCode.INVALID_PARAM,
                message="❌ 参数验证失败：缺少必需的参数",
            )

        if not self.initialized:
            return ToolResponse.error(
                code=ToolErrorCode.INTERNAL_ERROR,
                message=f"❌ RAG工具未正确初始化，请检查配置: {getattr(self, 'init_error', '未知错误')}",
            )

        action = parameters.get("action")

        # 根据action调用对应的方法，传入提取的参数
        try:
            if action == "add_document":
                return _rag_response_from_text(
                    self._add_document(
                        file_path=parameters.get("file_path"),
                        document_id=parameters.get("document_id"),
                        namespace=parameters.get("namespace", "default"),
                        chunk_size=parameters.get("chunk_size", 800),
                        chunk_overlap=parameters.get("chunk_overlap", 100),
                    )
                )
            elif action == "add_text":
                return _rag_response_from_text(
                    self._add_text(
                        text=parameters.get("text"),
                        document_id=parameters.get("document_id"),
                        namespace=parameters.get("namespace", "default"),
                        chunk_size=parameters.get("chunk_size", 800),
                        chunk_overlap=parameters.get("chunk_overlap", 100),
                    )
                )
            elif action == "ask":
                question = parameters.get("question") or parameters.get("query")
                return _rag_response_from_text(
                    self._ask(
                        question=question,
                        limit=parameters.get("limit", 5),
                        enable_advanced_search=parameters.get("enable_advanced_search", True),
                        include_citations=parameters.get("include_citations", True),
                        max_chars=parameters.get("max_chars", 1200),
                        namespace=parameters.get("namespace", "default"),
                        debug=parameters.get("debug", False),
                    )
                )
            elif action == "search":
                return _rag_response_from_text(
                    self._search(
                        query=parameters.get("query") or parameters.get("question"),
                        limit=parameters.get("limit", 5),
                        min_score=parameters.get("min_score", 0.1),
                        enable_advanced_search=parameters.get("enable_advanced_search", True),
                        max_chars=parameters.get("max_chars", 1200),
                        include_citations=parameters.get("include_citations", True),
                        namespace=parameters.get("namespace", "default"),
                    )
                )
            elif action == "stats":
                return _rag_response_from_text(
                    self._get_stats(namespace=parameters.get("namespace", "default"))
                )
            elif action == "clear":
                return _rag_response_from_text(
                    self._clear_knowledge_base(
                        confirm=parameters.get("confirm", False),
                        namespace=parameters.get("namespace", "default"),
                    )
                )
            else:
                return ToolResponse.error(
                    code=ToolErrorCode.INVALID_PARAM,
                    message=f"❌ 不支持的操作: {action}",
                )
        except Exception as e:
            return ToolResponse.error(
                code=ToolErrorCode.EXECUTION_ERROR,
                message=f"❌ 执行操作 '{action}' 时发生错误: {str(e)}",
            )

    def get_parameters(self) -> List[ToolParameter]:
        """获取工具参数定义 - Tool基类要求的接口"""
        return [
            ToolParameter(
                name="action",
                type="string",
                description=(
                    "必填。操作类型："
                    "add_document=文件入库(PDF/Word/Excel/PPT/图片/音频等，需 file_path)；"
                    "add_text=纯文本入库(需 text，无文件时用)；"
                    "ask=检索并由 LLM 生成综合答案(需 question，用户问资料内容时优先)；"
                    "search=仅返回原文片段与来源(需 query，不生成答案，用于核对引用)；"
                    "stats=查看分块数与管道状态(无副作用，无结果时先调用)；"
                    "clear=永久清空命名空间(需 confirm=true，仅用户明确要求时)"
                ),
                required=True,
            ),
            ToolParameter(
                name="file_path",
                type="string",
                description=(
                    "add_document 必填。文档路径；相对路径相对于工作空间根（如 uploads/report.pdf），"
                    "非进程 CWD。已有本地文件用此参数，纯文本用 add_text+text"
                ),
                required=False,
            ),
            ToolParameter(
                name="text",
                type="string",
                description="add_text 必填。待入库的纯文本（笔记、摘要、代码片段等）；有文件路径时用 add_document",
                required=False,
            ),
            ToolParameter(
                name="question",
                type="string",
                description="ask 必填（可与 query 二选一）。用户自然语言问题，用于检索并生成综合答案",
                required=False,
            ),
            ToolParameter(
                name="query",
                type="string",
                description="search 必填（可与 question 二选一）。检索关键词或问句，仅返回原文片段，不调用 LLM 生成答案",
                required=False,
            ),
            ToolParameter(
                name="namespace",
                type="string",
                description="知识库命名空间，用于多项目隔离，默认 default",
                required=False,
                default="default",
            ),
            ToolParameter(
                name="document_id",
                type="string",
                description="add_document/add_text 可选。自定义文档标识，便于区分来源；未传时自动生成",
                required=False,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description="ask/search 可选。检索返回的片段数量，默认 5",
                required=False,
                default=5,
            ),
            ToolParameter(
                name="min_score",
                type="number",
                description="search 可选。最低相似度阈值，低于此分数的结果被过滤，默认 0.1",
                required=False,
                default=0.1,
            ),
            ToolParameter(
                name="enable_advanced_search",
                type="boolean",
                description="ask/search 可选。是否启用 MQE/HyDE 扩展检索以提升召回，默认 true",
                required=False,
                default=True,
            ),
            ToolParameter(
                name="include_citations",
                type="boolean",
                description="ask/search 可选。是否在结果中附带来源文件与章节，默认 true",
                required=False,
                default=True,
            ),
            ToolParameter(
                name="max_chars",
                type="integer",
                description="ask/search 可选。注入上下文或展示内容的总字符上限，默认 1200",
                required=False,
                default=1200,
            ),
            ToolParameter(
                name="chunk_size",
                type="integer",
                description="add_document/add_text 可选。分块大小，默认 800，一般无需修改",
                required=False,
                default=800,
            ),
            ToolParameter(
                name="chunk_overlap",
                type="integer",
                description="add_document/add_text 可选。分块重叠字符数，默认 100，一般无需修改",
                required=False,
                default=100,
            ),
            ToolParameter(
                name="debug",
                type="boolean",
                description="ask 可选。是否在回答中展示检索、生成耗时与平均相似度，默认 false",
                required=False,
                default=False,
            ),
            ToolParameter(
                name="confirm",
                type="boolean",
                description="clear 必填且须为 true。未传或为 false 时仅返回警告，不执行清空",
                required=False,
                default=False,
            ),
        ]

    @tool_action(
        "rag_add_document",
        "将本地文件解析、分块、向量化后写入知识库。"
        "用于：用户上传或提供文件路径（PDF/Word/Excel/PPT/图片/音频/网页等）后需纳入检索范围。"
        "file_path 相对路径相对于工作空间根（如 uploads/），非进程 CWD。"
        "入库后可用 rag_ask 问答或 rag_search 检索；勿用 web_search 查私有文档。",
    )
    def _add_document(
        self,
        file_path: str,
        document_id: Optional[str] = None,
        namespace: str = "default",
        chunk_size: int = 800,
        chunk_overlap: int = 100
    ) -> str:
        """添加文档到知识库

        Args:
            file_path: 必填，文档路径；相对路径相对于工作空间根（如 uploads/report.pdf）
            document_id: 可选文档标识，用于区分来源
            namespace: 知识库命名空间，默认 default，用于多项目隔离
            chunk_size: 分块大小，默认 800，一般无需修改
            chunk_overlap: 分块重叠，默认 100，一般无需修改

        Returns:
            执行结果
        """
        try:
            if document_id:
                logger.debug("add_document 当前由文件路径生成文档标识，忽略外部 document_id=%s", document_id)
            if not file_path:
                return f"❌ 文件不存在: {file_path}"
            resolved = self._resolve_document_path(file_path)
            if not os.path.exists(resolved):
                return f"❌ 文件不存在: {resolved}"
            display_name = os.path.basename(resolved)
            target_ns = self._normalize_namespace(namespace)

            pipeline = self._get_pipeline(target_ns)
            t0 = time.time()

            chunks_added = pipeline["add_documents"](
                file_paths=[resolved],
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap
            )
            
            process_ms = int((time.time() - t0) * 1000)
            
            if chunks_added == 0:
                return f"⚠️ 未能从文件解析内容: {display_name}"
            
            return (
                f"✅ 文档已添加到知识库: {display_name}\n"
                f"📊 分块数量: {chunks_added}\n"
                f"⏱️ 处理时间: {process_ms}ms\n"
                f"📝 命名空间: {target_ns}"
            )
            
        except Exception as e:
            logger.exception("添加文档失败")
            return f"❌ 添加文档失败: {str(e)}"
    
    @tool_action(
        "rag_add_text",
        "将纯文本分块向量化入库（无需文件路径）。"
        "用于：用户粘贴笔记、摘要、代码片段等无对应本地文件的内容。"
        "已有文件请用 rag_add_document；入库后再用 rag_ask/rag_search 查询。",
    )
    def _add_text(
        self,
        text: str,
        document_id: Optional[str] = None,
        namespace: str = "default",
        chunk_size: int = 800,
        chunk_overlap: int = 100
    ) -> str:
        """添加文本到知识库

        Args:
            text: 必填，待入库的纯文本内容
            document_id: 可选文档标识，默认按内容哈希生成
            namespace: 知识库命名空间，默认 default
            chunk_size: 分块大小，默认 800，一般无需修改
            chunk_overlap: 分块重叠，默认 100，一般无需修改

        Returns:
            执行结果
        """
        try:
            if not text or not text.strip():
                return "❌ 文本内容不能为空"

            target_ns = self._normalize_namespace(namespace)
            chunks_added, process_ms, final_document_id = self._index_text_document(
                text=text,
                document_id=document_id,
                namespace=target_ns,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
            
            if chunks_added == 0:
                return "⚠️ 未能从文本生成有效分块"
            
            return (
                f"✅ 文本已添加到知识库: {final_document_id}\n"
                f"📊 分块数量: {chunks_added}\n"
                f"⏱️ 处理时间: {process_ms}ms\n"
                f"📝 命名空间: {target_ns}"
            )
            
        except Exception as e:
            logger.exception("添加文本失败")
            return f"❌ 添加文本失败: {str(e)}"
    
    @tool_action(
        "rag_search",
        "语义检索已入库文档，返回原文片段、来源文件与相似度（不生成综合答案）。"
        "用于：需自行阅读原文、核对引用、或仅需相关段落时。"
        "用户需要综合回答时用 rag_ask；查公开网页用 web_search。"
        "无结果时先用 rag_stats 确认是否已入库。",
    )
    def _search(
        self,
        query: str,
        limit: int = 5,
        min_score: float = 0.1,
        enable_advanced_search: bool = True,
        max_chars: int = 1200,
        include_citations: bool = True,
        namespace: str = "default"
    ) -> str:
        """搜索知识库

        Args:
            query: 必填，检索关键词或自然语言问句
            limit: 返回片段数量，默认 5
            min_score: 最低相似度阈值，默认 0.1，低于此分数的结果被过滤
            enable_advanced_search: 是否启用 MQE/HyDE 扩展检索，默认 true
            max_chars: 上下文总字符上限，默认 1200
            include_citations: 是否附带来源文件与章节，默认 true
            namespace: 知识库命名空间，默认 default

        Returns:
            搜索结果
        """
        try:
            if not query or not query.strip():
                return "❌ 搜索查询不能为空"

            query_text = query.strip()
            target_ns = self._normalize_namespace(namespace)
            pipeline = self._get_pipeline(target_ns)

            if enable_advanced_search:
                results = pipeline["search_advanced"](
                    query=query_text,
                    top_k=limit,
                    enable_mqe=True,
                    enable_hyde=True,
                    score_threshold=min_score if min_score > 0 else None
                )
            else:
                results = pipeline["search"](
                    query=query_text,
                    top_k=limit,
                    score_threshold=min_score if min_score > 0 else None
                )
            
            if not results:
                return f"🔍 未找到与 '{query_text}' 相关的内容"
            
            search_result = ["搜索结果："]
            remaining_chars = max(0, int(max_chars or 0))
            total_results = len(results)
            for i, result in enumerate(results, 1):
                meta = result.get("metadata", {})
                score = result.get("score", 0.0)
                source = self._clean_text(meta.get("source_path", "unknown"))
                raw_content = self._clean_text(meta.get("content", "")).strip()
                results_left = max(1, total_results - i + 1)
                item_budget = max(80, remaining_chars // results_left) if remaining_chars else 0
                content = self._truncate_text(raw_content, item_budget) if item_budget else ""
                remaining_chars = max(0, remaining_chars - len(content))
                
                search_result.append(f"\n{i}. 文档: **{source}** (相似度: {score:.3f})")
                if content:
                    search_result.append(f"   {content}")
                
                if include_citations and meta.get("heading_path"):
                    clean_heading = self._clean_text(meta["heading_path"])
                    search_result.append(f"   章节: {clean_heading}")
            
            return "\n".join(search_result)
            
        except Exception as e:
            logger.exception("搜索知识库失败")
            return f"❌ 搜索失败: {str(e)}"
    
    @tool_action(
        "rag_ask",
        "检索已入库文档并由 LLM 生成综合答案（含引用来源）。"
        "用于：用户就私有/已入库资料提具体问题、需总结或解释时。"
        "勿查未入库内容（先 rag_add_document 或 rag_add_text）；"
        "仅需原文片段用 rag_search；公开资讯用 web_search。",
    )
    def _ask(
        self,
        question: str,
        limit: int = 5,
        enable_advanced_search: bool = True,
        include_citations: bool = True,
        max_chars: int = 1200,
        namespace: str = "default",
        debug: bool = False,
    ) -> str:
        """智能问答：检索 → 上下文注入 → LLM生成答案

        Args:
            question: 必填，用户问题（自然语言）
            limit: 检索片段数量，默认 5
            enable_advanced_search: 是否启用 MQE 扩展检索，默认 true
            include_citations: 是否在答案后附参考来源，默认 true
            max_chars: 注入 LLM 的上下文总字符上限，默认 1200
            namespace: 知识库命名空间，默认 default
            debug: 是否在答案中附加检索/生成性能信息

        Returns:
            智能问答结果
        """
        try:
            # 验证问题
            if not question or not question.strip():
                return "❌ 请提供要询问的问题"

            user_question = question.strip()
            target_ns = self._normalize_namespace(namespace)
            logger.info("RAG智能问答: namespace=%s, question=%s", target_ns, user_question)
            
            # 1. 检索相关内容
            pipeline = self._get_pipeline(target_ns)
            search_start = time.time()
            
            if enable_advanced_search:
                results = pipeline["search_advanced"](
                    query=user_question,
                    top_k=limit,
                    enable_mqe=True,
                    enable_hyde=False
                )
            else:
                results = pipeline["search"](
                    query=user_question,
                    top_k=limit
                )
            
            search_time = int((time.time() - search_start) * 1000)
            
            if not results:
                return (
                    f"🤔 抱歉，我在知识库中没有找到与「{user_question}」相关的信息。\n\n"
                    f"💡 建议：\n"
                    f"• 尝试使用更简洁的关键词\n"
                    f"• 检查是否已添加相关文档\n"
                    f"• 使用 stats 操作查看知识库状态"
                )
            
            # 2. 智能整理上下文
            context_parts = []
            citations = []
            total_score = 0
            
            for i, result in enumerate(results):
                meta = result.get("metadata", {})
                content = meta.get("content", "").strip()
                source = meta.get("source_path", "unknown")
                score = result.get("score", 0.0)
                total_score += score
                
                if content:
                    # 清理内容格式
                    cleaned_content = self._clean_content_for_context(content)
                    context_parts.append(f"片段 {i+1}：{cleaned_content}")
                    
                    if include_citations:
                        citations.append({
                            "index": i+1,
                            "source": os.path.basename(source),
                            "score": score
                        })
            
            # 3. 构建上下文（智能截断）
            context = "\n\n".join(context_parts)
            if len(context) > max_chars:
                # 智能截断，保持完整性
                context = self._smart_truncate_context(context, max_chars)
            
            # 4. 构建增强提示词
            system_prompt = self._build_system_prompt()
            user_prompt = self._build_user_prompt(user_question, context)
            
            enhanced_prompt = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
            
            # 5. 调用 LLM 生成答案；失败时退化为检索片段返回
            llm_start = time.time()
            try:
                answer = self.llm.invoke(enhanced_prompt)
                answer = str(answer)
            except Exception as llm_error:
                llm_time = int((time.time() - llm_start) * 1000)
                logger.warning("RAG LLM 生成失败，退化为检索结果: %s", llm_error, exc_info=True)
                return self._format_llm_fallback_answer(
                    question=user_question,
                    context=context,
                    citations=citations if include_citations else None,
                    search_time=search_time,
                    llm_time=llm_time,
                    avg_score=total_score / len(results) if results else 0,
                    debug=debug,
                )
            llm_time = int((time.time() - llm_start) * 1000)
            
            if not answer or not answer.strip():
                return self._format_llm_fallback_answer(
                    question=user_question,
                    context=context,
                    citations=citations if include_citations else None,
                    search_time=search_time,
                    llm_time=llm_time,
                    avg_score=total_score / len(results) if results else 0,
                    debug=debug,
                )
            
            # 6. 构建最终回答
            final_answer = self._format_final_answer(
                question=user_question,
                answer=answer.strip(),
                citations=citations if include_citations else None,
                search_time=search_time,
                llm_time=llm_time,
                avg_score=total_score / len(results) if results else 0,
                debug=debug,
            )
            
            return final_answer
            
        except Exception as e:
            logger.exception("智能问答失败")
            return f"❌ 智能问答失败: {str(e)}\n💡 请检查知识库状态或稍后重试"
    
    def _clean_content_for_context(self, content: str) -> str:
        """清理内容用于上下文，尽量保留 Markdown 段落、列表和表格结构。"""
        cleaned_lines = []
        previous_blank = False
        for raw_line in self._clean_text(content).splitlines():
            line = re.sub(r"[ \t]+", " ", raw_line).strip()
            if not line:
                if not previous_blank:
                    cleaned_lines.append("")
                previous_blank = True
                continue
            cleaned_lines.append(line)
            previous_blank = False

        cleaned = "\n".join(cleaned_lines).strip()
        return self._truncate_text(cleaned, 500)
    
    def _smart_truncate_context(self, context: str, max_chars: int) -> str:
        """智能截断上下文，保持段落完整性"""
        if max_chars <= 0:
            return ""
        if len(context) <= max_chars:
            return context
        
        # 寻找最近的段落分隔符
        truncated = context[:max_chars]
        last_break = truncated.rfind("\n\n")
        
        if last_break > max_chars * 0.7:  # 如果断点位置合理
            return truncated[:last_break] + "\n\n[...更多内容被截断]"
        return self._truncate_text(truncated, max_chars, suffix="...[内容被截断]")
    
    def _build_system_prompt(self) -> str:
        """构建系统提示词"""
        return (
            "你是一个专业的知识助手，具备以下能力：\n"
            "1. 📖 精准理解：仔细理解用户问题的核心意图\n"
            "2. 🎯 可信回答：严格基于提供的上下文信息回答，不编造内容\n"
            "3. 🔍 信息整合：从多个片段中提取关键信息，形成完整答案\n"
            "4. 💡 清晰表达：用简洁明了的语言回答，适当使用结构化格式\n"
            "5. 🚫 诚实表达：如果上下文不足以回答问题，请坦诚说明\n\n"
            "回答格式要求：\n"
            "• 直接回答核心问题\n"
            "• 必要时使用要点或步骤\n"
            "• 引用关键原文时使用引号\n"
            "• 避免重复和冗余"
        )
    
    def _build_user_prompt(self, question: str, context: str) -> str:
        """构建用户提示词"""
        return (
            f"请基于以下上下文信息回答问题：\n\n"
            f"【问题】{question}\n\n"
            f"【相关上下文】\n{context}\n\n"
            f"【要求】请提供准确、有帮助的回答。如果上下文信息不足，请说明需要什么额外信息。"
        )
    
    def _format_final_answer(
        self,
        question: str,
        answer: str,
        citations: Optional[List[Dict]] = None,
        search_time: int = 0,
        llm_time: int = 0,
        avg_score: float = 0,
        debug: bool = False,
    ) -> str:
        """格式化最终答案。"""
        _ = question
        result = ["🤖 **智能问答结果**\n"]
        result.append(answer)
        
        if citations:
            result.append("\n\n📚 **参考来源**")
            for citation in citations:
                score_emoji = "🟢" if citation["score"] > 0.8 else "🟡" if citation["score"] > 0.6 else "🔵"
                result.append(f"{score_emoji} [{citation['index']}] {citation['source']} (相似度: {citation['score']:.3f})")
        
        if debug:
            result.append(f"\n⚡ 检索: {search_time}ms | 生成: {llm_time}ms | 平均相似度: {avg_score:.3f}")
        
        return "\n".join(result)

    def _format_llm_fallback_answer(
        self,
        question: str,
        context: str,
        citations: Optional[List[Dict]] = None,
        search_time: int = 0,
        llm_time: int = 0,
        avg_score: float = 0,
        debug: bool = False,
    ) -> str:
        """LLM 不可用时返回检索上下文，避免丢失已召回的信息。"""
        fallback_text = (
            "⚠️ LLM 暂时未能生成综合答案，以下为知识库中检索到的相关片段：\n\n"
            f"{context or '（无可展示上下文）'}"
        )
        return self._format_final_answer(
            question=question,
            answer=fallback_text,
            citations=citations,
            search_time=search_time,
            llm_time=llm_time,
            avg_score=avg_score,
            debug=debug,
        )

    @tool_action(
        "rag_clear",
        "永久清空指定命名空间全部向量数据（不可恢复）。"
        "仅当用户明确要求删除/重置知识库时使用，且必须传 confirm=true。"
        "默认勿调用；清空前可用 rag_stats 确认范围。",
    )
    def _clear_knowledge_base(self, confirm: bool = False, namespace: str = "default") -> str:
        """清空知识库

        Args:
            confirm: 必须为 true 才会执行，否则仅返回警告
            namespace: 要清空的命名空间，默认 default

        Returns:
            执行结果
        """
        try:
            if not confirm:
                return (
                    "⚠️ 危险操作：清空知识库将删除所有数据！\n"
                    "请使用 confirm=true 参数确认执行。"
                )
            
            pipeline = self._get_pipeline(namespace)
            store = pipeline.get("store")
            namespace_id = pipeline.get("namespace", self.rag_namespace)
            if store and hasattr(store, "clear_namespace"):
                success = store.clear_namespace(namespace_id)
            else:
                success = store.clear_collection() if store else False
            
            if success:
                # 清理该命名空间 pipeline 缓存，下次访问自动重建
                self._pipelines.pop(namespace_id, None)
                return f"✅ 知识库已成功清空（命名空间：{namespace_id}）"
            else:
                return "❌ 清空知识库失败"
            
        except Exception as e:
            logger.exception("清空知识库失败")
            return f"❌ 清空知识库失败: {str(e)}"

    @tool_action(
        "rag_stats",
        "查看知识库状态：命名空间、分块数、向量存储与管道是否正常。"
        "用于：检索/问答无结果时排查是否空库；入库后确认分块数；切换 namespace 前确认范围。"
        "无副作用，可优先调用。",
    )
    def _get_stats(self, namespace: str = "default") -> str:
        """获取知识库统计

        Args:
            namespace: 要查看的命名空间，默认 default

        Returns:
            统计信息
        """
        try:
            target_ns = self._normalize_namespace(namespace)
            pipeline = self._get_pipeline(target_ns)
            stats = pipeline["get_stats"]()
            
            stats_info = [
                "📊 **RAG 知识库统计**",
                f"📝 命名空间: {pipeline.get('namespace', target_ns)}",
                f"📋 集合名称: {self.collection_name}",
                f"📂 存储根路径: {self.knowledge_base_path}",
                f"🗂️ 已加载命名空间: {', '.join(sorted(self._pipelines.keys())) or '无'}"
            ]
            
            # 添加存储统计
            if stats:
                store_type = stats.get("store_type", "unknown")
                total_vectors = (
                    stats.get("points_count") or 
                    stats.get("vectors_count") or 
                    stats.get("count") or 0
                )
                
                stats_info.extend([
                    f"📦 存储类型: {store_type}",
                    f"📊 文档分块数: {int(total_vectors)}",
                ])
                
                if "config" in stats:
                    config = stats["config"]
                    if isinstance(config, dict):
                        vector_size = config.get("vector_size", "unknown")
                        distance = config.get("distance", "unknown")
                        stats_info.extend([
                            f"🔢 向量维度: {vector_size}",
                            f"📎 距离度量: {distance}"
                        ])
            
            # 添加系统状态
            stats_info.extend([
                "",
                "🟢 **系统状态**",
                f"✅ RAG 管道: {'正常' if self.initialized else '异常'}",
                f"✅ LLM 连接: {'正常' if hasattr(self, 'llm') else '异常'}"
            ])
            
            return "\n".join(stats_info)
            
        except Exception as e:
            logger.exception("获取 RAG 统计信息失败")
            return f"❌ 获取统计信息失败: {str(e)}"

    def get_relevant_context(self, query: str, limit: int = 3, max_chars: int = 1200, namespace: Optional[str] = None) -> str:
        """为查询获取相关上下文
        
        这个方法可以被Agent调用来获取相关的知识库上下文
        """
        try:
            if not query:
                return ""
            
            # 使用统一 RAG 管道搜索
            pipeline = self._get_pipeline(namespace)
            results = pipeline["search"](
                query=query,
                top_k=limit
            )
            
            if not results:
                return ""
            
            # 合并上下文
            context_parts = []
            for result in results:
                content = result.get("metadata", {}).get("content", "")
                if content:
                    context_parts.append(content)
            
            merged_context = "\n\n".join(context_parts)
            
            # 限制长度
            if len(merged_context) > max_chars:
                merged_context = merged_context[:max_chars] + "..."
            
            return merged_context
            
        except Exception as e:
            return f"获取上下文失败: {str(e)}"
    
    def batch_add_texts(self, texts: List[str], document_ids: Optional[List[str]] = None, chunk_size: int = 800, chunk_overlap: int = 100, namespace: Optional[str] = None) -> str:
        """批量添加文本。"""
        try:
            if not texts:
                return "❌ 文本列表不能为空"
            
            if document_ids and len(document_ids) != len(texts):
                return "❌ 文本数量和文档ID数量不匹配"
            
            target_ns = self._normalize_namespace(namespace)
            t0 = time.time()
            total_chunks = 0
            successful_files = []
            failed_files = []
            
            for i, text in enumerate(texts):
                if not text or not text.strip():
                    continue
                    
                doc_id = document_ids[i] if document_ids else self._stable_text_document_id(text)
                try:
                    chunks_added, _, final_doc_id = self._index_text_document(
                        text=text,
                        document_id=doc_id,
                        namespace=target_ns,
                        chunk_size=chunk_size,
                        chunk_overlap=chunk_overlap,
                    )
                    total_chunks += chunks_added
                    if chunks_added > 0:
                        successful_files.append(final_doc_id)
                    else:
                        failed_files.append(f"{final_doc_id}: 未生成有效分块")
                except Exception as item_error:
                    failed_files.append(f"{doc_id}: {item_error}")
                    logger.warning("批量添加文本项失败: %s", doc_id, exc_info=True)
            
            process_ms = int((time.time() - t0) * 1000)
            result = [
                "✅ 批量添加完成",
                f"📊 成功文件: {len(successful_files)}/{len(texts)}",
                f"📊 总分块数: {total_chunks}",
                f"⏱️ 处理时间: {process_ms}ms",
                f"📝 命名空间: {target_ns}",
            ]
            if failed_files:
                result.append(f"❌ 失败: {len(failed_files)} 个文本")
                result.extend(failed_files)
            return "\n".join(result)
            
        except Exception as e:
            logger.exception("批量添加文本失败")
            return f"❌ 批量添加失败: {str(e)}"
    
    def clear_all_namespaces(self, confirm: bool = False) -> str:
        """清空当前工具管理的所有命名空间数据。"""
        if not confirm:
            return (
                "⚠️ 危险操作：清空所有命名空间将删除当前集合中的全部 RAG 数据！\n"
                "请传入 confirm=True 确认执行。"
            )

        try:
            for ns, pipeline in list(self._pipelines.items()):
                store = pipeline.get("store")
                if store:
                    logger.warning("清空 RAG 命名空间: %s", ns)
                    if hasattr(store, "clear_namespace"):
                        store.clear_namespace(ns)
                    else:
                        store.clear_collection()
            self._pipelines.clear()
            self._init_components()
            return "✅ 所有已加载命名空间数据已清空并重新初始化"
        except Exception as e:
            logger.exception("清空所有命名空间失败")
            return f"❌ 清空所有命名空间失败: {str(e)}"

    def shutdown(self) -> None:
        """关闭 RAG 相关连接并释放资源。"""
        QdrantConnectionManager.close_instances(
            url=self.qdrant_url,
            collection_name=self.collection_name,
        )
        self._pipelines.clear()
    
    # ========================================
    # 便捷接口方法（简化用户调用）
    # ========================================
    
    def add_document(self, file_path: str, namespace: str = "default") -> str:
        """便捷方法：添加单个文档"""
        return self.run({
            "action": "add_document",
            "file_path": file_path,
            "namespace": namespace
        }).text
    
    def add_text(self, text: str, namespace: str = "default", document_id: Optional[str] = None) -> str:
        """便捷方法：添加文本内容"""
        return self.run({
            "action": "add_text",
            "text": text,
            "namespace": namespace,
            "document_id": document_id
        }).text
    
    def ask(self, question: str, namespace: str = "default", **kwargs) -> str:
        """便捷方法：智能问答"""
        params = {
            "action": "ask",
            "question": question,
            "namespace": namespace
        }
        params.update(kwargs)
        return self.run(params).text
    
    def search(self, query: str, namespace: str = "default", **kwargs) -> str:
        """便捷方法：搜索知识库"""
        params = {
            "action": "search",
            "query": query,
            "namespace": namespace
        }
        params.update(kwargs)
        return self.run(params).text
    
    def add_documents_batch(self, file_paths: List[str], namespace: str = "default") -> str:
        """批量添加多个文档"""
        if not file_paths:
            return "❌ 文件路径列表不能为空"
        
        results = []
        successful = 0
        failed = 0
        total_chunks = 0
        start_time = time.time()
        
        for i, file_path in enumerate(file_paths, 1):
            logger.info("处理 RAG 文档 %s/%s: %s", i, len(file_paths), os.path.basename(file_path))
            
            try:
                result = self.add_document(file_path, namespace)
                if "✅" in result:
                    successful += 1
                    # 提取分块数量
                    if "分块数量:" in result:
                        chunks = int(result.split("分块数量: ")[1].split("\n")[0])
                        total_chunks += chunks
                else:
                    failed += 1
                    results.append(f"❌ {os.path.basename(file_path)}: 处理失败")
            except Exception as e:
                failed += 1
                results.append(f"❌ {os.path.basename(file_path)}: {str(e)}")
        
        process_time = int((time.time() - start_time) * 1000)
        
        summary = [
            "📊 **批量处理完成**",
            f"✅ 成功: {successful}/{len(file_paths)} 个文档",
            f"📊 总分块数: {total_chunks}",
            f"⏱️ 总耗时: {process_time}ms",
            f"📝 命名空间: {namespace}"
        ]
        
        if failed > 0:
            summary.append(f"❌ 失败: {failed} 个文档")
            summary.append("\n**失败详情:**")
            summary.extend(results)
        
        return "\n".join(summary)
    
    def add_texts_batch(self, texts: List[str], namespace: str = "default", document_ids: Optional[List[str]] = None) -> str:
        """批量添加多个文本。"""
        return self.batch_add_texts(
            texts=texts,
            document_ids=document_ids,
            namespace=namespace,
        )

