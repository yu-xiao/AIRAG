from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AdminKeyCreateIn(BaseModel):
    """M9.1:admin 代发密钥(user_id 为绑定目标;权限/配额按目标用户)。"""

    user_id: int
    name: str = Field(min_length=1, max_length=64)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    role: Literal["read_only", "editor"] = "read_only"


class AdminUserOut(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool

    model_config = {"from_attributes": True}


class AdminUserIn(BaseModel):
    role: Literal["viewer", "editor", "admin"] | None = None
    is_active: bool | None = None


class AuditLogOut(BaseModel):
    id: int
    username: str
    action: str
    target: str
    detail: str | None
    ip: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
