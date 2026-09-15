from pathlib import Path

import pymupdf as fitz

from app.services.parsing.base import ParseResult, ParsedBlock, Parser, register


@register(".pdf")
class PdfParser(Parser):
    def parse(self, path: Path) -> ParseResult:
        result = ParseResult()
        with fitz.open(str(path)) as doc:
            result.page_count = doc.page_count
            for i, page in enumerate(doc, start=1):
                text = page.get_text("text").strip()
                if text:
                    result.blocks.append(ParsedBlock(content=text, page_no=i))
        return result
