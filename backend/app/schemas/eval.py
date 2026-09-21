from datetime import datetime

from pydantic import BaseModel, Field, field_validator


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


def _strip_question(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("question must not be blank")
    return v


class EvalQuestionIn(BaseModel):
    kb_id: int
    question: str = Field(min_length=1, max_length=2000)
    expect_doc_ids: list[int] = []
    expect_keywords: list[str] = []
    reference_answer: str | None = None

    @field_validator("question")
    @classmethod
    def _q(cls, v: str) -> str:
        return _strip_question(v)


class EvalQuestionUpdate(BaseModel):
    """PUT 全量更新;不带 kb_id(题不可搬家)。"""
    question: str = Field(min_length=1, max_length=2000)
    expect_doc_ids: list[int] = []
    expect_keywords: list[str] = []
    reference_answer: str | None = None

    @field_validator("question")
    @classmethod
    def _q(cls, v: str) -> str:
        return _strip_question(v)


class EvalQuestionOut(BaseModel):
    id: int
    kb_id: int
    question: str
    expect_doc_ids: list | None
    expect_keywords: list | None
    reference_answer: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
