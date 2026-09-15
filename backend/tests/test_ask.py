from sqlalchemy import select as select_


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

    resp = await client.post(
        "/api/chat/ask",
        json={"kb_ids": [1], "question": "问个问题"},
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
