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


# ---- M12:kb_scope 铸造校验与回显 ----
async def test_create_key_kb_scope_validation(client, auth_headers, db_session):
    from sqlalchemy import text

    # 空列表 422
    r0 = await client.post("/api/auth/keys",
                           json={"name": "空范围", "kb_scope": []},
                           headers=auth_headers)
    assert r0.status_code == 422
    assert "empty" in r0.json()["detail"]
    # 不存在 422
    r1 = await client.post("/api/auth/keys",
                           json={"name": "幽灵库", "kb_scope": [999999]},
                           headers=auth_headers)
    assert r1.status_code == 422
    assert "999999" in r1.json()["detail"]
    # 他人库(无 perm)422
    await client.post("/api/auth/register",
                      json={"username": "scope_own1", "password": "secret123"})
    # 注册默认 viewer 不能建库,提权 editor(与 admin 面用例同型)
    await db_session.execute(
        text("UPDATE users SET role = 'editor' WHERE username = 'scope_own1'"))
    await db_session.commit()
    other = await client.post("/api/auth/login",
                              json={"username": "scope_own1",
                                    "password": "secret123"})
    other_kb = await client.post(
        "/api/kbs", json={"name": "别人的范围库"},
        headers={"Authorization": f"Bearer {other.json()['access_token']}"})
    r2 = await client.post(
        "/api/auth/keys",
        json={"name": "越界", "kb_scope": [other_kb.json()["id"]]},
        headers=auth_headers)
    assert r2.status_code == 422
    assert "inaccessible" in r2.json()["detail"]


async def test_create_key_kb_scope_echo_and_audit(client, auth_headers,
                                                  db_session):
    import json as _json

    from sqlalchemy import select

    from app.models import AuditLog
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(client, auth_headers, "回显库")
    r = await client.post(
        "/api/auth/keys",
        json={"name": "范围key", "kb_scope": [kb_id, kb_id]},  # 重复 id 去重
        headers=auth_headers)
    assert r.status_code == 201
    assert r.json()["kb_scope"] == [kb_id]  # 去重落库 + 创建响应回显
    listed = await client.get("/api/auth/keys", headers=auth_headers)
    row = next(k for k in listed.json() if k["id"] == r.json()["id"])
    assert row["kb_scope"] == [kb_id]
    # 不传 → None
    r3 = await client.post("/api/auth/keys", json={"name": "无范围"},
                           headers=auth_headers)
    assert r3.json()["kb_scope"] is None
    db_session.expire_all()
    audit_row = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "key_create",
                               AuditLog.detail.contains("范围key"))
    )).scalars().one()
    assert _json.loads(audit_row.detail)["kb_scope"] == [kb_id]


# ---- M12 Task8:小项⑥ ApiKeyOut.role 强类型 ----
async def test_api_key_out_role_literal():
    """M12 小项⑥:ApiKeyOut.role 强类型(裸 str → Literal)。"""
    import pytest as _pytest

    from pydantic import ValidationError

    from app.schemas.auth import ApiKeyOut

    base = dict(id=1, name="n", key_prefix="airag_x", is_active=True,
                expires_at=None, last_used_at=None,
                created_at="2026-09-19T00:00:00", kb_scope=None)
    assert ApiKeyOut.model_validate({**base, "role": "editor"}).role == "editor"
    with _pytest.raises(ValidationError):
        ApiKeyOut.model_validate({**base, "role": "bogus"})


# ---- M13 Task6:快修③ kb_scope 缺省 ----
async def test_api_key_out_kb_scope_default():
    """M13 快修③:kb_scope 缺省 None(第三方构造不必显式传)。"""
    from app.schemas.auth import ApiKeyOut

    base = dict(id=1, name="n", key_prefix="airag_x", role="editor",
                is_active=True, expires_at=None, last_used_at=None,
                created_at="2026-09-20T00:00:00")
    assert ApiKeyOut.model_validate(base).kb_scope is None
