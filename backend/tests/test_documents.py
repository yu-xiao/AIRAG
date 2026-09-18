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


async def _register_and_login(client, username):
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_upload_permission_matrix(client, auth_headers, db_session):
    from app.models import KbPermission

    mine = await client.post("/api/kbs", json={"name": "上传权限库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    stranger = await _register_and_login(client, "up_stranger1")
    r1 = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("a.pdf", b"x", "application/pdf")}, headers=stranger,
    )
    assert r1.status_code == 404  # 不可见,不泄露

    viewer = await _register_and_login(client, "up_viewer1")
    me = await client.get("/api/auth/me", headers=viewer)
    db_session.add(KbPermission(kb_id=kb_id, user_id=me.json()["id"], perm="viewer"))
    await db_session.commit()
    r2 = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("a.pdf", b"x", "application/pdf")}, headers=viewer,
    )
    assert r2.status_code == 403  # 可见但无 editor

    stranger_list = await client.get(f"/api/kbs/{kb_id}/documents", headers=stranger)
    assert stranger_list.status_code == 404


async def test_reprocess_resets_failed_document(client, auth_headers, db_session, monkeypatch):
    import app.services.doc_ops as docs_mod
    from app.models import Chunk, Document, KnowledgeBase

    me = await client.get("/api/auth/me", headers=auth_headers)
    uid = me.json()["id"]
    kb = KnowledgeBase(name="重解析库", owner_id=uid)
    db_session.add(kb)
    await db_session.flush()
    doc = Document(
        kb_id=kb.id, filename="bad.pdf", file_path="x", mime="application/pdf",
        size=1, sha256="f" * 64, status="failed", error_msg="boom",
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(Chunk(document_id=doc.id, kb_id=kb.id, chunk_index=0,
                         content="旧块", page_no=1, char_len=3, content_hash="old"))
    await db_session.commit()

    calls = []

    class _FakeTask:
        @staticmethod
        def delay(doc_id):
            calls.append(doc_id)

    monkeypatch.setattr(docs_mod, "process_document", _FakeTask())
    resp = await client.post(f"/api/documents/{doc.id}/reprocess", headers=auth_headers)
    assert resp.status_code == 200
    assert calls == [doc.id]
    body = resp.json()
    assert body["status"] == "pending"
    assert body["error_msg"] is None
    from sqlalchemy import select

    left = (await db_session.execute(select(Chunk).where(Chunk.document_id == doc.id))).scalars().all()
    assert left == []


async def test_reprocess_conflict_while_processing(client, auth_headers, db_session):
    from app.models import Document, KnowledgeBase

    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = KnowledgeBase(name="处理中库", owner_id=me.json()["id"])
    db_session.add(kb)
    await db_session.flush()
    doc = Document(kb_id=kb.id, filename="p.pdf", file_path="x", mime="application/pdf",
                   size=1, sha256="e" * 64, status="parsing")
    db_session.add(doc)
    await db_session.commit()
    resp = await client.post(f"/api/documents/{doc.id}/reprocess", headers=auth_headers)
    assert resp.status_code == 409


async def test_reprocess_requires_editor(client, auth_headers, db_session):
    from app.models import Document, KbPermission, KnowledgeBase

    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = KnowledgeBase(name="只读重解析库", owner_id=me.json()["id"])
    db_session.add(kb)
    await db_session.flush()
    doc = Document(kb_id=kb.id, filename="v.pdf", file_path="x", mime="application/pdf",
                   size=1, sha256="d" * 64, status="done")
    db_session.add(doc)
    await db_session.commit()

    viewer = await _register_and_login(client, "rp_viewer1")
    vme = await client.get("/api/auth/me", headers=viewer)
    db_session.add(KbPermission(kb_id=kb.id, user_id=vme.json()["id"], perm="viewer"))
    await db_session.commit()
    resp = await client.post(f"/api/documents/{doc.id}/reprocess", headers=viewer)
    assert resp.status_code == 403


# ---- M11:DELETE /api/documents/{doc_id}(editor+,级联) ----
async def test_web_delete_document(client, auth_headers, db_session):
    kb = await client.post("/api/kbs", json={"name": "删除库"},
                           headers=auth_headers)
    kb_id = kb.json()["id"]
    up = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("a.docx", __import__("io").BytesIO(b"del-me"),
                        "application/octet-stream")},
        data={"ocr": "off"}, headers=auth_headers,
    )
    assert up.status_code == 201
    doc_id = up.json()["id"]
    r = await client.delete(f"/api/documents/{doc_id}", headers=auth_headers)
    assert r.status_code == 204
    assert (await client.get(f"/api/documents/{doc_id}",
                             headers=auth_headers)).status_code == 404


async def test_web_delete_visible_only_404(client, auth_headers):
    r = await client.delete("/api/documents/999999", headers=auth_headers)
    assert r.status_code == 404
