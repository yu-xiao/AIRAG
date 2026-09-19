# backend/app/services/agent_facade.py
"""M9:Agent 能力核心(唯一业务实现;REST 与 MCP 共用)。"""
import asyncio
import time
from dataclasses import dataclass

from langchain_core.callbacks import BaseCallbackHandler
from loguru import logger
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


async def _permitted_kb_ids(
    db: AsyncSession, user: User, kb_ids: list[int],
    key_scope: frozenset[int] | None = None,
) -> list[int]:
    """去重归一 + 逐库权限校验 + key scope 活交集(M12);无权限/不存在/
    界外统一抛 AgentKbDenied(不区分三者,不泄露存在性)。"""
    kb_ids = list(dict.fromkeys(kb_ids))
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
        or (key_scope is not None and kb_id not in key_scope)
    ]
    if denied:
        raise AgentKbDenied(denied)
    return kb_ids


async def list_kbs_for(db: AsyncSession, user: User,
                       key_scope: frozenset[int] | None = None) -> list[KbBrief]:
    """与 GET /api/kbs 同语义:admin 全库 owner;否则 自有 ∪ 被授权;
    key scope 非空时再取交集(M12)。"""
    rows = (await db.execute(select(KnowledgeBase))).scalars().all()
    out = []
    for kb in rows:
        if key_scope is not None and kb.id not in key_scope:
            continue
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
    key_scope: frozenset[int] | None = None,
) -> SearchOutcome:
    """权限过滤 → hybrid_search → 可选 rerank(与 rerank_node 同阈值语义)。"""
    kb_ids = await _permitted_kb_ids(db, user, kb_ids, key_scope)

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


# ---- M10:ask(非流式 RAG,复用完整问答图) ----

class TokenMeter(BaseCallbackHandler):
    """累计一次 ask 内全部 LLM 调用的 total_tokens(spec B)。
    取不到 usage 按 0(FakeListChatModel 等测试替身即 0,不阻断)。"""

    def __init__(self):
        self.total = 0

    def on_llm_end(self, response, **kwargs):
        try:
            got = 0
            for gens in response.generations:
                for g in gens:
                    um = getattr(getattr(g, "message", None),
                                 "usage_metadata", None)
                    if um:
                        got += int(um.get("total_tokens") or 0)
            if not got:  # 老形态兜底,避免与 message 双计
                tu = (getattr(response, "llm_output", None) or {}).get(
                    "token_usage")
                if tu:
                    got = int(tu.get("total_tokens") or 0)
            self.total += got
            if got == 0:
                logger.warning("token meter: no usage on llm response, counted 0")
        except Exception:
            logger.warning("token meter: usage unreadable, counted as 0",
                           exc_info=True)


_ask_graph = None


def _get_ask_graph():
    """checkpointer=None 的无状态单轮图,模块级单例(编译一次);
    测试 monkeypatch 模块属性 _ask_graph 注入假 LLM 图。"""
    global _ask_graph
    if _ask_graph is None:
        from app.services.chat_graph.graph import build_graph

        _ask_graph = build_graph(checkpointer=None)
    return _ask_graph


@dataclass
class AskOutcome:
    answer: str
    citations: list[dict]
    refused: bool
    tokens_used: int
    elapsed_ms: int


async def agent_ask(
    db: AsyncSession,
    user: User,
    kb_ids: list[int],
    query: str,
    rerank: bool,
    key_scope: frozenset[int] | None = None,
) -> AskOutcome:
    """权限过滤 → 完整问答图(非流式、单轮、无 checkpointer)→ 终态直出。
    图运行可达 90s:权限过滤后 commit 归还连接,避免长占 asyncpg 池
    (顺带持久化 principal 解析写入的 last_used_at,幂等无害)。"""
    kb_ids = await _permitted_kb_ids(db, user, kb_ids, key_scope)
    await db.commit()
    meter = TokenMeter()
    start = time.perf_counter()
    final = await _get_ask_graph().ainvoke(
        {"question": query, "kb_ids": kb_ids, "rerank": rerank, "history": []},
        config={"callbacks": [meter]},
    )
    return AskOutcome(
        answer=final.get("answer") or "",
        citations=final.get("citations") or [],
        refused=bool(final.get("refused")),
        tokens_used=meter.total,
        elapsed_ms=int((time.perf_counter() - start) * 1000),
    )
