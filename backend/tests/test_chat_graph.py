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
    assert ("rerank", "generate") in edges


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
