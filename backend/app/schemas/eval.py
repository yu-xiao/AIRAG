from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class EvalRunOut(BaseModel):
    id: int
    kb_id: int
    kb_name: str | None = None  # 装配时联查填入;KB 已删 → None
    mode: str
    item_count: int
    summary: dict | None  # running/failed 行终态前为 NULL(M15)
    status: str = "completed"
    created_by: str | None = None  # 联查 users.username;CLI 行为 None
    done_count: int = 0            # 进度分子 = len(items)
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
    error: str | None = None


class EvalTriggerIn(BaseModel):
    kb_id: int
    mode: Literal["retrieval", "generation"]
    rerank: bool = False
    top_k: int | None = Field(None, ge=1, le=50)  # 仅 retrieval 用


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
