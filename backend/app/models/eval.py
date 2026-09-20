# backend/app/models/eval.py
from sqlalchemy import Boolean, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class EvalRun(Base, TimestampMixin):
    """M13:一次评估运行(retrieval|generation);kb_id 无 FK——KB 删除后
    历史评估保留(spec B 设计意图)。"""

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(16))  # retrieval | generation
    summary: Mapped[dict] = mapped_column(JSON)    # 各指标均值 + 计数
    item_count: Mapped[int]

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
