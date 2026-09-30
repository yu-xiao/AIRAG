import io
from pathlib import Path

import pymupdf as fitz
import pytest
from docx import Document as Docx
from openpyxl import Workbook

from app.services.parsing.base import get_parser


def _make_pdf(tmp_path: Path) -> Path:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "AIRag pdf parser test page one")
    page2 = doc.new_page()
    page2.insert_text((72, 72), "page two content")
    p = tmp_path / "t.pdf"
    doc.save(str(p))
    doc.close()
    return p


def _make_docx(tmp_path: Path) -> Path:
    d = Docx()
    d.add_paragraph("第一段介绍")
    d.add_paragraph("")
    d.add_paragraph("第二段内容")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Name"
    table.cell(0, 1).text = "Score"
    table.cell(1, 0).text = "Alice"
    table.cell(1, 1).text = "90"
    p = tmp_path / "t.docx"
    d.save(str(p))
    return p


def _make_xlsx(tmp_path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["Name", "Score"])
    ws.append(["Alice", "90"])
    ws2 = wb.create_sheet("Sheet2")
    ws2.append(["City", "Code"])
    ws2.append(["BJ", "010"])
    p = tmp_path / "t.xlsx"
    wb.save(str(p))
    return p


def test_pdf_parses_pages(tmp_path):
    result = get_parser(".pdf").parse(_make_pdf(tmp_path))
    assert result.page_count == 2
    assert result.blocks[0].page_no == 1
    assert "page one" in result.blocks[0].content
    assert result.blocks[1].content == "page two content"


def test_docx_keeps_order_and_table(tmp_path):
    result = get_parser(".docx").parse(_make_docx(tmp_path))
    texts = [b.content for b in result.blocks]
    assert "第一段介绍" in texts
    tables = [b for b in result.blocks if b.is_table]
    assert len(tables) == 1
    assert "Name | Score" in tables[0].content
    assert "Alice | 90" in tables[0].content
    assert tables[0].page_no is None


def test_xlsx_sheet_as_whole_block(tmp_path):
    result = get_parser(".xlsx").parse(_make_xlsx(tmp_path))
    assert result.page_count == 2
    assert result.blocks[0].is_table is True
    assert "Name | Score" in result.blocks[0].content
    assert result.blocks[1].page_no == 2
    assert "BJ | 010" in result.blocks[1].content


def test_unknown_ext_raises():
    with pytest.raises(KeyError):
        get_parser(".xyz")


def test_txt_parses_paragraphs_and_gb18030(tmp_path):
    p = tmp_path / "t.txt"
    p.write_bytes("第一段内容\n\n第二段内容".encode("gb18030"))
    result = get_parser(".txt").parse(p)
    assert [b.content for b in result.blocks] == ["第一段内容", "第二段内容"]


def test_md_reuses_markdown_blocks(tmp_path):
    p = tmp_path / "t.md"
    p.write_text("# 标题\n\n正文一段\n\n| a | b |\n|---|---|\n| 1 | 2 |",
                 encoding="utf-8")
    result = get_parser(".md").parse(p)
    contents = [b.content for b in result.blocks]
    assert "# 标题" in contents
    tables = [b for b in result.blocks if b.is_table]
    assert len(tables) == 1 and "| 1 | 2 |" in tables[0].content


def test_csv_whole_file_one_table_block(tmp_path):
    p = tmp_path / "t.csv"
    p.write_bytes("名称,数量\n甲,1\n乙,2".encode("gb18030"))
    result = get_parser(".csv").parse(p)
    assert len(result.blocks) == 1
    assert result.blocks[0].is_table is True
    assert "名称 | 数量" in result.blocks[0].content
    assert "乙 | 2" in result.blocks[0].content


def test_json_list_per_element_block(tmp_path):
    import json as _json

    p = tmp_path / "t.json"
    p.write_text(_json.dumps(
        [{"name": "甲", "v": 1}, {"name": "乙", "v": 2}],
        ensure_ascii=False), encoding="utf-8")
    result = get_parser(".json").parse(p)
    assert len(result.blocks) == 2
    assert "甲" in result.blocks[0].content
    assert "乙" in result.blocks[1].content


def test_json_invalid_raises_value_error(tmp_path):
    import pytest as _pytest

    p = tmp_path / "bad.json"
    p.write_text("{not valid", encoding="utf-8")
    with _pytest.raises(ValueError):
        get_parser(".json").parse(p)


def test_allowed_exts_derived_from_registry():
    from app.services import doc_ops
    from app.services.parsing.base import REGISTRY

    assert doc_ops.ALLOWED_EXTS == frozenset(REGISTRY)
    for ext in (".txt", ".md", ".csv", ".json"):
        assert ext in doc_ops.ALLOWED_EXTS


def test_html_extracts_text_skips_script(tmp_path):
    p = tmp_path / "t.html"
    p.write_text(
        "<html><head><style>body{}</style><script>var x=1;</script></head>"
        "<body><h1>标题甲</h1><p>正文一段</p><p>正文二段</p></body></html>",
        encoding="utf-8")
    result = get_parser(".html").parse(p)
    text = "\n".join(b.content for b in result.blocks)
    assert "标题甲" in text and "正文二段" in text
    assert "var x" not in text and "body{}" not in text
    assert get_parser(".htm")


def _make_pptx(tmp_path: Path) -> Path:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[5])  # blank
    s1.shapes.title.text = "第一页标题"
    tb = s1.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
    tb.text_frame.text = "第一页要点"
    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    gf = s2.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(4), Inches(1))
    gf.table.cell(0, 0).text = "名称"
    gf.table.cell(0, 1).text = "数量"
    gf.table.cell(1, 0).text = "甲"
    gf.table.cell(1, 1).text = "7"
    p = tmp_path / "t.pptx"
    prs.save(str(p))
    return p


def test_pptx_slides_tables_notes(tmp_path):
    result = get_parser(".pptx").parse(_make_pptx(tmp_path))
    assert result.page_count == 2
    texts = [b.content for b in result.blocks if not b.is_table]
    assert any("第一页标题" in t for t in texts)
    assert any("第一页要点" in t for t in texts)
    tables = [b for b in result.blocks if b.is_table]
    assert len(tables) == 1 and tables[0].page_no == 2
    assert "名称 | 数量" in tables[0].content and "甲 | 7" in tables[0].content
