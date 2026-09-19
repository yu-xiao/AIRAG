# backend/tests/test_kb_scope.py
"""M12 Task2:per-KB scope 白名单(REST 面:search/ask/list_kbs)。

活交集语义:实际可访问 = 用户实时 perm ∩ key scope;None=不限(存量 key 零影响);
JWT 调试通道不受限。scope 外的库进 denied_kb_ids(403 kb_forbidden)或从 list 剔除。
"""
import pytest

from app.models import ApiKey
from app.services.api_keys import generate_api_key


async def _scoped_key(client, auth_headers, db_session, kb_ids,
                      role="read_only") -> str:
    """直插 DB 铸 scoped key(铸造 API 校验在 Task4;此处绕过以单测传播链)。"""
    me = await client.get("/api/auth/me", headers=auth_headers)
    raw, prefix, digest = generate_api_key()
    # brief 原文为 kb_scope=list(kb_ids),但 None 调用点(test_unscoped_key_
    # sees_all)会 list(None) 抛 TypeError;按其注释意图铸 NULL 列,做此最小修正。
    db_session.add(ApiKey(user_id=me.json()["id"], name=f"scoped-{role}",
                          key_prefix=prefix, key_hash=digest, role=role,
                          kb_scope=(list(kb_ids)
                                    if kb_ids is not None else None)))
    await db_session.commit()
    return raw


async def _two_kbs(client, auth_headers) -> tuple[int, int]:
    a = await client.post("/api/kbs", json={"name": "界内库"}, headers=auth_headers)
    b = await client.post("/api/kbs", json={"name": "界外库"}, headers=auth_headers)
    return a.json()["id"], b.json()["id"]


async def test_scoped_key_search_denied(client, auth_headers, db_session,
                                        monkeypatch):
    from app.services.retrieval.searcher import SearchHit

    kb_in, kb_out = await _two_kbs(client, auth_headers)
    key = await _scoped_key(client, auth_headers, db_session, [kb_in])

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        return [SearchHit(chunk_id=1, document_id=10, kb_id=kb_ids[0],
                          filename="a.pdf", page_no=1, content="c",
                          score=0.9, source="both")]

    monkeypatch.setattr("app.services.agent_facade.hybrid_search", fake_hybrid)
    hdr = {"Authorization": f"Bearer {key}"}
    r1 = await client.post("/api/agent/search",
                           json={"kb_ids": [kb_in], "query": "q"}, headers=hdr)
    assert r1.status_code == 200
    r2 = await client.post("/api/agent/search",
                           json={"kb_ids": [kb_out], "query": "q"}, headers=hdr)
    assert r2.status_code == 403
    assert r2.json()["detail"]["code"] == "kb_forbidden"
    assert r2.json()["detail"]["denied_kb_ids"] == [kb_out]


async def test_scoped_key_ask_denied(client, auth_headers, db_session):
    """denied 在 _permitted_kb_ids 即抛,不触达问答图(无需 LLM)。"""
    kb_in, kb_out = await _two_kbs(client, auth_headers)
    key = await _scoped_key(client, auth_headers, db_session, [kb_in])
    r = await client.post("/api/agent/ask",
                          json={"kb_ids": [kb_out], "query": "q"},
                          headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403
    assert r.json()["detail"]["denied_kb_ids"] == [kb_out]


async def test_scoped_key_list_kbs_filtered(client, auth_headers, db_session):
    kb_in, kb_out = await _two_kbs(client, auth_headers)
    key = await _scoped_key(client, auth_headers, db_session, [kb_in])
    r = await client.get("/api/agent/kbs",
                         headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert [i["id"] for i in r.json()["items"]] == [kb_in]


async def test_unscoped_key_sees_all(client, auth_headers, db_session):
    kb_in, kb_out = await _two_kbs(client, auth_headers)
    key = await _scoped_key(client, auth_headers, db_session, None)  # kb_scope NULL
    r = await client.get("/api/agent/kbs",
                         headers={"Authorization": f"Bearer {key}"})
    ids = [i["id"] for i in r.json()["items"]]
    assert kb_in in ids and kb_out in ids


async def test_jwt_unrestricted(client, auth_headers, db_session):
    kb_in, kb_out = await _two_kbs(client, auth_headers)
    await _scoped_key(client, auth_headers, db_session, [kb_in])  # 造出 scoped key 但不用
    r = await client.get("/api/agent/kbs", headers=auth_headers)
    ids = [i["id"] for i in r.json()["items"]]
    assert kb_in in ids and kb_out in ids


async def test_scope_intersects_user_perm(client, auth_headers, db_session):
    """scope 写了他人的库:用户 perm 拦在前面(spec B1 活交集)。"""
    from sqlalchemy import text

    kb_in, _ = await _two_kbs(client, auth_headers)
    await client.post("/api/auth/register",
                      json={"username": "scope_str1", "password": "secret123"})
    await db_session.execute(
        text("UPDATE users SET role = 'editor' WHERE username = 'scope_str1'"))
    await db_session.commit()
    other = await client.post("/api/auth/login",
                              json={"username": "scope_str1",
                                    "password": "secret123"})
    other_kb = await client.post(
        "/api/kbs", json={"name": "别人的库2"},
        headers={"Authorization": f"Bearer {other.json()['access_token']}"})
    key = await _scoped_key(client, auth_headers, db_session,
                            [kb_in, other_kb.json()["id"]])
    r = await client.post("/api/agent/search",
                          json={"kb_ids": [other_kb.json()["id"]],
                                "query": "q"},
                          headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403
    assert r.json()["detail"]["denied_kb_ids"] == [other_kb.json()["id"]]
