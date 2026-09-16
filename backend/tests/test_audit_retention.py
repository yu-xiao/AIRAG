from sqlalchemy import select, text

from app.models import AuditLog


async def _make_admin(client, db_session, username="ret_admin"):
    created = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": created.json()["id"]},
    )
    await db_session.commit()
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_purge_deletes_only_expired(db_session, monkeypatch):
    from app.core.config import settings
    from app.services.audit import audit, purge_expired

    await audit(db_session, "u1", "login_success", "t")
    await db_session.commit()
    await db_session.execute(
        text("UPDATE audit_logs SET created_at = now() - interval '200 days'")
    )
    await audit(db_session, "u2", "login_fail", "t")
    await db_session.commit()

    monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS", 180)
    deleted = await purge_expired(db_session)
    await db_session.commit()

    assert deleted == 1
    actions = (await db_session.execute(select(AuditLog.action))).scalars().all()
    assert "login_fail" in actions
    assert "audit_purge" in actions  # 摘要留在同事务
    assert "login_success" not in actions


async def test_purge_disabled_returns_zero(db_session, monkeypatch):
    from app.core.config import settings
    from app.services.audit import audit, purge_expired

    await audit(db_session, "u1", "login_success", "t")
    await db_session.commit()
    await db_session.execute(
        text("UPDATE audit_logs SET created_at = now() - interval '900 days'")
    )
    await db_session.commit()

    monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS", 0)
    assert await purge_expired(db_session) == 0
    await db_session.commit()
    assert (
        await db_session.execute(select(AuditLog.action))
    ).scalars().one() == "login_success"


async def test_purge_endpoint_admin_only(client, db_session, auth_headers, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS", 180)
    admin = await _make_admin(client, db_session)
    resp = await client.post("/api/admin/audit-logs/purge", headers=admin)
    assert resp.status_code == 200
    assert set(resp.json()) == {"deleted", "retention_days"}
    assert resp.json()["retention_days"] == 180

    denied = await client.post("/api/admin/audit-logs/purge", headers=auth_headers)
    assert denied.status_code == 403
