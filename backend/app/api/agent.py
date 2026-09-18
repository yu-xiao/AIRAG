# backend/app/api/agent.py
"""M9:REST 面(薄壳;能力全部来自 services/agent_facade)。"""
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_agent_principal
from app.db.session import get_db
from app.schemas.agent import (
    AgentAskIn,
    AgentAskOut,
    AgentHitOut,
    AgentKbListOut,
    AgentKbOut,
    AgentSearchIn,
    AgentSearchOut,
)
from app.services import agent_facade
from app.services.agent_ratelimit import (
    allow as rate_allow,
    quota_check,
    quota_consume,
)
from app.services.audit import audit

router = APIRouter(prefix="/agent", tags=["agent"])


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _check_rate(principal: Principal) -> None:
    """仅对 API Key 生效;JWT(人工调试)不限流。"""
    if principal.kind != "api_key" or principal.key_id is None:
        return
    ok, retry_after = await rate_allow(principal.key_id)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "retry_after": retry_after},
            headers={"Retry-After": str(retry_after)},
        )


async def _check_quota(principal: Principal) -> None:
    """ask 前置配额检查(仅 api_key);429 附 Retry-After 头。"""
    if principal.kind != "api_key" or principal.key_id is None:
        return
    ok, retry_after = await quota_check(principal.key_id)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail={"code": "quota_exhausted", "retry_after": retry_after},
            headers={"Retry-After": str(retry_after)},
        )


@router.get("/kbs", response_model=AgentKbListOut)
async def agent_kbs(
    request: Request,
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    await _check_rate(principal)
    items = await agent_facade.list_kbs_for(db, principal.user)
    await audit(
        db, principal.user.username, "agent.list_kbs", "agent",
        {"client": "rest", "key_name": principal.key_name, "kb_count": len(items)},
        ip=_ip(request),
    )
    await db.commit()
    return AgentKbListOut(items=[AgentKbOut(**asdict(i)) for i in items])


@router.post("/search", response_model=AgentSearchOut)
async def agent_search(
    payload: AgentSearchIn,
    request: Request,
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    await _check_rate(principal)
    try:
        outcome = await agent_facade.agent_search(
            db, principal.user, payload.kb_ids, payload.query,
            payload.top_k, payload.rerank,
        )
    except agent_facade.AgentKbDenied as e:
        raise HTTPException(
            status_code=403,
            detail={"code": "kb_forbidden", "denied_kb_ids": e.denied_kb_ids},
        )
    await audit(
        db, principal.user.username, "agent.search", "agent",
        {"client": "rest", "key_name": principal.key_name,
         "kb_ids": payload.kb_ids, "query": payload.query[:200],
         "hit_count": len(outcome.hits)},
        ip=_ip(request),
    )
    await db.commit()
    return AgentSearchOut(
        hits=[AgentHitOut(**asdict(h)) for h in outcome.hits],
        total=len(outcome.hits),
        elapsed_ms=outcome.elapsed_ms,
    )


@router.post("/ask", response_model=AgentAskOut)
async def agent_ask(
    payload: AgentAskIn,
    request: Request,
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    await _check_rate(principal)
    await _check_quota(principal)
    try:
        outcome = await agent_facade.agent_ask(
            db, principal.user, payload.kb_ids, payload.query, payload.rerank,
        )
    except agent_facade.AgentKbDenied as e:
        raise HTTPException(
            status_code=403,
            detail={"code": "kb_forbidden", "denied_kb_ids": e.denied_kb_ids},
        )
    if principal.kind == "api_key" and principal.key_id is not None:
        await quota_consume(principal.key_id, outcome.tokens_used)
    await audit(
        db, principal.user.username, "agent.ask", "agent",
        {"client": "rest", "key_name": principal.key_name,
         "kb_ids": payload.kb_ids, "query": payload.query[:200],
         "refused": outcome.refused, "tokens": outcome.tokens_used,
         "elapsed_ms": outcome.elapsed_ms},
        ip=_ip(request),
    )
    await db.commit()
    return AgentAskOut(
        answer=outcome.answer, citations=outcome.citations,
        refused=outcome.refused, tokens_used=outcome.tokens_used,
        elapsed_ms=outcome.elapsed_ms,
    )
