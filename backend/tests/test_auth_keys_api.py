# backend/tests/test_auth_keys_api.py
"""M9 Task2:key 管理端点(JWT-only,创建一次性明文/列表/吊销/配额)。"""
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.models import ApiKey


async def _mk_key(client, headers, name="t", days=None):
    return await client.post(
        "/api/auth/keys",
        json={"name": name, "expires_in_days": days},
        headers=headers,
    )


async def test_create_returns_plaintext_once(client, auth_headers, db_session):
    resp = await _mk_key(client, auth_headers, name="Cursor", days=30)
    assert resp.status_code == 201
    body = resp.json()
    assert body["key"].startswith("airag_")
    assert body["name"] == "Cursor"
    assert body["expires_at"] is not None
    # 库里只有哈希
    from sqlalchemy import select
    row = (await db_session.execute(select(ApiKey))).scalar_one()
    assert row.key_hash != body["key"] and row.key_prefix == body["key"][:14]


async def test_list_has_no_plaintext(client, auth_headers):
    await _mk_key(client, auth_headers, name="a")
    resp = await client.get("/api/auth/keys", headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert "key" not in items[0] and items[0]["key_prefix"].startswith("airag_")


async def test_revoke_then_404_for_foreign_key(client, auth_headers, db_session):
    created = (await _mk_key(client, auth_headers, name="r")).json()
    resp = await client.delete(f"/api/auth/keys/{created['id']}", headers=auth_headers)
    assert resp.status_code == 204
    row = await db_session.get(ApiKey, created["id"])
    assert row.is_active is False

    # 他人 key → 404(不泄露存在性)
    other = await client.post(
        "/api/auth/register",
        json={"username": "keyother1", "password": "secret123"},
    )
    other_login = await client.post(
        "/api/auth/login",
        json={"username": "keyother1", "password": "secret123"},
    )
    other_headers = {"Authorization": f"Bearer {other_login.json()['access_token']}"}
    resp = await client.delete(
        f"/api/auth/keys/{created['id']}", headers=other_headers
    )
    assert resp.status_code == 404


async def test_key_quota_409(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MAX_KEYS_PER_USER", 2)
    assert (await _mk_key(client, auth_headers, name="1")).status_code == 201
    assert (await _mk_key(client, auth_headers, name="2")).status_code == 201
    resp = await _mk_key(client, auth_headers, name="3")
    assert resp.status_code == 409


async def test_api_key_cannot_create_key(client, auth_headers):
    created = (await _mk_key(client, auth_headers, name="nochain")).json()
    resp = await client.post(
        "/api/auth/keys", json={"name": "x"},
        headers={"Authorization": f"Bearer {created['key']}"},
    )
    assert resp.status_code == 401  # get_current_user 只认 JWT


async def test_create_key_with_role_and_default(client, auth_headers):
    r = await client.post("/api/auth/keys",
                          json={"name": "编辑", "role": "editor"},
                          headers=auth_headers)
    assert r.status_code == 201
    assert r.json()["role"] == "editor"
    r2 = await client.post("/api/auth/keys", json={"name": "默认"}, headers=auth_headers)
    assert r2.status_code == 201
    assert r2.json()["role"] == "read_only"
    r3 = await client.post("/api/auth/keys",
                           json={"name": "坏角色", "role": "admin"},
                           headers=auth_headers)
    assert r3.status_code == 422
    lst = await client.get("/api/auth/keys", headers=auth_headers)
    assert all("role" in k for k in lst.json())
