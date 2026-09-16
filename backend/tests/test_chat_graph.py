async def test_graph_end_to_end_with_fakes(client, auth_headers, db_session, monkeypatch):
    """fake embedding + FakeListChatModel 跑通三节点,retrieve 被 stub。"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services.chat_graph.graph import build_graph

    async def fake_search(db, kb_ids, query, top_k=20):
        from app.services.retrieval.searcher import SearchHit

        return [
            SearchHit(101, 9, 1, "手.pdf", 1, "报销需要发票", 0.5, "vector"),
            SearchHit(102, 9, 1, "手.pdf", 2, "审批三天", 0.4, "keyword"),
        ]

    import app.services.chat_graph.nodes as nodes_mod

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)

    g = build_graph(llm=FakeListChatModel(responses=["报销需要发票,审批三天。[1]"]))
    init = {"question": "报销要什么", "kb_ids": [1]}
    final = await g.ainvoke(init)
    assert "报销需要发票" in final["answer"]
    assert final["citations"][0]["number"] == 1
    assert final["citations"][0]["filename"] == "手.pdf"
    assert final["citations"][1]["page_no"] == 2


async def test_citations_truncate_excerpt():
    from app.services.chat_graph.nodes import build_citations

    from app.services.retrieval.searcher import SearchHit

    hits = [SearchHit(1, 2, 3, "a.pdf", None, "x" * 500, 0.1, "vector")]
    cits = build_citations(hits)
    assert cits[0]["number"] == 1
    assert len(cits[0]["excerpt"]) <= 160


async def test_graph_always_has_rerank_node():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services.chat_graph.graph import build_graph

    g = build_graph(llm=FakeListChatModel(responses=["x"]))
    edges = {(e.source, e.target) for e in g.get_graph().edges}
    assert ("retrieve", "rerank") in edges  # 测试进程 RERANK_ENABLED 未设
    assert ("rerank", "grade") in edges  # M5 起 rerank 后接 grade,拓扑仍恒含 rerank


async def test_rerank_node_respects_state_flag(monkeypatch):
    from app.services.chat_graph import nodes as nodes_mod

    class FakeReranker:
        def rerank(self, query, documents, top_n=8):
            return list(range(len(documents)))[::-1]  # 全量倒序

    monkeypatch.setattr(nodes_mod, "get_reranker", lambda: FakeReranker())
    hits = [
        {"content": "甲", "chunk_id": 1, "document_id": 1, "kb_id": 1,
         "filename": "a.pdf", "page_no": 1, "score": 0.9, "source": "vector"},
        {"content": "乙", "chunk_id": 2, "document_id": 1, "kb_id": 1,
         "filename": "a.pdf", "page_no": 2, "score": 0.8, "source": "keyword"},
    ]
    off = await nodes_mod.rerank_node({"question": "q", "hits": hits})
    assert off == {}  # 未开开关:直通

    on = await nodes_mod.rerank_node({"question": "q", "hits": hits, "rerank": True})
    ids = [h["chunk_id"] for h in on["hits"]]
    assert ids == [2, 1]  # 倒序生效


async def test_retrieve_uses_search_query(monkeypatch):
    import app.services.chat_graph.nodes as nodes_mod

    captured = {}

    async def fake_search(db, kb_ids, query, top_k=20):
        captured["q"] = query
        return []

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    await nodes_mod.retrieve_node({"question": "原问题", "kb_ids": [1], "search_query": "改写后"})
    assert captured["q"] == "改写后"


async def test_retrieve_falls_back_to_question(monkeypatch):
    import app.services.chat_graph.nodes as nodes_mod

    captured = {}

    async def fake_search(db, kb_ids, query, top_k=20):
        captured["q"] = query
        return []

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    await nodes_mod.retrieve_node({"question": "原问题", "kb_ids": [1]})
    assert captured["q"] == "原问题"


async def test_rewrite_disabled_resets_state():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services.chat_graph.nodes import rewrite_node

    out = await rewrite_node(
        {"question": "它是什么", "history": [{"role": "user", "content": "x"}]},
        llm=FakeListChatModel(responses=["不该被调用"]),
    )
    assert out == {"search_query": "它是什么", "retries": 0, "grade": ""}


async def test_rewrite_resolves_coreference():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.core.config import settings
    from app.services.chat_graph.nodes import rewrite_node

    old = settings.AGENTIC_REWRITE_ENABLED
    settings.AGENTIC_REWRITE_ENABLED = True
    try:
        out = await rewrite_node(
            {
                "question": "它的负责人是谁",
                "history": [
                    {"role": "user", "content": "星际探索项目是什么"},
                    {"role": "assistant", "content": "是一个项目"},
                ],
            },
            llm=FakeListChatModel(responses=["星际探索项目的负责人"]),
        )
        assert out["search_query"] == "星际探索项目的负责人"
        assert out["retries"] == 0
    finally:
        settings.AGENTIC_REWRITE_ENABLED = old


async def test_rewrite_llm_failure_falls_back():
    from app.core.config import settings
    from app.services.chat_graph.nodes import rewrite_node

    class Boom:
        async def ainvoke(self, msgs, config=None):
            raise RuntimeError("llm down")

    settings.AGENTIC_REWRITE_ENABLED = True
    try:
        out = await rewrite_node(
            {"question": "q", "history": [{"role": "user", "content": "h"}]}, llm=Boom()
        )
        assert out["search_query"] == "q"
    finally:
        settings.AGENTIC_REWRITE_ENABLED = False


async def test_grade_disabled_and_empty_hits():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services.chat_graph.nodes import grade_node

    llm = FakeListChatModel(responses=["{}"])
    assert await grade_node({"question": "q"}, llm=llm) == {}
    hits = [{"filename": "a", "page_no": 1, "content": "c"}]
    assert await grade_node({"question": "q", "hits": hits}, llm=llm) == {}


class _Resp:
    def __init__(self, text):
        self.content = text


class _LLMScript:
    def __init__(self, replies):
        self.replies = list(replies)

    async def ainvoke(self, msgs, config=None):
        return _Resp(self.replies.pop(0))


async def test_grade_json_parsing_paths():
    from app.services.chat_graph.nodes import grade_node

    hits = [{"filename": "a.pdf", "page_no": 2, "content": "预算三千万"}]

    from app.core.config import settings

    settings.AGENTIC_CRAG_ENABLED = True
    try:
        out = await grade_node(
            {"question": "q", "hits": hits},
            llm=_LLMScript(['{"verdict": "insufficient", "query": "新检索词"}']),
        )
        assert out == {"grade": "insufficient", "proposed_query": "新检索词"}
        ok = await grade_node(
            {"question": "q", "hits": hits},
            llm=_LLMScript(['{"verdict": "sufficient", "query": "ignored"}']),
        )
        assert ok == {"grade": "sufficient"}
        fenced = await grade_node(
            {"question": "q", "hits": hits},
            llm=_LLMScript(["```json\n{\"verdict\": \"insufficient\"}\n```"]),
        )
        assert fenced == {"grade": "insufficient"}
        bad = await grade_node(
            {"question": "q", "hits": hits}, llm=_LLMScript(["不是 json"])
        )
        assert bad == {"grade": "sufficient"}
    finally:
        settings.AGENTIC_CRAG_ENABLED = False


async def test_transform_node_increments():
    from app.services.chat_graph.nodes import transform_node

    out = await transform_node({"question": "q", "proposed_query": "p", "retries": 0})
    assert out == {"search_query": "p", "retries": 1}
    fallback = await transform_node({"question": "q", "retries": 0})
    assert fallback == {"search_query": "q", "retries": 1}


async def test_route_after_grade_branches():
    from app.services.chat_graph.graph import route_after_grade

    assert route_after_grade({"grade": "insufficient", "retries": 0}) == "transform"
    assert route_after_grade({"grade": "insufficient", "retries": 1}) == "generate"
    assert route_after_grade({"grade": "sufficient"}) == "generate"
    assert route_after_grade({}) == "generate"


async def test_graph_topology_contains_agentic_edges():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services.chat_graph.graph import build_graph

    g = build_graph(llm=FakeListChatModel(responses=["x"]))
    edges = {(e.source, e.target) for e in g.get_graph().edges}
    assert ("__start__", "rewrite") in edges
    assert ("rewrite", "retrieve") in edges
    assert ("rerank", "grade") in edges
    assert ("transform", "retrieve") in edges
    assert ("grade", "generate") in edges  # 条件边在图结构里表现为可达


async def test_crag_retries_once_end_to_end(monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.core.config import settings
    from app.services.chat_graph.graph import build_graph
    from app.services.retrieval.searcher import SearchHit

    calls = {"n": 0}

    async def fake_search(db, kb_ids, query, top_k=20):
        calls["n"] += 1
        return [SearchHit(1, 1, 1, "a.pdf", 1, f"内容-{query}", 0.5, "vector")]

    import app.services.chat_graph.nodes as nodes_mod

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)

    llm = FakeListChatModel(
        responses=[
            '{"verdict": "insufficient", "query": "第二次检索词"}',
            '{"verdict": "sufficient"}',
            "最终答案[1]",
        ]
    )
    settings.AGENTIC_CRAG_ENABLED = True
    try:
        g = build_graph(llm=llm)
        final = await g.ainvoke({"question": "问个问题", "kb_ids": [1]})
        assert calls["n"] == 2  # 重检恰好一次
        assert "最终答案" in final["answer"]
        assert final["retries"] == 1
    finally:
        settings.AGENTIC_CRAG_ENABLED = False
