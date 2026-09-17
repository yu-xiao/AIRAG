# backend/tests/test_mcp.py
"""M9 Task6:MCP 面(原始 JSON-RPC over httpx ASGI + lifespan)。"""
import json

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.main import app

ACCEPT = "application/json, text/event-stream"
INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "t", "version": "0"}},
}


@pytest_asyncio.fixture(scope="session")
async def _mcp_lifespan():
    """fastmcp 的 StreamableHTTPSessionManager 仅允许 run() 一次,
    整个测试会话只启停一次宿主 lifespan(所有 async fixture/用例共用
    session 事件循环,见 pyproject asyncio_default_*_loop_scope)。"""
    async with LifespanManager(app):
        yield


@pytest_asyncio.fixture
async def mcp_client(client, _mcp_lifespan):
    """client fixture 已 override get_db;lifespan 已启动 mcp session manager。"""
    yield client


def _rpc(method: str, params=None, msg_id=1):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


async def _init(c, headers):
    resp = await c.post("/mcp", json=INIT,
                        headers={**headers, "Accept": ACCEPT})
    assert resp.status_code == 200
    sid = resp.headers.get("mcp-session-id")
    await c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                 headers={**headers, "Accept": ACCEPT,
                          **({"mcp-session-id": sid} if sid else {})})
    return sid


def _tool_result(resp_json) -> dict:
    text = resp_json["result"]["content"][0]["text"]
    return json.loads(text)


async def test_mcp_401_without_key(mcp_client):
    resp = await mcp_client.post("/mcp", json=INIT,
                                 headers={"Accept": ACCEPT})
    assert resp.status_code == 401


async def test_mcp_401_bad_key(mcp_client):
    resp = await mcp_client.post(
        "/mcp", json=INIT,
        headers={"Accept": ACCEPT, "Authorization": "Bearer airag_bogus"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_key"


async def test_mcp_initialize_and_tools_list(mcp_client, auth_headers):
    from tests.test_agent_api import _create_key

    key = await _create_key(mcp_client, auth_headers)
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    assert sid  # fastmcp streamable http 会下发会话 id
    resp = await mcp_client.post(
        "/mcp", json=_rpc("tools/list", {}, 2),
        headers={"Accept": ACCEPT, "Authorization": f"Bearer {key}",
                 "mcp-session-id": sid},
    )
    names = [t["name"] for t in resp.json()["result"]["tools"]]
    assert "list_knowledge_bases" in names and "search_knowledge_base" in names


async def test_mcp_tool_list_kbs(mcp_client, auth_headers):
    from tests.test_agent_api import _create_kb, _create_key

    kb_id = await _create_kb(mcp_client, auth_headers, "MCP可见库")
    key = await _create_key(mcp_client, auth_headers)
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    resp = await mcp_client.post(
        "/mcp",
        json=_rpc("tools/call",
                  {"name": "list_knowledge_bases", "arguments": {}}, 3),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    body = _tool_result(resp.json())
    assert kb_id in [i["id"] for i in body["items"]]


async def test_mcp_tool_search(mcp_client, auth_headers, monkeypatch):
    from tests.test_agent_api import _create_kb, _create_key
    from app.services.retrieval.searcher import SearchHit

    kb_id = await _create_kb(mcp_client, auth_headers, "MCP检索库")
    key = await _create_key(mcp_client, auth_headers)

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        return [SearchHit(chunk_id=1, document_id=10, kb_id=kb_id,
                          filename="a.pdf", page_no=1, content="命中",
                          score=0.9, source="both")]

    monkeypatch.setattr("app.services.agent_facade.hybrid_search", fake_hybrid)
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    resp = await mcp_client.post(
        "/mcp",
        json=_rpc("tools/call",
                  {"name": "search_knowledge_base",
                   "arguments": {"kb_ids": [kb_id], "query": "命中?"}}, 4),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    body = _tool_result(resp.json())
    assert body["total"] == 1 and "命中" in body["hits"][0]["content"]


async def test_mcp_tool_denied_kb(mcp_client, auth_headers):
    from tests.test_agent_api import _create_key

    key = await _create_key(mcp_client, auth_headers)
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    resp = await mcp_client.post(
        "/mcp",
        json=_rpc("tools/call",
                  {"name": "search_knowledge_base",
                   "arguments": {"kb_ids": [99999], "query": "q"}}, 5),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    text = resp.json()["result"]["content"][0]["text"]
    assert "kb_forbidden" in text


async def test_mcp_rate_limited_429(mcp_client, auth_headers, monkeypatch):
    from tests.test_agent_api import _create_key
    from app.core.config import settings
    from app.services import agent_ratelimit
    from tests.test_agent_ratelimit import FakeRedis

    key = await _create_key(mcp_client, auth_headers)
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 1)
    # 须共享同一 FakeRedis 实例(allow 每次 get_redis() 取一次;
    # 每次新建实例窗口永不累计,429 不会触发——同 test_agent_ratelimit 写法)
    fake_redis = FakeRedis()
    monkeypatch.setattr(agent_ratelimit, "get_redis", lambda: fake_redis)
    hdr = {"Authorization": f"Bearer {key}", "Accept": ACCEPT}
    assert (await mcp_client.post("/mcp", json=INIT, headers=hdr)).status_code == 200
    resp = await mcp_client.post("/mcp", json=INIT, headers=hdr)
    assert resp.status_code == 429
    assert resp.json()["detail"]["code"] == "rate_limited"
