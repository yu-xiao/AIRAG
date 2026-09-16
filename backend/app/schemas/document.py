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
    # M5 OCR:上传时选择(auto/force/off);ocr_used 为实际是否走了 MinerU
    ocr_mode: str | None = "auto"
    ocr_used: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}
