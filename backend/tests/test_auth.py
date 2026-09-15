import uuid


def _username() -> str:
    return f"user_{uuid.uuid4().hex[:8]}"


async def test_register_success(client):
    resp = await client.post(
        "/api/auth/register", json={"username": _username(), "password": "secret123"}
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] > 0
    assert data["role"] == "viewer"  # M4-R1:注册默认 viewer,admin 由晋升而来
    assert "password_hash" not in data


async def test_register_duplicate_username(client):
    name = _username()
    await client.post(
        "/api/auth/register", json={"username": name, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/register", json={"username": name, "password": "secret123"}
    )
    assert resp.status_code == 409


async def test_register_password_too_short(client):
    resp = await client.post(
        "/api/auth/register", json={"username": _username(), "password": "123"}
    )
    assert resp.status_code == 422


async def test_login_success(client):
    name = _username()
    await client.post(
        "/api/auth/register", json={"username": name, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": name, "password": "secret123"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


async def test_login_wrong_password(client):
    name = _username()
    await client.post(
        "/api/auth/register", json={"username": name, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": name, "password": "wrongpass1"}
    )
    assert resp.status_code == 401


async def _register_and_login(client, username: str) -> str:
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    return resp.json()["access_token"]


async def test_me_without_token(client):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


async def test_me_with_token(client):
    name = _username()
    token = await _register_and_login(client, name)
    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["username"] == name


async def test_me_with_garbage_token(client):
    resp = await client.get(
        "/api/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert resp.status_code == 401
