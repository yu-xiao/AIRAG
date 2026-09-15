from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class KBIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)


class KBOut(BaseModel):
    id: int
    name: str
    description: str | None
    owner_id: int
    embed_provider: str
    embed_model: str
    created_at: datetime
    my_perm: str | None = None

    model_config = {"from_attributes": True}


class MemberOut(BaseModel):
    user_id: int
    username: str
    perm: str


class GrantIn(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    perm: Literal["viewer", "editor"]
