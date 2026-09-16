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


async def test_ask_filters_non_answer_llm_tokens(
    client, auth_headers, monkeypatch, db_session
):
    """rewrite 的 LLM 输出不得进入 SSE token 流(带 answer tag 过滤)。"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    import app.services.chat_graph.nodes as nodes_mod
    from app.api import ask as ask_mod
    from app.services.chat_graph.graph import build_graph
    from app.services.retrieval.searcher import SearchHit

    captured = {"q": None}

    async def cap_search(db, kb_ids, query, top_k=20):
        captured["q"] = query
        return [SearchHit(1, 1, 1, "a.pdf", 1, "答案内容", 0.5, "vector")]

    monkeypatch.setattr(nodes_mod, "hybrid_search", cap_search)
    real_build = build_graph

    def patched(**kw):
        return real_build(
            llm=FakeListChatModel(responses=["改写后的独立查询", "最终答案[1]"])
        )

    monkeypatch.setattr(ask_mod, "build_graph", patched)

    from app.core.config import settings

    monkeypatch.setattr(settings, "AGENTIC_REWRITE_ENABLED", True)

    kb = await client.post("/api/kbs", json={"name": "改写链路库"}, headers=auth_headers)
    # 先建会话并落两条历史消息,让 rewrite 有上下文可用
    conv = await client.post(
        "/api/chat/conversations",
        json={"kb_ids": [kb.json()["id"]], "name": "历史会话"},
        headers=auth_headers,
    )
    conv_id = conv.json()["id"]
    from app.models import Message

    db_session.add(Message(conversation_id=conv_id, role="user", content="星际探索项目是什么"))
    db_session.add(Message(conversation_id=conv_id, role="assistant", content="是个项目"))
    await db_session.commit()

    resp = await client.post(
        "/api/chat/ask",
        json={"kb_ids": [kb.json()["id"]], "question": "它的负责人是谁",
              "conversation_id": conv_id},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.text
    assert "改写后的独立查询" not in body  # rewrite 输出被 tag 过滤
    assert "最终答案" in body
    assert captured["q"] == "改写后的独立查询"  # 检索用了改写查询
    # ask 审计落库
    from app.models import AuditLog

    logs = (await db_session.execute(select_(AuditLog))).scalars().all()
    assert any(l.action == "ask" for l in logs)


async def test_ask_checkpointer_wired_when_enabled(
    client, auth_headers, monkeypatch, db_session
):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    import app.services.chat_graph.nodes as nodes_mod
    from app.api import ask as ask_mod
    from app.core.config import settings
    from app.services.chat_graph.graph import build_graph
    from app.services.retrieval.searcher import SearchHit

    async def fake_search(db, kb_ids, query, top_k=20):
        return [SearchHit(1, 1, 1, "a.pdf", 1, "内容", 0.5, "vector")]

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    real_build = build_graph
    seen = {}

    def patched(**kw):
        seen["checkpointer"] = kw.get("checkpointer")
        return real_build(llm=FakeListChatModel(responses=["答案[1]"]))

    monkeypatch.setattr(ask_mod, "build_graph", patched)
    monkeypatch.setattr(settings, "CHECKPOINTER_ENABLED", True)

    sentinel = object()

    async def fake_cp():
        return sentinel

    monkeypatch.setattr(ask_mod, "get_checkpointer", fake_cp)

    kb = await client.post("/api/kbs", json={"name": "cp库"}, headers=auth_headers)
    resp = await client.post(
        "/api/chat/ask",
        json={"kb_ids": [kb.json()["id"]], "question": "q"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert seen["checkpointer"] is sentinel
