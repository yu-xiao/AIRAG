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


async def _make_admin(client, db_session, username="aud_admin"):
    from sqlalchemy import text as _text

    created = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    await db_session.execute(
        _text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": created.json()["id"]},
    )
    await db_session.commit()
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_audit_helper_jsonifies_detail(db_session):
    from app.services.audit import audit

    await audit(db_session, "u1", "kb_grant", "kb:3", {"perm": "viewer"})
    await db_session.commit()
    row = (await db_session.execute(select(AuditLog))).scalars().one()
    assert row.detail == '{"perm": "viewer"}'


async def test_audit_logs_api_admin_only_and_filters(client, db_session):
    from app.services.audit import audit

    await audit(db_session, "alice", "login_fail", "user:1")
    await audit(db_session, "bob", "doc_upload", "doc:9")
    await db_session.commit()

    admin = await _make_admin(client, db_session)
    resp = await client.get("/api/admin/audit-logs", headers=admin)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 2  # 挂点启用后还有注册/登录等系统审计
    seeded = [i for i in body["items"] if i["action"] in ("login_fail", "doc_upload")]
    assert {i["action"] for i in seeded} == {"login_fail", "doc_upload"}
    assert seeded[0]["action"] == "doc_upload"  # 时间倒序(id desc 近似)

    filtered = await client.get(
        "/api/admin/audit-logs", params={"username": "alice"}, headers=admin
    )
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["username"] == "alice"

    paged = await client.get(
        "/api/admin/audit-logs", params={"page": 2, "page_size": 1}, headers=admin
    )
    assert paged.json()["total"] >= 2
    assert len(paged.json()["items"]) == 1

    await client.post(
        "/api/auth/register", json={"username": "aud_plain", "password": "secret123"}
    )
    login = await client.post(
        "/api/auth/login", json={"username": "aud_plain", "password": "secret123"}
    )
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}
    denied = await client.get("/api/admin/audit-logs", headers=h)
    assert denied.status_code == 403


async def _seed_action(client, auth_headers):
    """跑一组会触发审计的动作,返回 (username, kb_id)。"""
    me = await client.get("/api/auth/me", headers=auth_headers)
    username = me.json()["username"]
    kb = await client.post("/api/kbs", json={"name": "审计库"}, headers=auth_headers)
    return username, kb.json()["id"]


async def test_audit_hooks_cover_write_paths(client, auth_headers, db_session):
    username, kb_id = await _seed_action(client, auth_headers)

    async def actions_of(action):
        return (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == action)
            )
        ).scalars().all()

    # kb_create
    rows = await actions_of("kb_create")
    assert any(r.username == username and r.target == f"kb:{kb_id}" for r in rows)

    # kb_grant / kb_revoke
    await client.post(
        "/api/auth/register", json={"username": "aud_grantee", "password": "secret123"}
    )
    await client.put(
        f"/api/kbs/{kb_id}/permissions",
        json={"username": "aud_grantee", "perm": "viewer"},
        headers=auth_headers,
    )
    await client.delete(
        f"/api/kbs/{kb_id}/permissions", params={"username": "aud_grantee"},
        headers=auth_headers,
    )
    assert await actions_of("kb_grant")
    assert await actions_of("kb_revoke")

    # doc_upload(fake docx,eager 流水线失败无妨,上传审计在入队前落)
    import io

    up = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("a.docx", io.BytesIO(b"dummy"), "application/octet-stream")},
        headers=auth_headers,
    )
    if up.status_code == 201:
        assert await actions_of("doc_upload")

    # conv_delete
    conv = await client.post(
        "/api/chat/conversations", json={"kb_ids": [kb_id], "name": "c"},
        headers=auth_headers,
    )
    await client.delete(
        f"/api/chat/conversations/{conv.json()['id']}", headers=auth_headers
    )
    assert await actions_of("conv_delete")

    # login_fail / register / user_admin_update
    await client.post(
        "/api/auth/login", json={"username": "aud_grantee", "password": "wrong"}
    )
    assert await actions_of("login_fail")
    assert await actions_of("register")
    admin = await _make_admin(client, db_session, username="aud_admin2")
    target = await client.post(
        "/api/auth/register", json={"username": "aud_target", "password": "secret123"}
    )
    await client.patch(
        f"/api/admin/users/{target.json()['id']}",
        json={"role": "editor"},
        headers=admin,
    )
    rows = await actions_of("user_admin_update")
    assert any("aud_target" in (r.detail or "") for r in rows)

    # login_success
    assert await actions_of("login_success")


async def test_kb_list_includes_doc_count(client, auth_headers, db_session):
    import io

    username, kb_id = await _seed_action(client, auth_headers)
    await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("n.docx", io.BytesIO(b"dummy"), "application/octet-stream")},
        headers=auth_headers,
    )
    empty_kb = await client.post(
        "/api/kbs", json={"name": "空库"}, headers=auth_headers
    )
    lst = await client.get("/api/kbs", headers=auth_headers)
    by_id = {k["id"]: k for k in lst.json()}
    assert by_id[kb_id]["doc_count"] == 1
    assert by_id[empty_kb.json()["id"]]["doc_count"] == 0
    detail = await client.get(f"/api/kbs/{kb_id}", headers=auth_headers)
    assert detail.json()["doc_count"] == 1

