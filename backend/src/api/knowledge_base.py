"""知识库 API 路由"""
import os
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional

router = APIRouter(prefix="/knowledge-base", tags=["knowledge-base"])


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


@router.get("/list", response_model=KnowledgeBaseListResponse)
async def list_documents(namespace: Optional[str] = Query(default=None)):
    """获取知识库文档列表。

    按 source_path 聚合 Qdrant 中的 RAG chunk，返回每个文档的名称和 chunk 数量。

    Args:
        namespace: 可选，限定 RAG 命名空间（默认扫描全部）
    """
    store = _get_store()
    docs = store.get_document_list(namespace=namespace)
    return KnowledgeBaseListResponse(
        documents=[DocumentInfo(**d) for d in docs],
        total=len(docs),
    )


@router.delete("/document")
async def delete_document(request: DeleteDocumentRequest):
    """删除指定文档及其所有 chunk。

    Args:
        source_path: 文档源路径
        namespace: RAG 命名空间
    """
    if not request.source_path or not request.source_path.strip():
        raise HTTPException(status_code=400, detail="source_path 不能为空")

    store = _get_store()
    conditions = {
        "source_path": request.source_path.strip(),
        "memory_type": "rag_chunk",
        "is_rag_data": True,
    }
    if request.namespace and request.namespace.strip():
        conditions["rag_namespace"] = request.namespace.strip()

    success = store.delete_by_filter(conditions)
    if not success:
        raise HTTPException(status_code=500, detail="删除文档失败")

    return {
        "message": "文档已删除",
        "source_path": request.source_path,
    }
