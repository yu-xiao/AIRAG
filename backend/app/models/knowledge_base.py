from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class KnowledgeBase(Base, TimestampMixin):
    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    embed_provider: Mapped[str] = mapped_column(String(32), default="zhipu")
    embed_model: Mapped[str] = mapped_column(String(64), default="embedding-3")

    permissions: Mapped[list["KbPermission"]] = relationship(back_populates="kb")


class KbPermission(Base):
    __tablename__ = "kb_permissions"
    __table_args__ = (UniqueConstraint("kb_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    perm: Mapped[str] = mapped_column(String(16), default="viewer")  # viewer|editor

    kb: Mapped[KnowledgeBase] = relationship(back_populates="permissions")
