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
