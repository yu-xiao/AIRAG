# AIRag M24 实施计划:文档类型扩容(轻量包 + PPTX + 图片格式)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 10 类文档解析(.txt/.md/.csv/.json/.html/.pptx/.bmp/.tif/.tiff/.webp/.gif,含别名共 18 扩展名),上传白名单单源化(REGISTRY 派生),前端 accept 扩容,端到端可检索。

**Architecture:** 全部走既有注册表模式(每类一个解析器模块 + @register);白名单从 REGISTRY 派生消两端漂移;.txt/.md 复用 ocr.markdown_to_blocks;.csv 镜像 xlsx 整表一块;图片格式双端白名单合一流;无转换层、无新架构。

**Tech Stack:** 纯 Python 解析器 + python-pptx + beautifulsoup4(新依赖,html.parser 后端不引 lxml)。

**Spec:** `docs/superpowers/specs/2026-09-30-airag-m24-doc-types-design.md`

## Global Constraints

- 测试一律 `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest …`(Windows CMD;`&&` 可用,禁 bash 语法);vitest 一律 `cd E:\Projects\AIRag\frontend && pnpm vitest run …`。
- 新依赖须同步 `backend/pyproject.toml`(dependencies 列表)并 `pip install` 进 venv。
- 表格块格式与 xlsx/docx 一致:行内 `" | ".join(单元格)`,行间换行,is_table=True;表格块整体不切(切块器既有语义)。
- 解析失败(解码失败/JSON 无效)抛 ValueError/RuntimeError → 流水线确定性 failed,不重试、不静默。
- 基线只增不减(pytest 502P / vitest 85 / build 零错)。
- 每任务一个 commit,conventional 消息照 brief。

---

### Task 1: 白名单单源化 + 文本族解析器(.txt/.md/.csv/.json)

**Files:**
- Create: `backend/app/services/parsing/text_parser.py`
- Modify: `backend/app/services/doc_ops.py:22`(ALLOWED_EXTS 派生)
- Modify: `backend/app/services/parsing/__init__.py`(导 text_parser)
- Test: `backend/tests/test_parsers.py`(新增 4 用例 + 反例换 + 单源断言)、`backend/tests/test_documents.py:19`(反例 .txt→.exe)

**Interfaces:**
- Consumes: `markdown_to_blocks`(app.services.parsing.ocr)、REGISTRY(parsing.base)。
- Produces: `text_parser` 内 `TxtParser(.txt)`/`MdParser(.md)`/`CsvParser(.csv)`/`JsonParser(.json)` + `_read_text(path)`(utf-8-sig → gb18030 回退);`doc_ops.ALLOWED_EXTS = frozenset(REGISTRY)`。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_parsers.py`:把 `test_unknown_ext_raises` 的 `get_parser(".txt")` 改为 `get_parser(".xyz")`,并追加:

```python
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
    assert '"甲"' in result.blocks[0].content or "甲" in result.blocks[0].content
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
```

`backend/tests/test_documents.py`:`test_upload_document` 的 `("hello.txt", …)` 改为 `("hello.exe", content, "application/octet-stream")`,注释改 `# .exe 不在白名单`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_parsers.py tests\test_documents.py -v`
Expected: 新解析器用例 FAIL(get_parser(".txt") KeyError、ALLOWED_EXTS 派生断言败);`test_upload_document`(反例已换 .exe)保持 PASS(.exe 本就被拒)。

- [ ] **Step 3: 实现**

新建 `backend/app/services/parsing/text_parser.py`:

```python
import csv
import io
import json
from pathlib import Path

from app.services.parsing.base import ParseResult, ParsedBlock, Parser, register
from app.services.parsing.ocr import markdown_to_blocks


def _read_text(path: Path) -> str:
    """utf-8(容 BOM)优先,回退 gb18030(国内存量 txt/csv 的 GBK 现实);
    两者皆败 → ValueError:流水线按确定性 failed 收口,不重试。"""
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030")
    except UnicodeDecodeError as e:
        raise ValueError(f"cannot decode text file: {path.name}") from e


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
```

`backend/app/services/parsing/__init__.py`:import 行补 `text_parser`。

`backend/app/services/doc_ops.py:22` 替换为:

```python
from app.services.parsing import REGISTRY

# M24 单源化:白名单即解析器注册表(包 __init__ 已导入全部解析器,
# REGISTRY 此时完整)——加类型只动解析器注册与前端 accept 两处
ALLOWED_EXTS = frozenset(REGISTRY)
```

(import 按文件既有分组放置;原字面量集合删除。)

- [ ] **Step 4: 跑测试**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_parsers.py tests\test_documents.py tests\test_doc_ops.py tests\test_agent_docs_api.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/parsing/text_parser.py backend/app/services/parsing/__init__.py backend/app/services/doc_ops.py backend/tests/test_parsers.py backend/tests/test_documents.py
git commit -m "feat(parsing): txt/md/csv/json parsers + allowlist derived from registry (m24)"
```

---

### Task 2: .html/.htm 解析器

**Files:**
- Create: `backend/app/services/parsing/html_parser.py`
- Modify: `backend/pyproject.toml`(dependencies + beautifulsoup4)
- Modify: `backend/app/services/parsing/__init__.py`(导 html_parser)
- Test: `backend/tests/test_parsers.py`(新增 1 用例)

**Interfaces:**
- Consumes: `_read_text`(text_parser)、`markdown_to_blocks`(ocr)。
- Produces: `HtmlParser` 注册 `.html` 与 `.htm`。

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_parsers.py -k html -v`
Expected: FAIL(KeyError: '.html')。

- [ ] **Step 3: 实现**

安装依赖并登记:

```bash
cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pip install "beautifulsoup4>=4.12"
```

`backend/pyproject.toml` dependencies 列表加 `"beautifulsoup4>=4.12",`(字母序,排在 asyncpg 之后 bc 前)。

新建 `backend/app/services/parsing/html_parser.py`:

```python
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
```

`parsing/__init__.py` 补 `html_parser`。

- [ ] **Step 4: 跑测试**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_parsers.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/parsing/html_parser.py backend/app/services/parsing/__init__.py backend/pyproject.toml backend/tests/test_parsers.py
git commit -m "feat(parsing): html/htm parser via beautifulsoup4 (m24)"
```

---

### Task 3: .pptx 解析器

**Files:**
- Create: `backend/app/services/parsing/pptx_parser.py`
- Modify: `backend/pyproject.toml`(+ python-pptx)
- Modify: `backend/app/services/parsing/__init__.py`(导 pptx_parser)
- Test: `backend/tests/test_parsers.py`(新增 1 用例,夹具用 python-pptx 生成)

**Interfaces:**
- Consumes: 无新(表格块格式对齐 xlsx/docx:行内 " | ",行间换行)。
- Produces: `PptxParser` 注册 `.pptx`;page_no=幻灯片序号,page_count=len(slides)。

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_parsers.py -k pptx -v`
Expected: FAIL(KeyError: '.pptx')。

- [ ] **Step 3: 实现**

```bash
cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pip install "python-pptx>=1.0"
```

`backend/pyproject.toml` dependencies 加 `"python-pptx>=1.0",`(排在 python-multipart 前)。

新建 `backend/app/services/parsing/pptx_parser.py`:

```python
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
```

`parsing/__init__.py` 补 `pptx_parser`。

- [ ] **Step 4: 跑测试**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_parsers.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/parsing/pptx_parser.py backend/app/services/parsing/__init__.py backend/pyproject.toml backend/tests/test_parsers.py
git commit -m "feat(parsing): pptx parser — slides, tables, notes (m24)"
```

---

### Task 4: 图片格式扩容(.bmp/.tif/.tiff/.webp/.gif)+ IMAGE_EXTS 单源化

**Files:**
- Modify: `backend/app/services/parsing/image_parser.py`(注册 + 常量权威定义)
- Modify: `backend/app/services/parsing/ocr.py:7`(IMAGE_EXTS 改 import)
- Test: `backend/tests/test_ocr.py`(新增 1 用例;`test_parsers.py` 补注册断言)

**Interfaces:**
- Consumes: 既有 `maybe_ocr` 门(ext ∈ IMAGE_EXTS 才考虑 OCR)。
- Produces: `IMAGE_EXTS` 权威定义移至 image_parser.py(ocr.py import),含 9 个扩展(jpg/jpeg/png/bmp/tif/tiff/webp/gif)。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_parsers.py` 追加:

```python
def test_image_exts_registered():
    from app.services import doc_ops
    from app.services.parsing.ocr import IMAGE_EXTS
    from app.services.parsing.base import get_parser

    for ext in (".bmp", ".tif", ".tiff", ".webp", ".gif"):
        assert ext in IMAGE_EXTS
        assert get_parser(ext)
    assert set(IMAGE_EXTS) <= doc_ops.ALLOWED_EXTS
```

`backend/tests/test_ocr.py` 追加(maybe_ocr 门,不真调 MinerU——照该文件既有 monkeypatch 模式):

```python
async def test_maybe_ocr_routes_new_image_formats(tmp_path, monkeypatch):
    """M24:bmp/webp 等新图片格式进 OCR 门(auto 即走 MinerU),
    .txt 不进。"""
    from app.services import parsing  # noqa: F401 — 触发注册
    from app.services.parsing import ocr
    from app.services.parsing.base import ParseResult

    called = []

    async def fake_mineru(path, name):
        called.append(name)
        return "ocr 结果"

    monkeypatch.setattr(ocr, "parse_via_mineru", fake_mineru)
    p = tmp_path / "x.bmp"
    p.write_bytes(b"bm")
    out = ocr.maybe_ocr(p, ".bmp", "auto", ParseResult())
    assert called == ["x.bmp"]
    assert out.blocks[0].content == "ocr 结果"
```

(若该文件已有更贴近的既有 helper/模式,以文件内风格为准调整。)

- [ ] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_parsers.py tests\test_ocr.py -k "image_exts or new_image" -v`
Expected: FAIL(.bmp 不在 IMAGE_EXTS/REGISTRY)。

- [ ] **Step 3: 实现**

`backend/app/services/parsing/image_parser.py` 整体替换为:

```python
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
```

`backend/app/services/parsing/ocr.py`:删除 `IMAGE_EXTS = {".jpg", ".jpeg", ".png"}` 一行,顶部改加 `from app.services.parsing.image_parser import IMAGE_EXTS`(供本模块与既有引用继续可用)。

- [ ] **Step 4: 跑测试**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_ocr.py tests\test_parsers.py tests\test_pipeline.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/parsing/image_parser.py backend/app/services/parsing/ocr.py backend/tests/test_parsers.py backend/tests/test_ocr.py
git commit -m "feat(parsing): bmp/tif/tiff/webp/gif image support, single-sourced ocr gate (m24)"
```

---

### Task 5: 前端 accept 扩容 + 端到端用例

**Files:**
- Modify: `frontend/src/pages/DocsPage.vue:266`(accept 串)
- Test: `backend/tests/test_pipeline.py`(新增 1 端到端用例)、`frontend/src/pages/__tests__/` (若有 DocsPage spec 则加 accept 断言;无则不建)

**Interfaces:**
- Consumes: T1~T4 全部解析器。
- Produces: 前端 18 扩展名 accept;端到端证明(GBK txt/pptx/csv 上传→done→可检索)。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_pipeline.py` 追加(文件头按需补 `select`/`Document` import):

```python
async def test_new_types_end_to_end(client, auth_headers, db_session):
    """M24:GBK txt / csv / pptx 上传 → eager 流水线 done → 检索命中
    (fake embed;MINERU_TOKEN 空置不影响非图片路径)。"""
    import io

    from pptx import Presentation

    from app.models import Document
    from app.services.retrieval.searcher import hybrid_search
    from sqlalchemy import select

    kb_id = (await client.post(
        "/api/kbs", json={"name": "m24端到端库"}, headers=auth_headers)
    ).json()["id"]

    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[5])
    s1.shapes.title.text = "燃油泵检修规程"
    buf = io.BytesIO()
    prs.save(buf)

    files = [
        ("gb.txt", "机型甲的排故要点是先查燃油泵".encode("gb18030"),
         "text/plain"),
        ("t.csv", "部件,数量\n燃油泵,3".encode("utf-8"), "text/csv"),
        ("s.pptx", buf.getvalue(), "application/octet-stream"),
    ]
    for name, content, mime in files:
        r = await client.post(
            f"/api/kbs/{kb_id}/documents",
            files={"file": (name, content, mime)}, headers=auth_headers)
        assert r.status_code == 201, (name, r.text[:120])

    docs = (await db_session.execute(
        select(Document).where(Document.kb_id == kb_id))).scalars().all()
    assert len(docs) == 3
    assert all(d.status == "done" for d in docs), \
        [d.filename for d in docs if d.status != "done"]
    hits = await hybrid_search(db_session, [kb_id], "燃油泵 排故", 8)
    assert {h.document_id for h in hits} & {d.id for d in docs}
```

(向量和关键词任一命中即可;gb.txt 解码后含「燃油泵」、csv 表块含
「燃油泵」、pptx 标题含「燃油泵」,jieba 关键词路必中。)

`frontend/src/pages/DocsPage.vue` accept 行改为:

```html
        accept=".pdf,.docx,.xlsx,.txt,.md,.csv,.json,.html,.htm,.pptx,.jpg,.jpeg,.png,.bmp,.tif,.tiff,.webp,.gif"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_pipeline.py -k new_types -v`
Expected: FAIL(上传 415)。

- [ ] **Step 3: 实现**

后端无代码改动(T1~T4 已就位)——跑通端到端用例即绿;前端 accept 替换
如上。若 `frontend/src/pages/__tests__/DocsPage.spec.ts` 存在则加一条 accept
属性断言(`wrapper.find('input[type=file]').attributes('accept')` 含
`.pptx` 与 `.bmp`),不存在则不新建文件。

- [ ] **Step 4: 跑测试**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_pipeline.py -v` 及 `cd E:\Projects\AIRag\frontend && pnpm vitest run`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_pipeline.py frontend/src/pages/DocsPage.vue
git commit -m "feat(docs): widen upload accept to 18 extensions; new-types end-to-end (m24)"
```

---

## 收尾(控制端执行,不派子代理)

1. **全量门禁**:后端 pytest -q(≥509P 预期:502+T1 6+T2 1+T3 1+T4 2+T5 1 净变化后);前端 vitest(≥85)+ build 零错。
2. **真栈验收** `backend/scripts/m24_acceptance.py`(照 m23 模式):①上传 GBK .txt/.pptx/.csv → done + 可检索;②上传一张 .bmp(auto OCR)→ done 且 ocr_used=True(真 MinerU token,先例 M5);③前端 accept 串含 18 扩展(grep DocsPage.vue);④ALLOWED_EXTS 即 REGISTRY(运行时断言)。
3. **终审 whole-branch**(12037aa 起)+ M25 候选回流(老 Office/WPS 转换层若用户后续需要)。
4. **执行记录** + 记忆更新(推送等走查,既定节奏)。
