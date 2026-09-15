from typing import Literal

from pydantic import BaseModel


class AdminUserOut(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool

    model_config = {"from_attributes": True}


class AdminUserIn(BaseModel):
    role: Literal["viewer", "editor", "admin"] | None = None
    is_active: bool | None = None
