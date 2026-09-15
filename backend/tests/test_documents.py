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
