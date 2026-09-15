async def test_create_kb(client, auth_headers):
    resp = await client.post(
        "/api/kbs",
        json={"name": "产品手册库", "description": "内部产品文档"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] > 0
    assert data["name"] == "产品手册库"
    assert data["embed_provider"] == "zhipu"
    assert data["embed_model"] == "embedding-3"


async def test_create_kb_requires_auth(client):
    resp = await client.post("/api/kbs", json={"name": "匿名库"})
    assert resp.status_code == 401


async def test_list_and_get_kb(client, auth_headers):
    await client.post(
        "/api/kbs", json={"name": "法务库"}, headers=auth_headers
    )
    listed = await client.get("/api/kbs", headers=auth_headers)
    assert listed.status_code == 200
    names = [item["name"] for item in listed.json()]
    assert "法务库" in names

    kb_id = listed.json()[0]["id"]
    got = await client.get(f"/api/kbs/{kb_id}", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["id"] == kb_id

    missing = await client.get("/api/kbs/999999", headers=auth_headers)
    assert missing.status_code == 404
