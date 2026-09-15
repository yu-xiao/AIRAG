import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user
from app.db.session import get_db
from app.models import Document, KnowledgeBase, User
from app.schemas.document import DocumentOut

router = APIRouter(tags=["documents"])

ALLOWED_EXTS = {".pdf", ".docx", ".xlsx"}


async def _get_kb_or_404(db: AsyncSession, kb_id: int) -> KnowledgeBase:
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
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
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_kb_or_404(db, kb_id)

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
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


@router.get("/kbs/{kb_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    kb_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_kb_or_404(db, kb_id)
    result = await db.execute(
        select(Document)
        .where(Document.kb_id == kb_id)
        .order_by(Document.id.desc())
    )
    return list(result.scalars().all())


@router.get("/documents/{doc_id}", response_model=DocumentOut)
async def get_document(
    doc_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    return doc
