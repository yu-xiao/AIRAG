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


# ---- M12:admin 代发 scoped key(校验按目标用户) ----
async def test_admin_issue_scoped_key(client, auth_headers, db_session):
    from sqlalchemy import text

    # 提权 admin(仅请求者;校验须按目标用户而非请求者)
    me = await client.get("/api/auth/me", headers=auth_headers)
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": me.json()["id"]})
    await db_session.commit()
    # 目标用户(非 admin):自建库可访问;第三方库不可访问
    tid = (await client.post(
        "/api/auth/register",
        json={"username": "scope_tgt1", "password": "secret123"},
    )).json()["id"]
    await client.post("/api/auth/register",
                      json={"username": "scope_tg1", "password": "secret123"})
    await db_session.execute(text(
        "UPDATE users SET role = 'editor' "
        "WHERE username IN ('scope_tgt1', 'scope_tg1')"))
    await db_session.commit()
    tgt = await client.post("/api/auth/login",
                            json={"username": "scope_tgt1",
                                  "password": "secret123"})
    tg = await client.post("/api/auth/login",
                           json={"username": "scope_tg1",
                                 "password": "secret123"})
    target_kb = (await client.post(
        "/api/kbs", json={"name": "目标用户的库"},
        headers={"Authorization": f"Bearer {tgt.json()['access_token']}"},
    )).json()["id"]
    tg_kb = await client.post(
        "/api/kbs", json={"name": "第三方库"},
        headers={"Authorization": f"Bearer {tg.json()['access_token']}"})
    r = await client.post("/api/admin/keys",
                          json={"user_id": tid,
                                "name": "代发范围",
                                "kb_scope": [target_kb]},
                          headers=auth_headers)
    assert r.status_code == 201 and r.json()["kb_scope"] == [target_kb]
    # 若按 admin 请求者判,tg 库对其隐式可见;按目标用户判 → 422
    r2 = await client.post("/api/admin/keys",
                           json={"user_id": tid,
                                 "name": "代发越界",
                                 "kb_scope": [tg_kb.json()["id"]]},
                          headers=auth_headers)
    assert r2.status_code == 422


# ---- M12:admin 查目标用户可见库(代发范围下拉数据源) ----
async def test_admin_user_kbs(client, auth_headers, db_session):
    from sqlalchemy import text

    from tests.test_agent_api import _create_kb

    mine_kb = await _create_kb(client, auth_headers, "我的库A")
    # 他人库并授予我 viewer
    await client.post("/api/auth/register",
                      json={"username": "scope_gr1", "password": "secret123"})
    await db_session.execute(
        text("UPDATE users SET role = 'editor' WHERE username = 'scope_gr1'"))
    await db_session.commit()
    gr = await client.post("/api/auth/login",
                           json={"username": "scope_gr1",
                                 "password": "secret123"})
    gr_kb = await _create_kb(
        client,
        {"Authorization": f"Bearer {gr.json()['access_token']}"}, "授权给我的库")
    me = await client.get("/api/auth/me", headers=auth_headers)
    # 授权(gr 用户是 owner)
    await client.put(
        f"/api/kbs/{gr_kb}/permissions",
        json={"username": me.json()["username"], "perm": "viewer"},
        headers={"Authorization": f"Bearer {gr.json()['access_token']}"})
    # 提权 admin
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": me.json()["id"]})
    await db_session.commit()
    r = await client.get(f"/api/admin/users/{me.json()['id']}/kbs",
                         headers=auth_headers)
    assert r.status_code == 200
    ids = {k["id"] for k in r.json()}
    assert {"id", "name"} == set(r.json()[0].keys())
    assert mine_kb in ids and gr_kb in ids
    # 目标不存在 404;非 admin 403
    assert (await client.get("/api/admin/users/999999/kbs",
                             headers=auth_headers)).status_code == 404
    r2 = await client.get(
        f"/api/admin/users/{me.json()['id']}/kbs",
        headers={"Authorization": f"Bearer {gr.json()['access_token']}"})
    assert r2.status_code == 403


async def test_admin_target_admin_sees_all(client, auth_headers, db_session):
    """目标用户本身是 admin → 其可见集 = 全库(get_kb_perm 同语义)。"""
    from sqlalchemy import text

    from tests.test_agent_api import _create_kb

    await _create_kb(client, auth_headers, "全库可见性库")
    await client.post("/api/auth/register",
                      json={"username": "scope_ad1", "password": "secret123"})
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE username = 'scope_ad1'"))
    await db_session.commit()
    ad = await client.post("/api/auth/login",
                           json={"username": "scope_ad1",
                                 "password": "secret123"})
    ad_id = (await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {ad.json()['access_token']}"}
    )).json()["id"]
    # 提权自身为 admin 后查询
    me = await client.get("/api/auth/me", headers=auth_headers)
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": me.json()["id"]})
    await db_session.commit()
    r = await client.get(f"/api/admin/users/{ad_id}/kbs", headers=auth_headers)
    assert r.status_code == 200
    assert any(k["name"] == "全库可见性库" for k in r.json())
