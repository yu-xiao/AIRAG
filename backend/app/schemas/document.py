from datetime import datetime

from pydantic import BaseModel


class DocumentOut(BaseModel):
    id: int
    kb_id: int
    filename: str
    mime: str
    size: int
    sha256: str
    status: str
    error_msg: str | None
    page_count: int | None
    chunk_count: int
    created_at: datetime

    model_config = {"from_attributes": True}
