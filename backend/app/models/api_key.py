# backend/app/models/api_key.py
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ApiKey(Base, TimestampMixin):
    """M9:对外 Agent 调用凭证;绑定用户,权限实时继承归属用户。"""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    key_prefix: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(64))  # sha256 hex,不存明文
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # M12:库粒度白名单;NULL=不限(继承用户全部可访问库),非空=仅列出的库
    kb_scope: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    # M11:能力位;read_only=只读检索/问答,editor=可写文档(仍受用户 KB 权限约束)
    role: Mapped[str] = mapped_column(String(16), default="read_only",
                                      server_default="read_only")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
