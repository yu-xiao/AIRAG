# backend/tests/test_agent_api.py
"""M9 Task3:agent principal 双路径鉴权 + GET /api/agent/kbs 可见性 + 审计。"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.models import ApiKey, AuditLog
from app.services.api_keys import generate_api_key


async def _create_key(client, auth_headers, name="agent-key", days=None) -> str:
    resp = await client.post(
        "/api/auth/keys",
        json={"name": name, "expires_in_days": days},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    return resp.json()["key"]


async def _create_kb(client, auth_headers, name) -> int:
    resp = await client.post("/api/kbs", json={"name": name}, headers=auth_headers)
    assert resp.status_code == 201
    return resp.json()["id"]


async def test_kbs_with_jwt(client, auth_headers):
    kb_id = await _create_kb(client, auth_headers, "JWT可见库")
    resp = await client.get("/api/agent/kbs", headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [i["id"] for i in items] == [kb_id]
    assert items[0]["my_perm"] == "owner"


async def test_kbs_with_api_key_only_permitted(client, auth_headers, db_session):
    mine = await _create_kb(client, auth_headers, "我的库")
    # 他人库(注册默认 viewer,建库会被 403,先 SQL 提权为 editor——conftest 同法)
    await client.post(
        "/api/auth/register", json={"username": "agother1", "password": "secret123"}
    )
    await db_session.execute(
        text("UPDATE users SET role = 'editor' WHERE username = 'agother1'")
    )
    await db_session.commit()
    other_login = await client.post(
        "/api/auth/login", json={"username": "agother1", "password": "secret123"}
    )
    other_headers = {"Authorization": f"Bearer {other_login.json()['access_token']}"}
    await _create_kb(client, other_headers, "别人的库")

    key = await _create_key(client, auth_headers)
    resp = await client.get("/api/agent/kbs",
                            headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    ids = [i["id"] for i in resp.json()["items"]]
    assert mine in ids
    assert resp.json()["items"][0]["name"] == "我的库"

    # 他人 key 看不到我的库
    resp = await client.get("/api/agent/kbs", headers=other_headers)
    names = [i["name"] for i in resp.json()["items"]]
    assert "我的库" not in names


async def test_revoked_key_401(client, auth_headers):
    key = await _create_key(client, auth_headers)
    created = (await client.get("/api/auth/keys", headers=auth_headers)).json()[0]
    await client.delete(f"/api/auth/keys/{created['id']}", headers=auth_headers)
    resp = await client.get("/api/agent/kbs",
                            headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "key_revoked"


async def test_expired_key_401(client, auth_headers, db_session):
    me = await client.get("/api/auth/me", headers=auth_headers)
    uid = me.json()["id"]
    raw, prefix, digest = generate_api_key()
    db_session.add(ApiKey(user_id=uid, name="exp", key_prefix=prefix, key_hash=digest,
                          expires_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    await db_session.commit()
    resp = await client.get("/api/agent/kbs",
                            headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "key_expired"


async def test_disabled_user_key_401(client, auth_headers, db_session):
    me = await client.get("/api/auth/me", headers=auth_headers)
    key = await _create_key(client, auth_headers)
    await db_session.execute(
        text("UPDATE users SET is_active = false WHERE id = :i"),
        {"i": me.json()["id"]},
    )
    await db_session.commit()
    resp = await client.get("/api/agent/kbs",
                            headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 401


async def test_no_auth_401(client):
    resp = await client.get("/api/agent/kbs")
    assert resp.status_code == 401


async def test_audit_written(client, auth_headers, db_session):
    await _create_key(client, auth_headers, name="审计key")
    key = await _create_key(client, auth_headers)
    await client.get("/api/agent/kbs", headers={"Authorization": f"Bearer {key}"})
    db_session.expire_all()  # AsyncSession.expire_all 为同步方法,不可 await
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "agent.list_kbs")
    )).scalars().all()
    assert len(rows) == 1 and rows[0].username
