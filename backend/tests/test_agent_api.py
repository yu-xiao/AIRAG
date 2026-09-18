# backend/tests/test_agent_api.py
"""M9 Task3:agent principal 双路径鉴权 + GET /api/agent/kbs 可见性 + 审计。"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.core.deps import (
    _api_key_id,
    require_editor_key,
    resolve_bearer_principal,
)
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


# ---- Task4:POST /api/agent/search ----
from app.services.retrieval.searcher import SearchHit  # noqa: E402


def _fake_hits(n=2):
    return [
        SearchHit(chunk_id=i, document_id=i * 10, kb_id=1, filename="a.pdf",
                  page_no=i + 1, content=f"内容{i}", score=0.5 + i * 0.1,
                  source="both")
        for i in range(n)
    ]


async def test_search_ok(client, auth_headers, monkeypatch):
    kb_id = await _create_kb(client, auth_headers, "检索库")
    key = await _create_key(client, auth_headers)

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        hits = _fake_hits()
        for h in hits:
            h.kb_id = kb_id
        return hits

    monkeypatch.setattr("app.services.agent_facade.hybrid_search", fake_hybrid)
    resp = await client.post(
        "/api/agent/search",
        json={"kb_ids": [kb_id], "query": "测试问题", "top_k": 5},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2 and body["elapsed_ms"] >= 0
    hit = body["hits"][0]
    assert set(hit) == {"chunk_id", "document_id", "kb_id", "filename",
                        "page_no", "content", "score", "source"}


async def test_search_denied_includes_nonexistent(client, auth_headers):
    kb_id = await _create_kb(client, auth_headers, "被拒库")
    key = await _create_key(client, auth_headers)
    resp = await client.post(
        "/api/agent/search",
        json={"kb_ids": [kb_id, 99999], "query": "x"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "kb_forbidden" and 99999 in detail["denied_kb_ids"]
    assert kb_id not in detail["denied_kb_ids"]


async def test_search_validation_422(client, auth_headers):
    key = await _create_key(client, auth_headers)
    resp = await client.post(
        "/api/agent/search", json={"kb_ids": [], "query": ""},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422


async def test_search_rerank_applied(client, auth_headers, monkeypatch):
    kb_id = await _create_kb(client, auth_headers, "重排库")
    key = await _create_key(client, auth_headers)

    class FakeReranker:
        def rerank(self, query, documents, top_n=8):
            return [(1, 0.95), (0, 0.10)]  # 原顺序反转

    monkeypatch.setattr("app.services.agent_facade.settings.RERANK_ENABLED", True)
    monkeypatch.setattr("app.services.agent_facade.get_reranker", lambda: FakeReranker())

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        return _fake_hits()

    monkeypatch.setattr("app.services.agent_facade.hybrid_search", fake_hybrid)
    resp = await client.post(
        "/api/agent/search",
        json={"kb_ids": [kb_id], "query": "q", "rerank": True},
        headers={"Authorization": f"Bearer {key}"},
    )
    body = resp.json()
    assert body["hits"][0]["score"] == 0.95 and body["hits"][0]["chunk_id"] == 1


async def test_search_rerank_min_score_gate(client, auth_headers, monkeypatch):
    kb_id = await _create_kb(client, auth_headers, "阈值库")
    key = await _create_key(client, auth_headers)

    class FakeReranker:
        def rerank(self, query, documents, top_n=8):
            return [(0, 0.50)]

    monkeypatch.setattr("app.services.agent_facade.settings.RERANK_ENABLED", True)
    monkeypatch.setattr("app.services.agent_facade.settings.RETRIEVAL_MIN_SCORE", 0.9)
    monkeypatch.setattr("app.services.agent_facade.get_reranker", lambda: FakeReranker())

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        return _fake_hits(1)

    monkeypatch.setattr("app.services.agent_facade.hybrid_search", fake_hybrid)
    resp = await client.post(
        "/api/agent/search",
        json={"kb_ids": [kb_id], "query": "q", "rerank": True},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.json()["total"] == 0


# ---- M10:POST /api/agent/ask ----

def _ask_graph_patch(monkeypatch, response_text: str):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services import agent_facade
    from app.services.chat_graph import nodes
    from app.services.chat_graph.graph import build_graph

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        return _fake_hits()

    monkeypatch.setattr(nodes, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(agent_facade, "_ask_graph",
                        build_graph(llm=FakeListChatModel(responses=[response_text]),
                                    checkpointer=None))


async def test_ask_ok(client, auth_headers, monkeypatch):
    kb_id = await _create_kb(client, auth_headers, "问答库")
    key = await _create_key(client, auth_headers)
    _ask_graph_patch(monkeypatch, "巡航升限为八千五百米。")
    resp = await client.post(
        "/api/agent/ask",
        json={"kb_ids": [kb_id], "query": "升限?"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "巡航升限为八千五百米。"
    assert body["refused"] is False and body["tokens_used"] == 0
    assert body["citations"] and isinstance(body["elapsed_ms"], int)


async def test_ask_refused_is_200(client, auth_headers, monkeypatch):
    from app.services.chat_graph.nodes import REFUSAL_PHRASE

    kb_id = await _create_kb(client, auth_headers, "拒答库")
    key = await _create_key(client, auth_headers)
    _ask_graph_patch(monkeypatch, REFUSAL_PHRASE)
    resp = await client.post(
        "/api/agent/ask",
        json={"kb_ids": [kb_id], "query": "无关问题"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200 and resp.json()["refused"] is True


async def test_ask_denied_dedup(client, auth_headers):
    kb_id = await _create_kb(client, auth_headers, "去重库")
    key = await _create_key(client, auth_headers)
    resp = await client.post(
        "/api/agent/ask",
        json={"kb_ids": [kb_id, kb_id, 99999, 99999], "query": "q"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["denied_kb_ids"] == [99999]


async def test_ask_validation_422(client, auth_headers):
    key = await _create_key(client, auth_headers)
    resp = await client.post(
        "/api/agent/ask", json={"kb_ids": [], "query": ""},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422


async def test_ask_quota_429_with_retry_after_header(
    client, auth_headers, monkeypatch
):
    from app.core.config import settings
    from app.services import agent_ratelimit
    from tests.test_agent_ratelimit import FakeRedis

    kb_id = await _create_kb(client, auth_headers, "配额库")
    key = await _create_key(client, auth_headers)
    key_id = (await client.get("/api/auth/keys", headers=auth_headers)
              ).json()[0]["id"]
    fake_redis = FakeRedis()
    monkeypatch.setattr(agent_ratelimit, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(settings, "AGENT_ASK_DAILY_TOKENS", 100)
    await agent_ratelimit.quota_consume(key_id, 100)  # 烧穿

    resp = await client.post(
        "/api/agent/ask",
        json={"kb_ids": [kb_id], "query": "q"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["code"] == "quota_exhausted" and detail["retry_after"] >= 1
    assert int(resp.headers["Retry-After"]) >= 1


async def test_ask_audit_written(client, auth_headers, db_session,
                                 monkeypatch):
    kb_id = await _create_kb(client, auth_headers, "审计ask库")
    key = await _create_key(client, auth_headers)
    _ask_graph_patch(monkeypatch, "正常答案")
    await client.post(
        "/api/agent/ask",
        json={"kb_ids": [kb_id], "query": "q"},
        headers={"Authorization": f"Bearer {key}"},
    )
    db_session.expire_all()  # AsyncSession.expire_all 为同步方法,不可 await
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "agent.ask")
    )).scalars().all()
    assert len(rows) == 1
    d = json.loads(rows[0].detail)
    assert d["client"] == "rest" and d["refused"] is False \
        and "tokens" in d and "elapsed_ms" in d


async def test_key_last_used_at_persisted(client, auth_headers):
    """小项⑤:key 调用后 last_used_at 落库(REST 面经端点 commit 持久化)。"""
    key = await _create_key(client, auth_headers)
    await client.get("/api/agent/kbs",
                     headers={"Authorization": f"Bearer {key}"})
    rows = (await client.get("/api/auth/keys", headers=auth_headers)).json()
    assert rows[0]["last_used_at"] is not None


async def test_agent_api_disabled_smoke(monkeypatch):
    """小项②:总开关关闭后 agent REST 与 /mcp 全 404,常规路由不受影响。"""
    from httpx import ASGITransport, AsyncClient

    from app.core.config import settings
    from app.main import create_app

    monkeypatch.setattr(settings, "AGENT_API_ENABLED", False)
    app2 = create_app()
    async with AsyncClient(transport=ASGITransport(app=app2),
                           base_url="http://t") as c:
        assert (await c.get("/api/health")).status_code == 200
        assert (await c.get("/api/agent/kbs")).status_code == 404
        assert (await c.post("/api/agent/ask", json={})).status_code == 404
        assert (await c.post("/mcp", json={})).status_code == 404


# ---- M11:能力传播与守卫 ----
async def _create_key_role(client, headers, name, role):
    r = await client.post("/api/auth/keys",
                          json={"name": name, "role": role}, headers=headers)
    assert r.status_code == 201
    return r.json()["key"]


async def test_principal_key_role_and_guards(client, auth_headers, db_session):
    editor_key = await _create_key_role(client, auth_headers, "编辑", "editor")
    ro_key = await _create_key_role(client, auth_headers, "只读", "read_only")

    p = await resolve_bearer_principal(db_session, editor_key)
    assert p.kind == "api_key" and p.key_role == "editor"
    assert _api_key_id(p) == p.key_id
    require_editor_key(p)  # editor key 放行

    p2 = await resolve_bearer_principal(db_session, ro_key)
    assert p2.key_role == "read_only"
    with pytest.raises(HTTPException) as ei:
        require_editor_key(p2)
    assert ei.value.status_code == 403
    assert ei.value.detail == {"code": "editor_key_required"}

    token = auth_headers["Authorization"].split(" ", 1)[1]
    pj = await resolve_bearer_principal(db_session, token)
    assert pj.kind == "jwt" and pj.key_role is None
    assert _api_key_id(pj) is None
    require_editor_key(pj)  # JWT 调试通道视为 editor 能力
