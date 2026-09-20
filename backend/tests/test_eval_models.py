# backend/tests/test_eval_models.py
"""M13 Task2:评估入库两张表的模型行为(create_all 真源)。"""
from sqlalchemy import select

from app.models import EvalItem, EvalRun


async def test_eval_run_roundtrip(db_session):
    run = EvalRun(kb_id=3, mode="retrieval",
                  summary={"hit": 0.8, "mrr": 0.6}, item_count=2)
    db_session.add(run)
    await db_session.flush()
    db_session.add_all([
        EvalItem(run_id=run.id, question="q1", expect_doc_ids=[11],
                 expect_keywords=["三千"], hit_at_k=1.0, mrr=1.0,
                 keyword_recall=1.0),
        EvalItem(run_id=run.id, question="q2", answer="答",
                 refused=False, faithfulness=0.9, relevancy=0.8,
                 reference_score=None),
    ])
    await db_session.commit()
    db_session.expire_all()
    got = (await db_session.execute(
        select(EvalRun).where(EvalRun.mode == "retrieval"))).scalars().one()
    assert got.summary["hit"] == 0.8 and got.item_count == 2
    assert [i.question for i in got.items] == ["q1", "q2"]
    assert got.items[1].reference_score is None


async def test_eval_item_cascade_on_run_delete(db_session):
    run = EvalRun(kb_id=1, mode="generation", summary={}, item_count=1)
    db_session.add(run)
    await db_session.flush()
    item = EvalItem(run_id=run.id, question="q")
    db_session.add(item)
    await db_session.commit()
    await db_session.delete(run)
    await db_session.commit()
    assert (await db_session.execute(
        select(EvalItem))).scalars().first() is None
