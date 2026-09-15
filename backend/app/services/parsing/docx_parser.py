from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.services.parsing.base import ParseResult, ParsedBlock, Parser, register


def _table_text(table: Table) -> str:
    lines = []
    for row in table.rows:
        cells = [cell.text.strip() for cell in row.cells]
        lines.append(" | ".join(cells))
    return "\n".join(lines)


@register(".docx")
class DocxParser(Parser):
    def parse(self, path: Path) -> ParseResult:
        result = ParseResult()
        document = Document(str(path))
        for item in document.iter_inner_content():
            if isinstance(item, Paragraph):
                text = item.text.strip()
                if text:
                    result.blocks.append(ParsedBlock(content=text))
            elif isinstance(item, Table):
                result.blocks.append(
                    ParsedBlock(content=_table_text(item), is_table=True)
                )
        return result
