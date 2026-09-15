from datetime import datetime

from pydantic import BaseModel, Field


class ConversationIn(BaseModel):
    kb_ids: list[int] = Field(min_length=1)
    name: str | None = Field(default=None, max_length=128)


class ConversationOut(BaseModel):
    id: int
    kb_ids: list[int]
    title: str
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    citations: list | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AskIn(BaseModel):
    conversation_id: int | None = None
    kb_ids: list[int] = Field(min_length=1)
    question: str = Field(min_length=1, max_length=2000)
