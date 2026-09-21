"""M15 T1:eval_sets/*.json 一次性种子函数(sqlite 同步引擎测,免起 PG)。"""
import json

from sqlalchemy import create_engine, text

from app.services.eval_seed import _EVAL_QUESTIONS, seed_eval_questions


def _make_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'seed.db'}")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE knowledge_bases (id INTEGER PRIMARY KEY)"))
        _EVAL_QUESTIONS.create(c)
        c.execute(text("INSERT INTO knowledge_bases (id) VALUES (5)"))
    return engine


def test_seed_imports_json_and_skips_missing_kb(tmp_path):
    sets = tmp_path / "sets"
    sets.mkdir()
    (sets / "5.json").write_text(json.dumps(
        {"items": [{"question": "预算多少", "expect_doc_ids": [1],
                    "expect_keywords": ["预算"], "reference_answer": "三千万"}]},
    ), encoding="utf-8")
    (sets / "999.json").write_text(  # KB 已删 → 跳过
        json.dumps({"items": [{"question": "gone"}]}), encoding="utf-8")
    (sets / "ignored.txt").write_text("x", encoding="utf-8")  # 非数字名不理
    engine = _make_db(tmp_path)
    with engine.begin() as c:
        report = seed_eval_questions(c, sets)
    assert report == {"imported": 1, "skipped_kb_ids": [999]}
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT kb_id, question, expect_doc_ids, reference_answer "
            "FROM eval_questions")).one()
    assert row == (5, "预算多少", "[1]", "三千万")  # sqlite JSON 列落 TEXT


def test_seed_missing_dir_is_noop(tmp_path):
    engine = _make_db(tmp_path)
    with engine.begin() as c:
        report = seed_eval_questions(c, tmp_path / "nope")
    assert report == {"imported": 0, "skipped_kb_ids": []}
