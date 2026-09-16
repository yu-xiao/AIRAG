from pathlib import Path

from app.services.parsing.base import ParseResult, Parser, register


@register(".jpg")
@register(".jpeg")
@register(".png")
class ImageParser(Parser):
    """图片无本地文本层:占位解析,内容由 ocr.maybe_ocr 的 MinerU 路径产出。"""

    def parse(self, path: Path) -> ParseResult:
        return ParseResult()
