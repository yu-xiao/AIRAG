# backend/tests/test_eval_store.py
"""M13 Task3:eval_store.save_run 落库与 summary 口径。"""
from sqlalchemy import select

from app.models import EvalItem, EvalRun
from scripts.eval_store import save_run, summarize


def test_summarize_retrieval():
    results = [
        {"question": "q1", "expect_doc_ids": [1], "expect_keywords": ["a"],
         "hit_at_k": True, "mrr": 1.0, "keyword_recall": 1.0},
        {"question": "q2", "expect_doc_ids": [2], "expect_keywords": [],
         "hit_at_k": False, "mrr": 0.0, "keyword_recall": 0.0},
    ]
    s = summarize(results)
    assert s["hit"] == 0.5 and s["mrr"] == 0.5 and s["keyword_recall"] == 0.5
    assert s["item_count"] == 2


def test_summarize_generation_skips_none_scores():
    results = [
        {"question": "q", "faithfulness": {"score": 0.9},
         "relevancy": {"score": None}, "refused": True},
    ]
    s = summarize(results)
    assert s["faithfulness_avg"] == 0.9 and s["relevancy_avg"] is None
    assert s["refused_count"] == 1


def test_summarize_reference_key_with_none_value():
    """T10 修复回归:reference 键在值 None(评估集无 reference_answer 的题,
    eval_generation 产出 "reference": None)不崩溃;键在故 reference_avg
    出现为 None(无分可聚合),faithfulness/relevancy 不受影响。"""
    results = [
        {"question": "q", "faithfulness": {"score": 0.9},
         "relevancy": {"score": 0.8}, "refused": False,
         "reference": None},
    ]
    s = summarize(results)
    assert s["faithfulness_avg"] == 0.9
    assert s["reference_avg"] is None


def test_summarize_retrieval_skips_unmeasured():
    """M16 A1:None(未测量)不计入均值分母;item_count 仍是全题数。"""
    results = [
        {"question": "q1", "hit_at_k": True, "mrr": 1.0,
         "keyword_recall": 1.0},
        {"question": "q2", "hit_at_k": None, "mrr": None,
         "keyword_recall": None},
    ]
    s = summarize(results)
    assert s["hit"] == 1.0 and s["mrr"] == 1.0 and s["keyword_recall"] == 1.0
    assert s["item_count"] == 2


def test_summarize_all_unmeasured_omits_metric_keys():
    results = [{"question": "q", "hit_at_k": None, "mrr": None,
                "keyword_recall": None}]
    s = summarize(results)
    assert s == {"item_count": 1}


def test_summarize_keyword_only_measured():
    """文档期望未设、关键词设了:hit/mrr 缺席,keyword_recall 独立测量。"""
    results = [{"question": "q", "hit_at_k": None, "mrr": None,
                "keyword_recall": 0.5}]
    s = summarize(results)
    assert s == {"item_count": 1, "keyword_recall": 0.5}


async def test_save_run_roundtrip(db_session):
    results = [
        {"question": "q1", "expect_doc_ids": [11], "expect_keywords": ["三千"],
         "hit_at_k": True, "mrr": 1.0, "keyword_recall": 1.0},
    ]
    run_id = await save_run(3, "retrieval", results)
    assert run_id > 0
    db_session.expire_all()
    rows = (await db_session.execute(select(EvalRun))).scalars().all()
    assert len(rows) == 1 and rows[0].kb_id == 3
    items = (await db_session.execute(select(EvalItem))).scalars().all()
    assert len(items) == 1 and items[0].hit_at_k == 1.0
