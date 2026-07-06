"""知识库管理后台路由：文档上传/列表/删除 + 语义召回。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from athena.api.auth import TenantContext, require_tenant
from athena.api.response import ApiResponse
from athena.api.routes._deps import get_knowledge_base
from athena.api.services import ApiServiceError
from athena.memory.knowledge_base import KnowledgeBaseManager, RecallRule

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


class IngestRequest(BaseModel):
    """上传知识文档请求。"""

    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class RecallRequest(BaseModel):
    """语义召回请求。"""

    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)


@router.post("/documents")
async def ingest_document(
    payload: IngestRequest,
    kb: KnowledgeBaseManager = Depends(get_knowledge_base),
    tenant: TenantContext = Depends(require_tenant),
) -> ApiResponse[dict]:
    """上传并入库一篇知识文档。"""
    try:
        doc = await kb.ingest(payload.title, payload.content, payload.tags)
    except ValueError as exc:
        raise ApiServiceError("KNOWLEDGE_INVALID", str(exc), status_code=400) from exc
    return ApiResponse.ok(
        {"doc_id": doc.doc_id, "title": doc.title, "chunk_count": doc.chunk_count}
    )


@router.get("/documents")
async def list_documents(
    kb: KnowledgeBaseManager = Depends(get_knowledge_base),
    tenant: TenantContext = Depends(require_tenant),
) -> ApiResponse[list]:
    """列出所有已入库文档。"""
    docs = [
        {
            "doc_id": d.doc_id,
            "title": d.title,
            "tags": list(d.tags),
            "chunk_count": d.chunk_count,
            "created_at": d.created_at,
        }
        for d in kb.list_documents()
    ]
    return ApiResponse.ok(docs)


@router.delete("/documents/{doc_id}")
async def delete_document(
    doc_id: str,
    kb: KnowledgeBaseManager = Depends(get_knowledge_base),
    tenant: TenantContext = Depends(require_tenant),
) -> ApiResponse[dict]:
    """删除一篇知识文档。"""
    if not kb.delete_document(doc_id):
        raise ApiServiceError("KNOWLEDGE_NOT_FOUND", "document not found", status_code=404)
    return ApiResponse.ok({"deleted": doc_id})


@router.post("/search")
async def recall(
    payload: RecallRequest,
    kb: KnowledgeBaseManager = Depends(get_knowledge_base),
    tenant: TenantContext = Depends(require_tenant),
) -> ApiResponse[list]:
    """按召回规则做语义检索。"""
    rule = RecallRule(
        top_k=payload.top_k,
        min_score=payload.min_score,
        tags=tuple(payload.tags),
    )
    try:
        hits = await kb.recall(payload.query, rule)
    except ValueError as exc:
        raise ApiServiceError("KNOWLEDGE_INVALID", str(exc), status_code=400) from exc
    return ApiResponse.ok(
        [
            {
                "doc_id": h.doc_id,
                "title": h.title,
                "chunk": h.chunk,
                "score": round(h.score, 4),
                "tags": list(h.tags),
            }
            for h in hits
        ]
    )
