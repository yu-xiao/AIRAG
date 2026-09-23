# backend/app/models/webhook.py
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class WebhookEndpoint(Base, TimestampMixin):
    """M17:出站 webhook 端点(admin 全局);secret 原文存储——HMAC 签名
    需要原文,与 ApiKey hash 模式 deliberately 不同,任何读路径不得回显。"""
    __tablename__ = "webhook_endpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    url: Mapped[str] = mapped_column(String(500))
    secret: Mapped[str] = mapped_column(String(64))
    events: Mapped[list | None] = mapped_column(JSON)   # None/空=订阅全部
    enabled: Mapped[bool] = mapped_column(Boolean, default=True,
                                          server_default="true")
    created_by: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(String(200))


class WebhookDelivery(Base, TimestampMixin):
    """单事件×单端点的一次投递;payload 是信封快照(event_id 幂等键)。
    状态机:pending → succeeded | retrying →(到期)succeeded|retrying|dead;
    4xx(非 429)直接 dead(对方拒收,重试无意义)。"""
    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    event_id: Mapped[str] = mapped_column(String(36))
    payload: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    response_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(500))
