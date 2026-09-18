# backend/tests/test_mcp.py
"""M9 Task6:MCP 面(原始 JSON-RPC over httpx ASGI + lifespan)。"""
import base64
import json

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.test_agent_api import _create_key_role  # M11

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
    assert "list_knowledge_bases" in names
    assert "search_knowledge_base" in names
    assert "ask_knowledge_base" in names


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


async def test_mcp_tool_list_kbs_audit_ip(mcp_client, auth_headers, db_session):
    """spec B:MCP 面审计须落客户端 ip(修:此前仅 REST 面带 ip)。"""
    from sqlalchemy import select

    from app.models import AuditLog
    from tests.test_agent_api import _create_key

    key = await _create_key(mcp_client, auth_headers)
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    resp = await mcp_client.post(
        "/mcp",
        json=_rpc("tools/call",
                  {"name": "list_knowledge_bases", "arguments": {}}, 6),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    assert isinstance(_tool_result(resp.json())["items"], list)
    db_session.expire_all()  # AsyncSession.expire_all 为同步方法,不可 await
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "agent.list_kbs")
    )).scalars().all()
    assert len(rows) == 1 and rows[0].ip


async def test_mcp_unmatched_path_404_not_401(mcp_client):
    """mount("/") 是兜底,非 /mcp 路径应 404 而非 401;/mcp 本体仍须鉴权。"""
    resp = await mcp_client.post("/nope", json={})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not found"
    resp = await mcp_client.get("/mcp", headers={"Accept": ACCEPT})
    assert resp.status_code == 401


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


# ---- M10:ask_knowledge_base ----

async def test_mcp_ask_tool(mcp_client, auth_headers, monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services import agent_facade
    from app.services.chat_graph import nodes
    from app.services.chat_graph.graph import build_graph
    from tests.test_agent_api import _create_kb, _create_key

    kb_id = await _create_kb(mcp_client, auth_headers, "MCP问答库")
    key = await _create_key(mcp_client, auth_headers)

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        from app.services.retrieval.searcher import SearchHit
        return [SearchHit(chunk_id=1, document_id=10, kb_id=kb_id,
                          filename="a.pdf", page_no=1, content="八千五百米",
                          score=0.9, source="both")]

    monkeypatch.setattr(nodes, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(agent_facade, "_ask_graph",
                        build_graph(llm=FakeListChatModel(
                            responses=["巡航升限为八千五百米。"]),
                            checkpointer=None))
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    resp = await mcp_client.post(
        "/mcp",
        json=_rpc("tools/call",
                  {"name": "ask_knowledge_base",
                   "arguments": {"kb_ids": [kb_id], "query": "升限?"}}, 10),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    body = _tool_result(resp.json())
    assert body["answer"] == "巡航升限为八千五百米。"
    assert body["refused"] is False and body["tokens_used"] == 0
    assert {"citations", "elapsed_ms"} <= set(body)


async def test_mcp_ask_quota_toolerror(mcp_client, auth_headers, monkeypatch):
    from app.core.config import settings
    from app.services import agent_ratelimit
    from tests.test_agent_api import _create_kb, _create_key
    from tests.test_agent_ratelimit import FakeRedis

    kb_id = await _create_kb(mcp_client, auth_headers, "MCP配额库")
    key = await _create_key(mcp_client, auth_headers)
    key_id = (await mcp_client.get("/api/auth/keys",
                                   headers=auth_headers)).json()[0]["id"]
    fake_redis = FakeRedis()
    monkeypatch.setattr(agent_ratelimit, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(settings, "AGENT_ASK_DAILY_TOKENS", 100)
    await agent_ratelimit.quota_consume(key_id, 100)

    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    resp = await mcp_client.post(
        "/mcp",
        json=_rpc("tools/call",
                  {"name": "ask_knowledge_base",
                   "arguments": {"kb_ids": [kb_id], "query": "q"}}, 11),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    text = resp.json()["result"]["content"][0]["text"]
    assert "quota_exhausted" in text and "retry_after" in text


async def test_mcp_429_retry_after_header(mcp_client, auth_headers,
                                          monkeypatch):
    from app.core.config import settings
    from app.services import agent_ratelimit
    from tests.test_agent_api import _create_key
    from tests.test_agent_ratelimit import FakeRedis

    key = await _create_key(mcp_client, auth_headers)
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 1)
    fake_redis = FakeRedis()
    monkeypatch.setattr(agent_ratelimit, "get_redis", lambda: fake_redis)
    hdr = {"Authorization": f"Bearer {key}", "Accept": ACCEPT}
    assert (await mcp_client.post("/mcp", json=INIT, headers=hdr)).status_code == 200
    resp = await mcp_client.post("/mcp", json=INIT, headers=hdr)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1


async def test_mcp_key_last_used_at_updated(mcp_client, auth_headers,
                                            db_session):
    """小项⑤:MCP 面经中间件显式 commit,last_used_at 须落库。"""
    from sqlalchemy import select

    from app.models import ApiKey
    from tests.test_agent_api import _create_key

    key = await _create_key(mcp_client, auth_headers)
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    await mcp_client.post(
        "/mcp", json=_rpc("tools/list", {}, 12),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    db_session.expire_all()  # AsyncSession.expire_all 为同步方法,不可 await
    row = (await db_session.execute(select(ApiKey))).scalars().one()
    assert row.last_used_at is not None


async def test_mcp_tool_descriptions_present(mcp_client, auth_headers):
    """小项④:描述须随 tools/list 下发,search 默认值与配置同源
    (f-string 首语句不会成为 __doc__,曾致描述静默丢失)。"""
    from app.core.config import settings
    from tests.test_agent_api import _create_key

    key = await _create_key(mcp_client, auth_headers)
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    resp = await mcp_client.post(
        "/mcp", json=_rpc("tools/list", {}, 13),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    tools = {t["name"]: t for t in resp.json()["result"]["tools"]}
    for name in ("list_knowledge_bases", "search_knowledge_base",
                 "ask_knowledge_base"):
        assert tools[name].get("description"), name
    assert f"默认 {settings.RETRIEVAL_TOP_K}" in \
        tools["search_knowledge_base"]["description"]


# ---- M11:文档工具 ----
async def _keyed_session(c, auth_headers, role=None):
    from tests.test_agent_api import _create_key, _create_kb
    if role:
        key = await _create_key_role(c, auth_headers, f"mcp-{role}", role)
    else:
        key = await _create_key(c, auth_headers)
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(c, hdr)
    return hdr, sid


async def test_mcp_doc_tools_in_list(mcp_client, auth_headers):
    hdr, sid = await _keyed_session(mcp_client, auth_headers)
    resp = await mcp_client.post(
        "/mcp", json=_rpc("tools/list", {}, 2),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    names = [t["name"] for t in resp.json()["result"]["tools"]]
    for n in ("list_documents", "get_document", "upload_document",
              "delete_document", "reprocess_document"):
        assert n in names


async def _tool_call(c, hdr, sid, name, args, msg_id):
    resp = await c.post(
        "/mcp",
        json=_rpc("tools/call", {"name": name, "arguments": args}, msg_id),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    return resp.json()


def _is_error(rj) -> bool:
    return rj.get("error") is not None or rj["result"].get("isError", False)


def _err_text(rj) -> str:
    if rj.get("error"):
        return str(rj["error"].get("message", ""))
    return rj["result"]["content"][0]["text"]


async def test_mcp_upload_get_delete_cycle(mcp_client, auth_headers):
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(mcp_client, auth_headers, "MCP写库")
    hdr, sid = await _keyed_session(mcp_client, auth_headers, role="editor")
    b64 = base64.b64encode(b"mcp-upload-dummy").decode()
    rj = await _tool_call(mcp_client, hdr, sid, "upload_document",
                          {"kb_id": kb_id, "filename": "m11.docx",
                           "content_b64": b64}, 3)
    body = _tool_result(rj)
    doc_id = body["id"]
    assert body["filename"] == "m11.docx"
    rj2 = await _tool_call(mcp_client, hdr, sid, "get_document",
                           {"doc_id": doc_id}, 4)
    assert _tool_result(rj2)["status"] in ("pending", "parsing", "chunking",
                                           "embedding", "done", "failed")
    rj3 = await _tool_call(mcp_client, hdr, sid, "list_documents",
                           {"kb_id": kb_id}, 5)
    assert doc_id in [d["id"] for d in _tool_result(rj3)["items"]]
    rj4 = await _tool_call(mcp_client, hdr, sid, "delete_document",
                           {"doc_id": doc_id}, 6)
    assert _tool_result(rj4)["deleted"] is True
    rj5 = await _tool_call(mcp_client, hdr, sid, "get_document",
                           {"doc_id": doc_id}, 7)
    assert _is_error(rj5) and "not_found" in _err_text(rj5)


async def test_mcp_write_guard_and_validation(mcp_client, auth_headers):
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(mcp_client, auth_headers, "MCP守卫库")
    hdr, sid = await _keyed_session(mcp_client, auth_headers)  # read_only
    rj = await _tool_call(mcp_client, hdr, sid, "upload_document",
                          {"kb_id": kb_id, "filename": "a.docx",
                           "content_b64": base64.b64encode(b"x").decode()}, 3)
    assert _is_error(rj) and "editor_key_required" in _err_text(rj)
    # editor key 坏 base64 / 坏扩展名
    hdr2, sid2 = await _keyed_session(mcp_client, auth_headers, role="editor")
    rj2 = await _tool_call(mcp_client, hdr2, sid2, "upload_document",
                           {"kb_id": kb_id, "filename": "a.docx",
                            "content_b64": "!!!not-base64!!!"}, 4)
    assert _is_error(rj2) and "bad_base64" in _err_text(rj2)
    rj3 = await _tool_call(mcp_client, hdr2, sid2, "upload_document",
                           {"kb_id": kb_id, "filename": "a.exe",
                            "content_b64": base64.b64encode(b"x").decode()}, 5)
    assert _is_error(rj3) and "unsupported_type" in _err_text(rj3)
