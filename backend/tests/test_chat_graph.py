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
            return [(i, 0.9) for i in range(len(documents))][::-1]  # 全量倒序

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
    assert out == {
        "search_query": "它是什么",
        "retries": 0,
        "grade": "",
        "hopped": False,
        "sub_queries": [],  # M6:每轮清空,防 checkpointer 跨轮残留
        "proposed_query": "",
    }


async def test_rewrite_clears_stale_multihop_state():
    """上一轮经 checkpointer 残留的多跳字段必须在本轮 rewrite 被清空。"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services.chat_graph.nodes import rewrite_node

    out = await rewrite_node(
        {
            "question": "新问题",
            "sub_queries": ["旧子问题"],
            "proposed_query": "旧提示",
            "hopped": True,
        },
        llm=FakeListChatModel(responses=["不该被调用"]),  # 开关关闭,llm 不应被调用
    )
    assert out["sub_queries"] == []
    assert out["proposed_query"] == ""
    assert out["hopped"] is False
    assert out["search_query"] == "新问题"


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


async def test_decompose_parses_truncates_and_dedupes():
    from app.services.chat_graph.nodes import decompose_node

    out = await decompose_node(
        {"question": "复合", "search_query": "A 和 B", "proposed_query": "改写"},
        llm=_LLMScript(['["子问题一", "子问题二", "子问题三", "子问题四"]']),
    )
    assert out["sub_queries"] == ["子问题一", "子问题二", "子问题三"]  # 截断到 MAX_SUBQ=3
    assert out["hopped"] is True


async def test_decompose_filters_and_fenced_json():
    from app.services.chat_graph.nodes import decompose_node

    out = await decompose_node(
        {"question": "复合", "search_query": "改写后查询"},
        llm=_LLMScript(['```json\n["甲", 42, " ", "甲", "乙"]\n```']),
    )
    assert out["sub_queries"] == ["甲", "乙"]  # 非字符串/空白被滤,重复去重,围栏剥离


async def test_decompose_bad_json_falls_back():
    from app.services.chat_graph.nodes import decompose_node

    # 解析失败:退化为 [proposed_query or search_query or question]
    out = await decompose_node(
        {"question": "原问题", "search_query": "改写后", "proposed_query": "提议词"},
        llm=_LLMScript(["不是 json"]),
    )
    assert out["sub_queries"] == ["提议词"]
    assert out["hopped"] is True
    out2 = await decompose_node(
        {"question": "原问题"}, llm=_LLMScript(["[]"])  # 空数组也走兜底
    )
    assert out2["sub_queries"] == ["原问题"]


async def test_decompose_llm_exception_falls_back():
    from app.services.chat_graph.nodes import decompose_node

    class Boom:
        async def ainvoke(self, msgs, config=None):
            raise RuntimeError("llm down")

    out = await decompose_node(
        {"question": "原问题", "search_query": "改写后"},
        llm=Boom(),
    )
    assert out == {"sub_queries": ["改写后"], "hopped": True}


async def test_retrieve_multi_query_merges_round_robin(monkeypatch):
    import app.services.chat_graph.nodes as nodes_mod
    from app.services.retrieval.searcher import SearchHit

    async def fake_search(db, kb_ids, query, top_k=20):
        data = {
            "甲": [(1, "甲一", 0.9), (2, "甲二", 0.7)],
            "乙": [(2, "乙一", 0.8), (3, "乙二", 0.6)],
        }[query]
        return [
            SearchHit(cid, 1, 1, "f.pdf", i + 1, txt, s, "vector")
            for i, (cid, txt, s) in enumerate(data)
        ]

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    out = await nodes_mod.retrieve_node(
        {"question": "q", "kb_ids": [1], "sub_queries": ["甲", "乙"]}
    )
    # 轮转交错:甲1→乙1(chunk 2)→甲2(chunk 2 去重跳过)→乙2
    assert [h["chunk_id"] for h in out["hits"]] == [1, 2, 3]


async def test_retrieve_multi_query_caps_at_2x_topk(monkeypatch):
    import app.services.chat_graph.nodes as nodes_mod
    from app.core.config import settings
    from app.services.retrieval.searcher import SearchHit

    async def fake_search(db, kb_ids, query, top_k=20):
        return [
            SearchHit(query == "甲" and i or 100 + i, 1, 1, "f.pdf", i + 1,
                      f"{query}-{i}", 0.9, "vector")
            for i in range(8)
        ]

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    monkeypatch.setattr(settings, "RETRIEVAL_TOP_K", 2)  # 合并上限 = 2*2 = 4
    out = await nodes_mod.retrieve_node(
        {"question": "q", "kb_ids": [1], "sub_queries": ["甲", "乙"]}
    )
    assert len(out["hits"]) == 4


async def test_route_after_grade_multihop_branches(monkeypatch):
    from app.core.config import settings
    from app.services.chat_graph.graph import route_after_grade

    monkeypatch.setattr(settings, "MULTI_HOP_ENABLED", True)
    assert route_after_grade(
        {"grade": "insufficient", "retries": 0}) == "transform"  # 重检优先(M5)
    assert route_after_grade(
        {"grade": "insufficient", "retries": 1, "hits": [{}]}) == "decompose"
    assert route_after_grade({"hits": []}) == "decompose"  # 零命中兜底
    assert route_after_grade({"hits": [{}]}) == "generate"  # 正常
    assert route_after_grade(
        {"grade": "insufficient", "retries": 1, "hits": [{}], "hopped": True}
    ) == "generate"  # 防环


async def test_route_after_grade_multihop_disabled(monkeypatch):
    from app.core.config import settings
    from app.services.chat_graph.graph import route_after_grade

    monkeypatch.setattr(settings, "MULTI_HOP_ENABLED", False)
    assert route_after_grade(
        {"grade": "insufficient", "retries": 1, "hits": [{}]}) == "generate"
    assert route_after_grade({"hits": []}) == "generate"  # 回到 M5 行为


async def test_route_after_rerank_bypasses_grade_when_hopped():
    from app.services.chat_graph.graph import route_after_rerank

    assert route_after_rerank({"hopped": True}) == "generate"
    assert route_after_rerank({}) == "grade"
    assert route_after_rerank({"hopped": False}) == "grade"


async def test_graph_topology_contains_multihop_edges():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services.chat_graph.graph import build_graph

    g = build_graph(llm=FakeListChatModel(responses=["x"]))
    edges = {(e.source, e.target) for e in g.get_graph().edges}
    assert ("decompose", "retrieve") in edges
    assert ("rerank", "generate") in edges  # hopped 直通(条件边可达)
    assert ("rerank", "grade") in edges  # 条件边两分支都在图结构里
    assert ("grade", "decompose") in edges


async def test_multihop_fallback_end_to_end(monkeypatch):
    """CRAG+多跳全开:首轮不足→transform 重检→仍不足→decompose 拆两问→合并检索→直通 generate。"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.core.config import settings
    from app.services.chat_graph.graph import build_graph
    from app.services.retrieval.searcher import SearchHit

    calls = []

    async def fake_search(db, kb_ids, query, top_k=20):
        calls.append(query)
        return [SearchHit(len(calls), 1, 1, "a.pdf", 1, f"内容-{query}", 0.5, "vector")]

    import app.services.chat_graph.nodes as nodes_mod

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    monkeypatch.setattr(settings, "AGENTIC_CRAG_ENABLED", True)
    monkeypatch.setattr(settings, "MULTI_HOP_ENABLED", True)

    llm = FakeListChatModel(responses=[
        '{"verdict": "insufficient", "query": "重检词"}',   # grade 第 1 轮
        '{"verdict": "insufficient", "query": "再改写"}',   # grade 第 2 轮(重检后)
        '["子问题A", "子问题B"]',                            # decompose
        "最终答案[1]",                                       # generate
    ])
    g = build_graph(llm=llm)
    final = await g.ainvoke({"question": "复合问题", "kb_ids": [1]})
    assert "子问题A" in calls and "子问题B" in calls  # 两个子查询都检索了
    assert final["hopped"] is True
    assert final["sub_queries"] == ["子问题A", "子问题B"]
    assert "最终答案" in final["answer"]


async def test_multihop_state_does_not_leak_across_turns(monkeypatch):
    """checkpointer 回归:同 thread 第二问必须检索新问题本身,不得复用第一轮的子查询。"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langgraph.checkpoint.memory import InMemorySaver

    from app.core.config import settings
    from app.services.chat_graph.graph import build_graph
    from app.services.retrieval.searcher import SearchHit

    calls = []

    async def fake_search(db, kb_ids, query, top_k=20):
        calls.append(query)
        return [SearchHit(len(calls), 1, 1, "a.pdf", 1, f"内容-{query}", 0.5, "vector")]

    import app.services.chat_graph.nodes as nodes_mod

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    monkeypatch.setattr(settings, "AGENTIC_CRAG_ENABLED", True)
    monkeypatch.setattr(settings, "MULTI_HOP_ENABLED", True)

    llm = FakeListChatModel(responses=[
        '{"verdict": "insufficient", "query": "重检词"}',   # 第 1 问 grade(首次)
        '{"verdict": "insufficient", "query": "再改写"}',   # 第 1 问 grade(重检后)
        '["子问题A", "子问题B"]',                            # 第 1 问 decompose
        "第一轮答案[1]",                                     # 第 1 问 generate
        '{"verdict": "sufficient"}',                        # 第 2 问 grade
        "第二轮答案[1]",                                     # 第 2 问 generate
    ])
    g = build_graph(llm=llm, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t-multihop-leak"}}

    await g.ainvoke({"question": "第一问复合题", "kb_ids": [1]}, config=cfg)
    assert "子问题A" in calls and "子问题B" in calls  # 第 1 问确实走了多跳

    calls.clear()  # 只记录第 2 问的检索
    final2 = await g.ainvoke({"question": "第二问新问题", "kb_ids": [1]}, config=cfg)
    assert calls == ["第二问新问题"]  # 新问题本身被检索;旧子查询未泄漏
    assert "第二轮答案" in final2["answer"]


def _hit(cid: int, content: str) -> dict:
    return {"chunk_id": cid, "document_id": 1, "kb_id": 1,
            "filename": "a.pdf", "page_no": 1, "content": content,
            "score": 0.01, "source": "vector"}


async def test_rerank_node_sorts_and_filters_by_threshold(monkeypatch):
    from app.core.config import settings
    from app.services.chat_graph import nodes as nodes_mod

    class FakeReranker:
        def rerank(self, query, documents, top_n=8):
            return [(0, 0.4), (1, 0.9), (2, 0.1)]  # 乱序,含低于阈值

    monkeypatch.setattr(nodes_mod, "get_reranker", lambda: FakeReranker())
    monkeypatch.setattr(settings, "RETRIEVAL_MIN_SCORE", 0.3)
    out = await nodes_mod.rerank_node(
        {"question": "q", "hits": [_hit(1, "甲"), _hit(2, "乙"), _hit(3, "丙")],
         "rerank": True})
    assert [h["chunk_id"] for h in out["hits"]] == [2, 1]  # 按分降序,丙被滤
    assert out["hits"][0]["score"] == 0.9  # relevance 回写 score
    assert out["hits"][1]["score"] == 0.4


async def test_rerank_node_threshold_zero_disables_filter(monkeypatch):
    from app.core.config import settings
    from app.services.chat_graph import nodes as nodes_mod

    class FakeReranker:
        def rerank(self, query, documents, top_n=8):
            return [(0, 0.05), (1, 0.02)]

    monkeypatch.setattr(nodes_mod, "get_reranker", lambda: FakeReranker())
    monkeypatch.setattr(settings, "RETRIEVAL_MIN_SCORE", 0.0)
    out = await nodes_mod.rerank_node(
        {"question": "q", "hits": [_hit(1, "甲"), _hit(2, "乙")], "rerank": True})
    assert [h["chunk_id"] for h in out["hits"]] == [1, 2]  # 全保留,仅按分排序


async def test_rerank_node_filters_all_to_empty(monkeypatch):
    from app.core.config import settings
    from app.services.chat_graph import nodes as nodes_mod

    class FakeReranker:
        def rerank(self, query, documents, top_n=8):
            return [(0, 0.1), (1, 0.2)]

    monkeypatch.setattr(nodes_mod, "get_reranker", lambda: FakeReranker())
    monkeypatch.setattr(settings, "RETRIEVAL_MIN_SCORE", 0.3)
    out = await nodes_mod.rerank_node(
        {"question": "q", "hits": [_hit(1, "甲"), _hit(2, "乙")], "rerank": True})
    assert out == {"hits": []}
