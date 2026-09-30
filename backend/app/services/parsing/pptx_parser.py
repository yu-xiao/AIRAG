from pathlib import Path

from pptx import Presentation

from app.services.parsing.base import ParseResult, ParsedBlock, Parser, register


@register(".pptx")
class PptxParser(Parser):
    """逐 slide:文本形状每形状一块(阅读序),表格每表一块
    (行内 " | ",行间换行,与 xlsx/docx 一致),备注追加一块。"""

    def parse(self, path: Path) -> ParseResult:
        prs = Presentation(str(path))
        result = ParseResult(page_count=len(prs.slides))
        for idx, slide in enumerate(prs.slides, start=1):
            for shape in slide.shapes:
                if shape.has_table:
                    lines = [" | ".join(
                        c.text.strip() for c in row.cells)
                        for row in shape.table.rows]
                    lines = [ln for ln in lines if ln.strip(" |")]
                    if lines:
                        result.blocks.append(ParsedBlock(
                            content="\n".join(lines), page_no=idx,
                            is_table=True))
                elif shape.has_text_frame and shape.text_frame.text.strip():
                    result.blocks.append(ParsedBlock(
                        content=shape.text_frame.text, page_no=idx))
            if slide.has_notes_slide:
                note = slide.notes_slide.notes_text_frame.text.strip()
                if note:
                    result.blocks.append(ParsedBlock(content=note, page_no=idx))
        return result
