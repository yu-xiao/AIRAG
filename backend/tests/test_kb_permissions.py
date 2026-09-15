async def _register_and_login(client, username):
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_owner_grants_updates_and_lists(client, auth_headers, db_session):
    mine = await client.post("/api/kbs", json={"name": "成员库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    grantee = await _register_and_login(client, "member_ed1")

    put = await client.put(
        f"/api/kbs/{kb_id}/permissions",
        json={"username": "member_ed1", "perm": "viewer"}, headers=auth_headers,
    )
    assert put.status_code == 200
    assert put.json()["perm"] == "viewer"

    put2 = await client.put(
        f"/api/kbs/{kb_id}/permissions",
        json={"username": "member_ed1", "perm": "editor"}, headers=auth_headers,
    )
    assert put2.status_code == 200
    assert put2.json()["perm"] == "editor"

    listed = await client.get(f"/api/kbs/{kb_id}/permissions", headers=auth_headers)
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["username"] == "member_ed1"
    assert rows[0]["perm"] == "editor"

    # 授权后对方可见该库
    visible = await client.get("/api/kbs", headers=grantee)
    assert any(k["id"] == kb_id for k in visible.json())


async def test_grant_requires_owner(client, auth_headers, db_session):
    from app.models import KbPermission

    mine = await client.post("/api/kbs", json={"name": "越权库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    editor_user = await _register_and_login(client, "grant_ed1")
    me = await client.get("/api/auth/me", headers=editor_user)
    db_session.add(KbPermission(kb_id=kb_id, user_id=me.json()["id"], perm="editor"))
    await db_session.commit()
    resp = await client.put(
        f"/api/kbs/{kb_id}/permissions",
        json={"username": "grant_ed1", "perm": "viewer"}, headers=editor_user,
    )
    assert resp.status_code == 403


async def test_revoke_member(client, auth_headers):
    mine = await client.post("/api/kbs", json={"name": "回收库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    await _register_and_login(client, "revoke_me1")  # 授权目标必须真实存在
    await client.put(
        f"/api/kbs/{kb_id}/permissions",
        json={"username": "revoke_me1", "perm": "viewer"}, headers=auth_headers,
    )
    resp = await client.delete(
        f"/api/kbs/{kb_id}/permissions?username=revoke_me1", headers=auth_headers
    )
    assert resp.status_code == 204
    listed = await client.get(f"/api/kbs/{kb_id}/permissions", headers=auth_headers)
    assert listed.json() == []
    again = await client.delete(
        f"/api/kbs/{kb_id}/permissions?username=revoke_me1", headers=auth_headers
    )
    assert again.status_code == 404


async def test_grant_target_validation(client, auth_headers):
    mine = await client.post("/api/kbs", json={"name": "校验库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    unknown = await client.put(
        f"/api/kbs/{kb_id}/permissions",
        json={"username": "no_such_user_x", "perm": "viewer"}, headers=auth_headers,
    )
    assert unknown.status_code == 404
    me = await client.get("/api/auth/me", headers=auth_headers)
    self_grant = await client.put(
        f"/api/kbs/{kb_id}/permissions",
        json={"username": me.json()["username"], "perm": "viewer"}, headers=auth_headers,
    )
    assert self_grant.status_code == 400  # 库主天然全权,不入表
    bad_perm = await client.put(
        f"/api/kbs/{kb_id}/permissions",
        json={"username": "revoke_me2", "perm": "owner"}, headers=auth_headers,
    )
    assert bad_perm.status_code == 422  # Literal 校验挡掉 owner 注入
