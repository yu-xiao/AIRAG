# backend/tests/test_doc_ops.py
"""M11 Task3:doc_ops 服务(校验/级联删除/重解析重置)。"""
import json

import pytest
from sqlalchemy import select

from app.models import Chunk, Document, KnowledgeBase
from app.services import doc_ops


async def _mk_kb(db, user_id, name="docops库") -> KnowledgeBase:
    kb = KnowledgeBase(name=name, owner_id=user_id)
    db.add(kb)
    await db.flush()
    return kb


async def _mk_doc(db, kb_id, content=b"dummy", name="a.docx",
                  status="done") -> Document:
    doc = Document(kb_id=kb_id, filename=name, file_path="x", mime="m",
                   size=len(content), sha256=name, status=status)
    db.add(doc)
    await db.flush()
    db.add(Chunk(document_id=doc.id, kb_id=kb_id, chunk_index=0,
                 content="c", char_len=1, content_hash="h"))
    await db.commit()
    return doc


async def test_save_upload_rejects_ext_and_size(client, auth_headers, db_session):
    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = await _mk_kb(db_session, me.json()["id"])
    with pytest.raises(doc_ops.DocOpError) as e1:
        await doc_ops.save_upload(db_session, kb, filename="a.exe",
                                  payload=b"x", mime=None, ocr_mode="auto",
                                  username="u", action="doc_upload")
    assert e1.value.status == 415
    assert e1.value.code == "unsupported_type"
    from app.core.config import settings
    big = b"z" * (settings.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    with pytest.raises(doc_ops.DocOpError) as e2:
        await doc_ops.save_upload(db_session, kb, filename="a.pdf",
                                  payload=big, mime=None, ocr_mode="auto",
                                  username="u", action="doc_upload")
    assert e2.value.status == 413
    assert e2.value.code == "too_large"


async def test_save_upload_dedup_and_dispatch(client, auth_headers, db_session,
                                              monkeypatch):
    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = await _mk_kb(db_session, me.json()["id"])
    calls = []
    monkeypatch.setattr("app.services.doc_ops.process_document",
                        type("T", (), {"delay": staticmethod(
                            lambda i: calls.append(i))}))
    doc = await doc_ops.save_upload(db_session, kb, filename="a.docx",
                                    payload=b"unique-bytes", mime="m",
                                    ocr_mode="auto", username="u",
                                    action="doc_upload")
    assert doc.id and calls == [doc.id]
    with pytest.raises(doc_ops.DocOpError) as e:
        await doc_ops.save_upload(db_session, kb, filename="again.docx",
                                  payload=b"unique-bytes", mime="m",
                                  ocr_mode="auto", username="u",
                                  action="doc_upload")
    assert e.value.status == 409
    assert e.value.code == "duplicate"


async def test_delete_cascades_chunks_and_file(client, auth_headers, db_session,
                                               tmp_path):
    from app.core.config import settings
    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = await _mk_kb(db_session, me.json()["id"])
    f = tmp_path / "f.docx"
    f.write_bytes(b"x")
    doc = await _mk_doc(db_session, kb.id)
    doc.file_path = str(f)
    await db_session.commit()
    await doc_ops.delete_document(db_session, doc, username="u",
                                  action="doc_delete")
    assert (await db_session.execute(
        select(Chunk).where(Chunk.document_id == doc.id))).scalars().first() is None
    assert (await db_session.get(Document, doc.id)) is None
    assert not f.exists()


async def test_delete_busy_409(client, auth_headers, db_session):
    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = await _mk_kb(db_session, me.json()["id"])
    doc = await _mk_doc(db_session, kb.id, status="parsing")
    with pytest.raises(doc_ops.DocOpError) as e:
        await doc_ops.delete_document(db_session, doc, username="u",
                                      action="doc_delete")
    assert e.value.status == 409
    assert e.value.code == "busy"


async def test_reprocess_resets(client, auth_headers, db_session, monkeypatch):
    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = await _mk_kb(db_session, me.json()["id"])
    doc = await _mk_doc(db_session, kb.id, status="failed")
    doc.error_msg = "boom"
    await db_session.commit()
    calls = []
    monkeypatch.setattr("app.services.doc_ops.process_document",
                        type("T", (), {"delay": staticmethod(
                            lambda i: calls.append(i))}))
    out = await doc_ops.reprocess_document(db_session, doc, username="u",
                                           action="doc_reprocess")
    assert out.status == "pending" and out.error_msg is None
    assert out.chunk_count == 0 and out.page_count is None
    assert calls == [doc.id]
    assert (await db_session.execute(
        select(Chunk).where(Chunk.document_id == doc.id))).scalars().first() is None
