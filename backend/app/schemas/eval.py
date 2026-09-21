from datetime import datetime

from pydantic import BaseModel


class EvalRunOut(BaseModel):
    id: int
    kb_id: int
    kb_name: str | None = None  # 装配时联查填入;KB 已删 → None
    mode: str
    item_count: int
    summary: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class EvalItemOut(BaseModel):
    id: int
    question: str
    expect_doc_ids: list | None
    expect_keywords: list | None
    answer: str | None
    refused: bool | None
    hit_at_k: float | None
    mrr: float | None
    keyword_recall: float | None
    faithfulness: float | None
    relevancy: float | None
    reference_score: float | None

    model_config = {"from_attributes": True}


class EvalRunDetailOut(EvalRunOut):
    items: list[EvalItemOut]
    items_truncated: bool
