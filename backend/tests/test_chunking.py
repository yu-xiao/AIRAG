from app.services.chunking.splitter import split_blocks
from app.services.parsing.base import ParsedBlock


def test_short_blocks_pass_through():
    blocks = [ParsedBlock(content="短文本一", page_no=1), ParsedBlock(content="短文本二")]
    chunks = split_blocks(blocks, chunk_size=100, overlap=10)
    assert [c.content for c in chunks] == ["短文本一", "短文本二"]
    assert chunks[0].page_no == 1
    assert chunks[0].char_len == 4


def test_long_text_splits_with_overlap():
    body = "甲" * 600 + "\n" + "乙" * 600
    chunks = split_blocks([ParsedBlock(content=body, page_no=2)], chunk_size=500, overlap=100)
    assert len(chunks) >= 3
    assert all(c.char_len <= 500 + 100 for c in chunks)  # 允许 overlap 余量
    assert all(c.page_no == 2 for c in chunks)
    joined = "".join(c.content for c in chunks)
    assert "甲" in joined and "乙" in joined


def test_oversize_table_stays_whole():
    table = ParsedBlock(content="R | L\n" * 300, page_no=1, is_table=True)
    chunks = split_blocks([table], chunk_size=200, overlap=50)
    assert len(chunks) == 1
    assert chunks[0].char_len == len(table.content)


def test_empty_blocks_dropped():
    chunks = split_blocks([ParsedBlock(content="   "), ParsedBlock(content="有内容")])
    assert len(chunks) == 1
