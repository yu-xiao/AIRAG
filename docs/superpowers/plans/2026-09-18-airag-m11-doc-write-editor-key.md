# AIRag M11 实施计划:Agent 文档写操作与 editor key

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ApiKey 能力位(read_only/editor)+ 文档写操作(上传/删除/重解析/列表/状态)在 Web/REST/MCP 三面开放,并清偿 M10 六笔顺延小项。

**Architecture:** 沿用 M9 起"facade 唯一业务实现"原则:新增 `services/doc_ops.py` 承载文档写核心(校验/落盘/级联删除/审计/commit/dispatch),Web `api/documents.py` 薄壳化,REST `api/agent.py` 与 MCP `mcp_server.py` 各加薄壳端点/工具;key 能力经 `Principal.key_role` 全链路传播,写操作双重校验(key=editor ∧ 用户 perm≥editor)。

**Tech Stack:** FastAPI + SQLAlchemy async + alembic + fastmcp 2.14.7 + celery + Vue3/element-plus + pytest(pytest-asyncio)。

**Spec:** `docs/superpowers/specs/2026-09-18-airag-m11-doc-write-editor-key-design.md`(计划与 spec 同行,执行者两份都读)。

## Global Constraints

- 后端测试命令(Windows,backend 目录):`.venv\Scripts\python -m pytest -q`;单测:`.venv\Scripts\python -m pytest tests/test_xxx.py::test_name -v`。基线 **219 passed** 起步,任何任务后不得出现失败。
- 前端校验:`cd frontend && npm run build`(含 vue-tsc 类型检查)。前端无单测,验收=build+走查。
- conftest.py 顶部钉死的环境变量(DATABASE_URL 指向 airag_test、EMBED_PROVIDER=fake、MINERU_API_TOKEN 空、AGENTIC_*/MULTI_HOP/RERANK off)**不得改动**;测试库由 conftest `prepare_db` 用 `Base.metadata.create_all` 重建(不走 alembic),alembic 只对 dev 库执行。
- Redis key 命名约定:`agent_rl:{key_id}`(限流)、`agent_tq:{key_id}:{yyyymmdd}`(配额);本地时区日界。
- 错误语义:404 不泄露存在性(库/文档不可见统一 404);写端点校验顺序 **可见性(404)→ 用户权限级(403 "editor permission required")→ key 能力(403 {"code": "editor_key_required"})**;`refused=true` 也是 200 的既有语义不动。
- 审计:`audit()` 与业务同事务不自行 commit;`AuditLog.detail` 是 Text 存 JSON——**测试断言要 `json.loads`**;action ≤32 字符。Web 面 action 用 `doc_upload`/`doc_delete`/`doc_reprocess`,Agent 面用 `agent.list_documents`/`agent.get_document`/`agent.upload_document`/`agent.delete_document`/`agent.reprocess_document`。
- MCP 工具 description **必须**模块级常量字符串经 `@mcp.tool(description=...)` 传入(f-string docstring 不设 `__doc__`,M10 教训);MCP 工具内异常统一 `ToolError`,文本携带错误 code。
- celery 测试约定:conftest `celery_eager` fixture 已设 `task_always_eager=True, task_eager_propagates=False`(哑字节 .docx 在 eager 下解析失败不冒泡,文档终态 failed)。
- MCP 测试:session 级 `_mcp_lifespan` fixture 只启停一次,沿用 `tests/test_mcp.py` 既有 JSON-RPC 套路(`_init`/`_rpc`/`_tool_result`)。
- 提交风格与 git log 一致:`feat(agent): ...`/`test(agent): ...`/`refactor(agent): ...`/`docs(agent): ...`;每任务恰好一提交,提交前该任务全部测试绿。
- 走查期约束(交付后):不在 `backend\` 写临时文件(--reload 会打断在途长调用);后端 8001(start_dev.bat)。

---

### Task 1: `ApiKey.role` 能力位(模型+迁移+铸造链路端到端)

**Files:**
- Modify: `backend/app/models/api_key.py`
- Create: `backend/alembic/versions/c1d2e3f4a5b6_m11_api_key_role.py`
- Modify: `backend/app/services/api_keys.py`(issue_api_key 签名)
- Modify: `backend/app/schemas/auth.py`、`backend/app/schemas/admin.py`
- Modify: `backend/app/api/auth.py`(create_api_key)、`backend/app/api/admin.py`(admin_create_key)
- Test: `backend/tests/test_api_keys_service.py`、`backend/tests/test_auth_keys_api.py`、`backend/tests/test_admin_keys.py`

**Interfaces:**
- Consumes: 现有 `issue_api_key(db, user, name, expires_in_days) -> tuple[ApiKey, str]`、`ApiKeyCreatedOut`、conftest `client/auth_headers/db_session` fixtures。
- Produces(Task 2/4/5 依赖):
  - `ApiKey.role: Mapped[str]`(值域 `read_only|editor`,default/server_default=`read_only`)
  - `issue_api_key(db, user, name, expires_in_days, role="read_only") -> tuple[ApiKey, str]`
  - `ApiKeyCreateIn.role: Literal["read_only","editor"] = "read_only"`;`AdminKeyCreateIn.role` 同;`ApiKeyOut.role: str`(CreatedOut 继承)
  - POST `/api/auth/keys` 与 POST `/api/admin/keys` 接受 `role`,响应/列表带 `role`

- [ ] **Step 1: 写失败测试(三个文件各追加)**

`tests/test_api_keys_service.py` 追加(文件已有 `issue_api_key` 相关导入与用例,顶部补 `from app.models import User` 若缺):

```python
async def test_issue_api_key_role_passthrough(client, auth_headers, db_session):
    me = await client.get("/api/auth/me", headers=auth_headers)
    user = await db_session.get(User, me.json()["id"])
    key, _raw = await issue_api_key(db_session, user, "编辑key", 1, role="editor")
    assert key.role == "editor"
    key2, _raw2 = await issue_api_key(db_session, user, "默认key", 1)
    assert key2.role == "read_only"
    await db_session.commit()
```

`tests/test_auth_keys_api.py` 追加:

```python
async def test_create_key_with_role_and_default(client, auth_headers):
    r = await client.post("/api/auth/keys",
                          json={"name": "编辑", "role": "editor"},
                          headers=auth_headers)
    assert r.status_code == 201
    assert r.json()["role"] == "editor"
    r2 = await client.post("/api/auth/keys", json={"name": "默认"}, headers=auth_headers)
    assert r2.status_code == 201
    assert r2.json()["role"] == "read_only"
    r3 = await client.post("/api/auth/keys",
                           json={"name": "坏角色", "role": "admin"},
                           headers=auth_headers)
    assert r3.status_code == 422
    lst = await client.get("/api/auth/keys", headers=auth_headers)
    assert all("role" in k for k in lst.json())
```

`tests/test_admin_keys.py` 追加(`text` 导入沿用该文件既有写法):

```python
async def test_admin_issue_key_with_role(client, auth_headers, db_session):
    me = await client.get("/api/auth/me", headers=auth_headers)
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": me.json()["id"]},
    )
    await db_session.commit()
    r = await client.post(
        "/api/admin/keys",
        json={"user_id": me.json()["id"], "name": "代发编辑", "role": "editor"},
        headers=auth_headers,
    )
    assert r.status_code == 201
    assert r.json()["role"] == "editor"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_api_keys_service.py tests/test_auth_keys_api.py tests/test_admin_keys.py -q`
Expected: FAIL(role 字段不存在——TypeError/KeyError/422)

- [ ] **Step 3: 最小实现**

`app/models/api_key.py` 在 `is_active` 行后加:

```python
    # M11:能力位;read_only=只读检索/问答,editor=可写文档(仍受用户 KB 权限约束)
    role: Mapped[str] = mapped_column(String(16), default="read_only",
                                      server_default="read_only")
```

`app/services/api_keys.py` `issue_api_key` 签名与建行改为:

```python
async def issue_api_key(
    db: AsyncSession, user: User, name: str, expires_in_days: int | None,
    role: str = "read_only",
) -> tuple[ApiKey, str]:
```

```python
    key = ApiKey(
        user_id=user.id,
        name=name,
        key_prefix=prefix,
        key_hash=digest,
        role=role,
        expires_at=(
```

`app/schemas/auth.py`:`ApiKeyCreateIn` 加 `role`、`ApiKeyOut` 加 `role`(顶部 `from typing import Literal`):

```python
class ApiKeyCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)  # None=永久
    role: Literal["read_only", "editor"] = "read_only"
```

```python
class ApiKeyOut(BaseModel):
    id: int
    name: str
    key_prefix: str
    role: str
    is_active: bool
```

`app/schemas/admin.py` `AdminKeyCreateIn` 加(已有 `Literal` 导入):

```python
    role: Literal["read_only", "editor"] = "read_only"
```

`app/api/auth.py` `create_api_key` 调用处:

```python
        key, raw = await issue_api_key(db, current, payload.name,
                                       payload.expires_in_days, payload.role)
```

`app/api/admin.py` `admin_create_key` 调用处同改:

```python
        key, raw = await issue_api_key(db, target, payload.name,
                                       payload.expires_in_days, payload.role)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_api_keys_service.py tests/test_auth_keys_api.py tests/test_admin_keys.py -q`
Expected: PASS(原有用例 + 新增 3 例)

- [ ] **Step 5: 写 alembic 迁移并升级 dev 库**

Create `backend/alembic/versions/c1d2e3f4a5b6_m11_api_key_role.py`:

```python
# backend/alembic/versions/c1d2e3f4a5b6_m11_api_key_role.py
"""m11 api_keys.role

Revision ID: c1d2e3f4a5b6
Revises: b9c8d7e6f5a4
Create Date: 2026-09-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b9c8d7e6f5a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("role", sa.String(16), nullable=False,
                  server_default="read_only"),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "role")
```

Run: `.venv\Scripts\alembic upgrade head`(backend 目录;对 dev 库执行,存量 key 自动落 read_only)
验证:`.venv\Scripts\alembic current` 显示 c1d2e3f4a5b6。

- [ ] **Step 6: 全量回归**

Run: `.venv\Scripts\python -m pytest -q`
Expected: 全绿(219 + 3 新增 = 222 passed 左右)

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/api_key.py backend/alembic/versions/c1d2e3f4a5b6_m11_api_key_role.py backend/app/services/api_keys.py backend/app/schemas/auth.py backend/app/schemas/admin.py backend/app/api/auth.py backend/app/api/admin.py backend/tests/test_api_keys_service.py backend/tests/test_auth_keys_api.py backend/tests/test_admin_keys.py
git commit -m "feat(agent): api key role capability bit (read_only/editor)"
```

---

### Task 2: `Principal.key_role` 传播 + 守卫 helper(含 M10 小项② `_api_key_id` 去重)

**Files:**
- Modify: `backend/app/core/deps.py`(Principal 字段、resolve_bearer_principal、两个 helper)
- Modify: `backend/app/api/agent.py`(`_check_rate`/`_check_quota`/ask 端点守卫换 helper)
- Modify: `backend/app/mcp_server.py`(ask 工具守卫换 helper)
- Test: `backend/tests/test_agent_api.py`

**Interfaces:**
- Consumes: Task 1 的 `ApiKey.role`。
- Produces(Task 4/5/7 依赖):
  - `Principal(user, kind, key_id, key_name, key_role)`;`key_role: str | None = None`(仅 api_key 时非 None;JWT 恒 None)
  - `def _api_key_id(principal: Principal) -> int | None`(api_key 返回 key_id,JWT 返回 None)
  - `def require_editor_key(principal: Principal) -> None`(JWT 放行;api_key 且 key_role≠editor 抛 `HTTPException(403, {"code": "editor_key_required"})`)

- [ ] **Step 1: 写失败测试**

`tests/test_agent_api.py` 追加(顶部补 `from fastapi import HTTPException`、`import pytest`、`from app.core.deps import _api_key_id, require_editor_key, resolve_bearer_principal` 若缺):

```python
# ---- M11:能力传播与守卫 ----
async def _create_key_role(client, headers, name, role):
    r = await client.post("/api/auth/keys",
                          json={"name": name, "role": role}, headers=headers)
    assert r.status_code == 201
    return r.json()["key"]


async def test_principal_key_role_and_guards(client, auth_headers, db_session):
    editor_key = await _create_key_role(client, auth_headers, "编辑", "editor")
    ro_key = await _create_key_role(client, auth_headers, "只读", "read_only")

    p = await resolve_bearer_principal(db_session, editor_key)
    assert p.kind == "api_key" and p.key_role == "editor"
    assert _api_key_id(p) == p.key_id
    require_editor_key(p)  # editor key 放行

    p2 = await resolve_bearer_principal(db_session, ro_key)
    assert p2.key_role == "read_only"
    with pytest.raises(HTTPException) as ei:
        require_editor_key(p2)
    assert ei.value.status_code == 403
    assert ei.value.detail == {"code": "editor_key_required"}

    token = auth_headers["Authorization"].split(" ", 1)[1]
    pj = await resolve_bearer_principal(db_session, token)
    assert pj.kind == "jwt" and pj.key_role is None
    assert _api_key_id(pj) is None
    require_editor_key(pj)  # JWT 调试通道视为 editor 能力
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_agent_api.py::test_principal_key_role_and_guards -v`
Expected: FAIL(ImportError: `_api_key_id` 不存在)

- [ ] **Step 3: 实现 deps.py**

`app/core/deps.py`:

`Principal` 加字段:

```python
@dataclass
class Principal:
    user: User
    kind: str  # jwt | api_key
    key_id: int | None = None
    key_name: str | None = None
    key_role: str | None = None  # M11:read_only|editor;JWT 恒 None
```

`resolve_bearer_principal` api_key 分支(其余不动):

```python
        return Principal(user=user, kind="api_key", key_id=key.id,
                         key_name=key.name, key_role=key.role)
```

模块尾部追加两个 helper:

```python
def _api_key_id(principal: Principal) -> int | None:
    """api_key 主体返回 key_id,JWT(人工调试)返回 None——限流/配额/配额查询
    的统一守卫(M10 顺延小项②:替换各处 kind+key_id 双条件)。"""
    if principal.kind == "api_key":
        return principal.key_id
    return None


def require_editor_key(principal: Principal) -> None:
    """写操作 key 能力守卫:JWT 调试通道视为 editor(仍受用户 KB 权限约束,
    与限流豁免 JWT 同哲学);api_key 须为 editor。"""
    if principal.kind == "api_key" and principal.key_role != "editor":
        raise HTTPException(status_code=403,
                            detail={"code": "editor_key_required"})
```

- [ ] **Step 4: 替换既有守卫(小项②)**

`app/api/agent.py`:`_check_rate`/`_check_quota` 改为:

```python
async def _check_rate(principal: Principal) -> None:
    """仅对 API Key 生效;JWT(人工调试)不限流。"""
    key_id = _api_key_id(principal)
    if key_id is None:
        return
    ok, retry_after = await rate_allow(key_id)
```

```python
async def _check_quota(principal: Principal) -> None:
    """ask 前置配额检查(仅 api_key);429 附 Retry-After 头。"""
    key_id = _api_key_id(principal)
    if key_id is None:
        return
    ok, retry_after = await quota_check(key_id)
```

ask 端点内两处:

```python
    if (key_id := _api_key_id(principal)) is not None:
        await quota_consume(key_id, outcome.tokens_used)
```

`app/mcp_server.py` `ask_knowledge_base` 内两处同改(前置 check 与完成后 consume)。导入行更新:`from app.core.deps import Principal, _api_key_id, current_client_ip, current_principal, resolve_bearer_principal`。

- [ ] **Step 5: 跑测试(单例+受影响面)确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_agent_api.py tests/test_agent_ask.py tests/test_mcp.py tests/test_agent_ratelimit.py -q`
Expected: PASS(守卫替换为行为等价重构,既有用例全绿)

- [ ] **Step 6: Commit**

```bash
git add backend/app/core/deps.py backend/app/api/agent.py backend/app/mcp_server.py backend/tests/test_agent_api.py
git commit -m "feat(agent): principal key_role propagation and editor-key guard helpers"
```

---

### Task 3: `doc_ops` 共享服务 + Web 薄壳化 + Web 删除端点 + worker 早退

**Files:**
- Create: `backend/app/services/doc_ops.py`
- Modify: `backend/app/api/documents.py`
- Modify: `backend/app/workers/pipeline.py`(`_run` 缺行早退)
- Test: Create `backend/tests/test_doc_ops.py`;Modify `backend/tests/test_documents.py`、`backend/tests/test_pipeline.py`

**Interfaces:**
- Consumes: `audit()`、`get_kb_perm`/`has_perm`、`process_document.delay`、conftest `isolated_upload_dir`/`celery_eager`。
- Produces(Task 4/5 依赖,全部在 `app/services/doc_ops.py`):
  - `ALLOWED_EXTS = {".pdf", ".docx", ".xlsx", ".jpg", ".jpeg", ".png"}`;`BUSY_STATUSES = ("parsing", "chunking", "embedding")`
  - `async def visible_kb_or_404(db, user, kb_id) -> KnowledgeBase`(无权/不存在统一 404)
  - `async def visible_doc_or_404(db, user, doc_id) -> Document`(同上)
  - `async def save_upload(db, kb, *, filename: str, payload: bytes, mime: str | None, ocr_mode: str, username: str, action: str, audit_extra: dict | None = None) -> Document`(415/413/409;内部 flush→audit→commit→refresh→dispatch)
  - `async def delete_document(db, doc, *, username: str, action: str, audit_extra: dict | None = None) -> None`(409 busy;删 chunks→尽力删文件→audit→删行→commit)
  - `async def reprocess_document(db, doc, *, username: str, action: str, audit_extra: dict | None = None) -> Document`(409 busy;清 chunks→重置→audit→commit→dispatch)
  - Web 端点:`DELETE /api/documents/{doc_id}`(editor+,204,audit `doc_delete`)

- [ ] **Step 1: 写失败测试**

Create `tests/test_doc_ops.py`:

```python
# backend/tests/test_doc_ops.py
"""M11 Task3:doc_ops 服务(校验/级联删除/重解析重置)。"""
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models import Chunk, Document, KnowledgeBase
from app.services import doc_ops


async def _mk_kb(db, user_id, name="docops库") -> KnowledgeBase:
    kb = KnowledgeBase(name=name, owner_id=user_id)
    db.add(kb)
    await db.flush()
    return kb


async def _mk_doc(db, kb_id, content=b"dummy", name="a.docx",
                  status="done") -> Document:
    doc = Document(kb_id=kb_id, filename=name, file_path="x", mime="m",
                   size=len(content), sha256=name, status=status)
    db.add(doc)
    await db.flush()
    db.add(Chunk(document_id=doc.id, kb_id=kb_id, chunk_index=0,
                 content="c", char_len=1, content_hash="h"))
    await db.commit()
    return doc


async def test_save_upload_rejects_ext_and_size(client, auth_headers, db_session):
    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = await _mk_kb(db_session, me.json()["id"])
    with pytest.raises(HTTPException) as e1:
        await doc_ops.save_upload(db_session, kb, filename="a.exe",
                                  payload=b"x", mime=None, ocr_mode="auto",
                                  username="u", action="doc_upload")
    assert e1.value.status_code == 415
    from app.core.config import settings
    big = b"z" * (settings.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    with pytest.raises(HTTPException) as e2:
        await doc_ops.save_upload(db_session, kb, filename="a.pdf",
                                  payload=big, mime=None, ocr_mode="auto",
                                  username="u", action="doc_upload")
    assert e2.value.status_code == 413


async def test_save_upload_dedup_and_dispatch(client, auth_headers, db_session,
                                              monkeypatch):
    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = await _mk_kb(db_session, me.json()["id"])
    calls = []
    monkeypatch.setattr("app.services.doc_ops.process_document",
                        type("T", (), {"delay": staticmethod(
                            lambda i: calls.append(i))}))
    doc = await doc_ops.save_upload(db_session, kb, filename="a.docx",
                                    payload=b"unique-bytes", mime="m",
                                    ocr_mode="auto", username="u",
                                    action="doc_upload")
    assert doc.id and calls == [doc.id]
    with pytest.raises(HTTPException) as e:
        await doc_ops.save_upload(db_session, kb, filename="again.docx",
                                  payload=b"unique-bytes", mime="m",
                                  ocr_mode="auto", username="u",
                                  action="doc_upload")
    assert e.value.status_code == 409


async def test_delete_cascades_chunks_and_file(client, auth_headers, db_session,
                                               tmp_path):
    from app.core.config import settings
    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = await _mk_kb(db_session, me.json()["id"])
    f = tmp_path / "f.docx"
    f.write_bytes(b"x")
    doc = await _mk_doc(db_session, kb.id)
    doc.file_path = str(f)
    await db_session.commit()
    await doc_ops.delete_document(db_session, doc, username="u",
                                  action="doc_delete")
    assert (await db_session.execute(
        select(Chunk).where(Chunk.document_id == doc.id))).scalars().first() is None
    assert (await db_session.get(Document, doc.id)) is None
    assert not f.exists()


async def test_delete_busy_409(client, auth_headers, db_session):
    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = await _mk_kb(db_session, me.json()["id"])
    doc = await _mk_doc(db_session, kb.id, status="parsing")
    with pytest.raises(HTTPException) as e:
        await doc_ops.delete_document(db_session, doc, username="u",
                                      action="doc_delete")
    assert e.value.status_code == 409


async def test_reprocess_resets(client, auth_headers, db_session, monkeypatch):
    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = await _mk_kb(db_session, me.json()["id"])
    doc = await _mk_doc(db_session, kb.id, status="failed")
    doc.error_msg = "boom"
    await db_session.commit()
    calls = []
    monkeypatch.setattr("app.services.doc_ops.process_document",
                        type("T", (), {"delay": staticmethod(
                            lambda i: calls.append(i))}))
    out = await doc_ops.reprocess_document(db_session, doc, username="u",
                                           action="doc_reprocess")
    assert out.status == "pending" and out.error_msg is None
    assert out.chunk_count == 0 and out.page_count is None
    assert calls == [doc.id]
    assert (await db_session.execute(
        select(Chunk).where(Chunk.document_id == doc.id))).scalars().first() is None
```

`tests/test_documents.py` 追加(Web 删除端点;沿用该文件既有 upload 辅助,若名称不同以文件内现有用例为准):

```python
# ---- M11:DELETE /api/documents/{doc_id}(editor+,级联) ----
async def test_web_delete_document(client, auth_headers, db_session):
    kb = await client.post("/api/kbs", json={"name": "删除库"},
                           headers=auth_headers)
    kb_id = kb.json()["id"]
    up = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("a.docx", __import__("io").BytesIO(b"del-me"),
                        "application/octet-stream")},
        data={"ocr": "off"}, headers=auth_headers,
    )
    assert up.status_code == 201
    doc_id = up.json()["id"]
    r = await client.delete(f"/api/documents/{doc_id}", headers=auth_headers)
    assert r.status_code == 204
    assert (await client.get(f"/api/documents/{doc_id}",
                             headers=auth_headers)).status_code == 404


async def test_web_delete_visible_only_404(client, auth_headers):
    r = await client.delete("/api/documents/999999", headers=auth_headers)
    assert r.status_code == 404
```

`tests/test_pipeline.py` 追加(worker 早退):

```python
async def test_run_missing_doc_silent_exit():
    """M11:pending→celery pick 前被删的文档,worker 静默退出不重试。"""
    from app.core.config import settings
    from app.workers.pipeline import _run
    await _run(999999999, settings.DATABASE_URL)  # 不得抛
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_doc_ops.py tests/test_documents.py tests/test_pipeline.py -q`
Expected: FAIL(ModuleNotFoundError: app.services.doc_ops / DELETE 405 / _run 抛 RuntimeError)

- [ ] **Step 3: 实现 doc_ops.py**

Create `app/services/doc_ops.py`(完整):

```python
# backend/app/services/doc_ops.py
"""M11:文档写操作核心(Web/REST/MCP 三面唯一实现)。

校验失败抛 HTTPException(404/409/413/415),三面语义一致;MCP 工具捕获后
转 ToolError。审计与 commit/dispatch 在本模块完成,action 名由调用面传入
(doc_upload vs agent.upload_document),audit_extra 叠加 client/key_name。
"""
import hashlib
import uuid
from pathlib import Path

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.perms import get_kb_perm
from app.models import Chunk, Document, KnowledgeBase, User
from app.services.audit import audit
from app.workers.pipeline import process_document

ALLOWED_EXTS = {".pdf", ".docx", ".xlsx", ".jpg", ".jpeg", ".png"}
BUSY_STATUSES = ("parsing", "chunking", "embedding")


async def visible_kb_or_404(
    db: AsyncSession, user: User, kb_id: int
) -> KnowledgeBase:
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or await get_kb_perm(db, user, kb) is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return kb


async def visible_doc_or_404(
    db: AsyncSession, user: User, doc_id: int
) -> Document:
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    if kb is None or await get_kb_perm(db, user, kb) is None:
        raise HTTPException(status_code=404, detail="document not found")
    return doc


def _detail(doc_file: str, kb_id: int, extra: dict | None) -> dict:
    detail = {"filename": doc_file, "kb_id": kb_id}
    if extra:
        detail.update(extra)
    return detail


async def save_upload(
    db: AsyncSession, kb: KnowledgeBase, *, filename: str, payload: bytes,
    mime: str | None, ocr_mode: str, username: str, action: str,
    audit_extra: dict | None = None,
) -> Document:
    """校验(415/413/409)→ 落盘 → 建行 → 审计 → commit → dispatch。"""
    original = Path(filename or "unnamed").name
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=415,
                            detail=f"unsupported file type: {ext}")
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    if len(payload) > max_bytes:
        raise HTTPException(status_code=413, detail="file too large")
    sha256 = hashlib.sha256(payload).hexdigest()
    dup = await db.execute(
        select(Document).where(Document.kb_id == kb.id,
                               Document.sha256 == sha256)
    )
    if dup.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409,
                            detail="duplicate document in this kb")

    doc_dir = Path(settings.UPLOAD_DIR) / str(kb.id)
    doc_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex[:12]}_{original}"
    file_path = doc_dir / stored_name
    file_path.write_bytes(payload)

    doc = Document(
        kb_id=kb.id, filename=original, file_path=str(file_path),
        mime=mime or "application/octet-stream", size=len(payload),
        sha256=sha256, ocr_mode=ocr_mode,
    )
    db.add(doc)
    await db.flush()
    await audit(db, username, action, f"doc:{doc.id}",
                _detail(original, kb.id, audit_extra) | {"size": len(payload)})
    await db.commit()
    await db.refresh(doc)
    process_document.delay(doc.id)
    return doc


async def delete_document(
    db: AsyncSession, doc: Document, *, username: str, action: str,
    audit_extra: dict | None = None,
) -> None:
    """busy 409 → 删 chunks(pgvector/tsv 随行消失)→ 尽力删磁盘文件
    → 审计 → 删行 → commit(不可逆,调用面前置权限校验)。"""
    if doc.status in BUSY_STATUSES:
        raise HTTPException(status_code=409,
                            detail="document is being processed")
    await db.execute(delete(Chunk).where(Chunk.document_id == doc.id))
    try:
        Path(doc.file_path).unlink(missing_ok=True)
    except OSError:
        logger.warning(f"doc file removal failed: {doc.file_path}")
    await audit(db, username, action, f"doc:{doc.id}",
                _detail(doc.filename, doc.kb_id, audit_extra))
    await db.delete(doc)
    await db.commit()


async def reprocess_document(
    db: AsyncSession, doc: Document, *, username: str, action: str,
    audit_extra: dict | None = None,
) -> Document:
    """busy 409 → 清 chunks → 重置状态 → 审计 → commit → dispatch。"""
    if doc.status in BUSY_STATUSES:
        raise HTTPException(status_code=409,
                            detail="document is being processed")
    await db.execute(delete(Chunk).where(Chunk.document_id == doc.id))
    doc.status = "pending"
    doc.error_msg = None
    doc.chunk_count = 0
    doc.page_count = None
    await audit(db, username, action, f"doc:{doc.id}",
                _detail(doc.filename, doc.kb_id, audit_extra))
    await db.commit()
    await db.refresh(doc)
    process_document.delay(doc.id)
    return doc
```

- [ ] **Step 4: Web documents.py 薄壳化 + 新增 DELETE**

`app/api/documents.py` 改为(完整替换;`ALLOWED_EXTS` 与本地 `_get_visible_*` 移除,统一从 doc_ops 取;audit 已移入 doc_ops,本文件不再直接审计):

```python
# backend/app/api/documents.py
from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Response,
    UploadFile,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.perms import get_kb_perm, has_perm
from app.db.session import get_db
from app.models import Chunk, Document, KnowledgeBase, User
from app.schemas.document import DocumentOut
from app.services import doc_ops

router = APIRouter(tags=["documents"])


async def _require_kb_editor(db: AsyncSession, current: User,
                             kb_id: int) -> None:
    kb = await doc_ops.visible_kb_or_404(db, current, kb_id)
    if not has_perm(await get_kb_perm(db, current, kb), "editor"):
        raise HTTPException(status_code=403,
                            detail="editor permission required")


@router.post("/kbs/{kb_id}/documents", response_model=DocumentOut,
             status_code=201)
async def upload_document(
    kb_id: int,
    file: UploadFile,
    ocr: str = Form("auto"),
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_kb_editor(db, current, kb_id)
    kb = await db.get(KnowledgeBase, kb_id)
    payload = await file.read()
    return await doc_ops.save_upload(
        db, kb, filename=file.filename, payload=payload,
        mime=file.content_type, ocr_mode=ocr,
        username=current.username, action="doc_upload",
    )


@router.get("/kbs/{kb_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    kb_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await doc_ops.visible_kb_or_404(db, current, kb_id)
    result = await db.execute(
        select(Document).where(Document.kb_id == kb_id)
        .order_by(Document.id.desc())
    )
    return list(result.scalars().all())


@router.get("/documents/{doc_id}", response_model=DocumentOut)
async def get_document(
    doc_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await doc_ops.visible_doc_or_404(db, current, doc_id)
```


@router.get("/documents/{doc_id}/chunks")
async def list_chunks(
    doc_id: int,
    page: int = 1,
    page_size: int = 20,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await doc_ops.visible_doc_or_404(db, current, doc_id)
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    total = (await db.execute(
        select(Chunk.id).where(Chunk.document_id == doc_id)
    )).scalars().all()
    rows = (await db.execute(
        select(Chunk).where(Chunk.document_id == doc_id)
        .order_by(Chunk.chunk_index)
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {
        "total": len(total),
        "items": [
            {"id": c.id, "chunk_index": c.chunk_index, "page_no": c.page_no,
             "char_len": c.char_len, "content_preview": c.content[:200]}
            for c in rows
        ],
    }


@router.post("/documents/{doc_id}/reprocess", response_model=DocumentOut)
async def reprocess_document(
    doc_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await doc_ops.visible_doc_or_404(db, current, doc_id)
    kb = await db.get(KnowledgeBase, doc.kb_id)
    if not has_perm(await get_kb_perm(db, current, kb), "editor"):
        raise HTTPException(status_code=403,
                            detail="editor permission required")
    return await doc_ops.reprocess_document(
        db, doc, username=current.username, action="doc_reprocess")


@router.delete("/documents/{doc_id}", status_code=204)
async def delete_document(
    doc_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await doc_ops.visible_doc_or_404(db, current, doc_id)
    kb = await db.get(KnowledgeBase, doc.kb_id)
    if not has_perm(await get_kb_perm(db, current, kb), "editor"):
        raise HTTPException(status_code=403,
                            detail="editor permission required")
    await doc_ops.delete_document(
        db, doc, username=current.username, action="doc_delete")
    return Response(status_code=204)
```

`app/workers/pipeline.py` `_run` 开头改为(缺行早退,替换原 raise):

```python
            doc = await session.get(Document, document_id)
            if doc is None:
                # M11:pending→pick 前被删除的文档——静默退出不重试
                logger.info(f"document {document_id} gone, skip")
                return
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_doc_ops.py tests/test_documents.py tests/test_pipeline.py -q`
Expected: PASS(test_documents 既有用例回归全绿——薄壳化行为不变)

- [ ] **Step 6: 全量回归**

Run: `.venv\Scripts\python -m pytest -q`
Expected: 全绿(uploads/reprocess 既有语义不变)

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/doc_ops.py backend/app/api/documents.py backend/app/workers/pipeline.py backend/tests/test_doc_ops.py backend/tests/test_documents.py backend/tests/test_pipeline.py
git commit -m "feat(doc): shared doc_ops service, web delete endpoint, worker early-exit"
```

---

### Task 4: REST 面文档五端点

**Files:**
- Modify: `backend/app/api/agent.py`
- Test: Create `backend/tests/test_agent_docs_api.py`

**Interfaces:**
- Consumes: Task 2 `_api_key_id`/`require_editor_key`/`Principal.key_role`;Task 3 `doc_ops.*`;既有 `_check_rate`/`_ip`/`audit`。
- Produces(Task 5/9 依赖):
  - `GET /api/agent/kbs/{kb_id}/documents` → `list[DocumentOut]`(读)
  - `GET /api/agent/documents/{doc_id}` → `DocumentOut`(读,轮询用)
  - `POST /api/agent/kbs/{kb_id}/documents`(multipart `file`+`ocr`)→ 201 `DocumentOut`(editor)
  - `DELETE /api/agent/documents/{doc_id}` → 204(editor;busy 409)
  - `POST /api/agent/documents/{doc_id}/reprocess` → `DocumentOut`(editor;busy 409)
  - 审计 action:`agent.list_documents`/`agent.get_document`/`agent.upload_document`/`agent.delete_document`/`agent.reprocess_document`,detail 带 `{client, key_name, kb_id, ...}`

- [ ] **Step 1: 写失败测试**

Create `tests/test_agent_docs_api.py`:

```python
# backend/tests/test_agent_docs_api.py
"""M11 Task4:agent REST 文档五端点(守卫矩阵/全链路/审计)。"""
import io
import json

from sqlalchemy import select, text

from app.models import AuditLog

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def _create_key_role(client, headers, name, role=None):
    payload = {"name": name}
    if role:
        payload["role"] = role
    r = await client.post("/api/auth/keys", json=payload, headers=headers)
    assert r.status_code == 201
    return r.json()["key"]


async def _upload(client, headers, kb_id, content=b"m11", name="a.docx"):
    return await client.post(
        f"/api/agent/kbs/{kb_id}/documents",
        files={"file": (name, io.BytesIO(content), DOCX_MIME)},
        data={"ocr": "auto"}, headers=headers,
    )


async def test_read_only_key_matrix(client, auth_headers):
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(client, auth_headers, "矩阵库")
    ro = {"Authorization": f"Bearer {await _create_key_role(client, auth_headers, '只读')}"}
    # 读操作放行
    r_list = await client.get(f"/api/agent/kbs/{kb_id}/documents", headers=ro)
    assert r_list.status_code == 200
    # 写操作 403 editor_key_required
    r_up = await _upload(client, ro, kb_id)
    assert r_up.status_code == 403
    assert r_up.json()["detail"] == {"code": "editor_key_required"}
    # 不存在的文档:404 优先于能力校验(不泄露存在性)
    r_del = await client.delete("/api/agent/documents/999999", headers=ro)
    assert r_del.status_code == 404
    # editor key 造一个文档再验证 delete/reprocess 的 403
    ed = {"Authorization": f"Bearer {await _create_key_role(client, auth_headers, '编辑', 'editor')}"}
    doc_id = (await _upload(client, ed, kb_id)).json()["id"]
    r_del2 = await client.delete(f"/api/agent/documents/{doc_id}", headers=ro)
    assert r_del2.status_code == 403
    assert r_del2.json()["detail"] == {"code": "editor_key_required"}
    r_rep = await client.post(f"/api/agent/documents/{doc_id}/reprocess",
                              headers=ro)
    assert r_rep.status_code == 403


async def test_editor_key_full_cycle(client, auth_headers, db_session):
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(client, auth_headers, "全链路库")
    ed = {"Authorization": f"Bearer {await _create_key_role(client, auth_headers, '编辑', 'editor')}"}
    up = await _upload(client, ed, kb_id, content=b"m11-cycle")
    assert up.status_code == 201
    doc = up.json()
    got = await client.get(f"/api/agent/documents/{doc['id']}", headers=ed)
    assert got.status_code == 200 and got.json()["filename"] == doc["filename"]
    lst = await client.get(f"/api/agent/kbs/{kb_id}/documents", headers=ed)
    assert doc["id"] in [d["id"] for d in lst.json()]
    dup = await _upload(client, ed, kb_id, content=b"m11-cycle")
    assert dup.status_code == 409
    bad = await _upload(client, ed, kb_id, content=b"x", name="a.exe")
    assert bad.status_code == 415
    rep = await client.post(f"/api/agent/documents/{doc['id']}/reprocess",
                            headers=ed)
    assert rep.status_code == 200
    dele = await client.delete(f"/api/agent/documents/{doc['id']}", headers=ed)
    assert dele.status_code == 204
    assert (await client.get(f"/api/agent/documents/{doc['id']}",
                             headers=ed)).status_code == 404
    db_session.expire_all()
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action.in_([
            "agent.upload_document", "agent.delete_document",
            "agent.reprocess_document", "agent.list_documents",
            "agent.get_document",
        ]))
    )).scalars().all()
    actions = {r.action for r in rows}
    assert {"agent.upload_document", "agent.delete_document"} <= actions
    up_row = next(r for r in rows if r.action == "agent.upload_document")
    assert json.loads(up_row.detail)["client"] == "rest"


async def test_editor_key_viewer_user_403(client, auth_headers, db_session):
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(client, auth_headers, "授权库")
    await client.post("/api/auth/register",
                      json={"username": "m11viewer", "password": "secret123"})
    await db_session.execute(text(
        "UPDATE users SET role = 'editor' WHERE username = 'm11viewer'"))
    await db_session.commit()
    vl = await client.post("/api/auth/login",
                           json={"username": "m11viewer",
                                 "password": "secret123"})
    vh = {"Authorization": f"Bearer {vl.json()['access_token']}"}
    # 库主授予 viewer(字段名以 tests/test_kb_permissions.py 现有用例为准)
    r = await client.put(f"/api/kbs/{kb_id}/permissions",
                         json={"username": "m11viewer", "perm": "viewer"},
                         headers=auth_headers)
    assert r.status_code == 200
    vh_key = await _create_key_role(client, vh, "viewer的editor", "editor")
    hdr = {"Authorization": f"Bearer {vh_key}"}
    up = await _upload(client, hdr, kb_id)
    assert up.status_code == 403
    assert up.json()["detail"] == "editor permission required"
    # 读不受影响
    assert (await client.get(f"/api/agent/kbs/{kb_id}/documents",
                             headers=hdr)).status_code == 200


async def test_jwt_debug_upload_allowed(client, auth_headers):
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(client, auth_headers, "JWT调试库")
    r = await _upload(client, auth_headers, kb_id, content=b"jwt-dbg")
    assert r.status_code == 201


async def test_delete_busy_409(client, auth_headers, db_session):
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(client, auth_headers, "busy库")
    ed = {"Authorization": f"Bearer {await _create_key_role(client, auth_headers, '编辑', 'editor')}"}
    doc_id = (await _upload(client, ed, kb_id)).json()["id"]
    await db_session.execute(text(
        "UPDATE documents SET status = 'parsing' WHERE id = :i"),
        {"i": doc_id})
    await db_session.commit()
    r = await client.delete(f"/api/agent/documents/{doc_id}", headers=ed)
    assert r.status_code == 409
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_agent_docs_api.py -q`
Expected: FAIL(404 not found——端点不存在)

- [ ] **Step 3: 实现 agent.py 五端点**

`app/api/agent.py` 导入区补(`Response`/`UploadFile`/`Form` 并入既有 fastapi 导入行;`select`;`_api_key_id`/`require_editor_key` 并入 core.deps 导入行;其余为新增):

```python
from fastapi import Form, Response, UploadFile
from sqlalchemy import select

from app.core.deps import (
    Principal,
    _api_key_id,
    get_agent_principal,
    require_editor_key,
)
from app.core.perms import get_kb_perm, has_perm
from app.models import Document, KnowledgeBase
from app.schemas.document import DocumentOut
from app.services import doc_ops
```

文件尾部追加(权限序:404→perm→key 能力,见 Global Constraints):

```python
# ---- M11:文档写操作五端点(读:可见即可;写:key=editor ∧ perm≥editor) ----

async def _kb_editor_or_403(db, principal: Principal, kb: KnowledgeBase) -> None:
    if not has_perm(await get_kb_perm(db, principal.user, kb), "editor"):
        raise HTTPException(status_code=403,
                            detail="editor permission required")
    require_editor_key(principal)


@router.get("/kbs/{kb_id}/documents", response_model=list[DocumentOut])
async def agent_list_documents(
    kb_id: int,
    request: Request,
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    await _check_rate(principal)
    await doc_ops.visible_kb_or_404(db, principal.user, kb_id)
    rows = (await db.execute(
        select(Document).where(Document.kb_id == kb_id)
        .order_by(Document.id.desc())
    )).scalars().all()
    await audit(
        db, principal.user.username, "agent.list_documents", "agent",
        {"client": "rest", "key_name": principal.key_name, "kb_id": kb_id,
         "doc_count": len(rows)},
        ip=_ip(request),
    )
    await db.commit()
    return rows


@router.get("/documents/{doc_id}", response_model=DocumentOut)
async def agent_get_document(
    doc_id: int,
    request: Request,
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    await _check_rate(principal)
    doc = await doc_ops.visible_doc_or_404(db, principal.user, doc_id)
    await audit(
        db, principal.user.username, "agent.get_document", "agent",
        {"client": "rest", "key_name": principal.key_name,
         "kb_id": doc.kb_id, "doc_id": doc.id, "status": doc.status},
        ip=_ip(request),
    )
    await db.commit()
    return doc


@router.post("/kbs/{kb_id}/documents", response_model=DocumentOut,
             status_code=201)
async def agent_upload_document(
    kb_id: int,
    request: Request,
    file: UploadFile,
    ocr: str = Form("auto"),
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    await _check_rate(principal)
    kb = await doc_ops.visible_kb_or_404(db, principal.user, kb_id)
    await _kb_editor_or_403(db, principal, kb)
    payload = await file.read()
    return await doc_ops.save_upload(
        db, kb, filename=file.filename, payload=payload,
        mime=file.content_type, ocr_mode=ocr,
        username=principal.user.username, action="agent.upload_document",
        audit_extra={"client": "rest", "key_name": principal.key_name},
    )


@router.delete("/documents/{doc_id}", status_code=204)
async def agent_delete_document(
    doc_id: int,
    request: Request,
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    await _check_rate(principal)
    doc = await doc_ops.visible_doc_or_404(db, principal.user, doc_id)
    kb = await db.get(KnowledgeBase, doc.kb_id)
    await _kb_editor_or_403(db, principal, kb)
    await doc_ops.delete_document(
        db, doc, username=principal.user.username,
        action="agent.delete_document",
        audit_extra={"client": "rest", "key_name": principal.key_name},
    )
    return Response(status_code=204)


@router.post("/documents/{doc_id}/reprocess", response_model=DocumentOut)
async def agent_reprocess_document(
    doc_id: int,
    request: Request,
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    await _check_rate(principal)
    doc = await doc_ops.visible_doc_or_404(db, principal.user, doc_id)
    kb = await db.get(KnowledgeBase, doc.kb_id)
    await _kb_editor_or_403(db, principal, kb)
    return await doc_ops.reprocess_document(
        db, doc, username=principal.user.username,
        action="agent.reprocess_document",
        audit_extra={"client": "rest", "key_name": principal.key_name},
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_agent_docs_api.py tests/test_agent_api.py -q`
Expected: PASS

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/app/api/agent.py backend/tests/test_agent_docs_api.py
git commit -m "feat(agent): rest doc endpoints (list/get/upload/delete/reprocess)"
```

---

### Task 5: MCP 面五工具

**Files:**
- Modify: `backend/app/mcp_server.py`
- Test: `backend/tests/test_mcp.py`

**Interfaces:**
- Consumes: Task 2 `_api_key_id`/`require_editor_key`/`Principal.key_role`(require_editor_key 抛 HTTPException,MCP 侧捕获转 ToolError);Task 3/4 `doc_ops.*`;Task 4 `_create_key_role`(测试辅助,在 test_agent_api.py,Task 2 已建)。
- Produces(Task 9 依赖):MCP 工具 `list_documents(kb_id, limit=50)`、`get_document(doc_id)`、`upload_document(kb_id, filename, content_b64, ocr="auto")`、`delete_document(doc_id)`、`reprocess_document(doc_id)`;错误 `ToolError` 文本含 code(`editor_key_required`/`not_found`/`busy`/`duplicate`/`too_large`/`unsupported_type`/`bad_base64`)。

- [ ] **Step 1: 写失败测试**

`tests/test_mcp.py` 追加(文件已有 `_init`/`_rpc`/`_tool_result`/`ACCEPT`;补 `import base64`):

```python
# ---- M11:文档工具 ----
async def _keyed_session(c, auth_headers, role=None):
    from tests.test_agent_api import _create_key, _create_kb
    if role:
        key = await _create_key_role(c, auth_headers, f"mcp-{role}", role)
    else:
        key = await _create_key(c, auth_headers)
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(c, hdr)
    return hdr, sid


async def test_mcp_doc_tools_in_list(mcp_client, auth_headers):
    hdr, sid = await _keyed_session(mcp_client, auth_headers)
    resp = await mcp_client.post(
        "/mcp", json=_rpc("tools/list", {}, 2),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    names = [t["name"] for t in resp.json()["result"]["tools"]]
    for n in ("list_documents", "get_document", "upload_document",
              "delete_document", "reprocess_document"):
        assert n in names


async def _tool_call(c, hdr, sid, name, args, msg_id):
    resp = await c.post(
        "/mcp",
        json=_rpc("tools/call", {"name": name, "arguments": args}, msg_id),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    return resp.json()


def _is_error(rj) -> bool:
    return rj.get("error") is not None or rj["result"].get("isError", False)


def _err_text(rj) -> str:
    if rj.get("error"):
        return str(rj["error"].get("message", ""))
    return rj["result"]["content"][0]["text"]


async def test_mcp_upload_get_delete_cycle(mcp_client, auth_headers):
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(mcp_client, auth_headers, "MCP写库")
    hdr, sid = await _keyed_session(mcp_client, auth_headers, role="editor")
    b64 = base64.b64encode(b"mcp-upload-dummy").decode()
    rj = await _tool_call(mcp_client, hdr, sid, "upload_document",
                          {"kb_id": kb_id, "filename": "m11.docx",
                           "content_b64": b64}, 3)
    body = _tool_result(rj)
    doc_id = body["id"]
    assert body["filename"] == "m11.docx"
    rj2 = await _tool_call(mcp_client, hdr, sid, "get_document",
                           {"doc_id": doc_id}, 4)
    assert _tool_result(rj2)["status"] in ("pending", "parsing", "chunking",
                                           "embedding", "done", "failed")
    rj3 = await _tool_call(mcp_client, hdr, sid, "list_documents",
                           {"kb_id": kb_id}, 5)
    assert doc_id in [d["id"] for d in _tool_result(rj3)["items"]]
    rj4 = await _tool_call(mcp_client, hdr, sid, "delete_document",
                           {"doc_id": doc_id}, 6)
    assert _tool_result(rj4)["deleted"] is True
    rj5 = await _tool_call(mcp_client, hdr, sid, "get_document",
                           {"doc_id": doc_id}, 7)
    assert _is_error(rj5) and "not_found" in _err_text(rj5)


async def test_mcp_write_guard_and_validation(mcp_client, auth_headers):
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(mcp_client, auth_headers, "MCP守卫库")
    hdr, sid = await _keyed_session(mcp_client, auth_headers)  # read_only
    rj = await _tool_call(mcp_client, hdr, sid, "upload_document",
                          {"kb_id": kb_id, "filename": "a.docx",
                           "content_b64": base64.b64encode(b"x").decode()}, 3)
    assert _is_error(rj) and "editor_key_required" in _err_text(rj)
    # editor key 坏 base64 / 坏扩展名
    hdr2, sid2 = await _keyed_session(mcp_client, auth_headers, role="editor")
    rj2 = await _tool_call(mcp_client, hdr2, sid2, "upload_document",
                           {"kb_id": kb_id, "filename": "a.docx",
                            "content_b64": "!!!not-base64!!!"}, 4)
    assert _is_error(rj2) and "bad_base64" in _err_text(rj2)
    rj3 = await _tool_call(mcp_client, hdr2, sid2, "upload_document",
                           {"kb_id": kb_id, "filename": "a.exe",
                            "content_b64": base64.b64encode(b"x").decode()}, 5)
    assert _is_error(rj3) and "unsupported_type" in _err_text(rj3)
```

顶部还需 `from tests.test_agent_api import _create_key_role  # M11` 若 `_keyed_session` 引用(role 分支)。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_mcp.py -q`
Expected: FAIL(工具不存在)

- [ ] **Step 3: 实现五工具**

`app/mcp_server.py` 导入区补:`import base64`、`from pathlib import Path`、`from fastapi import HTTPException`(已有)、`from app.core.deps import ... require_editor_key`(并入)、`from app.services import doc_ops`、`from sqlalchemy import select`(list 用)。

模块级描述常量(沿用 `_SEARCH_DOC` 模式):

```python
_UPLOAD_DOC = """上传文档到指定知识库并触发解析流水线(异步)。

需 editor 密钥(read_only 密钥将被拒绝)。上传后用 get_document 轮询
status 直到 done/failed。

Args:
    kb_id: 目标知识库 id,须为当前密钥归属用户有 editor 权限的库。
    filename: 文件名(含扩展名;.pdf/.docx/.xlsx/.jpg/.jpeg/.png)。
    content_b64: 文件内容的 base64 编码(解码后不超过服务端 MAX_UPLOAD_MB)。
    ocr: OCR 模式 auto|force|off,默认 auto。

Returns:
    {"id", "filename", "status", "size", "chunk_count", ...}(DocumentOut 字段)。
"""
_LIST_DOC = """列出指定知识库的文档(id/文件名/状态/分块数等)。

Args:
    kb_id: 知识库 id,须为当前密钥有权访问的库。
    limit: 返回条数上限 1~200,默认 50。

Returns:
    {"items": [DocumentOut 字段], "total"}
"""
_GET_DOC = """查询单个文档详情与处理状态(上传后轮询用)。

Args:
    doc_id: 文档 id。

Returns:
    DocumentOut 字段(id/filename/status/error_msg/chunk_count/...)。
"""
_DELETE_DOC = """删除文档(不可逆:连同全部分块、向量与源文件)。

需 editor 密钥;文档处理中(status 为 parsing/chunking/embedding)返回 busy。

Args:
    doc_id: 文档 id。

Returns:
    {"deleted": true}
"""
_REPROCESS_DOC = """重新解析文档(清空旧分块后重跑解析流水线)。

需 editor 密钥;文档处理中返回 busy。

Args:
    doc_id: 文档 id。

Returns:
    DocumentOut 字段(status 回到 pending)。
"""
```

`_e(exc: HTTPException) -> ToolError` 辅助与五工具(追加在 `ask_knowledge_base` 之后):

```python
def _e(e: HTTPException) -> ToolError:
    """doc_ops 的 HTTPException → ToolError(detail 即错误语义)。"""
    return ToolError(str(e.detail))


@mcp.tool(description=_LIST_DOC)
async def list_documents(kb_id: int, limit: int = 50) -> dict:
    p = _principal()
    if not 1 <= limit <= 200:
        raise ToolError("limit must be 1~200")
    async with SessionLocal() as db:
        try:
            await doc_ops.visible_kb_or_404(db, p.user, kb_id)
        except HTTPException as e:
            raise _e(e)
        rows = (await db.execute(
            select(Document)
            .where(Document.kb_id == kb_id)
            .order_by(Document.id.desc()).limit(limit)
        )).scalars().all()
        items = [{"id": d.id, "filename": d.filename, "status": d.status,
                  "size": d.size, "error_msg": d.error_msg,
                  "page_count": d.page_count, "chunk_count": d.chunk_count,
                  "ocr_mode": d.ocr_mode, "ocr_used": d.ocr_used,
                  "created_at": d.created_at.isoformat()} for d in rows]
        await audit(db, p.user.username, "agent.list_documents", "agent",
                    {"client": "mcp", "key_name": p.key_name, "kb_id": kb_id,
                     "doc_count": len(items)},
                    ip=current_client_ip.get())
        await db.commit()
        return {"items": items, "total": len(items)}


@mcp.tool(description=_GET_DOC)
async def get_document(doc_id: int) -> dict:
    p = _principal()
    async with SessionLocal() as db:
        try:
            d = await doc_ops.visible_doc_or_404(db, p.user, doc_id)
        except HTTPException as e:
            raise _e(e)
        await audit(db, p.user.username, "agent.get_document", "agent",
                    {"client": "mcp", "key_name": p.key_name,
                     "kb_id": d.kb_id, "doc_id": d.id, "status": d.status},
                    ip=current_client_ip.get())
        await db.commit()
        return {"id": d.id, "kb_id": d.kb_id, "filename": d.filename,
                "status": d.status, "error_msg": d.error_msg,
                "size": d.size, "sha256": d.sha256,
                "page_count": d.page_count, "chunk_count": d.chunk_count,
                "ocr_mode": d.ocr_mode, "ocr_used": d.ocr_used,
                "created_at": d.created_at.isoformat()}


@mcp.tool(description=_UPLOAD_DOC)
async def upload_document(kb_id: int, filename: str, content_b64: str,
                          ocr: str = "auto") -> dict:
    p = _principal()
    try:
        payload = base64.b64decode(content_b64, validate=True)
    except Exception:
        raise ToolError("bad_base64: content_b64 is not valid base64")
    if not payload:
        raise ToolError("bad_base64: decoded content is empty")
    ext = Path(filename or "").suffix.lower()
    if ext not in doc_ops.ALLOWED_EXTS:
        raise ToolError(f"unsupported_type: {ext}")
    if len(payload) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise ToolError(f"too_large: exceeds {settings.MAX_UPLOAD_MB}MB")
    if ocr not in ("auto", "force", "off"):
        raise ToolError("ocr must be auto|force|off")
    async with SessionLocal() as db:
        # 校验顺序与 REST 一致(spec B):可见性(404)→ perm → key 能力
        try:
            kb = await doc_ops.visible_kb_or_404(db, p.user, kb_id)
        except HTTPException as e:
            raise _e(e)
        if not has_perm(await get_kb_perm(db, p.user, kb), "editor"):
            raise ToolError("editor permission required")
        try:
            require_editor_key(p)
        except HTTPException:
            raise ToolError("editor_key_required: this key is read-only")
        try:
            d = await doc_ops.save_upload(
                db, kb, filename=filename, payload=payload, mime=None,
                ocr_mode=ocr, username=p.user.username,
                action="agent.upload_document",
                audit_extra={"client": "mcp", "key_name": p.key_name},
            )
        except HTTPException as e:
            raise _e(e)
        return {"id": d.id, "filename": d.filename, "status": d.status,
                "size": d.size, "chunk_count": d.chunk_count,
                "created_at": d.created_at.isoformat()}


@mcp.tool(description=_DELETE_DOC)
async def delete_document(doc_id: int) -> dict:
    p = _principal()
    async with SessionLocal() as db:
        # 校验顺序与 REST 一致(spec B):可见性(404)→ perm → key 能力
        try:
            d = await doc_ops.visible_doc_or_404(db, p.user, doc_id)
        except HTTPException as e:
            raise _e(e)
        kb = await db.get(KnowledgeBase, d.kb_id)
        if not has_perm(await get_kb_perm(db, p.user, kb), "editor"):
            raise ToolError("editor permission required")
        try:
            require_editor_key(p)
        except HTTPException:
            raise ToolError("editor_key_required: this key is read-only")
        try:
            await doc_ops.delete_document(
                db, d, username=p.user.username,
                action="agent.delete_document",
                audit_extra={"client": "mcp", "key_name": p.key_name},
            )
        except HTTPException as e:
            raise _e(e)
        return {"deleted": True}


@mcp.tool(description=_REPROCESS_DOC)
async def reprocess_document(doc_id: int) -> dict:
    p = _principal()
    async with SessionLocal() as db:
        # 校验顺序与 REST 一致(spec B):可见性(404)→ perm → key 能力
        try:
            d = await doc_ops.visible_doc_or_404(db, p.user, doc_id)
        except HTTPException as e:
            raise _e(e)
        kb = await db.get(KnowledgeBase, d.kb_id)
        if not has_perm(await get_kb_perm(db, p.user, kb), "editor"):
            raise ToolError("editor permission required")
        try:
            require_editor_key(p)
        except HTTPException:
            raise ToolError("editor_key_required: this key is read-only")
        try:
            out = await doc_ops.reprocess_document(
                db, d, username=p.user.username,
                action="agent.reprocess_document",
                audit_extra={"client": "mcp", "key_name": p.key_name},
            )
        except HTTPException as e:
            raise _e(e)
        return {"id": out.id, "filename": out.filename, "status": out.status,
                "chunk_count": out.chunk_count}
```

导入区补齐:`from sqlalchemy import select`、`from app.core.perms import get_kb_perm, has_perm`、`from app.models import Document, KnowledgeBase`(裸名,上方代码即按裸名写);`require_editor_key` 并入 core.deps 导入;`settings` 顶部已有。

注:`visible_kb_or_404` 对无权库抛 404 detail "knowledge base not found" → ToolError 文本即此——测试对 not_found 的断言只出现在 get_document 已删分支(detail="document not found"),兼容。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_mcp.py -q`
Expected: PASS

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/app/mcp_server.py backend/tests/test_mcp.py
git commit -m "feat(agent): mcp document tools (upload/list/get/delete/reprocess)"
```

---

### Task 6: 前端(密钥角色选择/徽标 + 文档删除)

**Files:**
- Modify: `frontend/src/api/keys.ts`(role 类型)
- Modify: `frontend/src/api/documents.ts`(remove 方法)
- Modify: `frontend/src/pages/KeysPage.vue`(类型单选+徽标列)
- Modify: `frontend/src/pages/DocsPage.vue`(删除按钮+确认框)

**Interfaces:**
- Consumes: Task 1 后端 `role` 字段;Task 3 后端 `DELETE /api/documents/{doc_id}`。
- Produces: 走查可用的 UI(无下游代码依赖)。

- [ ] **Step 1: keys.ts 类型扩展**

`ApiKeyItem` 加 `role: 'read_only' | 'editor'`;`ApiKeyCreatePayload`/`AdminKeyCreatePayload` 各加 `role?: 'read_only' | 'editor'`:

```typescript
export interface ApiKeyItem {
  id: number
  name: string
  key_prefix: string
  role: 'read_only' | 'editor'
  is_active: boolean
  expires_at: string | null
  last_used_at: string | null
  created_at: string
}

export interface ApiKeyCreatePayload {
  name: string
  expires_in_days?: number | null
  role?: 'read_only' | 'editor'
}

export interface AdminKeyCreatePayload {
  user_id: number
  name: string
  expires_in_days?: number | null
  role?: 'read_only' | 'editor'
}
```

- [ ] **Step 2: KeysPage.vue**

script 区:`form` 加 `role: 'read_only' as 'read_only' | 'editor'`;`openCreate` 重置 `form.role = 'read_only'`;`submit` 的 payload 加 `role: form.role`(create 与 createAdmin 都带)。

```typescript
const form = reactive({
  name: '',
  expires: 'permanent' as 'permanent' | '7' | '30' | '90',
  userId: null as number | null,
  role: 'read_only' as 'read_only' | 'editor',
})
```

`submit` 内:

```typescript
    const payload = {
      name: form.name,
      role: form.role,
      expires_in_days:
        form.expires === 'permanent' ? null : Number(form.expires),
    }
```

`openCreate` 内加 `form.role = 'read_only'`;成功后重置段加 `form.role = 'read_only'`。

template 区:表格"状态"列前加类型列;对话框"有效期"前加类型单选:

```vue
        <el-table-column label="类型" width="90" align="center">
          <template #default="{ row }">
            <el-tag :type="row.role === 'editor' ? 'warning' : 'info'" size="small">
              {{ row.role === 'editor' ? '编辑' : '只读' }}
            </el-tag>
          </template>
        </el-table-column>
```

```vue
        <el-form-item label="密钥类型">
          <el-radio-group v-model="form.role">
            <el-radio value="read_only">只读(检索/问答)</el-radio>
            <el-radio value="editor">编辑(可维护文档)</el-radio>
          </el-radio-group>
        </el-form-item>
```

页头 description 更新为:`供外部 Agent(MCP / REST)访问知识库的凭证;编辑型密钥还可上传/删除/重解析文档。`

- [ ] **Step 3: documents.ts 加 remove**

```typescript
  async remove(docId: number): Promise<void> {
    await http.delete(`/documents/${docId}`)
  },
```

- [ ] **Step 4: DocsPage.vue 删除**

script 区(`ElMessageBox` 并入 element-plus 导入):

```typescript
import { ElMessage, ElMessageBox, type UploadRawFile, type UploadRequestOptions } from 'element-plus'

const DELETABLE = ['pending', 'done', 'failed']
const deleting = ref<number | null>(null)

async function delDoc(row: DocumentItem) {
  try {
    await ElMessageBox.confirm(
      `确定删除「${row.filename}」?该操作不可恢复,将同时删除其全部分块与向量。`,
      '删除文档',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  deleting.value = row.id
  try {
    await documentsApi.remove(row.id)
    ElMessage.success(`「${row.filename}」已删除`)
    await load()
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '删除失败')
  } finally {
    deleting.value = null
  }
}
```

template 操作列(width 170 → 230),追加删除按钮:

```vue
          <el-button
            v-if="canEdit && DELETABLE.includes(row.status)"
            link
            type="danger"
            :loading="deleting === row.id"
            @click="delDoc(row)"
          >
            删除
          </el-button>
```

- [ ] **Step 5: 构建 + Commit**

Run: `cd frontend && npm run build`
Expected: vue-tsc 零错误,vite build 成功。

```bash
git add frontend/src/api/keys.ts frontend/src/api/documents.ts frontend/src/pages/KeysPage.vue frontend/src/pages/DocsPage.vue
git commit -m "feat(web): key role picker/badge and document delete ui"
```

---

### Task 7: 配额硬化与余量查询(M10 小项①⑥)

**Files:**
- Modify: `backend/app/services/agent_ratelimit.py`(pipeline 原子化 + `quota_remaining`)
- Modify: `backend/app/schemas/agent.py`(`AgentQuotaOut`)
- Modify: `backend/app/api/agent.py`(`GET /agent/quota`)
- Modify: `backend/app/mcp_server.py`(`get_quota` 工具)
- Test: `backend/tests/test_agent_ratelimit.py`、`backend/tests/test_agent_docs_api.py`、`backend/tests/test_mcp.py`

**Interfaces:**
- Consumes: Task 2 `_api_key_id`。
- Produces:
  - `quota_consume` 内 INCRBY+EXPIRE 合入单条 redis `pipeline(transaction=False)`
  - `async def quota_remaining(key_id: int) -> dict`(键 `used: int|None`、`limit: int`、`reset_at: str` ISO 本地时区次日零点;禁用时 `limit=0, used=None`;Redis 异常 `used=None`)
  - `GET /api/agent/quota` → `AgentQuotaOut`(仅 api_key;JWT 403 `{"detail": "api key principal required"}`;不限流不审计——轮询不烧限流预算)
  - MCP `get_quota()` 工具(同语义,JWT 主体 ToolError)

- [ ] **Step 1: 写失败测试**

`tests/test_agent_ratelimit.py`:`FakeRedis` 追加 pipeline 支持(既有类体内加方法+内部类):

```python
class FakePipeline:
    def __init__(self, r: "FakeRedis"):
        self.r = r
        self.ops: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def incrby(self, key, amount):
        self.ops.append(("incrby", key, amount))
        return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))
        return self

    async def execute(self):
        for op in self.ops:
            if op[0] == "incrby":
                await self.r.incrby(op[1], op[2])
            else:
                await self.r.expire(op[1], op[2])
        self.r.pipeline_calls += 1
        return True
```

`FakeRedis.__init__` 加 `self.pipeline_calls = 0`;类体加:

```python
    def pipeline(self, transaction: bool = True):
        return FakePipeline(self)
```

用例追加:

```python
# ---- M11:小项① pipeline 原子化 + 小项⑥ 余量查询 ----

async def test_quota_consume_uses_pipeline(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_ASK_DAILY_TOKENS", 100)
    await agent_ratelimit.quota_consume(31, 42)
    assert fake_redis.pipeline_calls == 1
    key = next(k for k in fake_redis.kv if k.startswith("agent_tq:31:"))
    assert fake_redis.kv[key] == 42


async def test_quota_remaining_values(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_ASK_DAILY_TOKENS", 100)
    await agent_ratelimit.quota_consume(32, 30)
    out = await agent_ratelimit.quota_remaining(32)
    assert out["used"] == 30 and out["limit"] == 100
    assert out["reset_at"]  # ISO 字符串


async def test_quota_remaining_degrades_open(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_ASK_DAILY_TOKENS", 100)
    monkeypatch.setattr(agent_ratelimit, "get_redis",
                        lambda: FakeRedis(fail=True))
    out = await agent_ratelimit.quota_remaining(33)
    assert out["used"] is None and out["limit"] == 100


async def test_quota_remaining_disabled(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_ASK_DAILY_TOKENS", 0)
    out = await agent_ratelimit.quota_remaining(34)
    assert out["limit"] == 0 and out["used"] is None
```

`tests/test_agent_docs_api.py` 追加:

```python
async def test_agent_quota_endpoint(client, auth_headers):
    from app.core.config import settings

    key = await _create_key_role(client, auth_headers, "配额查询")
    r = await client.get("/api/agent/quota",
                         headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    body = r.json()
    assert body["limit"] == settings.AGENT_ASK_DAILY_TOKENS
    assert body["used"] is None or isinstance(body["used"], int)
    assert body["reset_at"]
    r2 = await client.get("/api/agent/quota", headers=auth_headers)
    assert r2.status_code == 403
    assert r2.json()["detail"] == "api key principal required"
```

`tests/test_mcp.py` 追加:

```python
async def test_mcp_get_quota(mcp_client, auth_headers):
    hdr, sid = await _keyed_session(mcp_client, auth_headers)
    rj = await _tool_call(mcp_client, hdr, sid, "get_quota", {}, 9)
    body = _tool_result(rj)
    assert "limit" in body and "reset_at" in body
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_agent_ratelimit.py tests/test_agent_docs_api.py tests/test_mcp.py -q`
Expected: FAIL(pipeline_calls 不存在/404/工具不存在)

- [ ] **Step 3: 实现**

`app/services/agent_ratelimit.py`:`quota_consume` 主体替换 + 追加 `quota_remaining`:

```python
async def quota_consume(key_id: int, tokens: int) -> None:
    """ask 完成后累计;软上限(check 前置,单次可小幅越限,spec 明示)。
    M11 小项①:INCRBY+EXPIRE 合入单条 pipeline(非事务,减往返;
    原子性诉求有限——两命令间崩溃最多少设一次 TTL,次日 key 换日后自愈)。"""
    if settings.AGENT_ASK_DAILY_TOKENS <= 0:
        return
    rkey, ttl = _quota_key(key_id)
    try:
        r = get_redis()
        async with r.pipeline(transaction=False) as p:
            p.incrby(rkey, tokens)
            p.expire(rkey, ttl)
            await p.execute()
    except Exception:
        pass


async def quota_remaining(key_id: int) -> dict:
    """M11 小项⑥:今日余量;Redis 异常或禁用时 used=None(降级语义)。
    reset_at 为本地时区次日零点 ISO 串(与 _quota_key 日界同基准)。"""
    limit = settings.AGENT_ASK_DAILY_TOKENS
    now = _dt.datetime.now()
    tomorrow = (now + _dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    reset_at = tomorrow.isoformat(timespec="seconds")
    if limit <= 0:
        return {"used": None, "limit": 0, "reset_at": reset_at}
    rkey, _ = _quota_key(key_id)
    try:
        used = int(await get_redis().get(rkey) or 0)
        return {"used": used, "limit": limit, "reset_at": reset_at}
    except Exception:
        return {"used": None, "limit": limit, "reset_at": reset_at}
```

`app/schemas/agent.py` 追加:

```python
class AgentQuotaOut(BaseModel):
    used: int | None  # None=禁用或 Redis 降级
    limit: int
    reset_at: str
```

`app/api/agent.py` 端点(不挂 `_check_rate`,理由见 Interfaces;`quota_remaining` 导入并入 agent_ratelimit 导入行):

```python
@router.get("/quota", response_model=AgentQuotaOut)
async def agent_quota(
    principal: Principal = Depends(get_agent_principal),
):
    """M11 小项⑥:今日 token 配额余量(仅 api_key;轮询不烧限流预算)。"""
    key_id = _api_key_id(principal)
    if key_id is None:
        raise HTTPException(status_code=403,
                            detail="api key principal required")
    return await quota_remaining(key_id)
```

`app/mcp_server.py` 追加工具(描述常量同模式):

```python
_QUOTA_DOC = """查询当前密钥今日 ask token 配额余量。

Returns:
    {"used", "limit", "reset_at"};used=null 表示禁用或 Redis 降级。
"""


@mcp.tool(description=_QUOTA_DOC)
async def get_quota() -> dict:
    key_id = _api_key_id(_principal())
    if key_id is None:
        raise ToolError("api key principal required")
    from app.services.agent_ratelimit import quota_remaining
    return await quota_remaining(key_id)
```

(`_api_key_id` 并入 core.deps 导入;`quota_remaining` 顶部导入亦可,与既有 `quota_check/quota_consume` 导入行合并。)

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_agent_ratelimit.py tests/test_agent_docs_api.py tests/test_mcp.py -q`
Expected: PASS

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/app/services/agent_ratelimit.py backend/app/schemas/agent.py backend/app/api/agent.py backend/app/mcp_server.py backend/tests/test_agent_ratelimit.py backend/tests/test_agent_docs_api.py backend/tests/test_mcp.py
git commit -m "feat(agent): quota pipeline atomization and remaining endpoint/tool"
```

---

### Task 8: 杂项清偿(M10 小项③④⑤:删 api_router、ask 失败日志、CitationOut)

**Files:**
- Modify: `backend/app/api/__init__.py`(删模块级 `api_router`)
- Modify: `backend/app/api/agent.py`(ask 异常 `logger.exception`)
- Modify: `backend/app/mcp_server.py`(同上 + citations 出口强类型)
- Modify: `backend/app/schemas/agent.py`(`CitationOut`)
- Test: `backend/tests/test_agent_ask.py`

**Interfaces:**
- Consumes: 无新依赖。
- Produces: `CitationOut(number, chunk_id, document_id, filename, page_no, excerpt)`;`AgentAskOut.citations: list[CitationOut]`;`api/__init__.py` 不再导出 `api_router`(全库唯一消费方 main.py 用 `build_api_router`)。

- [ ] **Step 1: 写失败测试**

`tests/test_agent_ask.py` 追加(该文件已有 `_create_kb`/`_create_key` 或等价辅助,名称不同则按文件内既有写法对齐):

```python
# ---- M11:小项④ ask 失败应用级日志 + 小项⑤ CitationOut 强类型 ----
async def test_ask_500_logs_exception(client, auth_headers, monkeypatch):
    from tests.test_agent_api import _create_kb, _create_key

    kb_id = await _create_kb(client, auth_headers, "500库")
    key = await _create_key(client, auth_headers)

    async def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.agent_facade.agent_ask", boom)
    import app.api.agent as agent_mod

    calls = []

    class FakeLogger:
        def exception(self, msg, *a, **k):
            calls.append(msg)

    monkeypatch.setattr(agent_mod, "logger", FakeLogger())
    r = await client.post(
        "/api/agent/ask", json={"kb_ids": [kb_id], "query": "q"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 500
    assert calls  # 应用级日志已记,不再只靠 uvicorn 兜底


async def test_ask_citations_strongly_typed(client, auth_headers, monkeypatch):
    from tests.test_agent_api import _create_kb, _create_key

    kb_id = await _create_kb(client, auth_headers, "引用库")
    key = await _create_key(client, auth_headers)

    async def fake_ask(db, user, kb_ids, query, rerank):
        from app.services.agent_facade import AskOutcome
        return AskOutcome(
            answer="a",
            citations=[{"number": 1, "chunk_id": 2, "document_id": 3,
                        "filename": "f.pdf", "page_no": 1, "excerpt": "e",
                        "junk": "dropped"}],
            refused=False, tokens_used=5, elapsed_ms=1,
        )

    monkeypatch.setattr("app.services.agent_facade.agent_ask", fake_ask)
    r = await client.post(
        "/api/agent/ask", json={"kb_ids": [kb_id], "query": "q"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 200
    assert r.json()["citations"] == [
        {"number": 1, "chunk_id": 2, "document_id": 3, "filename": "f.pdf",
         "page_no": 1, "excerpt": "e"}
    ]  # 多余键被 CitationOut 丢弃
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_agent_ask.py -q`
Expected: FAIL(新用例失败:500 无日志断言不过/引用带 junk 键)

- [ ] **Step 3: 实现**

`app/api/agent.py`:顶部 `from loguru import logger`;`agent_ask` 端点 facade 调用处包异常:

```python
    try:
        outcome = await agent_facade.agent_ask(
            db, principal.user, payload.kb_ids, payload.query, payload.rerank,
        )
    except agent_facade.AgentKbDenied as e:
        raise HTTPException(
            status_code=403,
            detail={"code": "kb_forbidden", "denied_kb_ids": e.denied_kb_ids},
        )
    except Exception:
        logger.exception("agent ask failed")
        raise HTTPException(status_code=500, detail="internal error")
```

`app/mcp_server.py`:顶部 `from loguru import logger`;`ask_knowledge_base` 的 facade 调用包:

```python
        try:
            outcome = await agent_facade.agent_ask(
                db, p.user, kb_ids, query, rerank,
            )
        except agent_facade.AgentKbDenied as e:
            raise ToolError(f"kb_forbidden, denied_kb_ids={e.denied_kb_ids}")
        except Exception:
            logger.exception("agent ask failed")
            raise ToolError("internal error")
```

`app/schemas/agent.py`:

```python
class CitationOut(BaseModel):
    """M11 小项⑤:引用强类型(与 Web 端 citations 结构一致,多余键丢弃)。"""
    number: int
    chunk_id: int
    document_id: int
    filename: str
    page_no: int | None
    excerpt: str


class AgentAskOut(BaseModel):
    answer: str
    citations: list[CitationOut]
    refused: bool
    tokens_used: int
    elapsed_ms: int
```

`app/mcp_server.py` `ask_knowledge_base` 返回的 citations 同步强类型化(导入 `CitationOut`):

```python
        return {"answer": outcome.answer,
                "citations": [CitationOut(**c).model_dump()
                              for c in outcome.citations],
                "refused": outcome.refused,
                "tokens_used": outcome.tokens_used,
                "elapsed_ms": outcome.elapsed_ms}
```

`app/api/__init__.py`:删除末行 `api_router = build_api_router()`(grep 确认:`findstr /s /n "api_router" backend\app` 仅剩 `build_api_router` 定义与 main.py 工厂调用)。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_agent_ask.py tests/test_agent_api.py tests/test_mcp.py -q`
Expected: PASS

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿(小项③ 删除后 import 链无破坏)。

```bash
git add backend/app/api/__init__.py backend/app/api/agent.py backend/app/mcp_server.py backend/app/schemas/agent.py backend/tests/test_agent_ask.py
git commit -m "refactor(agent): drop module-level api_router, ask failure logging, CitationOut"
```

---

### Task 9: README 增补 + 无头验收脚本 + MCP 走查扩展

**Files:**
- Modify: `E:\Projects\AIRag\README.md`(外部 Agent 接入指南)
- Create: `backend/scripts/m11_acceptance.py`
- Modify: `backend/scripts/m91_mcp_walkthrough.py`(追加文档工具走查)

**Interfaces:**
- Consumes: Task 1-8 全部成果;`m10_acceptance.py` 的脚本骨架(check/skip/summary_and_exit、make_user/login/cleanup、make_pdf_bytes)。
- Produces: 真栈验收 8 项 + MCP 走查新增 4 步;README 双面文档。

- [ ] **Step 1: README 增补**

定位 `README.md` 第 48 行附近"可用工具"段,替换为:

```markdown
可用工具:`list_knowledge_bases` / `search_knowledge_base` / `ask_knowledge_base`(直接生成答案+引用,单轮无上下文,内部多步 LLM 耗时 40~90 秒,客户端超时请设充足,如 Claude Code 的 `MCP_TIMEOUT`);文档维护工具 `list_documents` / `get_document` / `upload_document`(base64 内容,解码后不超过 MAX_UPLOAD_MB;上传后轮询 `get_document` 至 done/failed)/ `delete_document`(不可逆)/ `reprocess_document`;配额查询 `get_quota`。后三者及 `upload_document` 需**编辑型密钥**(创建密钥时类型选"编辑";存量只读密钥如需写操作请重新铸造)。REST 面同构:`GET/POST /api/agent/kbs/{kb_id}/documents`、`GET/DELETE /api/agent/documents/{doc_id}`、`POST /api/agent/documents/{doc_id}/reprocess`、`GET /api/agent/quota`。
```

- [ ] **Step 2: 写 m11_acceptance.py**

Create `backend/scripts/m11_acceptance.py`(骨架抄 m10,核心流程如下;真栈 8001 需 start_dev.bat + worker + Redis):

```python
"""M11 无头验收脚本(真栈:http://127.0.0.1:8001 + worker + Redis)。

用法(backend 目录,项目 venv,start_dev.bat 已起服务):
    .venv\\Scripts\\python scripts\\m11_acceptance.py

覆盖:editor key 上传→轮询 done→search 命中→删除→search 不再命中 /
read_only key 写 403 / 重复上传 409 / 非白名单扩展名 415 /
busy 409(可选) / 配额端点 / 审计落库。
"""
import asyncio
import base64
import datetime as dt
import io
import sys
import time
import uuid

import httpx

BASE = "http://127.0.0.1:8001"
API = f"{BASE}/api"
TIMEOUT = httpx.Timeout(120.0)
RESULTS = {"pass": [], "fail": [], "skip": []}

SUFFIX = uuid.uuid4().hex[:6]
FACT = f"镇海灯塔编号ZT-{SUFFIX}的塔身高四十二米"
FACT_Q = f"镇海灯塔编号ZT-{SUFFIX}的塔身高度是多少米?"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def check(name, cond, detail=""):
    (RESULTS["pass"] if cond else RESULTS["fail"]).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f"  {detail}" if detail and not cond else ""))


def summary_and_exit():
    total = sum(len(v) for v in RESULTS.values())
    print(f"\nM11 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
    if RESULTS["fail"]:
        print("FAILED:", *RESULTS["fail"], sep="\n  - ")
        sys.exit(1)


def _nullpool_sessionmaker():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import settings

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def promote_roles(usernames: dict[str, str]) -> None:
    from sqlalchemy import text

    engine, maker = _nullpool_sessionmaker()
    try:
        async with maker() as s:
            for username, role in usernames.items():
                await s.execute(
                    text("UPDATE users SET role = :r WHERE username = :u"),
                    {"r": role, "u": username},
                )
            await s.commit()
    finally:
        await engine.dispose()


async def cleanup(user_ids, kb_ids, usernames) -> None:
    from sqlalchemy import text

    engine, maker = _nullpool_sessionmaker()
    try:
        async with maker() as s:
            for uid in user_ids:
                await s.execute(
                    text("DELETE FROM api_keys WHERE user_id = :u"), {"u": uid})
            for kid in kb_ids:
                await s.execute(
                    text("DELETE FROM chunks WHERE kb_id = :k"), {"k": kid})
                await s.execute(
                    text("DELETE FROM documents WHERE kb_id = :k"), {"k": kid})
                await s.execute(
                    text("DELETE FROM kb_permissions WHERE kb_id = :k"),
                    {"k": kid})
                await s.execute(
                    text("DELETE FROM knowledge_bases WHERE id = :k"),
                    {"k": kid})
            for name in usernames:
                await s.execute(
                    text("DELETE FROM users WHERE username = :n"), {"n": name})
            await s.commit()
    finally:
        await engine.dispose()


async def make_user(c: httpx.AsyncClient, role="editor"):
    name = f"m11_{SUFFIX}_{role}"
    r = await c.post(f"{API}/auth/register",
                     json={"username": name, "password": "secret123"})
    r.raise_for_status()
    await promote_roles({name: role})
    return name, r.json()["id"]


async def login(c: httpx.AsyncClient, username: str) -> dict:
    r = await c.post(f"{API}/auth/login",
                     json={"username": username, "password": "secret123"})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def create_key(c, jwt, name, role="read_only") -> str:
    r = await c.post(f"{API}/auth/keys",
                     json={"name": name, "role": role,
                           "expires_in_days": 1},
                     headers=jwt)
    r.raise_for_status()
    return r.json()["key"]


def make_docx_bytes() -> bytes:
    """纯文本 .docx 最小构造:用 python-docx(backend venv 已装,M2 解析依赖)。"""
    from docx import Document as Dx

    dx = Dx()
    dx.add_paragraph(FACT)
    buf = io.BytesIO()
    dx.save(buf)
    return buf.getvalue()


async def main() -> None:
    user_ids, kb_ids, usernames = [], [], []
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            name, uid = await make_user(c)
            usernames.append(name)
            user_ids.append(uid)
            jwt = await login(c, name)
            r = await c.post(f"{API}/kbs", json={"name": f"m11验收库{SUFFIX}"},
                             headers=jwt)
            r.raise_for_status()
            kb_id = r.json()["id"]
            kb_ids.append(kb_id)

            editor_key = await create_key(c, jwt, "m11编辑", "editor")
            ro_key = await create_key(c, jwt, "m11只读")
            ek = {"Authorization": f"Bearer {editor_key}"}
            rk = {"Authorization": f"Bearer {ro_key}"}

            # ① editor key 上传真实 .docx → 201
            content = make_docx_bytes()
            up = await c.post(
                f"{API}/agent/kbs/{kb_id}/documents",
                files={"file": (f"m11-{SUFFIX}.docx", content,
                                DOCX_MIME)},
                data={"ocr": "off"}, headers=ek,
            )
            check("upload 201", up.status_code == 201, str(up.text[:200]))
            doc_id = up.json()["id"]

            # ② 轮询状态至 done(真栈 celery worker)
            status = None
            for _ in range(60):
                g = await c.get(f"{API}/agent/documents/{doc_id}", headers=ek)
                status = g.json()["status"]
                if status in ("done", "failed"):
                    break
                await asyncio.sleep(2)
            check("poll done", status == "done", f"status={status}")

            # ③ search 命中新内容
            s1 = await c.post(f"{API}/agent/search",
                              json={"kb_ids": [kb_id], "query": FACT_Q},
                              headers=ek)
            hit_ids = {h["document_id"] for h in s1.json()["hits"]}
            check("search hits new doc", doc_id in hit_ids,
                  f"hits={s1.json()['total']}")

            # ④ read_only key 写操作 403
            ro_up = await c.post(
                f"{API}/agent/kbs/{kb_id}/documents",
                files={"file": ("ro.docx", content, DOCX_MIME)},
                data={"ocr": "off"}, headers=rk,
            )
            check("read_only 403", ro_up.status_code == 403
                  and ro_up.json()["detail"]["code"] == "editor_key_required",
                  str(ro_up.text[:200]))

            # ⑤ 同内容重传 409(SHA256 去重)
            dup = await c.post(
                f"{API}/agent/kbs/{kb_id}/documents",
                files={"file": (f"dup-{SUFFIX}.docx", content, DOCX_MIME)},
                data={"ocr": "off"}, headers=ek,
            )
            check("duplicate 409", dup.status_code == 409)

            # ⑥ 非白名单扩展名 415
            bad = await c.post(
                f"{API}/agent/kbs/{kb_id}/documents",
                files={"file": ("bad.exe", b"x", "application/octet-stream")},
                headers=ek,
            )
            check("bad ext 415", bad.status_code == 415)

            # ⑦ 配额端点
            q = await c.get(f"{API}/agent/quota", headers=ek)
            check("quota endpoint", q.status_code == 200
                  and "limit" in q.json(), str(q.text[:200]))

            # ⑧ 删除 → search 不再命中
            d = await c.delete(f"{API}/agent/documents/{doc_id}", headers=ek)
            check("delete 204", d.status_code == 204, str(d.text[:200]))
            s2 = await c.post(f"{API}/agent/search",
                              json={"kb_ids": [kb_id], "query": FACT_Q},
                              headers=ek)
            hit_ids2 = {h["document_id"] for h in s2.json()["hits"]}
            check("search after delete", doc_id not in hit_ids2)

            # ⑨ 审计落库
            from sqlalchemy import select

            from app.models import AuditLog

            engine, maker = _nullpool_sessionmaker()
            try:
                async with maker() as s:
                    rows = (await s.execute(
                        select(AuditLog.action).where(
                            AuditLog.action.in_([
                                "agent.upload_document",
                                "agent.delete_document",
                            ]))
                    )).scalars().all()
            finally:
                await engine.dispose()
            check("audit rows", set(rows) >= {"agent.upload_document",
                                              "agent.delete_document"},
                  f"actions={rows}")
        finally:
            await cleanup(user_ids, kb_ids, usernames)
    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: 扩展 m91_mcp_walkthrough.py**

读 `backend/scripts/m91_mcp_walkthrough.py`,在其既有流程末尾(M10 ask 走查段之后)接入下述自包含函数(辅助函数名若与脚本既有命名冲突,加 `m11_` 前缀;调用处传 base/key/editor key 明文/kb_id——key 用脚本既有铸造流程新铸 editor 型):

```python
async def walk_m11_doc_tools(base: str, editor_key: str, kb_id: int) -> None:
    """M11:文档工具走查(upload→get→list→delete,base64 真文件)。"""
    import base64
    import io

    import httpx
    from docx import Document as Dx

    ACCEPT = "application/json, text/event-stream"
    headers = {"Authorization": f"Bearer {editor_key}",
               "Accept": ACCEPT}
    msg_id = 100
    sid = None

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as c:
        init = {"jsonrpc": "2.0", "id": msg_id, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26",
                           "capabilities": {},
                           "clientInfo": {"name": "m11", "version": "0"}}}
        r = await c.post(f"{base}/mcp", json=init, headers=headers)
        sid = r.headers.get("mcp-session-id")
        await c.post(f"{base}/mcp",
                     json={"jsonrpc": "2.0",
                           "method": "notifications/initialized"},
                     headers={**headers, "mcp-session-id": sid})

        async def call(name, args):
            nonlocal msg_id
            msg_id += 1
            r = await c.post(
                f"{base}/mcp",
                json={"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                      "params": {"name": name, "arguments": args}},
                headers={**headers, "mcp-session-id": sid},
            )
            rj = r.json()
            if rj.get("error") or rj["result"].get("isError"):
                return None, rj
            return __import__("json").loads(
                rj["result"]["content"][0]["text"]), rj

        dx = Dx()
        dx.add_paragraph(f"m11-mcp-walkthrough-{kb_id} 锚点内容")
        buf = io.BytesIO()
        dx.save(buf)
        b64 = base64.b64encode(buf.getvalue()).decode()

        body, _ = await call("upload_document",
                             {"kb_id": kb_id,
                              "filename": f"m11-mcp-{kb_id}.docx",
                              "content_b64": b64})
        print("PASS mcp upload" if body and body.get("id")
              else f"FAIL mcp upload: {_}")
        doc_id = body["id"]

        got, _ = await call("get_document", {"doc_id": doc_id})
        print("PASS mcp get" if got and got.get("status") else
              f"FAIL mcp get: {_}")

        lst, _ = await call("list_documents", {"kb_id": kb_id})
        hit = doc_id in [d["id"] for d in (lst or {}).get("items", [])]
        print("PASS mcp list" if hit else f"FAIL mcp list: {lst}")

        dele, _ = await call("delete_document", {"doc_id": doc_id})
        print("PASS mcp delete" if dele and dele.get("deleted") else
              f"FAIL mcp delete: {dele}")
```

- [ ] **Step 4: 真栈跑验收**

前置:`start_dev.bat`(后端 8001 + worker)已起、Redis 已起、alembic 已升级(Task 1)。
Run(backend 目录):`.venv\Scripts\python scripts\m11_acceptance.py`
Expected: `M11 ACCEPTANCE: 9/9 PASS`(真实 LLM/嵌入/worker 链路)

- [ ] **Step 5: Commit**

```bash
git add README.md backend/scripts/m11_acceptance.py backend/scripts/m91_mcp_walkthrough.py
git commit -m "test(agent): m11 acceptance script, mcp walkthrough extension, readme"
```

---

### Task 10: 收尾全量回归 + 执行记录

**Files:**
- Modify: `docs/superpowers/plans/2026-09-18-airag-m11-doc-write-editor-key.md`(本文件,追加执行记录附录)

**Interfaces:**
- Consumes: Task 1-9 全部提交。
- Produces: 可交付状态(全绿基线 + 执行记录),供用户走查后推送。

- [ ] **Step 1: 后端全量**

Run: `cd backend && .venv\Scripts\python -m pytest -q`
Expected: 全绿(记录用例数,预计 219 基线 + 约 30 新增)

- [ ] **Step 2: 前端构建**

Run: `cd frontend && npm run build`
Expected: 零错误

- [ ] **Step 3: 真栈冒烟**

确认 `start_dev.bat` 起的后端 8001 合并全部改动后重启(--reload 会自热载;若 8001 被旧实例占用,按 M10 记录先杀再起);重跑 `.venv\Scripts\python scripts\m11_acceptance.py` 一次确认 9/9。

- [ ] **Step 4: 执行记录附录**

在本文件末尾追加「执行记录」附录:每任务的实际裁决/偏差/修复波(格式对齐 `2026-09-18-airag-m10-agent-ask.md` 执行记录),含最终测试数、验收结果、遗留项。

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/plans/2026-09-18-airag-m11-doc-write-editor-key.md
git commit -m "docs(agent): m11 execution record"
```

---

## 交付后流程(计划外,用户主导)

1. 用户 ZCode/浏览器走查:KeysPage 铸 editor key、DocsPage 删除、MCP 真客户端(Inspector/ZCode)五工具、REST curl。
2. 走查通过后推送 origin/main;更新项目记忆(M11 收官 + M12 候选)。

## Self-Review 记录(计划完成后自查)

- Spec 覆盖:A(模型/迁移/铸造)=Task 1;B(传播/守卫/审计)=Task 2+4+5;C(doc_ops/worker 早退)=Task 3;D(REST 5 端点)=Task 4;E(MCP 5 工具/README)=Task 5+9;F(Web 删除+前端)=Task 3+6;G 小项①②③④⑤⑥=Task 7(①⑥)/2(②)/8(③④⑤);测试与验收=各任务+Task 9/10;风险项(pending 竞态=Task 3 worker 早退;base64 膨胀=Task 5 解码后校验+Task 9 真栈;eval_sets 陈旧引用=不改代码,spec 已明示)。无缺口。
- 类型一致性:`issue_api_key(..., role)`、`Principal.key_role`、`_api_key_id`/`require_editor_key`、`doc_ops.save_upload/delete_document/reprocess_document/visible_*`、`quota_remaining`、`CitationOut/AgentQuotaOut` 各任务间签名一致;测试辅助 `_create_key_role` 在 Task 2 建、Task 4/7 复用(test_agent_docs_api.py 内自带独立定义,避免跨文件依赖漂移)。
- 占位符:无 TBD/TODO;Task 9 走查扩展函数为自包含代码,注明与既有脚本命名的接线方式。
