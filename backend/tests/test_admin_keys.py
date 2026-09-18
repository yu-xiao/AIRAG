"""M9.1:POST /api/admin/keys —— admin 为指定账号发 key;配额按目标用户;审计 by/to。"""
import json

from sqlalchemy import select, text

from app.core.config import settings
from app.models import ApiKey, AuditLog


async def _promote_self(client, auth_headers, db_session):
    me = await client.get("/api/auth/me", headers=auth_headers)
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": me.json()["id"]},
    )
    await db_session.commit()
    return me.json()["username"]


async def _mk_user(client, username):
    r = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    return r.json()["id"]


async def test_admin_creates_key_for_other(client, auth_headers, db_session):
    admin_name = await _promote_self(client, auth_headers, db_session)
    tid = await _mk_user(client, "m91_target1")
    resp = await client.post(
        "/api/admin/keys",
        json={"user_id": tid, "name": "给同事的key", "expires_in_days": 30},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["key"].startswith("airag_") and body["name"] == "给同事的key"
    row = await db_session.get(ApiKey, body["id"])
    assert row.user_id == tid
    assert row.key_hash != body["key"]  # 库里只有哈希
    assert row.expires_at is not None


async def test_admin_key_audit_by_to(client, auth_headers, db_session):
    admin_name = await _promote_self(client, auth_headers, db_session)
    tid = await _mk_user(client, "m91_target2")
    await client.post(
        "/api/admin/keys",
        json={"user_id": tid, "name": "审计key"},
        headers=auth_headers,
    )
    db_session.expire_all()
    rows = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "key_create")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].username == admin_name  # actor 列即 by
    detail = json.loads(rows[0].detail)
    assert detail["by"] == admin_name and detail["to"] == "m91_target2"
    assert detail["name"] == "审计key"


async def test_quota_counts_target_user(client, auth_headers, db_session, monkeypatch):
    await _promote_self(client, auth_headers, db_session)
    tid = await _mk_user(client, "m91_quota")
    monkeypatch.setattr(settings, "AGENT_MAX_KEYS_PER_USER", 1)
    r1 = await client.post("/api/admin/keys",
                           json={"user_id": tid, "name": "1"}, headers=auth_headers)
    r2 = await client.post("/api/admin/keys",
                           json={"user_id": tid, "name": "2"}, headers=auth_headers)
    assert r1.status_code == 201
    assert r2.status_code == 409
    assert r2.json()["detail"] == "api key limit reached"


async def test_non_admin_403(client, auth_headers):
    tid = await _mk_user(client, "m91_v")
    resp = await client.post(
        "/api/admin/keys", json={"user_id": tid, "name": "x"}, headers=auth_headers
    )
    assert resp.status_code == 403


async def test_missing_and_disabled_target(client, auth_headers, db_session):
    await _promote_self(client, auth_headers, db_session)
    resp = await client.post(
        "/api/admin/keys", json={"user_id": 999999, "name": "x"},
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "user not found"

    tid = await _mk_user(client, "m91_off")
    await db_session.execute(
        text("UPDATE users SET is_active = false WHERE id = :i"), {"i": tid}
    )
    await db_session.commit()
    resp = await client.post(
        "/api/admin/keys", json={"user_id": tid, "name": "x"}, headers=auth_headers
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "target user is disabled"


async def test_user_id_required_422(client, auth_headers, db_session):
    await _promote_self(client, auth_headers, db_session)
    resp = await client.post(
        "/api/admin/keys", json={"name": "x"}, headers=auth_headers
    )
    assert resp.status_code == 422


async def test_admin_issue_key_with_role(client, auth_headers, db_session):
    me = await client.get("/api/auth/me", headers=auth_headers)
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": me.json()["id"]},
    )
    await db_session.commit()
    r = await client.post(
        "/api/admin/keys",
        json={"user_id": me.json()["id"], "name": "代发编辑", "role": "editor"},
        headers=auth_headers,
    )
    assert r.status_code == 201
    assert r.json()["role"] == "editor"
