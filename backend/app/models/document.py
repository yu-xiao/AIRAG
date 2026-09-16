from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, TSVector


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    filename: Mapped[str] = mapped_column(String(256))
    file_path: Mapped[str] = mapped_column(String(512))
    mime: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    # pending|parsing|chunking|embedding|done|failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    error_msg: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    # M5 OCR:auto/force/off(上传表单);ocr_used 为实际解析是否走了 MinerU
    ocr_mode: Mapped[str] = mapped_column(String(8), default="auto", server_default="auto")
    ocr_used: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    page_no: Mapped[int | None] = mapped_column(Integer)
    char_len: Mapped[int] = mapped_column(Integer)
    embedding = mapped_column(Vector(1024))  # 嵌入前为 NULL
    tsv = mapped_column(TSVector)  # M1 仅建列,M3 换 zhparser 配置并生成
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
