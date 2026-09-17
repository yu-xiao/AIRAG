async def test_create_kb(client, auth_headers):
    resp = await client.post(
        "/api/kbs",
        json={"name": "产品手册库", "description": "内部产品文档"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] > 0
    assert data["name"] == "产品手册库"
    assert data["embed_provider"] == "zhipu"
    assert data["embed_model"] == "embedding-3"


async def test_create_kb_requires_auth(client):
    resp = await client.post("/api/kbs", json={"name": "匿名库"})
    assert resp.status_code == 401


async def test_list_and_get_kb(client, auth_headers):
    await client.post(
        "/api/kbs", json={"name": "法务库"}, headers=auth_headers
    )
    listed = await client.get("/api/kbs", headers=auth_headers)
    assert listed.status_code == 200
    names = [item["name"] for item in listed.json()]
    assert "法务库" in names

    kb_id = listed.json()[0]["id"]
    got = await client.get(f"/api/kbs/{kb_id}", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["id"] == kb_id

    missing = await client.get("/api/kbs/999999", headers=auth_headers)
    assert missing.status_code == 404


async def _register_and_login(client, username):
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_viewer_cannot_create_kb(client, auth_headers):
    plain = await _register_and_login(client, "plain_viewer1")
    resp = await client.post("/api/kbs", json={"name": "游客库"}, headers=plain)
    assert resp.status_code == 403
    # editor 仍可建(auth_headers 已升 editor)
    ok = await client.post("/api/kbs", json={"name": "编辑库"}, headers=auth_headers)
    assert ok.status_code == 201


async def test_kb_invisible_to_stranger(client, auth_headers):
    mine = await client.post("/api/kbs", json={"name": "私库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    other = await _register_and_login(client, "stranger_ed1")  # 默认 viewer
    listed = await client.get("/api/kbs", headers=other)
    assert all(k["id"] != kb_id for k in listed.json())
    got = await client.get(f"/api/kbs/{kb_id}", headers=other)
    assert got.status_code == 404


async def test_list_returns_my_perm(client, auth_headers, db_session):
    from app.models import KbPermission

    mine = await client.post("/api/kbs", json={"name": "权限标注库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    listed = await client.get("/api/kbs", headers=auth_headers)
    row = next(k for k in listed.json() if k["id"] == kb_id)
    assert row["my_perm"] == "owner"

    viewer_headers = await _register_and_login(client, "perm_viewer1")
    reg = await client.get("/api/auth/me", headers=viewer_headers)
    db_session.add(KbPermission(kb_id=kb_id, user_id=reg.json()["id"], perm="viewer"))
    await db_session.commit()
    granted = await client.get("/api/kbs", headers=viewer_headers)
    row2 = next(k for k in granted.json() if k["id"] == kb_id)
    assert row2["my_perm"] == "viewer"


async def test_admin_sees_all_kbs(client, auth_headers, db_session):
    from sqlalchemy import text as _text

    mine = await client.post("/api/kbs", json={"name": "他人库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    me = await client.get("/api/auth/me", headers=auth_headers)
    await db_session.execute(
        _text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": me.json()["id"]},
    )
    await db_session.commit()
    listed = await client.get("/api/kbs", headers=auth_headers)
    row = next(k for k in listed.json() if k["id"] == kb_id)
    assert row["my_perm"] == "owner"


async def test_create_kb_duplicate_name_409(client, auth_headers):
    first = await client.post("/api/kbs", json={"name": "重名库"}, headers=auth_headers)
    dup = await client.post("/api/kbs", json={"name": "重名库"}, headers=auth_headers)
    assert first.status_code == 201
    assert dup.status_code == 409
    assert dup.json()["detail"] == "knowledge base name already exists"


async def test_create_kb_duplicate_after_strip_409(client, auth_headers):
    await client.post("/api/kbs", json={"name": "归一库"}, headers=auth_headers)
    dup = await client.post("/api/kbs", json={"name": "  归一库  "}, headers=auth_headers)
    assert dup.status_code == 409
    listed = await client.get("/api/kbs", headers=auth_headers)
    names = [k["name"] for k in listed.json() if k["name"].strip() == "归一库"]
    assert names == ["归一库"]  # 入库即 strip 后形态,仅一条


async def test_create_kb_blank_after_strip_422(client, auth_headers):
    resp = await client.post("/api/kbs", json={"name": "   "}, headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "knowledge base name cannot be blank"


async def test_create_kb_duplicate_name_409_when_db_already_has_two(client, auth_headers, db_session):
    """终审加固:库中已存在两条同名记录时不得 500(MultipleResultsFound),仍应 409。"""
    from app.models import KnowledgeBase

    me = await client.get("/api/auth/me", headers=auth_headers)
    owner_id = me.json()["id"]
    db_session.add_all([
        KnowledgeBase(name="双胞胎库", owner_id=owner_id),
        KnowledgeBase(name="双胞胎库", owner_id=owner_id),
    ])
    await db_session.commit()
    resp = await client.post("/api/kbs", json={"name": "双胞胎库"}, headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "knowledge base name already exists"
