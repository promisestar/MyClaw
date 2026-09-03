"""知识库 API 路由

性能说明：``QdrantVectorStore.get_document_list`` / ``delete_by_filter`` 是同步
阻塞 HTTP 调用，统一通过 ``run_in_threadpool`` 派发到线程池执行，避免阻塞
FastAPI 事件循环（否则点击知识库菜单时其他接口会一起卡死）。
"""
import logging
import os
from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from typing import Any, Dict, List, Optional

router = APIRouter(prefix="/knowledge-base", tags=["knowledge-base"])
logger = logging.getLogger(__name__)


class DocumentInfo(BaseModel):
    """知识库文档信息"""
    source_path: str
    chunk_count: int
    first_content: str = ""
    rag_namespace: str = "default"


class KnowledgeBaseListResponse(BaseModel):
    """知识库文档列表响应"""
    documents: List[DocumentInfo]
    total: int


class DeleteDocumentRequest(BaseModel):
    """删除文档请求"""
    source_path: str
    namespace: str = "default"


def _get_store():
    """获取 QdrantVectorStore 实例（使用连接管理器复用连接）。"""
    from ..rag.qdrant_store import QdrantConnectionManager
    from ..rag.embedding import get_dimension

    qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")
    qdrant_api_key = os.getenv("QDRANT_API_KEY", "")
    collection_name = os.getenv("QDRANT_COLLECTION_NAME", "rag_knowledge_base")
    dimension = get_dimension()

    return QdrantConnectionManager.get_instance(
        url=qdrant_url,
        api_key=qdrant_api_key,
        collection_name=collection_name,
        vector_size=dimension,
        distance="cosine",
    )


def _delete_document_sync(store: Any, source_path: str, namespace: Optional[str]) -> int:
    """按 source_path 删除 RAG chunk；namespace 优先，未命中再回退到路径级删除。

    列表接口按 ``source_path`` 聚合且默认扫全命名空间，因此删除必须以路径为准，
    避免仅因 namespace 写死 ``default`` / 历史点缺 ``rag_namespace`` 字段导致假成功。
    """
    base: Dict[str, Any] = {
        "source_path": source_path,
        "memory_type": "rag_chunk",
        "is_rag_data": True,
    }
    ns = (namespace or "").strip()
    if ns:
        scoped = store.delete_by_filter({**base, "rag_namespace": ns})
        if scoped < 0:
            return -1
        if scoped > 0:
            return scoped
        logger.warning(
            "按 namespace=%s 未删到点，回退为仅按 source_path 删除: %s",
            ns,
            source_path,
        )

    return store.delete_by_filter(base)


@router.get("/list", response_model=KnowledgeBaseListResponse)
async def list_documents(namespace: Optional[str] = Query(default=None)):
    """获取知识库文档列表。

    按 source_path 聚合 Qdrant 中的 RAG chunk，返回每个文档的名称和 chunk 数量。

    Args:
        namespace: 可选，限定 RAG 命名空间（默认扫描全部）
    """
    store = _get_store()
    # 同步 Qdrant 调用派发到线程池，避免阻塞事件循环
    docs = await run_in_threadpool(store.get_document_list, namespace=namespace)
    return KnowledgeBaseListResponse(
        documents=[DocumentInfo(**d) for d in docs],
        total=len(docs),
    )


@router.delete("/document")
async def delete_document(request: DeleteDocumentRequest):
    """删除指定文档及其所有 chunk。

    Args:
        source_path: 文档源路径
        namespace: RAG 命名空间（优先按此过滤；未命中时回退为仅按路径删除）
    """
    if not request.source_path or not request.source_path.strip():
        raise HTTPException(status_code=400, detail="source_path 不能为空")

    store = _get_store()
    source_path = request.source_path.strip()
    deleted = await run_in_threadpool(
        _delete_document_sync,
        store,
        source_path,
        request.namespace,
    )
    if deleted < 0:
        raise HTTPException(status_code=500, detail="删除文档失败")
    if deleted == 0:
        raise HTTPException(
            status_code=404,
            detail="未找到可删除的文档分段（可能已删除，或路径不匹配）",
        )

    return {
        "message": "文档已删除",
        "source_path": source_path,
        "deleted": deleted,
    }
