"""M15:eval_sets/*.json 一次性种子(从 f6a7b8c9d0e1 迁移调用;独立模块便于单测)。
用轻量 Table 而非 ORM 模型,防迁移期模型漂移;JSON 列显式声明类型
sqlite(单测)与 PG(迁移)都正确序列化。
"""
import json
from pathlib import Path

from sqlalchemy import JSON, Column, Integer, MetaData, Table, Text, insert, text

_META = MetaData()
_EVAL_QUESTIONS = Table(
    "eval_questions", _META,
    Column("kb_id", Integer),
    Column("question", Text),
    Column("expect_doc_ids", JSON),
    Column("expect_keywords", JSON),
    Column("reference_answer", Text),
)


def seed_eval_questions(conn, eval_dir: Path) -> dict:
    """把 eval_sets/{kb_id}.json 导入 eval_questions;KB 已删跳过。返回报告。"""
    imported, skipped = 0, []
    if eval_dir.exists():
        for f in sorted(eval_dir.glob("*.json")):
            if not f.stem.isdigit():
                continue
            kb_id = int(f.stem)
            if not conn.execute(
                text("SELECT 1 FROM knowledge_bases WHERE id = :kb"),
                {"kb": kb_id},
            ).scalar():
                skipped.append(kb_id)
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
            for item in data.get("items", []):
                conn.execute(insert(_EVAL_QUESTIONS).values(
                    kb_id=kb_id,
                    question=item["question"],
                    expect_doc_ids=item.get("expect_doc_ids") or [],
                    expect_keywords=item.get("expect_keywords") or [],
                    reference_answer=item.get("reference_answer"),
                ))
                imported += 1
    return {"imported": imported, "skipped_kb_ids": skipped}
