import hashlib


async def _make_kb(client, auth_headers) -> int:
    resp = await client.post(
        "/api/kbs", json={"name": "上传测试库"}, headers=auth_headers
    )
    return resp.json()["id"]


async def test_upload_document(client, auth_headers):
    kb_id = await _make_kb(client, auth_headers)
    content = "AIRag upload test content".encode()
    resp = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("hello.txt", content, "text/plain")},
        headers=auth_headers,
    )
    assert resp.status_code == 415  # .txt 不在白名单


async def test_upload_docx_ok_and_duplicate(client, auth_headers):
    kb_id = await _make_kb(client, auth_headers)
    import io

    from docx import Document as Docx

    buf = io.BytesIO()
    d = Docx()
    d.add_paragraph("上传验收段落")
    d.save(buf)
    payload = buf.getvalue()

    resp = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("测试.docx", payload, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "pending"
    assert data["sha256"] == hashlib.sha256(payload).hexdigest()
    assert data["filename"].endswith("测试.docx")

    dup = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("again.docx", payload, "application/octet-stream")},
        headers=auth_headers,
    )
    assert dup.status_code == 409


async def test_upload_to_missing_kb(client, auth_headers):
    resp = await client.post(
        "/api/kbs/999999/documents",
        files={"file": ("a.pdf", b"x", "application/pdf")},
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_get_document_detail(client, auth_headers):
    kb_id = await _make_kb(client, auth_headers)
    up = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("b.docx", b"dummy", "application/octet-stream")},
        headers=auth_headers,
    )
    doc_id = up.json()["id"]
    got = await client.get(f"/api/documents/{doc_id}", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["id"] == doc_id
    missing = await client.get("/api/documents/999999", headers=auth_headers)
    assert missing.status_code == 404


async def test_chunks_endpoint(client, auth_headers, db_session):
    from sqlalchemy import select

    from app.models import Chunk

    kb_id = (await _make_kb(client, auth_headers))
    up = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("c.docx", b"dummy", "application/octet-stream")},
        headers=auth_headers,
    )
    doc_id = up.json()["id"]
    for i in range(3):
        db_session.add(
            Chunk(
                document_id=doc_id,
                kb_id=kb_id,
                chunk_index=i,
                content=f"chunk-{i} " + "字" * 250,
                page_no=1,
                char_len=257,
                content_hash=f"hash{i}",
            )
        )
    await db_session.commit()

    resp = await client.get(
        f"/api/documents/{doc_id}/chunks?page=1&page_size=2",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["items"][0]["chunk_index"] == 0
    assert len(body["items"][0]["content_preview"]) <= 200


async def test_chunks_endpoint_missing_document(client, auth_headers):
    resp = await client.get("/api/documents/999999/chunks", headers=auth_headers)
    assert resp.status_code == 404


async def test_chunks_endpoint_requires_auth(client):
    resp = await client.get("/api/documents/1/chunks")
    assert resp.status_code == 401
