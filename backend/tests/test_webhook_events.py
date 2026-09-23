# backend/tests/test_webhook_events.py
"""M17 T3:事件在真实终态路径上展开成投递行 + commit 后 nudge。"""
import time
from sqlalchemy import select

from app.models import WebhookDelivery, WebhookEndpoint
from app.workers.eval_tasks import run_evaluation


async def _subscribed_ep(db_session, events=None):
    ep = WebhookEndpoint(name=f"ev{time.time_ns()}", url="http://x/h",
                         secret="wh_s", events=events, created_by=1)
    db_session.add(ep)
    await db_session.commit()
    return ep


async def _mk_running_run(client, auth_headers, db_session, n=1) -> int:
    from app.models import EvalQuestion, EvalRun
    kb_id = (await client.post(
        "/api/kbs", json={"name": f"ev库{time.time_ns()}"},
        headers=auth_headers)).json()["id"]
    db_session.add_all([EvalQuestion(kb_id=kb_id, question=f"q{i}")
                        for i in range(n)])
    run = EvalRun(kb_id=kb_id, mode="retrieval", summary=None,
                  item_count=n, status="running", triggered_by=1)
    db_session.add(run)
    await db_session.commit()
    return run.id


async def test_eval_completed_emits(client, auth_headers, db_session,
                                    monkeypatch):
    await _subscribed_ep(db_session)
    run_id = await _mk_running_run(client, auth_headers, db_session)
    nudged = []
    monkeypatch.setattr("app.services.eval_runner.nudge",
                        lambda: nudged.append(1))
    run_evaluation.run(run_id, "retrieval", False, 8)
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1 and rows[0].event_type == "eval.completed"
    assert rows[0].payload["data"]["run"]["id"] == run_id
    assert rows[0].payload["data"]["run"]["mode"] == "retrieval"
    assert nudged == [1]


async def test_eval_failed_emits(client, auth_headers, db_session,
                                 monkeypatch):
    import app.services.eval_runner as runner_mod
    await _subscribed_ep(db_session)
    run_id = await _mk_running_run(client, auth_headers, db_session)

    async def boom(db, kb_id, q, top_k, reranker):
        raise RuntimeError("炸")

    monkeypatch.setattr(runner_mod, "retrieval_item", boom)
    monkeypatch.setattr(runner_mod, "nudge", lambda: None)
    run_evaluation.run(run_id, "retrieval", False, 8)
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1 and rows[0].event_type == "eval.failed"
    assert "炸" in rows[0].payload["data"]["error"]


# ---- chat.refused 三面(REST / Web SSE / MCP)----

async def test_chat_refused_rest_emits(client, auth_headers, db_session,
                                       monkeypatch):
    from app.services.agent_facade import AskOutcome
    from tests.test_agent_api import _create_kb, _create_key

    await _subscribed_ep(db_session, ["chat.refused"])
    kb_id = await _create_kb(client, auth_headers, "拒答REST库")
    key = await _create_key(client, auth_headers)

    async def fake_ask(db, user, kb_ids, query, rerank, key_scope=None):
        return AskOutcome(answer="知识库中未找到相关内容", citations=[],
                          refused=True, tokens_used=0, elapsed_ms=1)

    monkeypatch.setattr("app.services.agent_facade.agent_ask", fake_ask)
    nudged = []
    monkeypatch.setattr("app.api.agent.nudge", lambda: nudged.append(1))
    r = await client.post(
        "/api/agent/ask", json={"kb_ids": [kb_id], "query": "无关问题"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 200 and r.json()["refused"] is True
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1 and rows[0].event_type == "chat.refused"
    data = rows[0].payload["data"]
    assert data["source"] == "rest" and data["kb_ids"] == [kb_id]
    assert data["question"] == "无关问题"
    assert nudged == [1]


async def test_chat_refused_web_sse_emits(client, auth_headers, db_session,
                                          monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    import app.services.chat_graph.nodes as nodes_mod
    from app.services.chat_graph.graph import build_graph

    async def fake_search(db, kb_ids, query, top_k=20):
        return []  # 零命中 → 拒答终态

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    real_build = build_graph
    monkeypatch.setattr(
        "app.api.ask.build_graph",
        lambda **kw: real_build(llm=FakeListChatModel(
            responses=["知识库中未找到相关内容"])),
    )
    await _subscribed_ep(db_session, ["chat.refused"])
    nudged = []
    monkeypatch.setattr("app.api.ask.nudge", lambda: nudged.append(1))
    kb = await client.post("/api/kbs", json={"name": "拒答SSE库"},
                           headers=auth_headers)
    resp = await client.post(
        "/api/chat/ask",
        json={"kb_ids": [kb.json()["id"]], "question": "无关问题"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert '"refused": true' in resp.text
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1 and rows[0].event_type == "chat.refused"
    data = rows[0].payload["data"]
    assert data["source"] == "web" and data["kb_ids"] == [kb.json()["id"]]
    assert nudged == [1]


async def test_chat_refused_mcp_emits(client, auth_headers, db_session,
                                      monkeypatch):
    """直调工具本体:JSON-RPC 壳已有 test_mcp 覆盖;test_mcp 的 session 级
    lifespan 夹具不可跨模块复用(StreamableHTTPSessionManager 只许 run 一次,
    第二个 FixtureDef 再启即炸),中间件写 principal/ip 的 contextvar 由本
    用例手工设置,走的是 ask_knowledge_base 真实代码路径。"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.core.deps import Principal, current_client_ip, current_principal
    from app.mcp_server import ask_knowledge_base
    from app.models import User
    from app.services import agent_facade
    from app.services.chat_graph import nodes
    from app.services.chat_graph.graph import build_graph

    await _subscribed_ep(db_session, ["chat.refused"])
    kb_id = (await client.post("/api/kbs", json={"name": "拒答MCP库"},
                               headers=auth_headers)).json()["id"]
    me = (await client.get("/api/auth/me", headers=auth_headers)).json()
    user = await db_session.get(User, me["id"])

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        return []  # 零命中 → 拒答终态

    monkeypatch.setattr(nodes, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(agent_facade, "_ask_graph",
                        build_graph(llm=FakeListChatModel(
                            responses=["知识库中未找到相关内容"]),
                            checkpointer=None))
    nudged = []
    monkeypatch.setattr("app.mcp_server.nudge", lambda: nudged.append(1))
    tp = current_principal.set(Principal(user=user, kind="jwt"))
    tip = current_client_ip.set("127.0.0.1")
    try:
        body = await ask_knowledge_base.fn(kb_ids=[kb_id], query="无关问题")
    finally:
        current_principal.reset(tp)
        current_client_ip.reset(tip)
    assert body["refused"] is True
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1 and rows[0].event_type == "chat.refused"
    data = rows[0].payload["data"]
    assert data["source"] == "mcp" and data["kb_ids"] == [kb_id]
    assert nudged == [1]
