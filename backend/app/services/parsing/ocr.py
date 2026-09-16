from pathlib import Path

from app.core.config import settings
from app.services.parsing.base import ParseResult, ParsedBlock
from app.services.parsing.mineru_client import parse_via_mineru

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
TABLE_MARKERS = ("|---", "---|")


def is_thin_text(primary: ParseResult) -> bool:
    pages = max(primary.page_count, 1)
    total = sum(len(b.content) for b in primary.blocks)
    return total / pages < settings.OCR_THIN_CHARS_PER_PAGE


def markdown_to_blocks(md_text: str) -> ParseResult:
    result = ParseResult()
    for para in md_text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        result.blocks.append(
            ParsedBlock(
                content=para,
                is_table=any(m in para for m in TABLE_MARKERS),
            )
        )
    return result


def maybe_ocr(
    path: Path, ext: str, ocr_mode: str | None, primary: ParseResult
) -> ParseResult:
    """按模式决定是否走 MinerU;不触发时原样返回 primary(同一对象)。"""
    mode = ocr_mode or "auto"
    if mode == "off" or not settings.MINERU_API_TOKEN:
        return primary
    ext = ext.lower()
    if not (ext == ".pdf" or ext in IMAGE_EXTS):
        return primary
    if mode == "force":
        use_ocr = True
    else:  # auto:图片无文本层必走;PDF 文本稀薄才走
        use_ocr = ext in IMAGE_EXTS or is_thin_text(primary)
    if not use_ocr:
        return primary
    # OCR 成功但无文字(纯图形图片等)也保持 ocr_used=True:由流水线按
    # 确定性失败直落 failed(NoContentError),不静默回退、不重试
    md_text = parse_via_mineru(path, path.name)
    return markdown_to_blocks(md_text)
