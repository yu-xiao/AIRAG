async def test_create_and_list_conversations(client, auth_headers):
    resp = await client.post(
        "/api/chat/conversations", json={"kb_ids": [1, 2], "name": "测试会话"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["kb_ids"] == [1, 2]
    listed = await client.get("/api/chat/conversations", headers=auth_headers)
    assert listed.status_code == 200
    assert any(c["title"] == "测试会话" for c in listed.json())


async def test_messages_history(client, auth_headers, db_session):
    from app.models import Message

    created = await client.post(
        "/api/chat/conversations", json={"kb_ids": [1], "name": "历史"},
        headers=auth_headers,
    )
    conv_id = created.json()["id"]
    db_session.add_all([
        Message(conversation_id=conv_id, role="user", content="问"),
        Message(conversation_id=conv_id, role="assistant", content="答",
                citations=[{"number": 1}]),
    ])
    await db_session.commit()
    resp = await client.get(
        f"/api/chat/conversations/{conv_id}/messages", headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [m["role"] for m in body] == ["user", "assistant"]
    assert body[1]["citations"][0]["number"] == 1


async def _register_and_login(client, username):
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_delete_own_conversation_cascades(client, auth_headers, db_session):
    from app.models import Message

    created = await client.post(
        "/api/chat/conversations", json={"kb_ids": [1], "name": "待删"},
        headers=auth_headers,
    )
    conv_id = created.json()["id"]
    db_session.add_all([
        Message(conversation_id=conv_id, role="user", content="q"),
        Message(conversation_id=conv_id, role="assistant", content="a"),
    ])
    await db_session.commit()
    resp = await client.delete(f"/api/chat/conversations/{conv_id}", headers=auth_headers)
    assert resp.status_code == 204
    from sqlalchemy import select

    rows = (await db_session.execute(select(Message).where(
        Message.conversation_id == conv_id))).scalars().all()
    assert rows == []
    gone = await client.get(
        f"/api/chat/conversations/{conv_id}/messages", headers=auth_headers
    )
    assert gone.status_code == 404


async def test_delete_stranger_conversation_404(client, auth_headers):
    created = await client.post(
        "/api/chat/conversations", json={"kb_ids": [1], "name": "别人的"},
        headers=auth_headers,
    )
    conv_id = created.json()["id"]
    other = await _register_and_login(client, "del_other1")
    resp = await client.delete(f"/api/chat/conversations/{conv_id}", headers=other)
    assert resp.status_code == 404


async def test_export_conversation_markdown(client, auth_headers, db_session):
    kb = await client.post("/api/kbs", json={"name": "导出库"}, headers=auth_headers)
    conv = await client.post(
        "/api/chat/conversations",
        json={"kb_ids": [kb.json()["id"]], "name": "导出会话"},
        headers=auth_headers,
    )
    conv_id = conv.json()["id"]
    from app.models import Message

    db_session.add(Message(conversation_id=conv_id, role="user", content="问个问题"))
    db_session.add(
        Message(
            conversation_id=conv_id, role="assistant", content="答案[1]",
            citations=[
                {
                    "number": 1, "chunk_id": 1, "document_id": 2,
                    "filename": "a.pdf", "page_no": 3, "excerpt": "引用摘录",
                }
            ],
        )
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/chat/conversations/{conv_id}/export", headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert 'attachment; filename="conv' in resp.headers["content-disposition"]
    body = resp.text
    assert "导出会话" in body
    assert "问个问题" in body
    assert "答案[1]" in body
    assert "a.pdf" in body and "引用摘录" in body  # 引用附录

    await client.post(
        "/api/auth/register", json={"username": "exp_other", "password": "secret123"}
    )
    login = await client.post(
        "/api/auth/login", json={"username": "exp_other", "password": "secret123"}
    )
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}
    denied = await client.get(f"/api/chat/conversations/{conv_id}/export", headers=h)
    assert denied.status_code == 404


async def test_messages_include_refused_flag(client, auth_headers, db_session):
    from app.models import Message

    kb = await client.post("/api/kbs", json={"name": "refused库"}, headers=auth_headers)
    conv = await client.post(
        "/api/chat/conversations",
        json={"kb_ids": [kb.json()["id"]], "name": "c"},
        headers=auth_headers,
    )
    conv_id = conv.json()["id"]
    db_session.add(Message(conversation_id=conv_id, role="user", content="q"))
    db_session.add(Message(conversation_id=conv_id, role="assistant",
                           content="知识库中未找到相关内容", refused=True))
    db_session.add(Message(conversation_id=conv_id, role="assistant",
                           content="正常回答"))
    await db_session.commit()

    resp = await client.get(
        f"/api/chat/conversations/{conv_id}/messages", headers=auth_headers
    )
    items = resp.json()
    refused_flags = [m["refused"] for m in items if m["role"] == "assistant"]
    assert refused_flags == [True, False]  # 旧数据/未传 → 默认 False
