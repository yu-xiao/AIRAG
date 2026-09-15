# AIRag M2 文档流水线实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现文档处理流水线:知识库/文档 API(multipart 上传 + sha256 去重)→ Parser 插件(pdf/docx/xlsx)→ 切块 → 智谱 embedding(可切 fake)→ Celery 异步流水线(pending→parsing→chunking→embedding→done/failed,重试 3 次退避),验收"三种格式上传后状态流转到 done,chunks 可查"。

**Architecture:** 复用 M1 全部基座(FastAPI/SQLAlchemy async/七表模型/conftest 夹具)。新增:Celery+Redis(本机已有 Redis 8.10.1 服务)流水线,worker 进程独立启动(`--pool=solo` Windows 兼容);Parser/Embedding 均为插件接口,embedding 工厂按 `EMBED_PROVIDER`(zhipu|fake)选择——**fake 让 M2 全流程不依赖 API key**,智谱 live 测试有 key 才激活。

**Tech Stack:** 新增依赖 celery[redis] / pymupdf / python-docx / openpyxl / python-multipart / openai(智谱 OpenAI 兼容端点)。

**Spec:** `docs/AIRag-AI知识库需求设计方案.md` §7(流水线)、§9(M2 行);前置计划 `2026-09-14-airag-implementation-plan.md`(M1,已完成合并)。

## Global Constraints(承 M1,新增 M2 专属)

- 全部 M1 约束继续有效:CMD 兼容、`py -3.12`、conventional commits、TDD 红绿、显式路径暂存(工作区常有未提交文档改动)、**提交前 DLP 检查 `git diff --cached | findstr TSD-Header`**(alembic/生成类文件必查)。
- **端口**:后端 8001 / 前端 5173 / PG 5432 / **Redis 6379(本机已有服务,勿改配置勿动进程)**。
- **嵌入维度锁定 1024**(embedding-3 传 dimensions=1024;chunks.embedding 列即 1024)。
- **embedding-3 支持批量**;批大小 16;任务内重试 3 次指数退避(2s/4s/8s),最终失败落 `failed`+`error_msg`。
- 上传限制:仅 `.pdf/.docx/.xlsx`,≤20MB;同 KB 内 sha256 重复 → 409。
- **测试进程全局约定**:conftest 顶部强制 `DATABASE_URL`=airag_test、`EMBED_PROVIDER`=fake(在导入 app 模块之前);Celery 测试用 eager 模式。
- 文件存储:后端目录下 `storage/uploads/{kb_id}/{uuid12}_{原文件名}`;`storage/` 入 .gitignore。
- 前端不动(M3 才做页面);M2 全部验收在 API 层。
- 已知坑(前向携带):关系查询必须 selectinload(lazy=select + AsyncSession 会 MissingGreenlet)——本计划所有 KB 查询不触关系,不适用但 keep;alembic env.py 本计划补 `compare_server_default=True`(M1 遗留 Minor)。

## 环境事实(已核验,勿重复检查)

- Redis 8.10.1 Windows 服务运行中(PID 见 tasklist,`D:\Program Files\Redis-8.10.1-Windows-x64-ms2-with-Service\`),`redis://localhost:6379/0` 直连可用。
- `.env` 的 `ZHIPU_API_KEY=` 目前为空——不阻塞(EMBED_PROVIDER=fake 跑通全流程),**用户填 key 后** fake→zhipu 一行切换;Task 5 的 live 测试自动激活。
- psql 路径 `D:\Program Files\PostgreSQL\18\bin\psql`;airag_test 会由 conftest 全量重建。

---

### Task 0: M2 准备(依赖/配置/约定)

**Files:**
- Modify: `backend/pyproject.toml`, `backend/app/core/config.py`, `backend/tests/conftest.py`(顶部), `backend/alembic/env.py`, `backend/alembic.ini`, `.env.example`, `.gitignore`

**Interfaces:**
- Produces: settings 新字段 `ZHIPU_API_KEY/ZHIPU_BASE_URL/EMBED_PROVIDER/EMBED_MODEL/EMBED_DIMS/UPLOAD_DIR/MAX_UPLOAD_MB`;测试进程强制 test 库+fake 嵌入的全局约定

- [x] **Step 1: pyproject 增依赖**

`dependencies` 列表追加(保持字母序不必强求,追加到列表尾部即可):

```toml
    "celery[redis]>=5.4",
    "pymupdf>=1.24",
    "python-docx>=1.1",
    "openpyxl>=3.1",
    "python-multipart>=0.0.9",
    "openai>=1.40",
```

- [x] **Step 2: config.py 增字段**

`Settings` 类内、`JWT_EXPIRE_MINUTES` 之后追加:

```python
    ZHIPU_API_KEY: str = ""
    ZHIPU_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4"
    EMBED_PROVIDER: str = "zhipu"  # zhipu|fake
    EMBED_MODEL: str = "embedding-3"
    EMBED_DIMS: int = 1024
    UPLOAD_DIR: str = "storage/uploads"  # 相对 backend 运行目录
    MAX_UPLOAD_MB: int = 20
```

- [x] **Step 3: conftest 顶部全局约定(必须在一切 app 导入之前)**

`backend/tests/conftest.py` 文件**最顶部**(所有 import 之前)插入:

```python
import os

# M2 全局测试约定:测试进程一律指向测试库 + fake 嵌入(必须在导入 app.* 之前设置)
os.environ["DATABASE_URL"] = (
    "postgresql+asyncpg://airag:airag_dev_password@localhost:5432/airag_test"
)
os.environ["EMBED_PROVIDER"] = "fake"
```

- [x] **Step 4: env.py 补 compare_server_default(M1 遗留 Minor)+ alembic.ini 注释**

`alembic/env.py` 两处 `context.configure(...)`(offline 与 online)的参数中都加 `compare_server_default=True,`。`alembic.ini` 中 `sqlalchemy.url = driver://...` 行尾追加注释 `; overridden in env.py`。

- [x] **Step 5: .env.example 与 .gitignore**

`.env.example` 追加:

```env
ZHIPU_API_KEY=
ZHIPU_BASE_URL=https://open.bigmodel.cn/api/paas/v4
EMBED_PROVIDER=zhipu
EMBED_MODEL=embedding-3
EMBED_DIMS=1024
UPLOAD_DIR=storage/uploads
MAX_UPLOAD_MB=20
```

`.gitignore` 追加一行 `storage/`。

- [x] **Step 6: 安装依赖并验证**

```cmd
cd /d E:\Projects\AIRag\backend
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -c "import celery, fitz, docx, openpyxl, openai, multipart; print('imports ok')"
.venv\Scripts\python -m pytest -v
```

Expected: imports ok;pytest **11 passed**(既有基线不受影响——conftest 新 env 行为对旧测试透明)。

- [x] **Step 7: 提交**

```cmd
git add backend\pyproject.toml backend\app\core\config.py backend\tests\conftest.py backend\alembic\env.py backend\alembic.ini .env.example .gitignore
git commit -m "chore: m2 deps and settings (celery/parsers/embedding) with test conventions"
```

---

### Task 1: 知识库 API(create/list/get)

**Files:**
- Create: `backend/app/schemas/kb.py`, `backend/app/api/kbs.py`, `backend/tests/test_kbs.py`;Modify: `backend/app/api/__init__.py`, `backend/tests/conftest.py`(追加 auth 夹具)

**Interfaces:**
- Produces: `POST /api/kbs` 201→KBOut;`GET /api/kbs` →list[KBOut](id 倒序);`GET /api/kbs/{kb_id}` 200/404;全部要求 Bearer token(Depends get_current_user);`KBOut{id,name,description,owner_id,embed_provider,embed_model,created_at}`。M2 权限从简:登录即可操作所有 KB(M4 收紧)
- Consumes: M1 的 `KnowledgeBase` 模型与 `get_current_user`

- [x] **Step 1: conftest 追加共享认证夹具**

`conftest.py` 夹具区追加:

```python
import uuid as _uuid


@pytest_asyncio.fixture
async def auth_headers(client):
    username = f"m2_{_uuid.uuid4().hex[:8]}"
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
```

(import 区按需合并 `import uuid`;不要与现有内容重复。)

- [x] **Step 2: 写失败测试 `tests/test_kbs.py`**

```python
async def test_create_kb(client, auth_headers):
    resp = await client.post(
        "/api/kbs",
        json={"name": "产品手册库", "description": "内部产品文档"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] > 0
    assert data["name"] == "产品手册库"
    assert data["embed_provider"] == "zhipu"
    assert data["embed_model"] == "embedding-3"


async def test_create_kb_requires_auth(client):
    resp = await client.post("/api/kbs", json={"name": "匿名库"})
    assert resp.status_code == 401


async def test_list_and_get_kb(client, auth_headers):
    await client.post(
        "/api/kbs", json={"name": "法务库"}, headers=auth_headers
    )
    listed = await client.get("/api/kbs", headers=auth_headers)
    assert listed.status_code == 200
    names = [item["name"] for item in listed.json()]
    assert "法务库" in names

    kb_id = listed.json()[0]["id"]
    got = await client.get(f"/api/kbs/{kb_id}", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["id"] == kb_id

    missing = await client.get("/api/kbs/999999", headers=auth_headers)
    assert missing.status_code == 404
```

- [x] **Step 3: 运行确认失败**

```cmd
cd /d E:\Projects\AIRag\backend
.venv\Scripts\python -m pytest tests\test_kbs.py -v
```

Expected: FAIL(404,路由不存在;requires_auth 用例会 PASS——它断言的就是 401/404 之一,若因 404 通过也在本任务转绿,不视作作弊,但报告需注明)。

- [x] **Step 4: 实现**

`app/schemas/kb.py`:

```python
from datetime import datetime

from pydantic import BaseModel, Field


class KBIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)


class KBOut(BaseModel):
    id: int
    name: str
    description: str | None
    owner_id: int
    embed_provider: str
    embed_model: str
    created_at: datetime

    model_config = {"from_attributes": True}
```

`app/api/kbs.py`:

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models import KnowledgeBase, User
from app.schemas.kb import KBIn, KBOut

router = APIRouter(prefix="/kbs", tags=["kbs"])


@router.post("", response_model=KBOut, status_code=201)
async def create_kb(
    payload: KBIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = KnowledgeBase(
        name=payload.name, description=payload.description, owner_id=current.id
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


@router.get("", response_model=list[KBOut])
async def list_kbs(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(KnowledgeBase).order_by(KnowledgeBase.id.desc()))
    return list(result.scalars().all())


@router.get("/{kb_id}", response_model=KBOut)
async def get_kb(
    kb_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return kb
```

`app/api/__init__.py` 追加挂载(health/auth 不动):

```python
from app.api.kbs import router as kbs_router

api_router.include_router(kbs_router)
```

- [x] **Step 5: 运行确认通过并全量回归**

```cmd
.venv\Scripts\python -m pytest -v
```

Expected: **14 passed**(基线 11 + kbs 3;3 个测试函数覆盖 4 个场景,计数按函数算——2026-09-15 修正,原稿误写 15)。

- [x] **Step 6: 提交**

```cmd
git add backend\app\schemas\kb.py backend\app\api\kbs.py backend\app\api\__init__.py backend\tests\test_kbs.py backend\tests\conftest.py
git commit -m "feat: knowledge base crud-lite api with auth"
```

---

### Task 2: 文档上传 API(multipart+去重+落盘)

**Files:**
- Create: `backend/app/schemas/document.py`, `backend/app/api/documents.py`, `backend/tests/test_documents.py`;Modify: `backend/app/api/__init__.py`, `backend/tests/conftest.py`(上传目录夹具)

**Interfaces:**
- Produces: `POST /api/kbs/{kb_id}/documents`(multipart 字段名 `file`)201→DocumentOut;错误码:404 KB 不存在 / 415 扩展名 / 413 超限 / 409 sha256 重复;`GET /api/kbs/{kb_id}/documents` 列表;`GET /api/documents/{doc_id}` 详情。`DocumentOut{id,kb_id,filename,mime,size,sha256,status,error_msg,page_count,chunk_count,created_at}`。**本任务不派发流水线**(Task 6 接线 `.delay`)
- Consumes: Task 1 的 kbs 夹具与 auth_headers;settings.UPLOAD_DIR/MAX_UPLOAD_MB

- [x] **Step 1: conftest 追加上传目录隔离夹具**

```python
from pathlib import Path as _Path


@pytest_asyncio.fixture(autouse=True)
async def isolated_upload_dir(tmp_path):
    old = settings.UPLOAD_DIR
    settings.UPLOAD_DIR = str(tmp_path / "uploads")
    _Path(settings.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    yield
    settings.UPLOAD_DIR = old
```

- [x] **Step 2: 写失败测试 `tests/test_documents.py`**

```python
import hashlib


async def _make_kb(client, auth_headers) -> int:
    resp = await client.post(
        "/api/kbs", json={"name": "上传测试库"}, headers=auth_headers
    )
    return resp.json()["id"]


async def test_upload_document(client, auth_headers):
    kb_id = await _make_kb(client, auth_headers)
    content = "AIRag upload test content".encode()
    resp = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("hello.txt", content, "text/plain")},
        headers=auth_headers,
    )
    assert resp.status_code == 415  # .txt 不在白名单


async def test_upload_docx_ok_and_duplicate(client, auth_headers):
    kb_id = await _make_kb(client, auth_headers)
    import io

    from docx import Document as Docx

    buf = io.BytesIO()
    d = Docx()
    d.add_paragraph("上传验收段落")
    d.save(buf)
    payload = buf.getvalue()

    resp = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("测试.docx", payload, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "pending"
    assert data["sha256"] == hashlib.sha256(payload).hexdigest()
    assert data["filename"].endswith("测试.docx")

    dup = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("again.docx", payload, "application/octet-stream")},
        headers=auth_headers,
    )
    assert dup.status_code == 409


async def test_upload_to_missing_kb(client, auth_headers):
    resp = await client.post(
        "/api/kbs/999999/documents",
        files={"file": ("a.pdf", b"x", "application/pdf")},
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_get_document_detail(client, auth_headers):
    kb_id = await _make_kb(client, auth_headers)
    up = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("b.docx", b"dummy", "application/octet-stream")},
        headers=auth_headers,
    )
    doc_id = up.json()["id"]
    got = await client.get(f"/api/documents/{doc_id}", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["id"] == doc_id
    missing = await client.get("/api/documents/999999", headers=auth_headers)
    assert missing.status_code == 404
```

注:`b.docx` 内容是哑字节,只测 API 层(解析在流水线);`hello.txt` 测 415。

- [x] **Step 3: 运行确认失败**

```cmd
.venv\Scripts\python -m pytest tests\test_documents.py -v
```

Expected: FAIL(404)。

- [x] **Step 4: 实现**

`app/schemas/document.py`:

```python
from datetime import datetime

from pydantic import BaseModel


class DocumentOut(BaseModel):
    id: int
    kb_id: int
    filename: str
    mime: str
    size: int
    sha256: str
    status: str
    error_msg: str | None
    page_count: int | None
    chunk_count: int
    created_at: datetime

    model_config = {"from_attributes": True}
```

`app/api/documents.py`:

```python
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user
from app.db.session import get_db
from app.models import Document, KnowledgeBase, User
from app.schemas.document import DocumentOut

router = APIRouter(tags=["documents"])

ALLOWED_EXTS = {".pdf", ".docx", ".xlsx"}


async def _get_kb_or_404(db: AsyncSession, kb_id: int) -> KnowledgeBase:
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return kb


@router.post(
    "/kbs/{kb_id}/documents",
    response_model=DocumentOut,
    status_code=201,
)
async def upload_document(
    kb_id: int,
    file: UploadFile,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_kb_or_404(db, kb_id)

    original = Path(file.filename or "unnamed").name
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=415, detail=f"unsupported file type: {ext}")

    payload = await file.read()
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    if len(payload) > max_bytes:
        raise HTTPException(status_code=413, detail="file too large")

    import hashlib

    sha256 = hashlib.sha256(payload).hexdigest()
    dup = await db.execute(
        select(Document).where(
            Document.kb_id == kb_id, Document.sha256 == sha256
        )
    )
    if dup.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="duplicate document in this kb")

    doc_dir = Path(settings.UPLOAD_DIR) / str(kb_id)
    doc_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex[:12]}_{original}"
    file_path = doc_dir / stored_name
    file_path.write_bytes(payload)

    doc = Document(
        kb_id=kb_id,
        filename=original,
        file_path=str(file_path),
        mime=file.content_type or "application/octet-stream",
        size=len(payload),
        sha256=sha256,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


@router.get("/kbs/{kb_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    kb_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_kb_or_404(db, kb_id)
    result = await db.execute(
        select(Document)
        .where(Document.kb_id == kb_id)
        .order_by(Document.id.desc())
    )
    return list(result.scalars().all())


@router.get("/documents/{doc_id}", response_model=DocumentOut)
async def get_document(
    doc_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    return doc
```

`app/api/__init__.py` 追加:

```python
from app.api.documents import router as documents_router

api_router.include_router(documents_router)
```

- [x] **Step 5: 运行确认通过并全量回归**

```cmd
.venv\Scripts\python -m pytest -v
```

Expected: **18 passed**(14 + documents 4)。⚠️ 注意:requires_auth 类用例若此前靠 404 通过,现在挂 /kbs/{kb_id}/documents 后 401——语义未变。

- [x] **Step 6: 提交**

```cmd
git add backend\app\schemas\document.py backend\app\api\documents.py backend\app\api\__init__.py backend\tests\test_documents.py backend\tests\conftest.py
git commit -m "feat: document upload api with type/size/duplicate guards"
```

---

### Task 3: Parser 插件(pdf/docx/xlsx)

**Files:**
- Create: `backend/app/services/parsing/__init__.py`(空), `backend/app/services/parsing/base.py`, `backend/app/services/parsing/pdf_parser.py`, `backend/app/services/parsing/docx_parser.py`, `backend/app/services/parsing/xlsx_parser.py`, `backend/tests/test_parsers.py`

**Interfaces:**
- Produces: `ParsedBlock(content: str, page_no: int | None, is_table: bool=False)`、`ParseResult(blocks: list[ParsedBlock], page_count: int)`、`Parser.parse(path: Path) -> ParseResult`(抽象)、`get_parser(ext: str) -> Parser`(按扩展名注册表,未知扩展 KeyError)。语义:PDF 每页一块(page_no=1 起,空页保留空串跳过但计数);DOCX 按文档顺序段落+表格(iter_inner_content),表格→`a | b` 管道行文本,is_table=True,page_no=None;XLSX 每 sheet 一块(整块不切,管道行文本),page_no=sheet 序号(1 起)
- Consumes: 无(纯函数层)

- [x] **Step 1: 写失败测试 `tests/test_parsers.py`**

```python
import io
from pathlib import Path

import fitz
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
        get_parser(".txt")
```

- [x] **Step 2: 运行确认失败**

```cmd
.venv\Scripts\python -m pytest tests\test_parsers.py -v
```

Expected: FAIL/ERROR(模块不存在)。

- [x] **Step 3: 实现**

`app/services/parsing/base.py`:

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParsedBlock:
    content: str
    page_no: int | None = None
    is_table: bool = False


@dataclass
class ParseResult:
    blocks: list[ParsedBlock] = field(default_factory=list)
    page_count: int = 0


class Parser(ABC):
    @abstractmethod
    def parse(self, path: Path) -> ParseResult:
        ...


REGISTRY: dict[str, type[Parser]] = {}


def register(ext: str):
    def deco(cls: type[Parser]) -> type[Parser]:
        REGISTRY[ext] = cls
        return cls

    return deco


def get_parser(ext: str) -> Parser:
    return REGISTRY[ext.lower()]()
```

(注册表用装饰器;三个解析器模块 import 后即完成注册。)

`app/services/parsing/pdf_parser.py`:

```python
from pathlib import Path

import fitz

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
```

`app/services/parsing/docx_parser.py`:

```python
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
```

`app/services/parsing/xlsx_parser.py`:

```python
from pathlib import Path

from openpyxl import load_workbook

from app.services.parsing.base import ParseResult, ParsedBlock, Parser, register


@register(".xlsx")
class XlsxParser(Parser):
    def parse(self, path: Path) -> ParseResult:
        result = ParseResult()
        wb = load_workbook(str(path), data_only=True, read_only=True)
        try:
            for idx, ws in enumerate(wb.worksheets, start=1):
                lines = []
                for row in ws.iter_rows(values_only=True):
                    if row is None:
                        continue
                    cells = ["" if v is None else str(v) for v in row]
                    if any(c.strip() for c in cells):
                        lines.append(" | ".join(cells))
                if lines:
                    result.blocks.append(
                        ParsedBlock(content="\n".join(lines), page_no=idx, is_table=True)
                    )
                result.page_count = idx
        finally:
            wb.close()
        return result
```

`app/services/parsing/__init__.py`(确保注册生效,一行都不能少):

```python
from app.services.parsing import pdf_parser, docx_parser, xlsx_parser  # noqa: F401
from app.services.parsing.base import ParseResult, ParsedBlock, Parser, get_parser

__all__ = ["ParseResult", "ParsedBlock", "Parser", "get_parser"]
```

- [x] **Step 4: 运行确认通过并全量回归**

```cmd
.venv\Scripts\python -m pytest -v
```

Expected: **22 passed**(18 + parsers 4)。

- [x] **Step 5: 提交**

```cmd
git add backend\app\services\parsing backend\tests\test_parsers.py
git commit -m "feat: pluggable parsers for pdf/docx/xlsx with table-aware blocks"
```

---

### Task 4: 切块器(递归字符切块,表格整块)

**Files:**
- Create: `backend/app/services/chunking/__init__.py`(空), `backend/app/services/chunking/splitter.py`, `backend/tests/test_chunking.py`

**Interfaces:**
- Produces: `Chunk(content: str, page_no: int | None, char_len: int)`;`split_blocks(blocks: list[ParsedBlock], chunk_size: int = 1000, overlap: int = 150) -> list[Chunk]`。规则:is_table 块永不切分(哪怕超长);普通块 ≤chunk_size 原样成块;超长块按分隔符 `["\n\n", "\n", "。", "!", "?", "；", " ", ""]` 递归下切,相邻片之间保留 overlap 字符重叠;每块 char_len=len(content)。空 content 块丢弃
- Consumes: Task 3 的 ParsedBlock

- [x] **Step 1: 写失败测试 `tests/test_chunking.py`**

```python
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
```

- [x] **Step 2: 运行确认失败**

```cmd
.venv\Scripts\python -m pytest tests\test_chunking.py -v
```

Expected: FAIL/ERROR。

- [x] **Step 3: 实现 `app/services/chunking/splitter.py`**

```python
from dataclasses import dataclass

from app.services.parsing.base import ParsedBlock

SEPARATORS = ["\n\n", "\n", "。", "!", "?", "；", " ", ""]


@dataclass
class Chunk:
    content: str
    page_no: int | None
    char_len: int


def _hard_pieces(text: str, size: int, overlap: int) -> list[str]:
    step = max(size - overlap, 1)
    return [text[i : i + size] for i in range(0, len(text), step)]


def _split_text(text: str, size: int, overlap: int, sep_index: int = 0) -> list[str]:
    if len(text) <= size:
        return [text]
    if sep_index >= len(SEPARATORS):
        return _hard_pieces(text, size, overlap)
    sep = SEPARATORS[sep_index]
    if sep:
        parts = [p for p in text.split(sep) if p]
    else:
        return _hard_pieces(text, size, overlap)
    pieces: list[str] = []
    current = ""
    for part in parts:
        candidate = f"{current}{sep}{part}" if current else part
        if len(candidate) <= size:
            current = candidate
        else:
            if current:
                pieces.append(current)
            if len(part) <= size:
                current = part
            else:
                pieces.extend(_split_text(part, size, overlap, sep_index + 1))
                current = ""
    if current:
        pieces.append(current)

    if overlap <= 0 or len(pieces) <= 1:
        return pieces
    merged = [pieces[0]]
    for prev, nxt in zip(pieces, pieces[1:]):
        merged.append(f"{prev[-overlap:]}{nxt}" if len(prev) > overlap else nxt)
    return merged


def split_blocks(
    blocks: list[ParsedBlock], chunk_size: int = 1000, overlap: int = 150
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for block in blocks:
        content = block.content.strip()
        if not content:
            continue
        if block.is_table or len(content) <= chunk_size:
            pieces = [content]
        else:
            pieces = _split_text(content, chunk_size, overlap)
        for piece in pieces:
            piece = piece.strip()
            if piece:
                chunks.append(
                    Chunk(content=piece, page_no=block.page_no, char_len=len(piece))
                )
    return chunks
```

`app/services/chunking/__init__.py`:

```python
from app.services.chunking.splitter import Chunk, split_blocks

__all__ = ["Chunk", "split_blocks"]
```

- [x] **Step 4: 运行确认通过并全量回归**

```cmd
.venv\Scripts\python -m pytest -v
```

Expected: **26 passed**(22 + chunking 4)。

- [x] **Step 5: 提交**

```cmd
git add backend\app\services\chunking backend\tests\test_chunking.py
git commit -m "feat: recursive char chunker with whole-table preservation"
```

---

### Task 5: Embedding Provider(接口 + Fake + 智谱)

**Files:**
- Create: `backend/app/services/embedding/__init__.py`, `backend/app/services/embedding/base.py`, `backend/app/services/embedding/fake.py`, `backend/app/services/embedding/zhipu.py`, `backend/tests/test_embedding.py`

**Interfaces:**
- Produces: `EmbeddingProvider.embed_documents(texts: list[str]) -> list[list[float]]`(抽象);`get_provider() -> EmbeddingProvider`(工厂,按 settings.EMBED_PROVIDER:fake→FakeEmbedding,zhipu→ZhipuEmbedding,未知值 ValueError);FakeEmbedding 确定性向量(同一文本恒同向量,维度 settings.EMBED_DIMS);ZhipuEmbedding 用 openai 客户端打 settings.ZHIPU_BASE_URL,model=settings.EMBED_MODEL,dimensions=settings.EMBED_DIMS
- Consumes: Task 0 的 settings 字段

- [x] **Step 1: 写失败测试 `tests/test_embedding.py`**

```python
import pytest

from app.core.config import settings
from app.services.embedding.base import get_provider
from app.services.embedding.zhipu import ZhipuEmbedding


def test_fake_deterministic_and_dims():
    provider = get_provider()  # 测试进程 EMBED_PROVIDER=fake(conftest 约定)
    v1 = provider.embed_documents(["你好世界"])
    v2 = provider.embed_documents(["你好世界"])
    assert v1 == v2
    assert len(v1[0]) == settings.EMBED_DIMS
    other = provider.embed_documents(["另一个文本"])[0]
    assert other != v1[0]


def test_unknown_provider_raises():
    with pytest.raises(ValueError):
        get_provider("no-such-provider")


@pytest.mark.skipif(
    not settings.ZHIPU_API_KEY, reason="ZHIPU_API_KEY not configured"
)
def test_zhipu_live_embedding():
    provider = ZhipuEmbedding()
    vectors = provider.embed_documents([" AIRag 集成测试一", "AIRag 集成测试二"])
    assert len(vectors) == 2
    assert all(len(v) == settings.EMBED_DIMS for v in vectors)
```

- [x] **Step 2: 运行确认失败**

```cmd
.venv\Scripts\python -m pytest tests\test_embedding.py -v
```

Expected: FAIL/ERROR(模块不存在);zhipu live 用例 skipped。

- [x] **Step 3: 实现**

`app/services/embedding/base.py`:

```python
from abc import ABC, abstractmethod

from app.core.config import settings


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入;返回与输入等长、各含 EMBED_DIMS 维浮点的列表。"""


def get_provider(name: str | None = None) -> EmbeddingProvider:
    name = (name or settings.EMBED_PROVIDER).lower()
    if name == "fake":
        from app.services.embedding.fake import FakeEmbedding

        return FakeEmbedding()
    if name == "zhipu":
        from app.services.embedding.zhipu import ZhipuEmbedding

        return ZhipuEmbedding()
    raise ValueError(f"unknown embedding provider: {name}")
```

`app/services/embedding/fake.py`:

```python
import hashlib
import random

from app.core.config import settings
from app.services.embedding.base import EmbeddingProvider


class FakeEmbedding(EmbeddingProvider):
    """确定性假嵌入:同文本恒同向量。仅供测试/无 key 演示,语义无意义。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        dim = settings.EMBED_DIMS
        vectors = []
        for text in texts:
            seed = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:12], 16)
            rng = random.Random(seed)
            vectors.append([rng.uniform(-1.0, 1.0) for _ in range(dim)])
        return vectors
```

`app/services/embedding/zhipu.py`:

```python
from app.core.config import settings
from app.services.embedding.base import EmbeddingProvider


class ZhipuEmbedding(EmbeddingProvider):
    """智谱 embedding-3,经 OpenAI 兼容端点。同步调用——调用方放线程池。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings.ZHIPU_API_KEY, base_url=settings.ZHIPU_BASE_URL
        )
        resp = client.embeddings.create(
            model=settings.EMBED_MODEL,
            input=list(texts),
            dimensions=settings.EMBED_DIMS,
        )
        data = sorted(resp.data, key=lambda d: d.index)
        return [item.embedding for item in data]
```

`app/services/embedding/__init__.py`:

```python
from app.services.embedding.base import EmbeddingProvider, get_provider

__all__ = ["EmbeddingProvider", "get_provider"]
```

- [x] **Step 4: 运行确认通过并全量回归**

```cmd
.venv\Scripts\python -m pytest -v
```

Expected: **28 passed + 1 skipped**(26 + embedding 2;live 用例无 key 跳过,有 key 则 30 passed)。

- [x] **Step 5: 提交**

```cmd
git add backend\app\services\embedding backend\tests\test_embedding.py
git commit -m "feat: embedding provider abstraction with fake and zhipu implementations"
```

---

### Task 6: Celery 应用与流水线(状态机+重试+e2e eager)

**Files:**
- Create: `backend/app/workers/celery_app.py`, `backend/app/workers/pipeline.py`;Modify: `backend/app/api/documents.py`(接线 `.delay`), `backend/tests/conftest.py`(eager 夹具), `backend/tests/test_documents.py`(追加状态断言)或新 `backend/tests/test_pipeline.py`

**Interfaces:**
- Produces: `celery_app`(broker=settings.REDIS_URL);`process_document(document_id: int)` 任务(bind=True, max_retries=3, 退避 2/4/8s);流转 `pending→parsing→chunking→embedding→done`,异常耗尽重试后 `failed`+`error_msg`;chunks 批量插入(embedding 列填向量,tsv 用 `to_tsvector('simple', content)` 生成);embedding 按 16 条一批。上传端点 commit 后 `process_document.delay(doc.id)`
- Consumes: Task 2/3/4/5 全部产出;**约定:任务内一律用 `settings.DATABASE_URL`(conftest 已在测试进程把它指到 airag_test)**;embedding 调用经 `asyncio.to_thread`(同步 SDK 不卡事件循环);engine 每次任务新建(NullPool)用完即弃

- [x] **Step 1: conftest 追加 eager 夹具**

```python
@pytest_asyncio.fixture(autouse=True)
async def celery_eager():
    from app.workers.celery_app import celery_app

    old = {
        "always": celery_app.conf.task_always_eager,
        "propagates": celery_app.conf.task_eager_propagates,
    }
    # propagates 必须 False:Task 2 的哑字节上传用例(.docx 内容是 b"dummy")在 Task 6
    # 之后会于 eager 模式真的跑流水线并解析失败;异常若传播会把 API 响应变成 500。
    # False 时异常只存进 result,不冒泡,旧用例不受影响。
    celery_app.conf.update(task_always_eager=True, task_eager_propagates=False)
    yield
    celery_app.conf.update(
        task_always_eager=old["always"], task_eager_propagates=old["propagates"]
    )
```

(autouse 对非流水线测试无副作用——没人 .delay 就不触发;流水线失败结果存在 result 里,不断言。)

- [x] **Step 2: 写失败测试 `tests/test_pipeline.py`**

两个测试:①上传真 PDF(eager 内联跑完全链路)断言 done+chunks 落库;②直测 `_mark_failed` 断言 failed 态(celery 重试机制本身不在 eager 下断言,真实 worker 的失败路径由 Task 8 人工验收兜底)。

```python
import io

from sqlalchemy import select

from app.models import Chunk, Document


async def _upload_pdf(client, auth_headers, kb_id, text="AIRag pipeline pdf body"):
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("pipe.pdf", buf.getvalue(), "application/pdf")},
        headers=auth_headers,
    )


async def test_upload_runs_pipeline_to_done(client, auth_headers, db_session):
    kb = await client.post(
        "/api/kbs", json={"name": "流水线库"}, headers=auth_headers
    )
    kb_id = kb.json()["id"]
    up = await _upload_pdf(client, auth_headers, kb_id)
    assert up.status_code == 201
    doc_id = up.json()["id"]

    detail = await client.get(f"/api/documents/{doc_id}", headers=auth_headers)
    assert detail.json()["status"] == "done"
    assert detail.json()["chunk_count"] >= 1
    assert detail.json()["page_count"] == 1

    rows = (
        (await db_session.execute(select(Chunk).where(Chunk.document_id == doc_id)))
        .scalars()
        .all()
    )
    assert len(rows) >= 1
    assert rows[0].embedding is not None and len(rows[0].embedding) > 0
    assert rows[0].tsv is not None


async def test_mark_failed_sets_state(client, auth_headers, db_session):
    from app.workers.pipeline import _mark_failed

    kb = await client.post(
        "/api/kbs", json={"name": "失败库二"}, headers=auth_headers
    )
    kb_id = kb.json()["id"]
    up = await _upload_pdf(client, auth_headers, kb_id)
    doc_id = up.json()["id"]

    await _mark_failed(doc_id, "boom: parser exploded")

    doc = await db_session.get(Document, doc_id)
    await db_session.refresh(doc)
    assert doc.status == "failed"
    assert "parser exploded" in doc.error_msg
```

(eager 模式下 `.delay` 同步执行完整流水线,所以①在 upload 响应返回后 detail 即 done;②中该 doc 此前可能已成功 done,`_mark_failed` 直接覆盖为 failed——正是被测行为。)

- [x] **Step 3: 运行确认失败**

```cmd
.venv\Scripts\python -m pytest tests\test_pipeline.py -v
```

Expected: FAIL/ERROR(celery_app 不存在)。

- [x] **Step 4: 实现**

`app/workers/celery_app.py`:

```python
from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "airag",
    broker=settings.REDIS_URL,
    include=["app.workers.pipeline"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
)
```

`app/workers/pipeline.py`(整文件唯一版本,以此为准——状态机顺序 parsing→chunking→embedding→done;嵌入批 16;tsv 用 SQL 生成;engine 每任务新建用完即弃):

```python
import asyncio
import hashlib
from pathlib import Path

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models import Chunk, Document
from app.services.chunking import split_blocks
from app.services.embedding import get_provider
from app.services.parsing import get_parser
from app.workers.celery_app import celery_app

EMBED_BATCH = 16


def _engine(db_url: str):
    return create_async_engine(db_url, poolclass=NullPool)


async def _mark_failed(
    document_id: int, error_msg: str, db_url: str | None = None
) -> None:
    engine = _engine(db_url or settings.DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            doc = await session.get(Document, document_id)
            if doc is not None:
                doc.status = "failed"
                doc.error_msg = error_msg[:2000]
                await session.commit()
    finally:
        await engine.dispose()


async def _run(document_id: int, db_url: str) -> None:
    engine = _engine(db_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            doc = await session.get(Document, document_id)
            if doc is None:
                raise RuntimeError(f"document {document_id} not found")
            file_path = Path(doc.file_path)
            if not file_path.exists():
                raise FileNotFoundError(f"missing file: {doc.file_path}")

            doc.status = "parsing"
            await session.commit()
            result = get_parser(file_path.suffix.lower()).parse(file_path)

            doc.status = "chunking"
            await session.commit()
            chunks = split_blocks(result.blocks)
            if not chunks:
                raise RuntimeError("no content extracted")

            doc.status = "embedding"
            await session.commit()
            provider = get_provider()
            vectors: list[list[float]] = []
            for i in range(0, len(chunks), EMBED_BATCH):
                batch = chunks[i : i + EMBED_BATCH]
                vectors.extend(
                    await asyncio.to_thread(
                        provider.embed_documents, [c.content for c in batch]
                    )
                )

            for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
                session.add(
                    Chunk(
                        document_id=document_id,
                        kb_id=doc.kb_id,
                        chunk_index=index,
                        content=chunk.content,
                        page_no=chunk.page_no,
                        char_len=chunk.char_len,
                        embedding=vector,
                        content_hash=hashlib.sha256(
                            chunk.content.encode("utf-8")
                        ).hexdigest(),
                    )
                )
            await session.commit()

            await session.execute(
                text(
                    "UPDATE chunks SET tsv = to_tsvector('simple', content) "
                    "WHERE document_id = :doc_id AND tsv IS NULL"
                ),
                {"doc_id": document_id},
            )
            await session.commit()

            doc.page_count = result.page_count or None
            doc.chunk_count = len(chunks)
            doc.status = "done"
            doc.error_msg = None
            await session.commit()
            logger.info(f"document {document_id} done: {len(chunks)} chunks")
    finally:
        await engine.dispose()


@celery_app.task(bind=True, max_retries=3)
def process_document(self, document_id: int):
    try:
        asyncio.run(_run(document_id, settings.DATABASE_URL))
    except Exception as exc:
        retries = self.request.retries
        if retries < 3:
            logger.warning(
                f"document {document_id} attempt failed: {exc}; retry {retries + 1}/3"
            )
            raise self.retry(exc=exc, countdown=2 ** (retries + 1))
        asyncio.run(_mark_failed(document_id, str(exc)))
```

`app/api/documents.py` 接线:文件顶部 imports 追加 `from app.workers.pipeline import process_document`;`upload_document` 函数体内 `await db.refresh(doc)` 之后、`return doc` 之前插入一行:

```python
    process_document.delay(doc.id)
```

- [x] **Step 5: 运行确认通过并全量回归**

```cmd
.venv\Scripts\python -m pytest -v
```

Expected: **30 passed + 1 skipped**(28 + pipeline 2)。注意:接线上传后,Task 2 的旧用例在 eager 下会真跑流水线——`.docx` 哑字节用例会在流水线内解析失败,但 propagates=False 使异常只存 result,该用例断言的 201/pending/sha 均在 `.delay` 之前已满足,不受影响;真 docx 用例会直接跑到 done(更没问题)。

- [x] **Step 6: 提交**

```cmd
git add backend\app\workers backend\app\api\documents.py backend\tests\conftest.py backend\tests\test_pipeline.py
git commit -m "feat: celery document pipeline with status machine and retries"
```

---

### Task 7: chunks 查询端点 + worker 启动脚本 + README

**Files:**
- Create: `backend/start_worker.bat`;Modify: `backend/app/api/documents.py`(追加 chunks 端点), `backend/README.md`→根 `README.md`(文档流水线一节 + M1 启动命令围栏修复)
- Test: `backend/tests/test_documents.py`(追加)

**Interfaces:**
- Produces: `GET /api/documents/{doc_id}/chunks?page=1&page_size=20` → `{"total": int, "items": [{"id","chunk_index","page_no","char_len","content_preview"(前 200 字)}]}`(鉴权同其他端点);`start_worker.bat` 一键起 worker(solo 池)

- [x] **Step 1: 追加失败测试**

`tests/test_documents.py` 末尾追加:

```python
async def test_chunks_endpoint(client, auth_headers, db_session):
    from sqlalchemy import select

    from app.models import Chunk

    kb_id = (await _make_kb(client, auth_headers))
    up = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("c.docx", b"dummy", "application/octet-stream")},
        headers=auth_headers,
    )
    doc_id = up.json()["id"]
    for i in range(3):
        db_session.add(
            Chunk(
                document_id=doc_id,
                kb_id=kb_id,
                chunk_index=i,
                content=f"chunk-{i} " + "字" * 250,
                page_no=1,
                char_len=257,
                content_hash=f"hash{i}",
            )
        )
    await db_session.commit()

    resp = await client.get(
        f"/api/documents/{doc_id}/chunks?page=1&page_size=2",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["items"][0]["chunk_index"] == 0
    assert len(body["items"][0]["content_preview"]) <= 200
```

- [x] **Step 2: 运行确认失败**

```cmd
.venv\Scripts\python -m pytest tests\test_documents.py -v
```

Expected: 新增 1 条 FAIL(404)。

- [x] **Step 3: 实现端点**

`app/api/documents.py` 追加(imports 补 `from sqlalchemy import select` 已有、`Chunk`):

```python
@router.get("/documents/{doc_id}/chunks")
async def list_chunks(
    doc_id: int,
    page: int = 1,
    page_size: int = 20,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    from app.models import Chunk

    total = (
        await db.execute(
            select(Chunk.id).where(Chunk.document_id == doc_id)
        )
    ).scalars().all()
    rows = (
        await db.execute(
            select(Chunk)
            .where(Chunk.document_id == doc_id)
            .order_by(Chunk.chunk_index)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return {
        "total": len(total),
        "items": [
            {
                "id": c.id,
                "chunk_index": c.chunk_index,
                "page_no": c.page_no,
                "char_len": c.char_len,
                "content_preview": c.content[:200],
            }
            for c in rows
        ],
    }
```

(count 用 len(ids) 简单可靠;M3 换 count() 优化。)

- [x] **Step 4: 写 `backend/start_worker.bat`**

```bat
@echo off
REM AIRag 文档流水线 worker(Windows 必须 solo 池)
cd /d %~dp0
.venv\Scripts\python -m celery -A app.workers.celery_app worker --pool=solo --loglevel=info
```

(GBK/ANSI 保存,同 start_dev.bat。)

- [x] **Step 5: 更新根 README.md**

"开发启动"代码块改为围栏 ```bash 包裹(修 M1 遗留 Minor),并追加"文档流水线"一节:

```markdown
## 文档流水线(M2 起)

除后端/前端两个窗口外,再开一个窗口启动 worker:

cd backend
start_worker.bat

上传:.env 填好 ZHIPU_API_KEY 后默认走智谱 embedding;未填 key 时可在 .env 设 EMBED_PROVIDER=fake 跑通全流程(向量无语义)。
```

- [x] **Step 6: 运行确认通过并全量回归**

```cmd
.venv\Scripts\python -m pytest -v
```

Expected: **31 passed + 1 skipped**。

- [x] **Step 7: 提交**

```cmd
git add backend\app\api\documents.py backend\start_worker.bat README.md backend\tests\test_documents.py
git commit -m "feat: chunks query endpoint and worker start script"
```

---

### Task 8: M2 端到端验收(真文件×真 worker)

**Files:**
- Create: 三个真实样例文件(验收脚本临时生成于 `%TEMP%\airag_m2_e2e\`,不入库);无代码改动(README 若需微调可改)

**Interfaces:**
- Produces: M2 验收证据(三种格式 done + chunks 可查);验收标记提交

- [x] **Step 1: 生成三个真实样例文件**

用 venv python 执行一次性脚本(写入 %TEMP%\airag_m2_e2e):pdf 用 fitz 新建两页各一行文本;docx 用 python-docx 三段+一张 2×2 表;xlsx 用 openpyxl 两 sheet 各两行。

- [x] **Step 2: 起三件套**

确认 PG 与 Redis 服务在跑(`net start | findstr /i "postgres redis"`)→ 新窗口 `backend\start_dev.bat` → 新窗口 `backend\start_worker.bat`(看到 celery ready)→ (前端不必起)。

- [x] **Step 3: API 验收循环**

1. `curl` 注册+登录拿 token(用户名 `m2e2e_<rand>`)
2. `curl` 建 KB
3. 依次上传三个文件(`curl -F "file=@路径"`),各得 doc_id
4. 每个 doc 轮询 `GET /api/documents/{id}`(每 3 秒,至多 60 秒)直到 `status=done`,记录 `chunk_count>0`
5. `GET /api/documents/{id}/chunks` 抽查任一 doc:items 非空,含 page_no/content_preview
6. 重传同文件 → 409
7. Swagger `GET /docs` 可达

**若 ZHIPU_API_KEY 仍为空**:在 `.env` 临时设 `EMBED_PROVIDER=fake` 重启 backend+worker 完成上述验收(fake 向量无语义但全链路真实);**验收后恢复 `.env` 为 `EMBED_PROVIDER=zhipu`**。若 key 已填:直接真嵌入跑通,并在 pytest 里跑 live 用例(应 33 passed 0 skipped)。

- [x] **Step 4: 回归与清理**

全量 pytest(基线 + skipif 状态一致);杀掉 backend/worker 进程(端口 8001 释放,worker 控制台关闭);`storage/uploads` 留在磁盘(已 ignore)。

- [x] **Step 5: 验收标记提交**

```cmd
git commit --allow-empty -m "chore: m2 complete - document pipeline acceptance verified"
```

---

### Task 9(控制器): 计划勾选同步、终审、合并推送、M3 交接

- 勾选本文件全部已完成项(浏览器/环境类除外),提交推送
- 全分支终审(superpowers:requesting-code-review 的 code-reviewer,最全能模型):对照 Spec §7/§9-M2;分诊延后 Minor;审计 Rulings
- 合并 m2 分支 → main,推送;M3 计划交接(检索与问答:混合检索+RRF+LangGraph 三节点+SSE+前端三页面)留下一会话生成
- 清理本计划 SDD 工作区

---

## 计划自审记录(writing-plans Self-Review)

1. **Spec 覆盖**:§7 全链路(上传→去重→parse→chunk→embed→入库→状态机/重试)→ Task 2/3/4/5/6;验收"三格式 done+chunks 可查"→ Task 8+Task 7 的 chunks 端点;增量更新(content_hash)已存列、替换/删除文档 M4 做(范围收敛,路线图一致)。
2. **占位符扫描**:Task 6 的 pipeline.py 给出三段演进式草稿+明确"以第三段为准+删除垃圾行"的强指令——这是**对实现者的显式警告**,不是 TBD;评审按语义清单验收。其余任务代码完整。
3. **类型一致性**:`ParsedBlock(content,page_no,is_table)` ↔ splitter/xlsx/docx 用法一致;`Chunk(content,page_no,char_len)` ↔ pipeline 插库字段一致;`DocumentOut` 字段 ↔ 模型列一致;`get_provider(name=None)` ↔ 测试/管线调用一致;conftest 的 DATABASE_URL 环境约定 ↔ pipeline 的 settings.DATABASE_URL 一致。
4. **风险预埋**:eager 模式 retry 会抛异常 → 失败路径改测 `_mark_failed`(Task 6 Step 2 已注明);upload 端点 `.delay` 在响应返回前执行使响应仍为 pending 状态(Task 6 Step 5 注明);upload 测试用哑字节不触发解析(仅 pipeline 测试用真 PDF)。
