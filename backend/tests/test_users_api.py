"""M9.1:GET /api/users 账号搜索(登录即可调,仅 id+username)+ /api/admin/users q/limit/offset。"""
from sqlalchemy import text


async def _mk_user(client, username):
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )


async def _promote_self(client, auth_headers, db_session):
    me = await client.get("/api/auth/me", headers=auth_headers)
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"), {"i": me.json()["id"]}
    )
    await db_session.commit()
    return me.json()


async def test_users_requires_auth(client):
    resp = await client.get("/api/users")
    assert resp.status_code == 401


async def test_users_allowed_for_plain_viewer(client):
    await _mk_user(client, "m91_viewer1")
    login = await client.post(
        "/api/auth/login", json={"username": "m91_viewer1", "password": "secret123"}
    )
    resp = await client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert resp.status_code == 200


async def test_users_brief_only(client, auth_headers):
    await _mk_user(client, "m91_alice")
    resp = await client.get("/api/users", headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()
    assert items and set(items[0]) == {"id", "username"}
    assert "m91_alice" in [i["username"] for i in items]


async def test_users_q_filter(client, auth_headers):
    for name in ("m91_bob", "m91_bobby", "m91_carol"):
        await _mk_user(client, name)
    resp = await client.get("/api/users", params={"q": "m91_bob"},
                            headers=auth_headers)
    names = [i["username"] for i in resp.json()]
    assert names == ["m91_bob", "m91_bobby"]


async def test_users_excludes_disabled(client, auth_headers, db_session):
    await _mk_user(client, "m91_off")
    await db_session.execute(
        text("UPDATE users SET is_active = false WHERE username = 'm91_off'")
    )
    await db_session.commit()
    resp = await client.get("/api/users", params={"q": "m91_off"},
                            headers=auth_headers)
    assert resp.json() == []


async def test_users_pagination(client, auth_headers):
    for i in range(3):
        await _mk_user(client, f"m91_pg{i:02d}")
    page1 = await client.get("/api/users", params={"limit": 2, "offset": 0},
                             headers=auth_headers)
    page2 = await client.get("/api/users", params={"limit": 2, "offset": 2},
                             headers=auth_headers)
    assert len(page1.json()) == 2
    n1 = {i["username"] for i in page1.json()}
    n2 = {i["username"] for i in page2.json()}
    assert n1 & n2 == set()


async def test_admin_users_q_limit_offset(client, auth_headers, db_session):
    await _promote_self(client, auth_headers, db_session)
    for i in range(3):
        await _mk_user(client, f"m91_aq{i}")
    resp = await client.get("/api/admin/users", params={"q": "m91_aq"},
                            headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 3
    resp = await client.get(
        "/api/admin/users", params={"q": "m91_aq", "limit": 2, "offset": 2},
        headers=auth_headers,
    )
    assert len(resp.json()) == 1


async def test_admin_users_no_params_returns_all(client, auth_headers, db_session):
    await _promote_self(client, auth_headers, db_session)
    resp = await client.get("/api/admin/users", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) >= 1  # 至少含自己;role/is_active 字段仍在
