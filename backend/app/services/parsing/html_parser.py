from pathlib import Path

from bs4 import BeautifulSoup

from app.services.parsing.base import ParseResult, Parser, register
from app.services.parsing.ocr import markdown_to_blocks
from app.services.parsing.text_parser import _read_text


@register(".html")
@register(".htm")
class HtmlParser(Parser):
    """纯文本提取:去 script/style/noscript 后 get_text,空行分段;
    不抓外链资源、不执行脚本。"""

    def parse(self, path: Path) -> ParseResult:
        soup = BeautifulSoup(_read_text(path), "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return markdown_to_blocks(soup.get_text("\n"))
