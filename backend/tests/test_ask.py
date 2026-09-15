from sqlalchemy import select as select_


async def _register_and_login(client, username):
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_ask_streams_tokens_and_saves(client, auth_headers, monkeypatch, db_session):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    import app.services.chat_graph.nodes as nodes_mod
    from app.services.chat_graph.graph import build_graph
    from app.services.retrieval.searcher import SearchHit

    async def fake_search(db, kb_ids, query, top_k=20):
        return [SearchHit(1, 1, 1, "a.pdf", 1, "答案内容来自这里", 0.5, "vector")]

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    real_build = build_graph
    monkeypatch.setattr(
        "app.api.ask.build_graph",
        lambda **kw: real_build(llm=FakeListChatModel(responses=["最终答案[1]"])),
    )

    # M4 起 ask 校验 kb 权限:先播种一个当前用户自己的库
    kb = await client.post(
        "/api/kbs", json={"name": "问答播种库"}, headers=auth_headers
    )
    kb_id = kb.json()["id"]

    resp = await client.post(
        "/api/chat/ask",
        json={"kb_ids": [kb_id], "question": "问个问题"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert "最终答案" in body
    assert '"type": "citations"' in body or '"type":"citations"' in body
    assert '"type": "done"' in body or '"type":"done"' in body

    from app.models import Message

    rows = (await db_session.execute(select_(Message))).scalars().all()
    assert {m.role for m in rows} >= {"user", "assistant"}


async def test_ask_requires_auth(client):
    resp = await client.post(
        "/api/chat/ask", json={"kb_ids": [1], "question": "x"}
    )
    assert resp.status_code == 401


async def test_ask_rejects_invisible_kb(client, auth_headers, db_session):
    from sqlalchemy import text as _text

    other = await _register_and_login(client, "ask_other1")
    ome = await client.get("/api/auth/me", headers=other)
    # 他人需 editor 才能建库(默认 viewer 被 T2 拦)
    await db_session.execute(
        _text("UPDATE users SET role = 'editor' WHERE id = :i"),
        {"i": ome.json()["id"]},
    )
    await db_session.commit()
    mine = await client.post("/api/kbs", json={"name": "他人私库"}, headers=other)
    kb_id = mine.json()["id"]
    resp = await client.post(
        "/api/chat/ask",
        json={"kb_ids": [kb_id], "question": "看得到吗"},
        headers=auth_headers,
    )
    assert resp.status_code == 403
    missing = await client.post(
        "/api/chat/ask",
        json={"kb_ids": [999999], "question": "不存在的库"},
        headers=auth_headers,
    )
    assert missing.status_code == 403
