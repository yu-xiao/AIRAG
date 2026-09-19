# backend/app/services/doc_ops.py
"""M11:文档写操作核心(Web/REST/MCP 三面唯一实现)。

校验失败抛 HTTPException(404/409/413/415),三面语义一致;MCP 工具捕获后
转 ToolError。审计与 commit/dispatch 在本模块完成,action 名由调用面传入
(doc_upload vs agent.upload_document),audit_extra 叠加 client/key_name。
"""
import hashlib
import uuid
from pathlib import Path

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.perms import get_kb_perm
from app.models import Chunk, Document, KnowledgeBase, User
from app.services.audit import audit
from app.workers.pipeline import process_document

ALLOWED_EXTS = {".pdf", ".docx", ".xlsx", ".jpg", ".jpeg", ".png"}
BUSY_STATUSES = ("parsing", "chunking", "embedding")


async def visible_kb_or_404(
    db: AsyncSession, user: User, kb_id: int,
    key_scope: frozenset[int] | None = None,
) -> KnowledgeBase:
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or await get_kb_perm(db, user, kb) is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if key_scope is not None and kb.id not in key_scope:  # M12:scope 折入可见性
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return kb


async def visible_doc_or_404(
    db: AsyncSession, user: User, doc_id: int,
    key_scope: frozenset[int] | None = None,
) -> Document:
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    if kb is None or await get_kb_perm(db, user, kb) is None:
        raise HTTPException(status_code=404, detail="document not found")
    if key_scope is not None and doc.kb_id not in key_scope:  # M12
        raise HTTPException(status_code=404, detail="document not found")
    return doc


def _detail(doc_file: str, kb_id: int, extra: dict | None) -> dict:
    detail = {"filename": doc_file, "kb_id": kb_id}
    if extra:
        detail.update(extra)
    return detail


async def save_upload(
    db: AsyncSession, kb: KnowledgeBase, *, filename: str, payload: bytes,
    mime: str | None, ocr_mode: str, username: str, action: str,
    audit_extra: dict | None = None,
) -> Document:
    """校验(415/413/409)→ 落盘 → 建行 → 审计 → commit → dispatch。"""
    original = Path(filename or "unnamed").name
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=415,
                            detail=f"unsupported file type: {ext}")
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    if len(payload) > max_bytes:
        raise HTTPException(status_code=413, detail="file too large")
    sha256 = hashlib.sha256(payload).hexdigest()
    dup = await db.execute(
        select(Document).where(Document.kb_id == kb.id,
                               Document.sha256 == sha256)
    )
    if dup.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409,
                            detail="duplicate document in this kb")

    doc_dir = Path(settings.UPLOAD_DIR) / str(kb.id)
    doc_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex[:12]}_{original}"
    file_path = doc_dir / stored_name
    file_path.write_bytes(payload)

    doc = Document(
        kb_id=kb.id, filename=original, file_path=str(file_path),
        mime=mime or "application/octet-stream", size=len(payload),
        sha256=sha256, ocr_mode=ocr_mode,
    )
    db.add(doc)
    await db.flush()
    await audit(db, username, action, f"doc:{doc.id}",
                _detail(original, kb.id, audit_extra) | {"size": len(payload)})
    await db.commit()
    await db.refresh(doc)
    process_document.delay(doc.id)
    return doc


async def delete_document(
    db: AsyncSession, doc: Document, *, username: str, action: str,
    audit_extra: dict | None = None,
) -> None:
    """busy 409 → 删 chunks(pgvector/tsv 随行消失)→ 尽力删磁盘文件
    → 审计 → 删行 → commit(不可逆,调用面前置权限校验)。"""
    if doc.status in BUSY_STATUSES:
        raise HTTPException(status_code=409,
                            detail="document is being processed")
    await db.execute(delete(Chunk).where(Chunk.document_id == doc.id))
    try:
        Path(doc.file_path).unlink(missing_ok=True)
    except OSError:
        logger.warning(f"doc file removal failed: {doc.file_path}")
    await audit(db, username, action, f"doc:{doc.id}",
                _detail(doc.filename, doc.kb_id, audit_extra))
    await db.delete(doc)
    await db.commit()


async def reprocess_document(
    db: AsyncSession, doc: Document, *, username: str, action: str,
    audit_extra: dict | None = None,
) -> Document:
    """busy 409 → 清 chunks → 重置状态 → 审计 → commit → dispatch。"""
    if doc.status in BUSY_STATUSES:
        raise HTTPException(status_code=409,
                            detail="document is being processed")
    await db.execute(delete(Chunk).where(Chunk.document_id == doc.id))
    doc.status = "pending"
    doc.error_msg = None
    doc.chunk_count = 0
    doc.page_count = None
    await audit(db, username, action, f"doc:{doc.id}",
                _detail(doc.filename, doc.kb_id, audit_extra))
    await db.commit()
    await db.refresh(doc)
    process_document.delay(doc.id)
    return doc
