# backend/tests/test_agent_ask.py
"""M10 Task2:facade.agent_ask(图复用/计量/去重拒权)。"""
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import select as sa_select

from app.models import User
from app.services import agent_facade
from app.services.chat_graph.graph import build_graph
from app.services.retrieval.searcher import SearchHit


class _UsageChatModel(BaseChatModel):
    """唯一响应带 usage_metadata,验证 TokenMeter 正向计量。"""

    @property
    def _llm_type(self) -> str:
        return "usage-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content="计量答案",
            usage_metadata={"input_tokens": 10, "output_tokens": 5,
                            "total_tokens": 15}))])


async def _me(client, auth_headers, db_session) -> User:
    me = (await client.get("/api/auth/me", headers=auth_headers)).json()
    return (await db_session.execute(
        sa_select(User).where(User.id == me["id"]))).scalars().one()


def _patch_graph(monkeypatch, llm, kb_id: int):
    from app.services.chat_graph import nodes

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        return [SearchHit(chunk_id=1, document_id=10, kb_id=kb_id,
                          filename="a.pdf", page_no=1, content="八千五百米",
                          score=0.9, source="both")]

    monkeypatch.setattr(nodes, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(agent_facade, "_ask_graph",
                        build_graph(llm=llm, checkpointer=None))


async def test_agent_ask_outcome_fields(client, auth_headers, db_session,
                                        monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    kb = await client.post("/api/kbs", json={"name": "ask库"},
                           headers=auth_headers)
    kb_id = kb.json()["id"]
    user = await _me(client, auth_headers, db_session)
    _patch_graph(
        monkeypatch,
        FakeListChatModel(responses=["巡航升限为八千五百米。"]), kb_id)

    out = await agent_facade.agent_ask(db_session, user, [kb_id, kb_id],
                                       "升限?", False)
    assert out.answer == "巡航升限为八千五百米。"
    assert out.refused is False
    assert out.tokens_used == 0  # FakeListChatModel 无 usage,按 0(spec B)
    assert out.citations and out.citations[0]["filename"] == "a.pdf"
    assert isinstance(out.elapsed_ms, int)


async def test_agent_ask_tokens_metered(client, auth_headers, db_session,
                                        monkeypatch):
    kb = await client.post("/api/kbs", json={"name": "计量库"},
                           headers=auth_headers)
    _patch_graph(monkeypatch, _UsageChatModel(), kb.json()["id"])
    user = await _me(client, auth_headers, db_session)
    out = await agent_facade.agent_ask(db_session, user, [kb.json()["id"]],
                                       "q", False)
    assert out.tokens_used == 15


async def test_agent_ask_denied_dedup(client, auth_headers, db_session):
    kb = await client.post("/api/kbs", json={"name": "拒权库"},
                           headers=auth_headers)
    kb_id = kb.json()["id"]
    user = await _me(client, auth_headers, db_session)
    with pytest.raises(agent_facade.AgentKbDenied) as ei:
        await agent_facade.agent_ask(db_session, user,
                                     [99999, kb_id, 99999], "q", False)
    assert ei.value.denied_kb_ids == [99999]  # 去重后唯一


# ---- M11:小项④ ask 失败应用级日志 + 小项⑤ CitationOut 强类型 ----
async def test_ask_500_logs_exception(client, auth_headers, monkeypatch):
    from tests.test_agent_api import _create_kb, _create_key

    kb_id = await _create_kb(client, auth_headers, "500库")
    key = await _create_key(client, auth_headers)

    async def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.agent_facade.agent_ask", boom)
    import app.api.agent as agent_mod

    calls = []

    class FakeLogger:
        def exception(self, msg, *a, **k):
            calls.append(msg)

    monkeypatch.setattr(agent_mod, "logger", FakeLogger())
    r = await client.post(
        "/api/agent/ask", json={"kb_ids": [kb_id], "query": "q"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 500
    assert calls  # 应用级日志已记,不再只靠 uvicorn 兜底


async def test_ask_citations_strongly_typed(client, auth_headers, monkeypatch):
    from tests.test_agent_api import _create_kb, _create_key

    kb_id = await _create_kb(client, auth_headers, "引用库")
    key = await _create_key(client, auth_headers)

    async def fake_ask(db, user, kb_ids, query, rerank):
        from app.services.agent_facade import AskOutcome
        return AskOutcome(
            answer="a",
            citations=[{"number": 1, "chunk_id": 2, "document_id": 3,
                        "filename": "f.pdf", "page_no": 1, "excerpt": "e",
                        "junk": "dropped"}],
            refused=False, tokens_used=5, elapsed_ms=1,
        )

    monkeypatch.setattr("app.services.agent_facade.agent_ask", fake_ask)
    r = await client.post(
        "/api/agent/ask", json={"kb_ids": [kb_id], "query": "q"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 200
    assert r.json()["citations"] == [
        {"number": 1, "chunk_id": 2, "document_id": 3, "filename": "f.pdf",
         "page_no": 1, "excerpt": "e"}
    ]  # 多余键被 CitationOut 丢弃
