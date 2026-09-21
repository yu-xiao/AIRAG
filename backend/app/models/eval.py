# backend/app/models/eval.py
from sqlalchemy import Boolean, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class EvalRun(Base, TimestampMixin):
    """一次评估运行(retrieval|generation);kb_id 无 FK——KB 删除后
    历史评估保留(M13 设计意图)。M15:status/error/triggered_by 支撑
    Web 触发;summary 完成/失败前为 NULL。"""

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(16))  # retrieval | generation
    summary: Mapped[dict | None] = mapped_column(JSON)  # 终态才写
    item_count: Mapped[int]
    status: Mapped[str] = mapped_column(
        String(16), default="completed", server_default="completed")
    error: Mapped[str | None] = mapped_column(Text)      # 失败原因(或 500)
    triggered_by: Mapped[int | None] = mapped_column(Integer)  # 无 FK,联查展示

    # lazy="selectin":async ORM 下 select 后直接访问集合属性会触发同步
    # lazy load(MissingGreenlet);selectin 预载避免之,亦无额外 N+1。
    items: Mapped[list["EvalItem"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin")


class EvalItem(Base):
    __tablename__ = "eval_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    expect_doc_ids: Mapped[list | None] = mapped_column(JSON)
    expect_keywords: Mapped[list | None] = mapped_column(JSON)
    answer: Mapped[str | None] = mapped_column(Text)
    refused: Mapped[bool | None] = mapped_column(Boolean)
    hit_at_k: Mapped[float | None] = mapped_column(Float)
    mrr: Mapped[float | None] = mapped_column(Float)
    keyword_recall: Mapped[float | None] = mapped_column(Float)
    faithfulness: Mapped[float | None] = mapped_column(Float)
    relevancy: Mapped[float | None] = mapped_column(Float)
    reference_score: Mapped[float | None] = mapped_column(Float)

    run: Mapped[EvalRun] = relationship(back_populates="items")


class EvalQuestion(Base, TimestampMixin):
    """M15:评估题集入库;kb_id FK CASCADE——题集随库删(内容资产,
    与 eval_runs 保留历史语义相反)。"""

    __tablename__ = "eval_questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    expect_doc_ids: Mapped[list | None] = mapped_column(JSON)
    expect_keywords: Mapped[list | None] = mapped_column(JSON)
    reference_answer: Mapped[str | None] = mapped_column(Text)
