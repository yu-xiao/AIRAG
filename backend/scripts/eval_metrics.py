import json
from pathlib import Path


def hit_at_k(retrieved_doc_ids: list[int], expect_doc_ids: list[int]) -> bool:
    top = set(retrieved_doc_ids)
    return any(d in top for d in expect_doc_ids)


def mrr(retrieved_doc_ids: list[int], expect_doc_ids: list[int]) -> float:
    exp = set(expect_doc_ids)
    for rank, doc_id in enumerate(retrieved_doc_ids, start=1):
        if doc_id in exp:
            return 1.0 / rank
    return 0.0


def keyword_recall(hit_contents: list[str], expect_keywords: list[str]) -> float:
    if not expect_keywords:
        return 1.0
    blob = "\n".join(hit_contents)
    found = [k for k in expect_keywords if k in blob]
    return round(len(found) / len(expect_keywords), 4)


def load_eval_set(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
