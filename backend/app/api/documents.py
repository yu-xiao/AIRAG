# backend/app/api/documents.py
from typing import Literal

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Response,
    UploadFile,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.perms import get_kb_perm, has_perm
from app.db.session import get_db
from app.models import Chunk, Document, KnowledgeBase, User
from app.schemas.document import DocumentOut
from app.services import doc_ops

router = APIRouter(tags=["documents"])


async def _require_kb_editor(db: AsyncSession, current: User,
                             kb_id: int) -> None:
    kb = await doc_ops.visible_kb_or_404(db, current, kb_id)
    if not has_perm(await get_kb_perm(db, current, kb), "editor"):
        raise HTTPException(status_code=403,
                            detail="editor permission required")


@router.post("/kbs/{kb_id}/documents", response_model=DocumentOut,
             status_code=201)
async def upload_document(
    kb_id: int,
    file: UploadFile,
    ocr: Literal["auto", "force", "off"] = Form("auto"),
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_kb_editor(db, current, kb_id)
    kb = await db.get(KnowledgeBase, kb_id)
    payload = await file.read()
    return await doc_ops.save_upload(
        db, kb, filename=file.filename, payload=payload,
        mime=file.content_type, ocr_mode=ocr,
        username=current.username, action="doc_upload",
    )


@router.get("/kbs/{kb_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    kb_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await doc_ops.visible_kb_or_404(db, current, kb_id)
    result = await db.execute(
        select(Document).where(Document.kb_id == kb_id)
        .order_by(Document.id.desc())
    )
    return list(result.scalars().all())


@router.get("/documents/{doc_id}", response_model=DocumentOut)
async def get_document(
    doc_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await doc_ops.visible_doc_or_404(db, current, doc_id)


@router.get("/documents/{doc_id}/chunks")
async def list_chunks(
    doc_id: int,
    page: int = 1,
    page_size: int = 20,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await doc_ops.visible_doc_or_404(db, current, doc_id)
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    total = (await db.execute(
        select(Chunk.id).where(Chunk.document_id == doc_id)
    )).scalars().all()
    rows = (await db.execute(
        select(Chunk).where(Chunk.document_id == doc_id)
        .order_by(Chunk.chunk_index)
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {
        "total": len(total),
        "items": [
            {"id": c.id, "chunk_index": c.chunk_index, "page_no": c.page_no,
             "char_len": c.char_len, "content_preview": c.content[:200]}
            for c in rows
        ],
    }


@router.post("/documents/{doc_id}/reprocess", response_model=DocumentOut)
async def reprocess_document(
    doc_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await doc_ops.visible_doc_or_404(db, current, doc_id)
    kb = await db.get(KnowledgeBase, doc.kb_id)
    if not has_perm(await get_kb_perm(db, current, kb), "editor"):
        raise HTTPException(status_code=403,
                            detail="editor permission required")
    return await doc_ops.reprocess_document(
        db, doc, username=current.username, action="doc_reprocess")


@router.delete("/documents/{doc_id}", status_code=204)
async def delete_document(
    doc_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await doc_ops.visible_doc_or_404(db, current, doc_id)
    kb = await db.get(KnowledgeBase, doc.kb_id)
    if not has_perm(await get_kb_perm(db, current, kb), "editor"):
        raise HTTPException(status_code=403,
                            detail="editor permission required")
    await doc_ops.delete_document(
        db, doc, username=current.username, action="doc_delete")
    return Response(status_code=204)
