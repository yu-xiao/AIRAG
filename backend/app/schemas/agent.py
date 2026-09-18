# backend/app/schemas/agent.py
"""M9:agent 面请求/响应 schema(Task 4 追加 search 部分)。"""
from pydantic import BaseModel, Field

from app.core.config import settings


class AgentKbOut(BaseModel):
    id: int
    name: str
    description: str | None
    my_perm: str


class AgentKbListOut(BaseModel):
    items: list[AgentKbOut]


class AgentSearchIn(BaseModel):
    kb_ids: list[int] = Field(min_length=1, max_length=5)
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default_factory=lambda: settings.RETRIEVAL_TOP_K, ge=1, le=20)
    rerank: bool = False


class AgentHitOut(BaseModel):
    chunk_id: int
    document_id: int
    kb_id: int
    filename: str
    page_no: int | None
    content: str
    score: float
    source: str


class AgentSearchOut(BaseModel):
    hits: list[AgentHitOut]
    total: int
    elapsed_ms: int


class AgentAskIn(BaseModel):
    kb_ids: list[int] = Field(min_length=1, max_length=5)
    query: str = Field(min_length=1, max_length=500)
    rerank: bool = False


class AgentAskOut(BaseModel):
    answer: str
    citations: list[dict]
    refused: bool
    tokens_used: int
    elapsed_ms: int


class AgentQuotaOut(BaseModel):
    used: int | None  # None=禁用或 Redis 降级
    limit: int
    reset_at: str
