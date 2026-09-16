from sqlalchemy import select

from app.models import AuditLog, Document


async def test_audit_log_roundtrip(db_session):
    db_session.add(
        AuditLog(username="u1", action="login_success", target="user:1",
                 detail='{"ip": "127.0.0.1"}', ip="127.0.0.1")
    )
    await db_session.commit()
    row = (await db_session.execute(select(AuditLog))).scalars().one()
    assert row.action == "login_success"
    assert row.created_at is not None


async def test_document_ocr_defaults(db_session):
    from app.models import KnowledgeBase, User
    from app.core.security import hash_password

    u = User(username="ocrdef", password_hash=hash_password("x"))
    db_session.add(u)
    await db_session.flush()
    kb = KnowledgeBase(name="k", owner_id=u.id)
    db_session.add(kb)
    await db_session.flush()
    doc = Document(kb_id=kb.id, filename="a.pdf", file_path="x", mime="a",
                   size=1, sha256="0" * 64)
    db_session.add(doc)
    await db_session.commit()
    assert doc.ocr_mode == "auto"
    assert doc.ocr_used is False
