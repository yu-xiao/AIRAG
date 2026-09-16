import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.perms import get_kb_perm, has_perm
from app.db.session import get_db
from app.models import Chunk, Document, KnowledgeBase, User
from app.schemas.document import DocumentOut
from app.services.audit import audit
from app.workers.pipeline import process_document

router = APIRouter(tags=["documents"])

ALLOWED_EXTS = {".pdf", ".docx", ".xlsx", ".jpg", ".jpeg", ".png"}


async def _get_visible_kb_or_404(
    db: AsyncSession, current: User, kb_id: int
) -> KnowledgeBase:
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or await get_kb_perm(db, current, kb) is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return kb


@router.post(
    "/kbs/{kb_id}/documents",
    response_model=DocumentOut,
    status_code=201,
)
async def upload_document(
    kb_id: int,
    file: UploadFile,
    ocr: Literal["auto", "force", "off"] = Form("auto"),
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_visible_kb_or_404(db, current, kb_id)
    if not has_perm(await get_kb_perm(db, current, kb), "editor"):
        raise HTTPException(status_code=403, detail="editor permission required")

    original = Path(file.filename or "unnamed").name
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=415, detail=f"unsupported file type: {ext}")

    payload = await file.read()
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    if len(payload) > max_bytes:
        raise HTTPException(status_code=413, detail="file too large")

    import hashlib

    sha256 = hashlib.sha256(payload).hexdigest()
    dup = await db.execute(
        select(Document).where(
            Document.kb_id == kb_id, Document.sha256 == sha256
        )
    )
    if dup.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="duplicate document in this kb")

    doc_dir = Path(settings.UPLOAD_DIR) / str(kb_id)
    doc_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex[:12]}_{original}"
    file_path = doc_dir / stored_name
    file_path.write_bytes(payload)

    doc = Document(
        kb_id=kb_id,
        filename=original,
        file_path=str(file_path),
        mime=file.content_type or "application/octet-stream",
        size=len(payload),
        sha256=sha256,
        ocr_mode=ocr,
    )
    db.add(doc)
    await db.flush()
    await audit(
        db, current.username, "doc_upload", f"doc:{doc.id}",
        {"filename": original, "kb_id": kb_id},
    )
    await db.commit()
    await db.refresh(doc)
    process_document.delay(doc.id)
    return doc


@router.get("/kbs/{kb_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    kb_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_visible_kb_or_404(db, current, kb_id)
    result = await db.execute(
        select(Document)
        .where(Document.kb_id == kb_id)
        .order_by(Document.id.desc())
    )
    return list(result.scalars().all())


async def _get_visible_document_or_404(
    db: AsyncSession, current: User, doc_id: int
) -> Document:
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    if kb is None or await get_kb_perm(db, current, kb) is None:
        raise HTTPException(status_code=404, detail="document not found")
    return doc


@router.get("/documents/{doc_id}", response_model=DocumentOut)
async def get_document(
    doc_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _get_visible_document_or_404(db, current, doc_id)


@router.get("/documents/{doc_id}/chunks")
async def list_chunks(
    doc_id: int,
    page: int = 1,
    page_size: int = 20,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_visible_document_or_404(db, current, doc_id)
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)

    total = (
        await db.execute(
            select(Chunk.id).where(Chunk.document_id == doc_id)
        )
    ).scalars().all()
    rows = (
        await db.execute(
            select(Chunk)
            .where(Chunk.document_id == doc_id)
            .order_by(Chunk.chunk_index)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return {
        "total": len(total),
        "items": [
            {
                "id": c.id,
                "chunk_index": c.chunk_index,
                "page_no": c.page_no,
                "char_len": c.char_len,
                "content_preview": c.content[:200],
            }
            for c in rows
        ],
    }


@router.post("/documents/{doc_id}/reprocess", response_model=DocumentOut)
async def reprocess_document(
    doc_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await _get_visible_document_or_404(db, current, doc_id)
    kb = await db.get(KnowledgeBase, doc.kb_id)
    if not has_perm(await get_kb_perm(db, current, kb), "editor"):
        raise HTTPException(status_code=403, detail="editor permission required")
    if doc.status in ("parsing", "chunking", "embedding"):
        raise HTTPException(status_code=409, detail="document is being processed")
    await db.execute(delete(Chunk).where(Chunk.document_id == doc_id))
    doc.status = "pending"
    doc.error_msg = None
    doc.chunk_count = 0
    doc.page_count = None
    await audit(db, current.username, "doc_reprocess", f"doc:{doc_id}")
    await db.commit()
    await db.refresh(doc)
    process_document.delay(doc_id)
    return doc
