import io

from sqlalchemy import select

from app.models import Chunk, Document


async def _upload_pdf(client, auth_headers, kb_id, text="AIRag pipeline pdf body"):
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("pipe.pdf", buf.getvalue(), "application/pdf")},
        headers=auth_headers,
    )


async def test_upload_runs_pipeline_to_done(client, auth_headers, db_session):
    kb = await client.post(
        "/api/kbs", json={"name": "流水线库"}, headers=auth_headers
    )
    kb_id = kb.json()["id"]
    up = await _upload_pdf(client, auth_headers, kb_id)
    assert up.status_code == 201
    doc_id = up.json()["id"]

    detail = await client.get(f"/api/documents/{doc_id}", headers=auth_headers)
    assert detail.json()["status"] == "done"
    assert detail.json()["chunk_count"] >= 1
    assert detail.json()["page_count"] == 1

    rows = (
        (await db_session.execute(select(Chunk).where(Chunk.document_id == doc_id)))
        .scalars()
        .all()
    )
    assert len(rows) >= 1
    assert rows[0].embedding is not None and len(rows[0].embedding) > 0
    assert rows[0].tsv is not None


async def test_mark_failed_sets_state(client, auth_headers, db_session):
    from app.workers.pipeline import _mark_failed

    kb = await client.post(
        "/api/kbs", json={"name": "失败库二"}, headers=auth_headers
    )
    kb_id = kb.json()["id"]
    up = await _upload_pdf(client, auth_headers, kb_id)
    doc_id = up.json()["id"]

    await _mark_failed(doc_id, "boom: parser exploded")

    doc = await db_session.get(Document, doc_id)
    await db_session.refresh(doc)
    assert doc.status == "failed"
    assert "parser exploded" in doc.error_msg
