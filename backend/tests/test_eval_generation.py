import json


class _FakeGraph:
    async def ainvoke(self, init, config=None):
        return {
            "answer": "三千万[1]",
            "hits": [{"content": "预算三千万"}],
            "citations": [{"number": 1}],
        }


async def test_eval_generation_run(tmp_path, monkeypatch, db_session):
    import scripts.eval_generation as eg
    from app.core.config import settings
    from app.core.security import hash_password
    from app.models import KnowledgeBase, User

    monkeypatch.setattr(settings, "ZHIPU_API_KEY", "k")

    u = User(username="evalgen", password_hash=hash_password("x"))
    db_session.add(u)
    await db_session.flush()
    kb = KnowledgeBase(name="评估库", owner_id=u.id)
    db_session.add(kb)
    await db_session.commit()

    (tmp_path / f"{kb.id}.json").write_text(
        json.dumps({"kb_id": kb.id,
                    "items": [{"question": "预算多少", "expect_doc_ids": [1]}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(eg, "EVAL_DIR", tmp_path)

    import app.services.chat_graph.graph as graph_mod
    monkeypatch.setattr(graph_mod, "build_graph",
                        lambda llm=None, checkpointer=None: _FakeGraph())

    import app.services.eval_judge as ej

    async def fake_f(llm, q, a, contexts):
        return {"score": 0.9, "reasons": "ok"}

    async def fake_r(llm, q, a):
        return {"score": 0.8, "reasons": "ok"}

    monkeypatch.setattr(ej, "faithfulness_score", fake_f)
    monkeypatch.setattr(ej, "relevancy_score", fake_r)

    results = await eg.run(kb.id, False)
    assert len(results) == 1
    assert results[0]["faithfulness"]["score"] == 0.9
    assert results[0]["relevancy"]["score"] == 0.8
    assert results[0]["citations"] == 1


async def test_eval_generation_requires_key(monkeypatch):
    from app.core.config import settings
    import scripts.eval_generation as eg

    monkeypatch.setattr(settings, "ZHIPU_API_KEY", "")
    try:
        await eg.run(1, False)
        raised = False
    except SystemExit:
        raised = True
    assert raised
