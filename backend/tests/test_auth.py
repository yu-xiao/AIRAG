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
    assert data["role"] == "admin"
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
