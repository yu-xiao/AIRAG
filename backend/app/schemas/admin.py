from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class AdminKeyCreateIn(BaseModel):
    """M9.1:admin 代发密钥(user_id 为绑定目标;权限/配额按目标用户)。"""

    user_id: int
    name: str = Field(min_length=1, max_length=64)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    role: Literal["read_only", "editor"] = "read_only"
    kb_scope: list[int] | None = None  # M12:校验按目标用户(user_id)的可见性


class AdminUserOut(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool

    model_config = {"from_attributes": True}


class AdminUserIn(BaseModel):
    role: Literal["viewer", "editor", "admin"] | None = None
    is_active: bool | None = None


class AdminUserKbOut(BaseModel):
    """M12:admin 代发 scoped key 的范围下拉条目(目标用户可见库)。"""

    id: int
    name: str


class AuditLogOut(BaseModel):
    id: int
    username: str
    action: str
    target: str
    detail: str | None
    ip: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---- M17:webhook 端点管理(secret 明文仅 POST/rotate 响应一次) ----
class WebhookCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: HttpUrl
    events: list[str] = []
    description: str | None = Field(None, max_length=200)
    secret: str | None = Field(None, min_length=16, max_length=64)


class WebhookUpdateIn(BaseModel):
    """无 secret 字段:明文写回一律拒绝(rotate_secret 专属通道)。
    多余键按 pydantic 默认忽略——传 secret 静默丢弃而非 422。"""

    name: str | None = Field(None, min_length=1, max_length=100)
    url: HttpUrl | None = None
    events: list[str] | None = None
    enabled: bool | None = None
    description: str | None = None
    rotate_secret: bool = False


class WebhookOut(BaseModel):
    id: int
    name: str
    url: str
    events: list[str] | None
    enabled: bool
    description: str | None
    secret_masked: str
    created_at: datetime


class WebhookCreatedOut(WebhookOut):
    secret: str  # 明文仅此一次


class WebhookDeliveryOut(BaseModel):
    id: int
    endpoint_id: int
    endpoint_name: str
    event_type: str
    status: str
    attempts: int
    response_status: int | None
    last_error: str | None
    payload: dict | None
    next_attempt_at: datetime | None
    created_at: datetime
