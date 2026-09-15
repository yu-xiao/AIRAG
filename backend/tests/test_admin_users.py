async def _register_and_login(client, username):
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _promote_to_admin(client, db_session, headers):
    from sqlalchemy import text

    me = await client.get("/api/auth/me", headers=headers)
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"), {"i": me.json()["id"]}
    )
    await db_session.commit()
    return me.json()["id"]


async def test_admin_lists_and_updates_users(client, auth_headers, db_session):
    my_id = await _promote_to_admin(client, db_session, auth_headers)
    target = await _register_and_login(client, "admin_target1")

    listed = await client.get("/api/admin/users", headers=auth_headers)
    assert listed.status_code == 200
    ids = [u["id"] for u in listed.json()]
    assert my_id in ids

    tme = await client.get("/api/auth/me", headers=target)
    tid = tme.json()["id"]
    patched = await client.patch(
        f"/api/admin/users/{tid}",
        json={"role": "editor", "is_active": False}, headers=auth_headers,
    )
    assert patched.status_code == 200
    assert patched.json()["role"] == "editor"
    assert patched.json()["is_active"] is False

    # 被禁用的用户登录被拒
    login = await client.post(
        "/api/auth/login", json={"username": "admin_target1", "password": "secret123"}
    )
    assert login.status_code == 401


async def test_admin_endpoints_forbid_non_admin(client, auth_headers):
    listed = await client.get("/api/admin/users", headers=auth_headers)
    assert listed.status_code == 403


async def test_admin_cannot_modify_self(client, auth_headers, db_session):
    my_id = await _promote_to_admin(client, db_session, auth_headers)
    resp = await client.patch(
        f"/api/admin/users/{my_id}", json={"role": "viewer"}, headers=auth_headers
    )
    assert resp.status_code == 400
