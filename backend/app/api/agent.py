# backend/app/api/agent.py
"""M9:REST 面(薄壳;能力全部来自 services/agent_facade)。"""
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, UploadFile
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import (
    Principal,
    _api_key_id,
    get_agent_principal,
    require_editor_key,
)
from app.core.perms import get_kb_perm, has_perm
from app.db.session import get_db
from app.models import Document, KnowledgeBase
from app.schemas.agent import (
    AgentAskIn,
    AgentAskOut,
    AgentHitOut,
    AgentKbListOut,
    AgentKbOut,
    AgentQuotaOut,
    AgentSearchIn,
    AgentSearchOut,
)
from app.schemas.document import DocumentOut
from app.services import agent_facade, doc_ops
from app.services.agent_ratelimit import (
    allow as rate_allow,
    quota_check,
    quota_consume,
    quota_remaining,
)
from app.services.audit import audit

router = APIRouter(prefix="/agent", tags=["agent"])


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _check_rate(principal: Principal) -> None:
    """仅对 API Key 生效;JWT(人工调试)不限流。"""
    key_id = _api_key_id(principal)
    if key_id is None:
        return
    ok, retry_after = await rate_allow(key_id)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "retry_after": retry_after},
            headers={"Retry-After": str(retry_after)},
        )


async def _check_quota(principal: Principal) -> None:
    """ask 前置配额检查(仅 api_key);429 附 Retry-After 头。"""
    key_id = _api_key_id(principal)
    if key_id is None:
        return
    ok, retry_after = await quota_check(key_id)
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
    items = await agent_facade.list_kbs_for(db, principal.user,
                                            principal.key_scope)
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
            payload.top_k, payload.rerank, principal.key_scope,
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
            principal.key_scope,
        )
    except agent_facade.AgentKbDenied as e:
        raise HTTPException(
            status_code=403,
            detail={"code": "kb_forbidden", "denied_kb_ids": e.denied_kb_ids},
        )
    except Exception:
        logger.exception("agent ask failed")
        raise HTTPException(status_code=500, detail="internal error")
    if (key_id := _api_key_id(principal)) is not None:
        await quota_consume(key_id, outcome.tokens_used)
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


@router.get("/quota", response_model=AgentQuotaOut)
async def agent_quota(
    principal: Principal = Depends(get_agent_principal),
):
    """M11 小项⑥:今日 token 配额余量(仅 api_key;轮询不烧限流预算)。"""
    key_id = _api_key_id(principal)
    if key_id is None:
        raise HTTPException(status_code=403,
                            detail="api key principal required")
    return await quota_remaining(key_id)


# ---- M11:文档写操作五端点(读:可见即可;写:key=editor ∧ perm≥editor) ----

async def _kb_editor_or_403(db, principal: Principal, kb: KnowledgeBase) -> None:
    if not has_perm(await get_kb_perm(db, principal.user, kb), "editor"):
        raise HTTPException(status_code=403,
                            detail="editor permission required")
    require_editor_key(principal)


@router.get("/kbs/{kb_id}/documents", response_model=list[DocumentOut])
async def agent_list_documents(
    kb_id: int,
    request: Request,
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    await _check_rate(principal)
    await doc_ops.visible_kb_or_404(db, principal.user, kb_id,
                                    principal.key_scope)
    rows = (await db.execute(
        select(Document).where(Document.kb_id == kb_id)
        .order_by(Document.id.desc())
    )).scalars().all()
    await audit(
        db, principal.user.username, "agent.list_documents", "agent",
        {"client": "rest", "key_name": principal.key_name, "kb_id": kb_id,
         "doc_count": len(rows)},
        ip=_ip(request),
    )
    await db.commit()
    return rows


@router.get("/documents/{doc_id}", response_model=DocumentOut)
async def agent_get_document(
    doc_id: int,
    request: Request,
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    await _check_rate(principal)
    doc = await doc_ops.visible_doc_or_404(db, principal.user, doc_id,
                                           principal.key_scope)
    await audit(
        db, principal.user.username, "agent.get_document", "agent",
        {"client": "rest", "key_name": principal.key_name,
         "kb_id": doc.kb_id, "doc_id": doc.id, "status": doc.status},
        ip=_ip(request),
    )
    await db.commit()
    return doc


@router.post("/kbs/{kb_id}/documents", response_model=DocumentOut,
             status_code=201)
async def agent_upload_document(
    kb_id: int,
    request: Request,
    file: UploadFile,
    ocr: Literal["auto", "force", "off"] = Form("auto"),
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    await _check_rate(principal)
    cl = request.headers.get("content-length")
    if cl and int(cl) > (settings.MAX_UPLOAD_MB + 1) * 1024 * 1024:
        # M12 小项②:multipart 开销 +1MB 松余量,宁可漏报不可误报;
        # 权威校验仍在 save_upload(413)
        raise HTTPException(status_code=413, detail="file too large")
    kb = await doc_ops.visible_kb_or_404(db, principal.user, kb_id,
                                         principal.key_scope)
    await _kb_editor_or_403(db, principal, kb)
    payload = await file.read()
    return await doc_ops.save_upload(
        db, kb, filename=file.filename, payload=payload,
        mime=file.content_type, ocr_mode=ocr,
        username=principal.user.username, action="agent.upload_document",
        audit_extra={"client": "rest", "key_name": principal.key_name},
    )


@router.delete("/documents/{doc_id}", status_code=204)
async def agent_delete_document(
    doc_id: int,
    request: Request,
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    await _check_rate(principal)
    doc = await doc_ops.visible_doc_or_404(db, principal.user, doc_id,
                                           principal.key_scope)
    kb = await db.get(KnowledgeBase, doc.kb_id)
    await _kb_editor_or_403(db, principal, kb)
    await doc_ops.delete_document(
        db, doc, username=principal.user.username,
        action="agent.delete_document",
        audit_extra={"client": "rest", "key_name": principal.key_name},
    )
    return Response(status_code=204)


@router.post("/documents/{doc_id}/reprocess", response_model=DocumentOut)
async def agent_reprocess_document(
    doc_id: int,
    request: Request,
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    await _check_rate(principal)
    doc = await doc_ops.visible_doc_or_404(db, principal.user, doc_id,
                                           principal.key_scope)
    kb = await db.get(KnowledgeBase, doc.kb_id)
    await _kb_editor_or_403(db, principal, kb)
    return await doc_ops.reprocess_document(
        db, doc, username=principal.user.username,
        action="agent.reprocess_document",
        audit_extra={"client": "rest", "key_name": principal.key_name},
    )
