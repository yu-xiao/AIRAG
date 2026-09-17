# backend/app/services/agent_facade.py
"""M9:Agent 能力核心(唯一业务实现;REST 与 MCP 共用)。"""
import asyncio
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.perms import get_kb_perm
from app.models import KnowledgeBase, User
from app.services.retrieval.searcher import SearchHit, hybrid_search
from app.services.rerank.base import get_reranker


@dataclass
class KbBrief:
    id: int
    name: str
    description: str | None
    my_perm: str


class AgentKbDenied(Exception):
    """任一 kb_id 无权限或不存在;denied_kb_ids 不区分两者(不泄露存在性)。"""

    def __init__(self, denied_kb_ids: list[int]):
        super().__init__(denied_kb_ids)
        self.denied_kb_ids = denied_kb_ids


async def list_kbs_for(db: AsyncSession, user: User) -> list[KbBrief]:
    """与 GET /api/kbs 同语义:admin 全库 owner;否则 自有 ∪ 被授权。"""
    rows = (await db.execute(select(KnowledgeBase))).scalars().all()
    out = []
    for kb in rows:
        perm = await get_kb_perm(db, user, kb)
        if perm is not None:
            out.append(KbBrief(id=kb.id, name=kb.name,
                               description=kb.description, my_perm=perm))
    out.sort(key=lambda x: -x.id)
    return out


@dataclass
class SearchOutcome:
    hits: list[SearchHit]
    elapsed_ms: int


async def agent_search(
    db: AsyncSession,
    user: User,
    kb_ids: list[int],
    query: str,
    top_k: int,
    rerank: bool,
) -> SearchOutcome:
    """权限过滤 → hybrid_search → 可选 rerank(与 rerank_node 同阈值语义)。"""
    rows = (
        await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids))
        )
    ).scalars().all()
    by_id = {kb.id: kb for kb in rows}
    denied = [
        kb_id for kb_id in kb_ids
        if kb_id not in by_id
        or await get_kb_perm(db, user, by_id[kb_id]) is None
    ]
    if denied:
        raise AgentKbDenied(denied)

    start = time.perf_counter()
    hits = await hybrid_search(db, kb_ids, query, top_k=top_k)
    reranker = get_reranker() if rerank else None
    if reranker is not None and hits:
        scored = await asyncio.to_thread(
            reranker.rerank, query, [h.content for h in hits], top_k,
        )
        ranked = sorted(scored, key=lambda p: p[1], reverse=True)[:top_k]
        out: list[SearchHit] = []
        for idx, rel in ranked:
            if not 0 <= idx < len(hits):
                continue
            if settings.RETRIEVAL_MIN_SCORE > 0 and rel < settings.RETRIEVAL_MIN_SCORE:
                continue
            h = hits[idx]
            out.append(SearchHit(
                chunk_id=h.chunk_id, document_id=h.document_id, kb_id=h.kb_id,
                filename=h.filename, page_no=h.page_no, content=h.content,
                score=round(float(rel), 6), source=h.source,
            ))
        hits = out
    return SearchOutcome(hits=hits,
                         elapsed_ms=int((time.perf_counter() - start) * 1000))
