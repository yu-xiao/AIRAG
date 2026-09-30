import csv
import io
import json
from pathlib import Path

from app.services.parsing.base import ParseResult, ParsedBlock, Parser, register
from app.services.parsing.ocr import markdown_to_blocks


def _read_text(path: Path) -> str:
    """utf-8(容 BOM)优先,回退 gb18030(国内存量 txt/csv 的 GBK 现实);
    两者皆败 → ValueError:流水线按损坏文件统一语义收口(3 次快速重试后 failed)。"""
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("gb18030")
        except UnicodeDecodeError as e:
            raise ValueError(f"cannot decode text file: {path.name}") from e
    # Windows 存量文本几乎全是 CRLF;markdown_to_blocks 按 \n\n 分段,
    # 不归一则整篇合成一块。归一仅在本(磁盘读取)边界做,OCR 路径不动。
    return text.replace("\r\n", "\n")


@register(".txt")
@register(".md")
class TextParser(Parser):
    """纯文本/Markdown 共用:复用 MinerU 输出路径的 markdown_to_blocks
    (空行分段 + |--- 表格标记识别),与 OCR 文档的块形状一致。"""

    def parse(self, path: Path) -> ParseResult:
        return markdown_to_blocks(_read_text(path))


@register(".csv")
class CsvParser(Parser):
    """整表一块(is_table),镜像 xlsx sheet 语义;表格块整体不切。"""

    def parse(self, path: Path) -> ParseResult:
        rows = csv.reader(io.StringIO(_read_text(path)))
        lines = [" | ".join(cells) for cells in rows if any(c.strip() for c in cells)]
        if not lines:
            return ParseResult()
        return ParseResult(blocks=[ParsedBlock(
            content="\n".join(lines), is_table=True)])


@register(".json")
class JsonParser(Parser):
    """顶层数组逐元素一块;对象整体美化一块。无效 JSON → ValueError。"""

    def parse(self, path: Path) -> ParseResult:
        try:
            data = json.loads(_read_text(path))
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid json: {e}") from e
        items = data if isinstance(data, list) else [data]
        blocks = [ParsedBlock(content=json.dumps(
            it, ensure_ascii=False, indent=2)) for it in items]
        return ParseResult(blocks=blocks)
