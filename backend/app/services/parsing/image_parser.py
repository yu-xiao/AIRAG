from pathlib import Path

from app.services.parsing.base import ParseResult, Parser, register

# M24:OCR 图片门权威集合(ocr.maybe_ocr 消费)——双端漂移单源化
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff",
              ".webp", ".gif"}


@register(".jpg")
@register(".jpeg")
@register(".png")
@register(".bmp")
@register(".tif")
@register(".tiff")
@register(".webp")
@register(".gif")
class ImageParser(Parser):
    """图片无本地文本层:占位解析,内容由 ocr.maybe_ocr 的 MinerU 路径
    产出(新格式为云端探查性支持,拒收则流水线确定性 failed)。"""

    def parse(self, path: Path) -> ParseResult:
        return ParseResult()
