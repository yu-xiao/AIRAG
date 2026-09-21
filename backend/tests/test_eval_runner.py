# backend/tests/test_eval_runner.py
"""M15 T3:eval_runner 单题计算(fake searcher/judge)与题加载顺序。"""
from app.models import EvalQuestion
from app.services.eval_runner import (
    generation_item,
    hit_at_k,
    keyword_recall,
    load_questions,
    mrr,
    retrieval_item,
)


class _Hit:
    def __init__(self, doc_id, content):
        self.document_id = doc_id
        self.content = content


async def test_retrieval_item_metrics(client, auth_headers, db_session,
                                      monkeypatch):
    kb_id = (await client.post(
        "/api/kbs", json={"name": "runner库"}, headers=auth_headers)
    ).json()["id"]
    q = EvalQuestion(kb_id=kb_id, question="预算多少",
                     expect_doc_ids=[11], expect_keywords=["三千万"])
    db_session.add(q)
    await db_session.commit()

    async def fake_search(db, kb_ids, question, top_k):
        return [_Hit(11, "预算三千万"), _Hit(12, "其他")]

    monkeypatch.setattr("app.services.eval_runner.hybrid_search", fake_search)
    out = await retrieval_item(db_session, kb_id, q, 8, None)
    assert out["question"] == "预算多少"
    assert out["hit_at_k"] is True and out["mrr"] == 1.0
    assert out["keyword_recall"] == 1.0
    assert out["expect_doc_ids"] == [11]


async def test_generation_item_shape(monkeypatch):
    q = EvalQuestion(kb_id=3, question="q", reference_answer="ref")

    class _Graph:
        async def ainvoke(self, state):
            return {"answer": "答", "refused": False, "citations": [1],
                    "hits": [{"content": "c1"}, {"content": "c2"}]}

    async def fake_judge(llm, question, answer, *a):
        return {"score": 0.9, "reasons": "r"}

    monkeypatch.setattr("app.services.eval_runner.faithfulness_score",
                        fake_judge)
    monkeypatch.setattr("app.services.eval_runner.relevancy_score",
                        fake_judge)
    monkeypatch.setattr("app.services.eval_runner.reference_score",
                        fake_judge)
    out = await generation_item(3, q, llm=None, graph=_Graph(), use_rerank=False)
    assert out == {"question": "q", "answer": "答", "refused": False,
                   "citations": 1, "faithfulness": {"score": 0.9, "reasons": "r"},
                   "relevancy": {"score": 0.9, "reasons": "r"},
                   "reference": {"score": 0.9, "reasons": "r"}}


async def test_load_questions_id_order(client, auth_headers, db_session):
    kb_id = (await client.post(
        "/api/kbs", json={"name": "runner库2"}, headers=auth_headers)
    ).json()["id"]
    db_session.add_all([EvalQuestion(kb_id=kb_id, question=f"q{i}")
                        for i in range(3)])
    await db_session.commit()
    got = await load_questions(db_session, kb_id)
    assert [x.question for x in got] == ["q0", "q1", "q2"]


def test_metric_functions_moved():
    assert hit_at_k([3, 4], [4]) is True
    assert mrr([3, 4], [4]) == 0.5
    assert keyword_recall(["a b"], ["a"]) == 1.0
