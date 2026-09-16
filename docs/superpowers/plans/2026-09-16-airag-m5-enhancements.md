# AIRag M5 增强实施计划(OOCR / Agentic / 审计 / 评估)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 落地 M5 四大增强(MinerU 云 API OCR、查询改写+CRAG 图节点、审计日志+会话导出、检索评估集 CLI)加 Minor 清偿与 checkpointer 接线。

**Architecture:** 后端先行:迁移(audit_logs 表 + documents.ocr 两列)→ audit helper/API → LangGraph 加 rewrite/grade/transform 三节点(恒定拓扑、运行时直通)→ ask.py 接线(history/checkpointer/tag 过滤/ask 审计)→ OCR(parsing 层 maybe_ocr 决策 + MinerU 同步客户端,Celery 内阻塞)→ 审计挂点 + KB 文档数聚合 → 会话导出 → 评估 CLI。前端跟进:审计页/导出按钮/OCR 标记/文档数列/渲染节流等 Minor。最后无头验收。

**Tech Stack:** FastAPI + SQLAlchemy(async)+ LangGraph 1.x + Celery + Vue3/Element Plus;MinerU 云 API v4(httpx);无新框架依赖(httpx 从 dev 提主依赖)。

**Spec:** `docs/superpowers/specs/2026-09-16-airag-m5-enhancements-design.md`(含四项默认决策:agentic env 全局开关 / 评估集 JSON+CLI / OCR auto+可覆盖 / ask 记审计摘要)

## Global Constraints

- 后端测试:`cd E:\Projects\AIRag\backend && py -3.12 -m pytest`(conftest 指向 airag_test 库 + fake 嵌入,session 级单事件循环);前端:`cd E:\Projects\AIRag\frontend && pnpm build && pnpm test`(vitest)+ `pnpm oxlint`(M5 起要求 0 错误)。
- M5 起 conftest 顶部(T3 落地)额外设:`AGENTIC_REWRITE_ENABLED=false`、`AGENTIC_CRAG_ENABLED=false`、`CHECKPOINTER_ENABLED=false`(必须在 import app.* 之前)。
- SSE 契约不可破坏:事件序 token* → citations → done,error 终态;新增 LLM 节点流式事件必须被 tag 过滤,不得出现在 SSE 输出。
- 拓扑恒定原则(沿 M4 rerank 先例):图节点恒在,关闭时运行时直通;`test_graph_always_has_rerank_node` 等拓扑断言不得改弱。
- 权限语义不可回退:直接端点不可见 404、ask 403、viewer 默认;新端点(审计/导出)按既有权限模式。
- Alembic 迁移手写(照 `3d15333378b3` 文件样式),revision 链:`e6ad26c40163 → 3d15333378b3 → a1b2c3d4e5f6`。
- 提交粒度:每 Task 一提交,信息用 conventional commits(中文注释内可用)。
- 智谱 rerank 模型代码 = `rerank`(M4 实测);CHAT_MODEL 等既有 env 不动。

---

### Task 1: 数据模型与迁移(audit_logs 表 + documents.ocr 两列)

**Files:**
- Create: `backend/app/models/audit.py`
- Modify: `backend/app/models/document.py`(Document 加两列)
- Modify: `backend/app/models/__init__.py`(导出 AuditLog)
- Create: `backend/alembic/versions/a1b2c3d4e5f6_m5_audit_logs_and_ocr_columns.py`
- Modify: `backend/tests/conftest.py`(CLEANUP_ORDER 首位插 "audit_logs")
- Test: `backend/tests/test_audit.py`(新建)

**Interfaces:**
- Produces: `AuditLog(id, username, action, target, detail, ip, created_at)` 模型;`Document.ocr_mode`(默认 'auto')/`Document.ocr_used`(默认 False);后续任务直接引用这些列名。

- [x] **Step 1: 写失败测试**(test_audit.py)

```python
from sqlalchemy import select

from app.models import AuditLog, Document


async def test_audit_log_roundtrip(db_session):
    db_session.add(
        AuditLog(username="u1", action="login_success", target="user:1",
                 detail='{"ip": "127.0.0.1"}', ip="127.0.0.1")
    )
    await db_session.commit()
    row = (await db_session.execute(select(AuditLog))).scalars().one()
    assert row.action == "login_success"
    assert row.created_at is not None


async def test_document_ocr_defaults(db_session):
    from app.models import KnowledgeBase, User
    from app.core.security import hash_password

    u = User(username="ocrdef", password_hash=hash_password("x"))
    db_session.add(u)
    await db_session.flush()
    kb = KnowledgeBase(name="k", owner_id=u.id)
    db_session.add(kb)
    await db_session.flush()
    doc = Document(kb_id=kb.id, filename="a.pdf", file_path="x", mime="a",
                   size=1, sha256="0" * 64)
    db_session.add(doc)
    await db_session.commit()
    assert doc.ocr_mode == "auto"
    assert doc.ocr_used is False
```

- [x] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\backend && py -3.12 -m pytest tests/test_audit.py -v`
Expected: FAIL(ImportError: cannot import name 'AuditLog')

- [x] **Step 3: 实现模型**

`backend/app/models/audit.py`(新建):

```python
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32), index=True)
    target: Mapped[str] = mapped_column(String(128), default="")
    detail: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(45))
```

`document.py` 的 Document 类在 `chunk_count` 行后追加(import 行加 `Boolean`):

```python
    ocr_mode: Mapped[str] = mapped_column(String(8), default="auto", server_default="auto")
    ocr_used: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
```

`models/__init__.py`:import 行加 `from app.models.audit import AuditLog`,`__all__` 加 `"AuditLog"`。

`conftest.py` 的 `CLEANUP_ORDER` 列表首项前插入 `"audit_logs",`(无外键,先删无妨)。

- [x] **Step 4: 跑测试确认通过**

Run: `py -3.12 -m pytest tests/test_audit.py -v`
Expected: 2 passed(conftest 用 metadata 建表,新列/新表自动生效)

- [x] **Step 5: 手写迁移并升级开发库**

`backend/alembic/versions/a1b2c3d4e5f6_m5_audit_logs_and_ocr_columns.py`(新建):

```python
"""m5 audit logs and ocr columns

Revision ID: a1b2c3d4e5f6
Revises: 3d15333378b3
Create Date: 2026-09-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '3d15333378b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('action', sa.String(length=32), nullable=False),
        sa.Column('target', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('ip', sa.String(length=45), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
    )
    op.create_index('ix_audit_logs_action', 'audit_logs', ['action'])
    op.add_column(
        'documents',
        sa.Column('ocr_mode', sa.String(length=8), nullable=False, server_default='auto'),
    )
    op.add_column(
        'documents',
        sa.Column('ocr_used', sa.Boolean(), nullable=False, server_default='false'),
    )


def downgrade() -> None:
    op.drop_column('documents', 'ocr_used')
    op.drop_column('documents', 'ocr_mode')
    op.drop_index('ix_audit_logs_action', table_name='audit_logs')
    op.drop_table('audit_logs')
```

Run: `cd E:\Projects\AIRag\backend && py -3.12 -m alembic upgrade head`
Expected: 无报错;`py -3.12 -m alembic current` 显示 a1b2c3d4e5f6。

- [x] **Step 6: 全量回归 + 提交**

Run: `py -3.12 -m pytest -q`
Expected: 72 passed(70 存量 + 2 新)

```bash
git add backend/app/models backend/alembic/versions/a1b2c3d4e5f6_m5_audit_logs_and_ocr_columns.py backend/tests/conftest.py backend/tests/test_audit.py
git commit -m "feat: audit_logs table and documents ocr columns"
```

### Task 2: audit helper + 审计查询 API

**Files:**
- Create: `backend/app/services/audit.py`
- Modify: `backend/app/schemas/admin.py`(加 AuditLogOut)
- Modify: `backend/app/api/admin.py`(加 GET /audit-logs)
- Test: `backend/tests/test_audit.py`(追加)

**Interfaces:**
- Produces: `async def audit(db, username: str, action: str, target: str = "", detail=None, ip: str | None = None) -> None`——只 add 不 commit(与业务同事务);detail 非 str 时 json.dumps。
- Produces: `GET /api/admin/audit-logs?username=&action=&page=&page_size=` → `{"total": int, "items": [AuditLogOut]}`,admin only。

- [x] **Step 1: 写失败测试**(追加到 test_audit.py)

```python
async def _make_admin(client, db_session, username="aud_admin"):
    from sqlalchemy import text as _text

    created = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    await db_session.execute(
        _text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": created.json()["id"]},
    )
    await db_session.commit()
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_audit_helper_jsonifies_detail(db_session):
    from app.services.audit import audit

    await audit(db_session, "u1", "kb_grant", "kb:3", {"perm": "viewer"})
    await db_session.commit()
    row = (await db_session.execute(select(AuditLog))).scalars().one()
    assert row.detail == '{"perm": "viewer"}'


async def test_audit_logs_api_admin_only_and_filters(client, db_session):
    from app.services.audit import audit

    await audit(db_session, "alice", "login_fail", "user:1")
    await audit(db_session, "bob", "doc_upload", "doc:9")
    await db_session.commit()

    admin = await _make_admin(client, db_session)
    resp = await client.get("/api/admin/audit-logs", headers=admin)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["items"][0]["action"] == "doc_upload"  # 时间倒序(id desc 近似)

    filtered = await client.get(
        "/api/admin/audit-logs", params={"username": "alice"}, headers=admin
    )
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["username"] == "alice"

    paged = await client.get(
        "/api/admin/audit-logs", params={"page": 2, "page_size": 1}, headers=admin
    )
    assert paged.json()["total"] == 2
    assert len(paged.json()["items"]) == 1

    from tests.test_admin_users import _register_and_login as _rl  # 若无此 helper 见 Step 3 备注

    plain = await client.post(
        "/api/auth/register", json={"username": "aud_plain", "password": "secret123"}
    )
    login = await client.post(
        "/api/auth/login", json={"username": "aud_plain", "password": "secret123"}
    )
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}
    denied = await client.get("/api/admin/audit-logs", headers=h)
    assert denied.status_code == 403
```

(注:最后一个断言块前的 `_rl` import 行是冗余的,实现时直接删掉那行,保留后面三段请求。)

- [x] **Step 2: 跑测试确认失败**

Run: `py -3.12 -m pytest tests/test_audit.py -v`
Expected: FAIL(ModuleNotFoundError: app.services.audit)

- [x] **Step 3: 实现**

`backend/app/services/audit.py`(新建):

```python
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def audit(
    db: AsyncSession,
    username: str,
    action: str,
    target: str = "",
    detail=None,
    ip: str | None = None,
) -> None:
    """追加一条审计记录;与业务同事务(不自行 commit)。"""
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    db.add(
        AuditLog(
            username=username[:64],
            action=action,
            target=target[:128],
            detail=detail,
            ip=ip,
        )
    )
```

`schemas/admin.py` 追加:

```python
class AuditLogOut(BaseModel):
    id: int
    username: str
    action: str
    target: str
    detail: str | None
    ip: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
```

(文件头部补 `from datetime import datetime`。)

`api/admin.py` 追加路由(import 行补 `func`、`AuditLog`、`AuditLogOut`):

```python
@router.get("/audit-logs")
async def list_audit_logs(
    username: str | None = None,
    action: str | None = None,
    page: int = 1,
    page_size: int = 20,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    where = []
    if username:
        where.append(AuditLog.username == username)
    if action:
        where.append(AuditLog.action == action)
    total = (
        await db.execute(select(func.count(AuditLog.id)).where(*where))
    ).scalar_one()
    rows = (
        await db.execute(
            select(AuditLog)
            .where(*where)
            .order_by(AuditLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return {"total": total, "items": [AuditLogOut.model_validate(r) for r in rows]}
```

- [x] **Step 4: 跑测试确认通过 + 全量回归**

Run: `py -3.12 -m pytest tests/test_audit.py -v && py -3.12 -m pytest -q`
Expected: 4 passed / 74 passed

- [x] **Step 5: 提交**

```bash
git add backend/app/services/audit.py backend/app/schemas/admin.py backend/app/api/admin.py backend/tests/test_audit.py
git commit -m "feat: audit helper and admin audit-logs api"
```

### Task 3: Agentic 图节点(rewrite / grade / transform)

**Files:**
- Modify: `backend/app/core/config.py`(两个开关)
- Modify: `backend/app/services/chat_graph/state.py`
- Modify: `backend/app/services/chat_graph/nodes.py`(三新节点 + retrieve 改读 search_query + generate 加 tag + rerank 负下标防御)
- Modify: `backend/app/services/chat_graph/graph.py`(新拓扑 + make_chat_llm 单例)
- Modify: `backend/tests/conftest.py`(顶部三 env)
- Test: `backend/tests/test_chat_graph.py`(追加)

**Interfaces:**
- Consumes: 无(自包含)。
- Produces: `ChatState` 新键 `history/search_query/proposed_query/grade/retries`;`rewrite_node(state, llm)`、`grade_node(state, llm)`、`transform_node(state)`;`route_after_grade(state) -> "transform"|"generate"`;`make_chat_llm()` 变 lru_cache 单例;generate 的 LLM 调用带 `config={"tags": ["answer"]}`(T4 的 ask.py 依赖此 tag)。
- 注意:rewrite 关闭时**不是**返回 `{}`,必须返回 `{"search_query": question, "retries": 0, "grade": ""}`(重置 checkpointer 残留 + 保证 retrieve 有查询可用)。

- [x] **Step 1: conftest 顶部加 env**(在 `os.environ["EMBED_PROVIDER"] = "fake"` 之后)

```python
os.environ["AGENTIC_REWRITE_ENABLED"] = "false"
os.environ["AGENTIC_CRAG_ENABLED"] = "false"
os.environ["CHECKPOINTER_ENABLED"] = "false"
```

- [x] **Step 2: 写失败测试**(追加到 test_chat_graph.py)

```python
async def test_retrieve_uses_search_query(monkeypatch):
    import app.services.chat_graph.nodes as nodes_mod

    captured = {}

    async def fake_search(db, kb_ids, query, top_k=20):
        captured["q"] = query
        return []

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    await nodes_mod.retrieve_node({"question": "原问题", "kb_ids": [1], "search_query": "改写后"})
    assert captured["q"] == "改写后"


async def test_retrieve_falls_back_to_question(monkeypatch):
    import app.services.chat_graph.nodes as nodes_mod

    captured = {}

    async def fake_search(db, kb_ids, query, top_k=20):
        captured["q"] = query
        return []

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    await nodes_mod.retrieve_node({"question": "原问题", "kb_ids": [1]})
    assert captured["q"] == "原问题"


async def test_rewrite_disabled_resets_state():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services.chat_graph.nodes import rewrite_node

    out = await rewrite_node(
        {"question": "它是什么", "history": [{"role": "user", "content": "x"}]},
        llm=FakeListChatModel(responses=["不该被调用"]),
    )
    assert out == {"search_query": "它是什么", "retries": 0, "grade": ""}


async def test_rewrite_resolves_coreference():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.core.config import settings
    from app.services.chat_graph.nodes import rewrite_node

    old = settings.AGENTIC_REWRITE_ENABLED
    settings.AGENTIC_REWRITE_ENABLED = True
    try:
        out = await rewrite_node(
            {
                "question": "它的负责人是谁",
                "history": [
                    {"role": "user", "content": "星际探索项目是什么"},
                    {"role": "assistant", "content": "是一个项目"},
                ],
            },
            llm=FakeListChatModel(responses=["星际探索项目的负责人"]),
        )
        assert out["search_query"] == "星际探索项目的负责人"
        assert out["retries"] == 0
    finally:
        settings.AGENTIC_REWRITE_ENABLED = old


async def test_rewrite_llm_failure_falls_back():
    from app.core.config import settings
    from app.services.chat_graph.nodes import rewrite_node

    class Boom:
        async def ainvoke(self, msgs, config=None):
            raise RuntimeError("llm down")

    settings.AGENTIC_REWRITE_ENABLED = True
    try:
        out = await rewrite_node(
            {"question": "q", "history": [{"role": "user", "content": "h"}]}, llm=Boom()
        )
        assert out["search_query"] == "q"
    finally:
        settings.AGENTIC_REWRITE_ENABLED = False


async def test_grade_disabled_and_empty_hits():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services.chat_graph.nodes import grade_node

    llm = FakeListChatModel(responses=["{}"])
    assert await grade_node({"question": "q"}, llm=llm) == {}
    hits = [{"filename": "a", "page_no": 1, "content": "c"}]
    assert await grade_node({"question": "q", "hits": hits}, llm=llm) == {}


async def test_grade_json_parsing_paths():
    from app.services.chat_graph.nodes import grade_node

    hits = [{"filename": "a.pdf", "page_no": 2, "content": "预算三千万"}]

    class Resp:
        def __init__(self, text):
            self.content = text

    class LLMScript:
        def __init__(self, replies):
            self.replies = list(replies)

        async def ainvoke(self, msgs, config=None):
            return Resp(self.replies.pop(0))

    from app.core.config import settings

    settings.AGENTIC_CRAG_ENABLED = True
    try:
        out = await grade_node(
            {"question": "q", "hits": hits},
            llm=LLMScript(['{"verdict": "insufficient", "query": "新检索词"}']),
        )
        assert out == {"grade": "insufficient", "proposed_query": "新检索词"}
        ok = await grade_node(
            {"question": "q", "hits": hits},
            llm=LLMScript(['{"verdict": "sufficient", "query": "ignored"}']),
        )
        assert ok == {"grade": "sufficient"}
        fenced = await grade_node(
            {"question": "q", "hits": hits},
            llm=LLMScript(["```json\n{\"verdict\": \"insufficient\"}\n```"]),
        )
        assert fenced == {"grade": "insufficient"}
        bad = await grade_node(
            {"question": "q", "hits": hits}, llm=LLMScript(["不是 json"])
        )
        assert bad == {"grade": "sufficient"}
    finally:
        settings.AGENTIC_CRAG_ENABLED = False


async def test_transform_node_increments():
    from app.services.chat_graph.nodes import transform_node

    out = await transform_node({"question": "q", "proposed_query": "p", "retries": 0})
    assert out == {"search_query": "p", "retries": 1}
    fallback = await transform_node({"question": "q", "retries": 0})
    assert fallback == {"search_query": "q", "retries": 1}


async def test_route_after_grade_branches():
    from app.services.chat_graph.graph import route_after_grade

    assert route_after_grade({"grade": "insufficient", "retries": 0}) == "transform"
    assert route_after_grade({"grade": "insufficient", "retries": 1}) == "generate"
    assert route_after_grade({"grade": "sufficient"}) == "generate"
    assert route_after_grade({}) == "generate"


async def test_graph_topology_contains_agentic_edges():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services.chat_graph.graph import build_graph

    g = build_graph(llm=FakeListChatModel(responses=["x"]))
    edges = {(e.source, e.target) for e in g.get_graph().edges}
    assert ("__start__", "rewrite") in edges
    assert ("rewrite", "retrieve") in edges
    assert ("rerank", "grade") in edges
    assert ("transform", "retrieve") in edges
    assert ("grade", "generate") in edges  # 条件边在图结构里表现为可达


async def test_crag_retries_once_end_to_end(monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.core.config import settings
    from app.services.chat_graph.graph import build_graph
    from app.services.retrieval.searcher import SearchHit

    calls = {"n": 0}

    async def fake_search(db, kb_ids, query, top_k=20):
        calls["n"] += 1
        return [SearchHit(1, 1, 1, "a.pdf", 1, f"内容-{query}", 0.5, "vector")]

    import app.services.chat_graph.nodes as nodes_mod

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)

    llm = FakeListChatModel(
        responses=[
            '{"verdict": "insufficient", "query": "第二次检索词"}',
            '{"verdict": "sufficient"}',
            "最终答案[1]",
        ]
    )
    settings.AGENTIC_CRAG_ENABLED = True
    try:
        g = build_graph(llm=llm)
        final = await g.ainvoke({"question": "问个问题", "kb_ids": [1]})
        assert calls["n"] == 2  # 重检恰好一次
        assert "最终答案" in final["answer"]
        assert final["retries"] == 1
    finally:
        settings.AGENTIC_CRAG_ENABLED = False
```

- [x] **Step 3: 跑测试确认失败**

Run: `py -3.12 -m pytest tests/test_chat_graph.py -v`
Expected: 新增用例 FAIL(AttributeError/ImportError/断言不符),存量 4 个 PASS

- [x] **Step 4: 实现**

`config.py` 在 `RERANK_MODEL` 之后加:

```python
    AGENTIC_REWRITE_ENABLED: bool = True
    AGENTIC_CRAG_ENABLED: bool = True
```

`state.py` 整体替换为:

```python
from typing import TypedDict


class ChatState(TypedDict, total=False):
    question: str
    kb_ids: list[int]
    rerank: bool
    history: list[dict]
    search_query: str
    proposed_query: str
    grade: str
    retries: int
    hits: list[dict]
    answer: str
    citations: list[dict]
```

`nodes.py` 头部 import 改为(`json`、`logger`):

```python
import json

from loguru import logger

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.retrieval.searcher import SearchHit, hybrid_search
from app.services.rerank.base import get_reranker
```

`retrieve_node` 替换:

```python
async def retrieve_node(state: dict) -> dict:
    query = state.get("search_query") or state["question"]
    async with SessionLocal() as db:
        hits = await hybrid_search(db, state["kb_ids"], query)
    return {"hits": [h.__dict__ for h in hits]}
```

`rerank_node` 最后一行改(负下标防御):

```python
    return {"hits": [hits[i] for i in order if 0 <= i < len(hits)]}
```

`generate_node` 的 `resp = await llm.ainvoke(messages)` 改:

```python
    resp = await llm.ainvoke(messages, config={"tags": ["answer"]})
```

文件末尾追加:

```python
REWRITE_SYSTEM = (
    "你是检索查询改写器。根据对话历史把用户最新问题改写成独立、无指代的检索查询,"
    "直接输出改写后的查询本身,不要任何解释或前后缀。无法改写时原样输出问题。"
)

GRADE_SYSTEM = (
    "你是检索质量评审。根据问题判断参考资料是否足以回答。"
    '只输出 JSON:{"verdict":"sufficient 或 insufficient",'
    '"query":"当 insufficient 时,给出一个更利于检索的改写查询"}'
)


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


async def rewrite_node(state: dict, llm) -> dict:
    question = state["question"]
    reset = {"search_query": question, "retries": 0, "grade": ""}
    if not settings.AGENTIC_REWRITE_ENABLED:
        return reset
    history = state.get("history") or []
    if not history:
        return reset
    try:
        msgs = [("system", REWRITE_SYSTEM)]
        for m in history:
            msgs.append((m["role"], m["content"]))
        msgs.append(("user", f"最新问题:{question}"))
        resp = await llm.ainvoke(msgs)
        rewritten = (resp.content or "").strip()
        if rewritten:
            reset["search_query"] = rewritten
    except Exception:
        logger.exception("query rewrite failed; fallback to raw question")
    return reset


async def grade_node(state: dict, llm) -> dict:
    if not settings.AGENTIC_CRAG_ENABLED:
        return {}
    hits = state.get("hits") or []
    if not hits:
        return {}
    context = "\n".join(
        f"[{i+1}] {h['filename']} 第{h['page_no'] or '?'}页:{h['content'][:120]}"
        for i, h in enumerate(hits[: settings.RETRIEVAL_TOP_K])
    )
    try:
        resp = await llm.ainvoke(
            [
                ("system", GRADE_SYSTEM),
                ("user", f"问题:{state['question']}\n参考资料:\n{context}"),
            ]
        )
        parsed = json.loads(_extract_json(resp.content))
        verdict = parsed.get("verdict")
        if verdict not in ("sufficient", "insufficient"):
            return {"grade": "sufficient"}
        out = {"grade": verdict}
        if verdict == "insufficient" and parsed.get("query"):
            out["proposed_query"] = str(parsed["query"])
        return out
    except Exception:
        logger.exception("grade failed; degrade to sufficient")
        return {"grade": "sufficient"}


async def transform_node(state: dict) -> dict:
    return {
        "search_query": state.get("proposed_query") or state["question"],
        "retries": state.get("retries", 0) + 1,
    }
```

`graph.py` 整体替换为:

```python
from functools import lru_cache, partial

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from app.core.config import settings
from app.services.chat_graph.nodes import (
    generate_node,
    grade_node,
    rerank_node,
    retrieve_node,
    rewrite_node,
    transform_node,
)
from app.services.chat_graph.state import ChatState


@lru_cache(maxsize=1)
def make_chat_llm():
    # Minor 清偿:单例复用,避免每次 ask 新建 OpenAI 客户端
    return ChatOpenAI(
        base_url=settings.ZHIPU_BASE_URL,
        api_key=settings.ZHIPU_API_KEY,
        model=settings.CHAT_MODEL,
        temperature=settings.CHAT_TEMPERATURE,
        max_tokens=settings.CHAT_MAX_TOKENS,
    )


def route_after_grade(state: dict) -> str:
    if state.get("grade") == "insufficient" and state.get("retries", 0) < 1:
        return "transform"
    return "generate"


def build_graph(llm=None, checkpointer=None):
    chat_llm = llm or make_chat_llm()
    gen = partial(generate_node, llm=chat_llm)
    rw = partial(rewrite_node, llm=chat_llm)
    gr = partial(grade_node, llm=chat_llm)
    g = StateGraph(ChatState)
    g.add_node("rewrite", rw)
    g.add_node("retrieve", retrieve_node)
    g.add_node("rerank", rerank_node)
    g.add_node("grade", gr)
    g.add_node("transform", transform_node)
    g.add_node("generate", gen)
    # 拓扑恒含全部节点:开关关闭时节点直通(M4 rerank 模式)
    g.add_edge(START, "rewrite")
    g.add_edge("rewrite", "retrieve")
    g.add_edge("retrieve", "rerank")
    g.add_edge("rerank", "grade")
    g.add_conditional_edges(
        "grade", route_after_grade, {"transform": "transform", "generate": "generate"}
    )
    g.add_edge("transform", "retrieve")
    g.add_edge("generate", END)
    return g.compile(checkpointer=checkpointer)
```

- [x] **Step 5: 跑测试确认通过 + 全量回归**

Run: `py -3.12 -m pytest tests/test_chat_graph.py -v && py -3.12 -m pytest -q`
Expected: 图文件 14 passed(4 存量 + 10 新)/ 全量 84 passed
注意:若 `("grade","generate")` 边断言因条件边表示差异失败,改断言 `g.get_graph().nodes` 含 "grade"/"transform" 且 `("transform","retrieve")` 在 edges(不许改弱其他断言)。

- [x] **Step 6: 提交**

```bash
git add backend/app/core/config.py backend/app/services/chat_graph backend/tests/conftest.py backend/tests/test_chat_graph.py
git commit -m "feat: query rewrite and corrective-rag nodes in chat graph"
```

### Task 4: ask.py 接线(history / checkpointer / tag 过滤 / ask 审计)

**Files:**
- Modify: `backend/app/core/config.py`(CHECKPOINTER_ENABLED)
- Modify: `backend/app/api/ask.py`
- Test: `backend/tests/test_ask.py`(追加)

**Interfaces:**
- Consumes: T3 的 `build_graph(checkpointer=...)`/tag "answer";T2 的 `audit()`;`get_checkpointer()`(既有)。
- Produces: ask init state 含 `history`(最近 6 条 [{role, content}],不含当前问题);`CHECKPOINTER_ENABLED: bool = True` env。

- [x] **Step 1: 写失败测试**(追加到 test_ask.py)

```python
async def test_ask_filters_non_answer_llm_tokens(
    client, auth_headers, monkeypatch, db_session
):
    """rewrite 的 LLM 输出不得进入 SSE token 流(带 answer tag 过滤)。"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    import app.services.chat_graph.nodes as nodes_mod
    from app.api import ask as ask_mod
    from app.services.chat_graph.graph import build_graph

    async def fake_search(db, kb_ids, query, top_k=20):
        return [SearchHit(1, 1, 1, "a.pdf", 1, "答案内容", 0.5, "vector")]

    captured = {"q": None}

    async def cap_search(db, kb_ids, query, top_k=20):
        captured["q"] = query
        return [SearchHit(1, 1, 1, "a.pdf", 1, "答案内容", 0.5, "vector")]

    monkeypatch.setattr(nodes_mod, "hybrid_search", cap_search)
    real_build = build_graph

    def patched(**kw):
        return real_build(
            llm=FakeListChatModel(responses=["改写后的独立查询", "最终答案[1]"])
        )

    monkeypatch.setattr(ask_mod, "build_graph", patched)

    from app.core.config import settings

    monkeypatch.setattr(settings, "AGENTIC_REWRITE_ENABLED", True)

    kb = await client.post("/api/kbs", json={"name": "改写链路库"}, headers=auth_headers)
    # 先建会话并落两条历史消息,让 rewrite 有上下文可用
    conv = await client.post(
        "/api/chat/conversations",
        json={"kb_ids": [kb.json()["id"]], "name": "历史会话"},
        headers=auth_headers,
    )
    conv_id = conv.json()["id"]
    from app.models import Message

    db_session.add(Message(conversation_id=conv_id, role="user", content="星际探索项目是什么"))
    db_session.add(Message(conversation_id=conv_id, role="assistant", content="是个项目"))
    await db_session.commit()

    resp = await client.post(
        "/api/chat/ask",
        json={"kb_ids": [kb.json()["id"]], "question": "它的负责人是谁",
              "conversation_id": conv_id},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.text
    assert "改写后的独立查询" not in body  # rewrite 输出被 tag 过滤
    assert "最终答案" in body
    assert captured["q"] == "改写后的独立查询"  # 检索用了改写查询
    # ask 审计落库
    from sqlalchemy import select as sel

    from app.models import AuditLog

    logs = (await db_session.execute(sel(AuditLog))).scalars().all()
    assert any(l.action == "ask" for l in logs)


async def test_ask_checkpointer_wired_when_enabled(
    client, auth_headers, monkeypatch, db_session
):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    import app.services.chat_graph.nodes as nodes_mod
    from app.api import ask as ask_mod
    from app.core.config import settings
    from app.services.chat_graph.graph import build_graph

    async def fake_search(db, kb_ids, query, top_k=20):
        return [SearchHit(1, 1, 1, "a.pdf", 1, "内容", 0.5, "vector")]

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    real_build = build_graph
    seen = {}

    def patched(**kw):
        seen["checkpointer"] = kw.get("checkpointer")
        return real_build(llm=FakeListChatModel(responses=["答案[1]"]))

    monkeypatch.setattr(ask_mod, "build_graph", patched)
    monkeypatch.setattr(settings, "CHECKPOINTER_ENABLED", True)

    sentinel = object()

    async def fake_cp():
        return sentinel

    monkeypatch.setattr(ask_mod, "get_checkpointer", fake_cp)

    kb = await client.post("/api/kbs", json={"name": "cp库"}, headers=auth_headers)
    resp = await client.post(
        "/api/chat/ask",
        json={"kb_ids": [kb.json()["id"]], "question": "q"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert seen["checkpointer"] is sentinel
```

(test_ask.py 头部已有 `from sqlalchemy import select as select_`,新用例里用 `select_`/直接 import 均可,保持一致用 `select_`。上面第二个用例 `fake_search` 未用 `captured` 可留空实现。)

- [x] **Step 2: 跑测试确认失败**

Run: `py -3.12 -m pytest tests/test_ask.py -v`
Expected: 新用例 FAIL(改写输出出现在 body / checkpointer 为 None)

- [x] **Step 3: 实现**

`config.py` 在 `AGENTIC_CRAG_ENABLED` 后加:

```python
    CHECKPOINTER_ENABLED: bool = True
```

`ask.py` 修改:
1. import 区加 `from fastapi import Request`、`from sqlalchemy import select`、`from app.core.config import settings`、`from app.services.audit import audit`、`from app.services.chat_graph.checkpointer import get_checkpointer`;`from app.models import ...` 行确认含 `Message`(已含)。
2. 新 helper(模块级,`_sse` 之后):

```python
async def _recent_history(db: AsyncSession, conv_id: int, limit: int = 6) -> list[dict]:
    rows = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(Message.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]
```

3. 端点签名加 `request: Request`。
4. 在 `db.add(Message(...user...))` **之前**插:

```python
    history = (
        await _recent_history(db, conv.id) if payload.conversation_id is not None else []
    )
```

5. `graph = build_graph()` 替换:

```python
    checkpointer = await get_checkpointer() if settings.CHECKPOINTER_ENABLED else None
    graph = build_graph(checkpointer=checkpointer)
    init = {
        "question": payload.question,
        "kb_ids": payload.kb_ids,
        "rerank": payload.rerank,
        "history": history,
    }
```

6. token 过滤条件替换:

```python
                if (
                    ev["event"] == "on_chat_model_stream"
                    and "answer" in (ev.get("tags") or [])
                ):
```

7. `gen()` 内 assistant 落库段替换(同事务加审计):

```python
            async with SessionLocal() as s2:
                s2.add(Message(conversation_id=conv.id, role="assistant",
                               content=answer, citations=citations))
                await audit(
                    s2, current.username, "ask", f"conv:{conv.id}",
                    {"q": payload.question[:50], "kb_ids": payload.kb_ids},
                    request.client.host if request.client else None,
                )
                await s2.commit()
```

- [x] **Step 4: 跑测试确认通过 + 全量回归**

Run: `py -3.12 -m pytest tests/test_ask.py -v && py -3.12 -m pytest -q`
Expected: ask 文件 5 passed / 全量 86 passed

- [x] **Step 5: 提交**

```bash
git add backend/app/core/config.py backend/app/api/ask.py backend/tests/test_ask.py
git commit -m "feat: ask wires history, checkpointer, token tag filter and ask audit"
```

### Task 5: MinerU 云 API OCR

**Files:**
- Modify: `backend/pyproject.toml`(httpx 提主依赖)
- Modify: `backend/app/core/config.py`(三个 OCR env)
- Create: `backend/app/services/parsing/mineru_client.py`
- Create: `backend/app/services/parsing/ocr.py`
- Create: `backend/app/services/parsing/image_parser.py`
- Modify: `backend/app/services/parsing/__init__.py`(导入 image_parser 触发注册,若现有 __init__ 为空则加一行 import)
- Modify: `backend/app/workers/pipeline.py`(maybe_ocr 接入)
- Modify: `backend/app/api/documents.py`(ocr 表单参数 + 图片白名单)
- Modify: `backend/app/schemas/document.py`(DocumentOut 两字段)
- Modify: `.env.example`(MINERU_API_TOKEN)
- Test: `backend/tests/test_ocr.py`(新建)

**Interfaces:**
- Consumes: T1 的 `Document.ocr_mode/ocr_used`。
- Produces: `parse_via_mineru(path: Path, filename: str) -> str`(markdown;失败抛 `MineruError`);`maybe_ocr(path, ext, ocr_mode, primary: ParseResult) -> ParseResult`;`markdown_to_blocks(md_text) -> ParseResult`;`is_thin_text(primary) -> bool`;上传表单字段 `ocr: auto|force|off`(默认 auto)。

- [x] **Step 0: 核对 MinerU v4 API 契约**

Run(WebFetch 或浏览器): `https://mineru.net/apiManage/docs`
核对四点,与 Step 3 客户端代码比对,不符则改客户端常量(端点/字段名以文档为准):①文件上传端点与返回 `data.file_url`;②建任务端点与 `data.task_id`;③轮询端点、`state` 枚举与 `full_zip_url` 字段;④鉴权头 `Authorization: Bearer <token>`。

- [x] **Step 1: 写失败测试**(test_ocr.py 新建)

```python
import zipfile
from pathlib import Path

import httpx
import pytest

from app.services.parsing.base import ParseResult, ParsedBlock
from app.services.parsing.ocr import is_thin_text, markdown_to_blocks, maybe_ocr


def _primary(chars: int = 5000, pages: int = 10) -> ParseResult:
    return ParseResult(blocks=[ParsedBlock(content="字" * chars)], page_count=pages)


def test_markdown_to_blocks_splits_and_flags_tables():
    md = "第一段文字\n\n| 列1 | 列2 |\n|---|---|\n| a | b |\n\n第三段"
    result = markdown_to_blocks(md)
    assert len(result.blocks) == 3
    assert result.blocks[0].is_table is False
    assert result.blocks[1].is_table is True
    assert result.blocks[2].content == "第三段"


def test_is_thin_text_threshold():
    assert is_thin_text(_primary(chars=49, pages=1)) is True
    assert is_thin_text(_primary(chars=50, pages=1)) is False
    assert is_thin_text(ParseResult(blocks=[], page_count=0)) is True


def _enable_mineru(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "MINERU_API_TOKEN", "test-token")


def test_maybe_ocr_off_or_no_token_returns_primary(tmp_path, monkeypatch):
    from app.core.config import settings

    p = tmp_path / "a.pdf"
    p.write_bytes(b"x")
    assert maybe_ocr(p, ".pdf", "off", _primary()) is not None
    # token 空:force 也直通
    monkeypatch.setattr(settings, "MINERU_API_TOKEN", "")
    assert maybe_ocr(p, ".pdf", "force", _primary()).blocks[0].content == "字" * 5000


def test_maybe_ocr_auto_thin_pdf_and_images(tmp_path, monkeypatch):
    import app.services.parsing.ocr as ocr_mod

    calls = []

    def fake_mineru(path, filename):
        calls.append((str(path), filename))
        return "OCR 出的内容\n\n|a|b|\n|---|---|"

    monkeypatch.setattr(ocr_mod, "parse_via_mineru", fake_mineru)
    _enable_mineru(monkeypatch)

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"x")
    thin = ParseResult(blocks=[ParsedBlock(content="稀")], page_count=3)
    out = maybe_ocr(pdf, ".pdf", "auto", thin)
    assert calls and out.blocks[0].content == "OCR 出的内容"
    assert out.blocks[1].is_table is True

    img = tmp_path / "photo.png"
    img.write_bytes(b"x")
    out2 = maybe_ocr(img, ".png", "auto", ParseResult())
    assert len(calls) == 2
    assert out2.blocks[0].content == "OCR 出的内容"


def test_maybe_ocr_auto_thick_pdf_and_docx_skip(tmp_path, monkeypatch):
    import app.services.parsing.ocr as ocr_mod

    calls = []

    def fake_mineru(path, filename):
        calls.append(1)
        return "不应被调用"

    monkeypatch.setattr(ocr_mod, "parse_via_mineru", fake_mineru)
    _enable_mineru(monkeypatch)

    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(b"x")
    assert maybe_ocr(pdf, ".pdf", "auto", _primary()).blocks[0].content == "字" * 5000
    docx = tmp_path / "t.docx"
    docx.write_bytes(b"x")
    assert maybe_ocr(docx, ".docx", "force", _primary()).blocks[0].content == "字" * 5000
    assert calls == []


def test_maybe_ocr_mineru_empty_falls_back(tmp_path, monkeypatch):
    import app.services.parsing.ocr as ocr_mod

    monkeypatch.setattr(ocr_mod, "parse_via_mineru", lambda p, f: "")
    _enable_mineru(monkeypatch)
    pdf = tmp_path / "e.pdf"
    pdf.write_bytes(b"x")
    thin = ParseResult(blocks=[ParsedBlock(content="原")], page_count=1)
    out = maybe_ocr(pdf, ".pdf", "auto", thin)
    assert out.blocks[0].content == "原"


def _zip_with_markdown(markdown: str) -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("full.md", markdown)
    return buf.getvalue()


def test_mineru_client_happy_path(monkeypatch, tmp_path):
    from app.services.parsing import mineru_client as mc

    _enable_mineru(monkeypatch)
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/file/upload":
            return httpx.Response(200, json={"code": 0, "data": {"file_url": "https://f/u.pdf"}})
        if request.url.path == "/api/v4/extract/task":
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "t1"}})
        if request.url.path == "/api/v4/extract-results/t1":
            state["polls"] += 1
            if state["polls"] < 2:
                return httpx.Response(200, json={"code": 0, "data": {"state": "running"}})
            return httpx.Response(
                200,
                json={"code": 0, "data": {"state": "done", "full_zip_url": "https://f/r.zip"}},
            )
        if request.url.host == "f" and request.url.path == "/r.zip":
            return httpx.Response(200, content=_zip_with_markdown("# 扫描件内容"))
        raise AssertionError(f"unexpected {request.url}")

    monkeypatch.setattr(
        httpx, "Client",
        lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "transport"}),
    )
    monkeypatch.setattr(mc, "POLL_INTERVAL", 0)
    p = tmp_path / "s.pdf"
    p.write_bytes(b"pdf")
    assert mc.parse_via_mineru(p, "s.pdf") == "# 扫描件内容"


def test_mineru_client_429_and_timeout(monkeypatch, tmp_path):
    from app.services.parsing import mineru_client as mc

    _enable_mineru(monkeypatch)

    def handler_429(request):
        return httpx.Response(429, json={"code": 429})

    monkeypatch.setattr(
        httpx, "Client",
        lambda **kw: httpx.Client(transport=httpx.MockTransport(handler_429), **{k: v for k, v in kw.items() if k != "transport"}),
    )
    p = tmp_path / "s.pdf"
    p.write_bytes(b"pdf")
    with pytest.raises(mc.MineruError):
        mc.parse_via_mineru(p, "s.pdf")

    state = {"n": 0}

    def handler_stuck(request):
        if request.url.path == "/api/v4/extract-results/t1":
            state["n"] += 1
            return httpx.Response(200, json={"code": 0, "data": {"state": "running"}})
        if request.url.path == "/api/v4/file/upload":
            return httpx.Response(200, json={"code": 0, "data": {"file_url": "https://f/u.pdf"}})
        return httpx.Response(200, json={"code": 0, "data": {"task_id": "t1"}})

    monkeypatch.setattr(
        httpx, "Client",
        lambda **kw: httpx.Client(transport=httpx.MockTransport(handler_stuck), **{k: v for k, v in kw.items() if k != "transport"}),
    )
    monkeypatch.setattr(mc, "POLL_MAX", 2)
    monkeypatch.setattr(mc, "POLL_INTERVAL", 0)
    with pytest.raises(mc.MineruError, match="timeout"):
        mc.parse_via_mineru(p, "s.pdf")


async def test_upload_jpg_and_ocr_mode(client, auth_headers, monkeypatch):
    import io

    png = io.BytesIO(b"\x89PNG\r\n\x1a\nfaked")
    resp = await client.post(
        "/api/kbs/1/documents",
        files={"file": ("扫描.png", png, "image/png")},
        data={"ocr": "force"},
        headers=auth_headers,
    )
    assert resp.status_code == 404  # kb 1 不存在:权限/存在性先行,白名单放行(不是 415)

    kb = await client.post("/api/kbs", json={"name": "ocr上传库"}, headers=auth_headers)
    kb_id = kb.json()["id"]
    png2 = io.BytesIO(b"\x89PNG\r\n\x1a\nfaked")
    ok = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("扫描.png", png2, "image/png")},
        data={"ocr": "force"},
        headers=auth_headers,
    )
    assert ok.status_code == 201
    assert ok.json()["ocr_mode"] == "force"
    assert ok.json()["ocr_used"] is False  # eager 流水线失败(假 png),但字段已落

    bad = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("a.pdf", io.BytesIO(b"x"), "application/pdf")},
        data={"ocr": "sometimes"},
        headers=auth_headers,
    )
    assert bad.status_code == 422
```

- [x] **Step 2: 跑测试确认失败**

Run: `py -3.12 -m pytest tests/test_ocr.py -v`
Expected: FAIL(ModuleNotFoundError: app.services.parsing.ocr)

- [x] **Step 3: 实现**

`pyproject.toml`:dependencies 里加 `"httpx>=0.27",`;dev 里 httpx 可留(重复无害)。
`config.py` 在 `CHECKPOINTER_ENABLED` 后加:

```python
    OCR_THIN_CHARS_PER_PAGE: int = 50
    MINERU_API_TOKEN: str = ""
    MINERU_BASE_URL: str = "https://mineru.net"
```

`backend/app/services/parsing/mineru_client.py`(新建;端点/字段名以 Step 0 核对结果为准,不符改这里):

```python
"""MinerU 云 API 同步客户端(Celery worker 内阻塞使用)。

v4 流程:文件上传 → 建任务 → 轮询 → 下载结果 zip → 读 full.md。
文档:https://mineru.net/apiManage/docs
"""
import io
import time
import zipfile

import httpx

from app.core.config import settings

POLL_INTERVAL = 5  # 秒
POLL_MAX = 60  # 5 分钟上限


class MineruError(RuntimeError):
    pass


def _check(resp: httpx.Response, step: str) -> dict:
    if resp.status_code == 429:
        raise MineruError(f"mineru rate limited at {step}")
    if resp.status_code != 200 or resp.json().get("code") != 0:
        raise MineruError(f"mineru {step} failed: {resp.status_code} {resp.text[:200]}")
    return resp.json()["data"]


def parse_via_mineru(path, filename: str) -> str:
    """返回解析出的 markdown 文本;任何失败抛 MineruError。"""
    token = settings.MINERU_API_TOKEN
    if not token:
        raise MineruError("MINERU_API_TOKEN not configured")
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=settings.MINERU_BASE_URL, timeout=120) as client:
        with open(path, "rb") as f:
            up = client.post(
                "/api/v4/file/upload",
                headers=headers,
                files={"file": (filename, f)},
                params={"enable_ocr": "true"},
            )
        data = _check(up, "file upload")
        task = client.post(
            "/api/v4/extract/task", headers=headers,
            json={"file_url": data["file_url"]},
        )
        task_id = _check(task, "create task")["task_id"]

        for _ in range(POLL_MAX):
            res = client.get(f"/api/v4/extract-results/{task_id}", headers=headers)
            state = _check(res, "poll")["state"]
            if state == "done":
                zip_url = res.json()["data"]["full_zip_url"]
                zip_resp = client.get(zip_url)
                if zip_resp.status_code != 200:
                    raise MineruError(f"mineru result download failed: {zip_resp.status_code}")
                with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as z:
                    return z.read("full.md").decode("utf-8")
            if state == "failed":
                raise MineruError(f"mineru task failed: {res.text[:200]}")
            time.sleep(POLL_INTERVAL)
        raise MineruError(f"mineru poll timeout after {POLL_MAX} attempts")
```

`backend/app/services/parsing/ocr.py`(新建):

```python
from pathlib import Path

from app.core.config import settings
from app.services.parsing.base import ParseResult, ParsedBlock
from app.services.parsing.mineru_client import parse_via_mineru

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
TABLE_MARKERS = ("|---", "---|")


def is_thin_text(primary: ParseResult) -> bool:
    pages = max(primary.page_count, 1)
    total = sum(len(b.content) for b in primary.blocks)
    return total / pages < settings.OCR_THIN_CHARS_PER_PAGE


def markdown_to_blocks(md_text: str) -> ParseResult:
    result = ParseResult()
    for para in md_text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        result.blocks.append(
            ParsedBlock(
                content=para,
                is_table=any(m in para for m in TABLE_MARKERS),
            )
        )
    return result


def maybe_ocr(
    path: Path, ext: str, ocr_mode: str | None, primary: ParseResult
) -> ParseResult:
    """按模式决定是否走 MinerU;不触发时原样返回 primary(同一对象)。"""
    mode = ocr_mode or "auto"
    if mode == "off" or not settings.MINERU_API_TOKEN:
        return primary
    ext = ext.lower()
    if not (ext == ".pdf" or ext in IMAGE_EXTS):
        return primary
    if mode == "force":
        use_ocr = True
    else:
        use_ocr = ext in IMAGE_EXTS or is_thin_text(primary)
    if not use_ocr:
        return primary
    md_text = parse_via_mineru(path, path.name)
    ocr_result = markdown_to_blocks(md_text)
    if not ocr_result.blocks:
        return primary
    return ocr_result
```

`backend/app/services/parsing/image_parser.py`(新建):

```python
from pathlib import Path

from app.services.parsing.base import ParseResult, Parser, register


@register(".jpg")
@register(".jpeg")
@register(".png")
class ImageParser(Parser):
    """图片无本地文本层:占位解析,内容由 ocr.maybe_ocr 的 MinerU 路径产出。"""

    def parse(self, path: Path) -> ParseResult:
        return ParseResult()
```

`parsing/__init__.py` 追加(保证注册执行;若该文件已有导入则并入):

```python
from app.services.parsing import image_parser  # noqa: F401 触发 @register
```

`pipeline.py`:`result = get_parser(...).parse(file_path)` 行替换为:

```python
            result = get_parser(file_path.suffix.lower()).parse(file_path)
            from app.services.parsing.ocr import maybe_ocr

            ocr_result = maybe_ocr(
                file_path, file_path.suffix.lower(), doc.ocr_mode, result
            )
            doc.ocr_used = ocr_result is not result
            result = ocr_result
```

(顶部统一 import 更佳:文件头部 `from app.services.parsing.ocr import maybe_ocr`,循环导入风险低——ocr 不回导 workers。)

`documents.py`:
1. import 加 `Form`、`Literal`;`ALLOWED_EXTS` 改 `{".pdf", ".docx", ".xlsx", ".jpg", ".jpeg", ".png"}`。
2. 端点签名加参数:

```python
    ocr: Literal["auto", "force", "off"] = Form("auto"),
```

3. `Document(...)` 构造加 `ocr_mode=ocr`。

`schemas/document.py` 的 `DocumentOut` 追加:

```python
    ocr_mode: str | None = "auto"
    ocr_used: bool = False
```

`.env.example` 追加一行 `MINERU_API_TOKEN=`(带注释 `# MinerU 云 API token,留空则 OCR 整体关闭`)。

- [x] **Step 4: 跑测试确认通过 + 全量回归**

Run: `py -3.12 -m pytest tests/test_ocr.py -v && py -3.12 -m pytest -q`
Expected: ocr 文件 8 passed / 全量 94 passed
注意:存量 `test_documents.py` 若因图片白名单/表单默认值有断言差异,按新行为修存量断言(允许,记录在提交信息)。

- [x] **Step 5: 提交**

```bash
git add backend/pyproject.toml backend/app/core/config.py backend/app/services/parsing backend/app/workers/pipeline.py backend/app/api/documents.py backend/app/schemas/document.py .env.example backend/tests/test_ocr.py
git commit -m "feat: mineru cloud ocr with auto detection and upload override"
```

### Task 6: 审计挂点 + KB 文档数聚合

**Files:**
- Modify: `backend/app/api/auth.py`(login 成败/register)
- Modify: `backend/app/api/kbs.py`(create/grant/revoke + doc_count 聚合)
- Modify: `backend/app/api/documents.py`(upload/reprocess)
- Modify: `backend/app/api/conversations.py`(delete)
- Modify: `backend/app/api/admin.py`(patch 用户)
- Modify: `backend/app/schemas/kb.py`(KBOut.doc_count)
- Test: `backend/tests/test_audit.py`(追加)

**Interfaces:**
- Consumes: T2 `audit()`;T1 列。
- Produces: `KBOut.doc_count: int = 0`;审计动作枚举 `login_success/login_fail/register/kb_create/kb_grant/kb_revoke/doc_upload/doc_reprocess/conv_delete/user_admin_update`。

- [x] **Step 1: 写失败测试**(追加到 test_audit.py)

```python
async def _seed_action(client, auth_headers):
    """跑一组会触发审计的动作,返回 (username, kb_id)。"""
    me = await client.get("/api/auth/me", headers=auth_headers)
    username = me.json()["username"]
    kb = await client.post("/api/kbs", json={"name": "审计库"}, headers=auth_headers)
    return username, kb.json()["id"]


async def test_audit_hooks_cover_write_paths(client, auth_headers, db_session):
    username, kb_id = await _seed_action(client, auth_headers)

    async def actions_of(action):
        return (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == action)
            )
        ).scalars().all()

    # kb_create
    rows = await actions_of("kb_create")
    assert any(r.username == username and r.target == f"kb:{kb_id}" for r in rows)

    # kb_grant / kb_revoke
    await client.post(
        "/api/auth/register", json={"username": "aud_grantee", "password": "secret123"}
    )
    await client.put(
        f"/api/kbs/{kb_id}/permissions",
        json={"username": "aud_grantee", "perm": "viewer"},
        headers=auth_headers,
    )
    await client.delete(
        f"/api/kbs/{kb_id}/permissions", params={"username": "aud_grantee"},
        headers=auth_headers,
    )
    assert await actions_of("kb_grant")
    assert await actions_of("kb_revoke")

    # doc_upload(fake docx,eager 流水线失败无妨,上传审计在入队前落)
    import io

    up = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("a.docx", io.BytesIO(b"dummy"), "application/vnd...")},
        headers=auth_headers,
    )
    if up.status_code == 201:  # kb 内未重复
        assert await actions_of("doc_upload")

    # conv_delete
    conv = await client.post(
        "/api/chat/conversations", json={"kb_ids": [kb_id], "name": "c"}, headers=auth_headers,
    )
    await client.delete(f"/api/chat/conversations/{conv.json()['id']}", headers=auth_headers)
    assert await actions_of("conv_delete")

    # login_fail / register / user_admin_update
    await client.post(
        "/api/auth/login", json={"username": "aud_grantee", "password": "wrong"}
    )
    assert await actions_of("login_fail")
    assert await actions_of("register")
    admin = await _make_admin(client, db_session, username="aud_admin2")
    target = await client.post(
        "/api/auth/register", json={"username": "aud_target", "password": "secret123"}
    )
    await client.patch(
        f"/api/admin/users/{target.json()['id']}",
        json={"role": "editor"},
        headers=admin,
    )
    rows = await actions_of("user_admin_update")
    assert any("aud_target" in (r.detail or "") for r in rows)

    # login_success
    assert await actions_of("login_success")


async def test_kb_list_includes_doc_count(client, auth_headers, db_session):
    import io

    username, kb_id = await _seed_action(client, auth_headers)
    await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("n.docx", io.BytesIO(b"dummy"), "application/octet-stream")},
        headers=auth_headers,
    )
    lst = await client.get("/api/kbs", headers=auth_headers)
    by_id = {k["id"]: k for k in lst.json()}
    assert by_id[kb_id]["doc_count"] == 1
    empty_kb = await client.post("/api/kbs", json={"name": "空库"}, headers=auth_headers)
    assert by_id[empty_kb.json()["id"]]["doc_count"] == 0
    detail = await client.get(f"/api/kbs/{kb_id}", headers=auth_headers)
    assert detail.json()["doc_count"] == 1
```

- [x] **Step 2: 跑测试确认失败**

Run: `py -3.12 -m pytest tests/test_audit.py -v`
Expected: 新用例 FAIL(无 kb_create 审计/doc_count 键缺失)

- [x] **Step 3: 实现(六处挂点 + 聚合)**

统一模式:各文件 import `from fastapi import Request`(需要的端点)与 `from app.services.audit import audit`;调用 `await audit(db, <username>, <action>, <target>, <detail>, ip)` 后**与业务同 commit**(已存在的 commit 覆盖;若挂点在 commit 之后,把 audit 调用挪到 commit 之前)。

`auth.py`:
- register:`db.add(user)` 后、commit 前加 `await audit(db, user.username, "register", f"user:{user.id}")`(flush 后才有 id:在 `await db.flush()` 后取 user.id,或 target 置 "user:?";实现取 flush 方案:register 现有代码在 commit 后 refresh,插入 `await db.flush()` 再 audit 再 commit)。
- login:签名加 `request: Request`;成功路径 return 前:`await audit(db, payload.username, "login_success", f"user:{user.id}", ip=request.client.host if request.client else None); await db.commit()`;两个 401 分支(凭证错/禁用)raise 前:`await audit(db, payload.username, "login_fail", ip=...); await db.commit()`。

`kbs.py`:
- import 加 `func`、`Document`、audit。
- create_kb:`db.add(kb)` 后加 `await db.flush()`(取 id)→ `await audit(db, current.username, "kb_create", f"kb:{kb.id}", {"name": payload.name})` → 原有 commit。
- grant_permission:commit 前 `await audit(db, current.username, "kb_grant", f"kb:{kb_id}", {"to": target.username, "perm": payload.perm})`。
- revoke_permission:commit 前 `await audit(db, current.username, "kb_revoke", f"kb:{kb_id}", {"from": username})`。
- list_kbs 两个分支(admin/普通)在返回前统一聚合:

```python
    doc_counts = dict(
        (await db.execute(
            select(Document.kb_id, func.count(Document.id)).group_by(Document.kb_id)
        )).all()
    )
```

  循环内 `item.doc_count = doc_counts.get(kb.id, 0)`。
- get_kb:单库 `item.doc_count = (await db.execute(select(func.count(Document.id)).where(Document.kb_id == kb_id))).scalar_one()`。

`schemas/kb.py` 的 KBOut 追加 `doc_count: int = 0`。

`documents.py`:
- upload:`db.add(doc)` 后加 `await db.flush()`,commit 前 `await audit(db, current.username, "doc_upload", f"doc:{doc.id}", {"filename": original, "kb_id": kb_id})`。
- reprocess:commit 前 `await audit(db, current.username, "doc_reprocess", f"doc:{doc_id}")`。

`conversations.py` delete:commit 前 `await audit(db, current.username, "conv_delete", f"conv:{conv_id}", {"title": conv.title})`。

`admin.py` patch:构造变更摘要(`changes = {k: v for k, v in {"role": payload.role, "is_active": payload.is_active}.items() if v is not None}`,commit 前 `await audit(db, current.username, "user_admin_update", f"user:{user_id}", {"target": user.username, **changes})`)。

- [x] **Step 4: 跑测试确认通过 + 全量回归**

Run: `py -3.12 -m pytest tests/test_audit.py -v && py -3.12 -m pytest -q`
Expected: audit 文件 6 passed / 全量 96 passed

- [x] **Step 5: 提交**

```bash
git add backend/app/api backend/app/schemas/kb.py backend/tests/test_audit.py
git commit -m "feat: audit hooks on write endpoints and kb doc_count aggregation"
```

### Task 7: 会话导出 markdown

**Files:**
- Modify: `backend/app/api/conversations.py`
- Test: `backend/tests/test_conversations.py`(追加)

**Interfaces:**
- Produces: `GET /api/chat/conversations/{id}/export` → `text/markdown` 附件 `conv-{id}.md`(owner only,他人/不存在 404)。

- [x] **Step 1: 写失败测试**(追加到 test_conversations.py;沿用该文件既有的建会话 helper/头,若无则按 test_ask 的 client/auth_headers 直用)

```python
async def test_export_conversation_markdown(client, auth_headers, db_session):
    kb = await client.post("/api/kbs", json={"name": "导出库"}, headers=auth_headers)
    conv = await client.post(
        "/api/chat/conversations",
        json={"kb_ids": [kb.json()["id"]], "name": "导出会话"},
        headers=auth_headers,
    )
    conv_id = conv.json()["id"]
    from app.models import Message

    db_session.add(Message(conversation_id=conv_id, role="user", content="问个问题"))
    db_session.add(
        Message(
            conversation_id=conv_id, role="assistant", content="答案[1]",
            citations=[
                {
                    "number": 1, "chunk_id": 1, "document_id": 2,
                    "filename": "a.pdf", "page_no": 3, "excerpt": "引用摘录",
                }
            ],
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/chat/conversations/{conv_id}/export", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert 'attachment; filename="conv' in resp.headers["content-disposition"]
    body = resp.text
    assert "导出会话" in body
    assert "问个问题" in body
    assert "答案[1]" in body
    assert "a.pdf" in body and "引用摘录" in body  # 引用附录

    other = await client.post(
        "/api/auth/register", json={"username": "exp_other", "password": "secret123"}
    )
    login = await client.post(
        "/api/auth/login", json={"username": "exp_other", "password": "secret123"}
    )
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}
    denied = await client.get(f"/api/chat/conversations/{conv_id}/export", headers=h)
    assert denied.status_code == 404
```

- [x] **Step 2: 跑测试确认失败**

Run: `py -3.12 -m pytest tests/test_conversations.py -v`
Expected: 新用例 FAIL(404,路由不存在)

- [x] **Step 3: 实现**(conversations.py 追加;import 加 `Response`)

```python
@router.get("/conversations/{conv_id}/export")
async def export_conversation(
    conv_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(Conversation, conv_id)
    if conv is None or conv.user_id != current.id:
        raise HTTPException(status_code=404, detail="conversation not found")
    rows = (
        await db.execute(
            select(Message).where(Message.conversation_id == conv_id).order_by(Message.id)
        )
    ).scalars().all()

    lines = [f"# {conv.title}", "", f"> 创建时间:{conv.created_at:%Y-%m-%d %H:%M}", ""]
    appendix: list[tuple[str, int | None, str]] = []
    for m in rows:
        speaker = "用户" if m.role == "user" else "助手"
        lines.append(f"**{speaker}**:{m.content}")
        lines.append("")
        for c in m.citations or []:
            appendix.append((c.get("filename", "?"), c.get("page_no"), c.get("excerpt", "")))
    if appendix:
        lines.append("---")
        lines.append("")
        lines.append("## 引用附录")
        for i, (fname, page, excerpt) in enumerate(appendix, start=1):
            page_s = f"第{page}页" if page else "页码未知"
            lines.append(f"[{i}] {fname} {page_s}:{excerpt}")
            lines.append("")
    md_text = "\n".join(lines)
    return Response(
        content=md_text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="conv-{conv_id}.md"'},
    )
```

- [x] **Step 4: 跑测试确认通过 + 全量回归**

Run: `py -3.12 -m pytest tests/test_conversations.py -v && py -3.12 -m pytest -q`
Expected: 会话文件含新用例 PASS / 全量 97 passed

- [x] **Step 5: 提交**

```bash
git add backend/app/api/conversations.py backend/tests/test_conversations.py
git commit -m "feat: export conversation as markdown with citation appendix"
```

### Task 8: 检索评估集(JSON + CLI)

**Files:**
- Create: `backend/scripts/__init__.py`(空文件)
- Create: `backend/scripts/eval_metrics.py`
- Create: `backend/scripts/eval_retrieval.py`
- Create: `backend/eval_sets/README.md`
- Test: `backend/tests/test_eval_metrics.py`(新建)

**Interfaces:**
- Produces: `hit_at_k(retrieved_doc_ids: list[int], expect_doc_ids: list[int]) -> bool`;`mrr(retrieved_doc_ids, expect_doc_ids) -> float`;`keyword_recall(hit_contents: list[str], expect_keywords: list[str]) -> float`;`load_eval_set(path: Path) -> dict`;CLI `py -m scripts.eval_retrieval --kb 3 [--top-k 8] [--rerank] [--json]`。

- [x] **Step 1: 写失败测试**(test_eval_metrics.py 新建)

```python
from pathlib import Path

from scripts.eval_metrics import hit_at_k, keyword_recall, load_eval_set, mrr


def test_hit_at_k():
    assert hit_at_k([5, 9, 2], [2]) is True
    assert hit_at_k([5, 9], [2]) is False
    assert hit_at_k([], [1]) is False
    assert hit_at_k([1], []) is False


def test_mrr():
    assert mrr([3, 7], [3]) == 1.0
    assert mrr([5, 3, 7], [3]) == 0.5
    assert mrr([1, 2], [9]) == 0.0


def test_keyword_recall():
    assert keyword_recall(["预算三千万元"], ["预算", "三千"]) == 1.0
    assert keyword_recall(["预算三千万元"], ["预算", "负责人"]) == 0.5
    assert keyword_recall(["内容"], []) == 1.0
    assert keyword_recall([], ["任何"]) == 0.0


def test_load_eval_set(tmp_path):
    f = tmp_path / "1.json"
    f.write_text(
        '{"kb_id": 1, "items": [{"question": "q", "expect_doc_ids": [1], "expect_keywords": ["k"]}]}',
        encoding="utf-8",
    )
    data = load_eval_set(f)
    assert data["kb_id"] == 1
    assert data["items"][0]["question"] == "q"
```

(conftest 位于 backend/tests,`scripts` 包在 backend/ 下——pytest 从 backend 目录跑时 sys.path 含当前目录,`import scripts.eval_metrics` 可用;若不可用,在 test 文件顶部加 `import sys; sys.path.insert(0, str(Path(__file__).resolve().parents[1]))`。)

- [x] **Step 2: 跑测试确认失败**

Run: `py -3.12 -m pytest tests/test_eval_metrics.py -v`
Expected: FAIL(ModuleNotFoundError: scripts)

- [x] **Step 3: 实现**

`scripts/__init__.py`:空文件。
`scripts/eval_metrics.py`:

```python
import json
from pathlib import Path


def hit_at_k(retrieved_doc_ids: list[int], expect_doc_ids: list[int]) -> bool:
    top = set(retrieved_doc_ids)
    return any(d in top for d in expect_doc_ids)


def mrr(retrieved_doc_ids: list[int], expect_doc_ids: list[int]) -> float:
    exp = set(expect_doc_ids)
    for rank, doc_id in enumerate(retrieved_doc_ids, start=1):
        if doc_id in exp:
            return 1.0 / rank
    return 0.0


def keyword_recall(hit_contents: list[str], expect_keywords: list[str]) -> float:
    if not expect_keywords:
        return 1.0
    blob = "\n".join(hit_contents)
    found = [k for k in expect_keywords if k in blob]
    return round(len(found) / len(expect_keywords), 4)


def load_eval_set(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
```

`scripts/eval_retrieval.py`:

```python
"""检索评估 CLI。

用法(backend 目录下):
    py -m scripts.eval_retrieval --kb 3 [--top-k 8] [--rerank] [--json]

评估集文件:eval_sets/{kb_id}.json,格式:
    {"kb_id": 3, "items": [{"question": "...", "expect_doc_ids": [12],
                            "expect_keywords": ["关键词"]}]}
指标:hit@k / MRR / 关键词 recall(纯检索,无 LLM)。
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from scripts.eval_metrics import hit_at_k, keyword_recall, load_eval_set, mrr

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval_sets"


async def run(kb_id: int, top_k: int, use_rerank: bool) -> list[dict]:
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models import KnowledgeBase
    from app.services.retrieval.searcher import hybrid_search
    from app.services.rerank.base import get_reranker

    set_path = EVAL_DIR / f"{kb_id}.json"
    if not set_path.exists():
        sys.exit(f"eval set not found: {set_path}(格式见 eval_sets/README.md)")
    data = load_eval_set(set_path)

    reranker = get_reranker() if use_rerank else None
    results = []
    async with SessionLocal() as db:
        kb = await db.get(KnowledgeBase, kb_id)
        if kb is None:
            sys.exit(f"knowledge base {kb_id} not found")
        for item in data["items"]:
            hits = await hybrid_search(db, [kb_id], item["question"], top_k)
            if reranker and hits:
                order = reranker.rerank(
                    item["question"],
                    [h.content for h in hits],
                    top_k,
                )
                hits = [hits[i] for i in order if 0 <= i < len(hits)]
            doc_ids = [h.document_id for h in hits]
            results.append(
                {
                    "question": item["question"],
                    "hit_at_k": hit_at_k(doc_ids, item.get("expect_doc_ids", [])),
                    "mrr": mrr(doc_ids, item.get("expect_doc_ids", [])),
                    "keyword_recall": keyword_recall(
                        [h.content for h in hits], item.get("expect_keywords", [])
                    ),
                }
            )
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", type=int, required=True)
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results = asyncio.run(run(args.kb, args.top_k, args.rerank))
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    print(f"{'问题':<28} hit@{args.top_k}  MRR    关键词recall")
    for r in results:
        print(
            f"{r['question'][:26]:<28} {str(r['hit_at_k']):<7} "
            f"{r['mrr']:<6.3f} {r['keyword_recall']}"
        )
    n = len(results)
    print(
        f"\n汇总:n={n}  hit={sum(r['hit_at_k'] for r in results) / n:.2f}  "
        f"MRR={sum(r['mrr'] for r in results) / n:.3f}  "
        f"recall={sum(r['keyword_recall'] for r in results) / n:.3f}"
    )


if __name__ == "__main__":
    main()
```

`eval_sets/README.md`:

```markdown
# 检索评估集

每个知识库一个文件 `{kb_id}.json`,git 版本化。运行(backend 目录):

    py -m scripts.eval_retrieval --kb <kb_id> [--top-k 8] [--rerank] [--json]

字段:
- `question`:评估问题
- `expect_doc_ids`:期望命中的文档 id 列表(hit@k / MRR)
- `expect_keywords`:期望出现在 top-k 内容中的关键词(recall)

示例见 M5 验收(T12)生成的样例文件。
```

- [x] **Step 4: 跑测试确认通过 + 全量回归**

Run: `py -3.12 -m pytest tests/test_eval_metrics.py -v && py -3.12 -m pytest -q`
Expected: 4 passed / 全量 101 passed

- [x] **Step 5: 提交**

```bash
git add backend/scripts backend/eval_sets backend/tests/test_eval_metrics.py
git commit -m "feat: retrieval eval set cli with hit mrr recall metrics"
```

### Task 9: 前端 API 模块 + 审计页 + 路由/菜单/高亮

**Files:**
- Modify: `frontend/src/api/admin.ts`(listAuditLogs)
- Modify: `frontend/src/api/chat.ts`(exportConversation)
- Modify: `frontend/src/api/kb.ts`(doc_count)
- Modify: `frontend/src/api/documents.ts`(ocr 参数与字段)
- Create: `frontend/src/pages/AuditLogPage.vue`
- Modify: `frontend/src/router/index.ts`
- Modify: `frontend/src/layouts/MainLayout.vue`

**Interfaces:**
- Consumes: T2 的 `/admin/audit-logs` 契约 `{total, items}`;T6 的 `doc_count`;T5 的 `ocr_mode/ocr_used`;T7 的 export 端点。
- Produces: `adminApi.listAuditLogs(params)`、`conversationsApi.export(id): Promise<Blob>`、`KbItem.doc_count`、`DocumentItem.ocr_mode/ocr_used`、`documentsApi.upload(kbId, file, onProgress?, ocr?)`、路由 `admin-audit-logs`。

- [x] **Step 1: API 模块修改**

`admin.ts` 追加:

```ts
/** 镜像后端 AuditLogOut(app/schemas/admin.py) */
export interface AuditLogItem {
  id: number
  username: string
  action: string
  target: string
  detail: string | null
  ip: string | null
  created_at: string
}

export interface AuditLogQuery {
  username?: string
  action?: string
  page?: number
  page_size?: number
}

export interface AuditLogResponse {
  total: number
  items: AuditLogItem[]
}
```

adminApi 对象内追加:

```ts
  async listAuditLogs(params: AuditLogQuery): Promise<AuditLogResponse> {
    const { data } = await http.get<AuditLogResponse>('/admin/audit-logs', { params })
    return data
  },
```

`chat.ts`:ConversationItem 不变;conversationsApi 追加:

```ts
  async export(conversationId: number): Promise<Blob> {
    const { data } = await http.get<Blob>(`/chat/conversations/${conversationId}/export`, {
      responseType: 'blob',
    })
    return data
  },
```

`kb.ts` 的 KbItem 在 `my_perm` 前加 `doc_count: number`。
`documents.ts`:DocumentItem 在 `chunk_count` 前加 `ocr_mode: string | null` 与 `ocr_used: boolean`;upload 签名与 form 追加:

```ts
  async upload(
    kbId: number,
    file: File,
    onProgress?: (percent: number) => void,
    ocr: 'auto' | 'force' | 'off' = 'auto',
  ): Promise<DocumentItem> {
    const form = new FormData()
    form.append('file', file)
    form.append('ocr', ocr)
```

- [x] **Step 2: AuditLogPage.vue**(新建,风格沿 UsersPage)

```vue
<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { adminApi, type AuditLogItem } from '@/api/admin'

const loading = ref(false)
const logs = ref<AuditLogItem[]>([])
const total = ref(0)
const query = reactive({ username: '', action: '', page: 1, pageSize: 20 })

const ACTION_OPTIONS = [
  'login_success', 'login_fail', 'register', 'kb_create',
  'kb_grant', 'kb_revoke', 'doc_upload', 'doc_reprocess',
  'conv_delete', 'user_admin_update', 'ask',
]

const ACTION_LABEL: Record<string, string> = {
  login_success: '登录成功',
  login_fail: '登录失败',
  register: '注册',
  kb_create: '建知识库',
  kb_grant: '授权',
  kb_revoke: '取消授权',
  doc_upload: '上传文档',
  doc_reprocess: '重新解析',
  conv_delete: '删会话',
  user_admin_update: '用户管理变更',
  ask: '提问',
}

const ACTION_TYPE: Record<string, 'success' | 'danger' | 'warning' | 'info' | 'primary'> = {
  login_success: 'success',
  login_fail: 'danger',
  ask: 'primary',
}

function label(a: string) {
  return ACTION_LABEL[a] ?? a
}

function tagType(a: string) {
  return ACTION_TYPE[a] ?? 'info'
}

async function load() {
  loading.value = true
  try {
    const resp = await adminApi.listAuditLogs({
      username: query.username || undefined,
      action: query.action || undefined,
      page: query.page,
      page_size: query.pageSize,
    })
    logs.value = resp.items
    total.value = resp.total
  } catch {
    ElMessage.error('加载审计日志失败')
  } finally {
    loading.value = false
  }
}

function search() {
  query.page = 1
  load()
}

function fmtTime(iso: string) {
  return iso.replace('T', ' ').slice(0, 19)
}

onMounted(load)
</script>

<template>
  <div class="audit-page">
    <div class="page-header">
      <h2>审计日志</h2>
      <div class="filters">
        <el-input
          v-model="query.username"
          placeholder="用户名"
          clearable
          class="filter-input"
          @keyup.enter="search"
        />
        <el-select v-model="query.action" placeholder="动作" clearable class="filter-select">
          <el-option v-for="a in ACTION_OPTIONS" :key="a" :label="label(a)" :value="a" />
        </el-select>
        <el-button type="primary" @click="search">查询</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="logs" class="audit-table">
      <template #empty>
        <el-empty description="暂无审计记录" />
      </template>
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column prop="username" label="用户" width="130" />
      <el-table-column label="动作" width="130">
        <template #default="{ row }">
          <el-tag :type="tagType(row.action)" size="small">{{ label(row.action) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="target" label="对象" width="110" />
      <el-table-column prop="detail" label="详情" min-width="260" show-overflow-tooltip />
      <el-table-column prop="ip" label="IP" width="130" />
    </el-table>

    <el-pagination
      v-model:current-page="query.page"
      v-model:page-size="query.pageSize"
      class="audit-pagination"
      layout="total, prev, pager, next, sizes"
      :page-sizes="[20, 50, 100]"
      :total="total"
      @current-change="load"
      @size-change="search"
    />
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.page-header h2 {
  margin: 0;
}
.filters {
  display: flex;
  gap: 8px;
}
.filter-input {
  width: 160px;
}
.filter-select {
  width: 160px;
}
.audit-table {
  width: 100%;
}
.audit-pagination {
  margin-top: 12px;
  justify-content: flex-end;
}
</style>
```

- [x] **Step 3: 路由与布局**

`router/index.ts` 的 children 里 `admin/users` 之后加:

```ts
        { path: 'admin/audit-logs', name: 'admin-audit-logs', component: () => import('@/pages/AuditLogPage.vue') },
```

`MainLayout.vue`:
1. import 加 `computed`;
2. script 加:

```ts
// kb 子路由(/kb/3/docs)时菜单仍高亮"知识库"
const activeIndex = computed(() => (route.path.startsWith('/kb') ? '/kb' : route.path))
```

3. `<el-menu :default-active="route.path" router>` 改 `:default-active="activeIndex"`;
4. 用户管理菜单项后加:

```html
        <el-menu-item v-if="auth.user?.role === 'admin'" index="/admin/audit-logs">审计日志</el-menu-item>
```

- [x] **Step 4: 构建 + 测试 + lint**

Run: `cd E:\Projects\AIRag\frontend && pnpm build && pnpm test && pnpm oxlint`
Expected: build 成功、4 passed、0 errors

- [x] **Step 5: 提交**

```bash
git add frontend/src
git commit -m "feat: audit log page, frontend api modules, menu highlight"
```

### Task 10: ChatPage 增强(导出 / 渲染节流 / Alt+Enter)

**Files:**
- Create: `frontend/src/utils/throttle.ts`
- Modify: `frontend/src/pages/ChatPage.vue`
- Test: `frontend/src/utils/__tests__/throttle.spec.ts`(新建)

**Interfaces:**
- Produces: `throttle(fn, ms)`(尾随触发版);ChatMessage 增 `html?: string`(渲染缓存,模板只 v-html m.html)。

- [x] **Step 1: 写失败测试**(throttle.spec.ts)

```ts
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { throttle } from '@/utils/throttle'

describe('throttle', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('coalesces bursts into fewer calls with trailing fire', () => {
    const fn = vi.fn()
    const t = throttle(fn, 120)
    for (let i = 0; i < 50; i++) t(i) // 一帧内 50 次
    expect(fn.mock.calls.length).toBeLessThanOrEqual(2) // 首次 + 至多一次尾随
    vi.advanceTimersByTime(200)
    expect(fn.mock.calls.length).toBeLessThanOrEqual(2)
  })

  it('fires again after the window passes', () => {
    const fn = vi.fn()
    const t = throttle(fn, 100)
    t(1)
    vi.advanceTimersByTime(150)
    t(2)
    expect(fn).toHaveBeenCalledTimes(2)
    expect(fn).toHaveBeenLastCalledWith(2)
  })
})
```

(vitest 配置若已有 @ 别名即可用;若 `frontend/src/utils/__tests__` 目录的 spec 不被收集,检查 vitest 配置 include,通常 `src/**/__tests__/**/*.spec.ts` 默认命中。)

- [x] **Step 2: 跑测试确认失败**

Run: `pnpm test`
Expected: FAIL(Cannot find module '@/utils/throttle')

- [x] **Step 3: 实现 throttle.ts**

```ts
/** 首次立即执行 + 窗口内合并 + 尾随补发的节流(流式渲染用)。 */
export function throttle<F extends (...args: never[]) => void>(fn: F, ms: number) {
  let last = 0
  let timer: number | undefined
  const wrapped = (...args: Parameters<F>) => {
    const now = Date.now()
    if (now - last >= ms) {
      last = now
      fn(...args)
    } else if (timer === undefined) {
      timer = window.setTimeout(() => {
        timer = undefined
        last = Date.now()
        fn(...args)
      }, ms - (now - last))
    }
  }
  return wrapped
}
```

- [x] **Step 4: 跑单测确认通过**

Run: `pnpm test`
Expected: 6 passed(4 存量 + 2 新)

- [x] **Step 5: ChatPage 接入**

1. import:`import { Download } from '@element-plus/icons-vue'`(并入现有 Delete import 行)、`import { throttle } from '@/utils/throttle'`。
2. `ChatMessage` 接口加 `html?: string`。
3. `toChatMessage` 返回值加 `html: render(m.content)`。
4. `removeConversation` 之后加导出函数:

```ts
async function exportConversation(c: ConversationItem) {
  try {
    const blob = await conversationsApi.export(c.id)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `conv-${c.id}.md`
    a.click()
    URL.revokeObjectURL(url)
  } catch {
    ElMessage.error('导出失败')
  }
}
```

5. `onSend` 里 assistant 创建后、ask 前定义节流渲染并改回调:

```ts
  const flushRender = throttle(() => {
    assistant.html = render(assistant.content)
  }, 120)
```

   `onToken` 改:

```ts
      onToken(t) {
        assistant.content += t
        flushRender()
        scrollToBottom()
      },
```

   `onDone` 里 `assistant.content = d.answer` 后加 `assistant.html = render(d.answer)`;`onSend` 开头 push 用户消息处加 `html`:

```ts
  messages.value.push({ role: 'user', content: q, html: render(q) })
```

   assistant reactive 初始对象加 `html: ''`。
6. 模板助手气泡行 `v-html="render(m.content)"` 改 `v-html="m.html"`。
7. 键盘:`onEnterKey` 整体替换:

```ts
function onEnterKey(e: KeyboardEvent) {
  if (e.isComposing) return
  // Alt/Meta+Enter:发送;Shift+Enter:换行;Enter:发送
  if (e.shiftKey) return
  if (e.altKey || e.metaKey || e.key === 'Enter') {
    e.preventDefault()
    onSend()
  }
}
```

   placeholder 改 `Enter 或 Alt+Enter 发送,Shift+Enter 换行`。
8. 会话项删除图标旁加导出图标(conv-item 内、Delete 之前):

```html
          <el-icon class="conv-export" :size="14" @click.stop="exportConversation(c)">
            <Download />
          </el-icon>
```

   样式追加(沿 conv-delete 模式):

```css
.conv-export {
  flex-shrink: 0;
  color: var(--el-text-color-secondary);
  visibility: hidden;
}
.conv-item:hover .conv-export {
  visibility: visible;
}
.conv-export:hover {
  color: var(--el-color-primary);
}
```

- [x] **Step 6: 构建 + 测试 + lint**

Run: `pnpm build && pnpm test && pnpm oxlint`
Expected: build 绿、6 passed、0 errors

- [x] **Step 7: 提交**

```bash
git add frontend/src
git commit -m "feat: chat export button, throttled markdown render, alt-enter send"
```

### Task 11: KbPage 文档数 + DocsPage(图片/OCR 标记/轮询修复)+ oxlint 清偿

**Files:**
- Modify: `frontend/src/pages/KbPage.vue`(文档数列)
- Modify: `frontend/src/pages/DocsPage.vue`(白名单/accept/OCR 标记/轮询静默)
- Modify: `frontend/src/stores/__tests__/auth.store.spec.ts`(三处 vi.fn 类型参数)

**Interfaces:**
- Consumes: T6 `doc_count`、T5 `ocr_used` 与图片白名单。

- [x] **Step 1: KbPage 加列**——"我的权限"列前插:

```html
      <el-table-column prop="doc_count" label="文档数" width="90" align="center" />
```

- [x] **Step 2: DocsPage 修改**

1. `ALLOWED_EXTS` 改 `['.pdf', '.docx', '.xlsx', '.jpg', '.jpeg', '.png']`;
2. beforeUpload 错误文案改 `仅支持 .pdf/.docx/.xlsx/.jpg/.png`;
3. el-upload 的 `accept=".pdf,.docx,.xlsx"` 改 `accept=".pdf,.docx,.xlsx,.jpg,.jpeg,.png"`,tip 文案同步加图片;
4. 状态列模板内、状态 tag 后追加 OCR 标记:

```html
            <el-tag v-if="row.ocr_used" type="success" size="small" class="ocr-tag">OCR</el-tag>
```

   (样式:`.ocr-tag { margin-left: 4px; }`,与状态 tag 并排需把两者包进 `<span>`:直接在 template 里用 `<span class="status-cell">` 包住两个 tag,style 加 `.status-cell { display: inline-flex; gap: 4px; align-items: center; }`,可省 ocr-tag 样式。)
5. 轮询静默:`async function load(silent = false)`——`loading.value = true` 改 `if (!silent) loading.value = true`,finally 同理 `if (!silent) loading.value = false`;`syncPolling` 里 `timer = window.setInterval(load, 3000)` 改 `timer = window.setInterval(() => load(true), 3000)`;onMounted 仍 `load()`(带 loading)。
6. 卸载窄窗:`let disposed = false`;`load` 开头加 `if (disposed) return`;`onUnmounted` 改 `disposed = true; if (timer !== undefined) window.clearInterval(timer)`;`syncPolling` 开头加 `if (disposed) return`。

- [x] **Step 3: oxlint 三错清偿**(auth.store.spec.ts 7-9 行)

```ts
    login: vi
      .fn<() => Promise<{ access_token: string; token_type: string }>>()
      .mockResolvedValue({ access_token: 'jwt-token', token_type: 'bearer' }),
    register: vi
      .fn<
        () => Promise<{
          id: number
          username: string
          role: string
          is_active: boolean
        }>
      >()
      .mockResolvedValue({ id: 1, username: 'alice', role: 'admin', is_active: true }),
    me: vi.fn<() => Promise<unknown>>(),
```

(与现有 mockResolvedValue 字面量对齐;若 role 字面量是 `'admin' as const` 之类,类型里用 `string` 兼容即可。)

- [x] **Step 4: 构建 + 测试 + lint + 全后端回归**

Run: `pnpm build && pnpm test && pnpm oxlint`
Expected: build 绿、6 passed、**0 errors 0 warnings**
Run: `cd E:\Projects\AIRag\backend && py -3.12 -m pytest -q`
Expected: 101 passed(前后端互不影响,例行回归)

- [x] **Step 5: 提交**

```bash
git add frontend/src
git commit -m "feat: kb doc count column, ocr tag, image upload and poll polish"
```

### Task 12: M5 端到端无头验收 + 收尾

**Files:**
- Create: `backend/scripts/m5_acceptance.py`(验收脚本,跑完保留供复现)
- Create: `backend/eval_sets/<验收库id>.json`(由脚本生成)

**Interfaces:**
- Consumes: 全部前序任务;真栈环境(PG/Redis/智谱 key/MinerU token)。

- [x] **Step 1: 起全栈**

确认 PG18/Redis 服务在跑 → `start_dev.bat`(后端)→ `start_worker.bat`(worker)→ 前端 `pnpm dev`。`.env` 确认 `ZHIPU_API_KEY` 已填;`MINERU_API_TOKEN` 若用户已提供则填(未提供则 Step 5 的 OCR 真调项跳过并记录)。

- [x] **Step 2: 造图片型 PDF 验收样例**(脚本内实现)

```python
def make_scanned_pdf(path: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (600, 800), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 24)
    d.text((40, 80), "星际探索项目预算总额为三千万元", fill="black", font=font)
    d.text((40, 130), "项目负责人是张三丰", fill="black", font=font)
    d.text((40, 180), "项目周期为2026至2028年", fill="black", font=font)
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_image(fitz.Rect(0, 0, 600, 800), filename=None, pixmap=fitz.Pixmap(img))
    doc.save(path)
```

(fitz.Pixmap(PIL Image) 接受 Image 对象;若该构造不可用,先 img.save(buf, format="png") 再 insert_image(filename=buf path)。)

- [x] **Step 3: 验收脚本(m5_acceptance.py,httpx 调真后端 http://127.0.0.1:8000/api,全部断言打印 PASS/FAIL)**

脚本按序执行并断言(伪码列全步骤,实现照此展开;沿用 M4 脚本的注册/SQL 晋升/轮询 helper):

1. **基线**:注册 `m5_user`(升 admin)+ `m5_owner`(升 editor);`GET /api/health` 200。
2. **OCR 全链路**(需 MINERU_API_TOKEN):`m5_owner` 建库"M5验收库" → 造扫描 PDF → 上传(`ocr=auto`)→ 轮询 done(上限 6 分钟,MinerU 云解析)→ `ocr_used == True`、`chunk_count > 0`;`GET chunks` 内容含"三千万"。
3. **ocr=off 对照**:同文件改名再传(`ocr=off`)→ 轮询 failed(零文本层 → no content extracted)。
4. **图片直传**:`.png` 同内容 → `ocr=auto` → done(如限额紧张可跳过,标记 OPTIONAL)。
5. **agentic 多轮**:对该库 ask"星际探索项目的预算总额是多少"(真智谱 LLM,SSE 断言 token→citations→done,答案含"三千");再带 conversation_id 问"它的负责人是谁" → 断言答案含"张三丰"(改写消解指代;两次断言失败重试一次)。
6. **agentic 关闭回归**:临时 `.env` 设 `AGENTIC_REWRITE_ENABLED=false` 重启后端 → 单轮 ask 正常 done → 还原重启。
7. **审计矩阵**:依次触发 login_fail/login_success/register/kb_create/kb_grant/kb_revoke/doc_upload/conv_delete/user_admin_update/ask → `GET /admin/audit-logs`(m5_user)按 action 逐项断言存在;`username=m5_owner` 筛选 total>0;分页 page_size=1 正常;`m5_owner` 访问 403。
8. **导出**:`GET /chat/conversations/{id}/export`(第 5 步的会话)→ 200、content-type markdown、含问题与"三千"、含引用附录(filename)。
9. **评估集**:对"M5验收库"写 `eval_sets/{kb_id}.json`(3 条:预算/负责人/周期,expect_doc_ids=[上传文档 id])→ `py -m scripts.eval_retrieval --kb {kb_id} --json` → 断言输出 JSON 3 条、hit 全 true、MRR==1.0。
10. **Minor 抽查**(curl/页面数据):`GET /api/kbs` 返回含 `doc_count>=1`;上传响应含 `ocr_mode/ocr_used` 字段。

脚本结束打印 `M5 ACCEPTANCE: N/M PASS` 与失败明细。

- [x] **Step 4: 跑验收与全量回归**

Run: `cd E:\Projects\AIRag\backend && py -3.12 scripts/m5_acceptance.py`
Expected: 全部 PASS(MinerU token 未提供时第 2/4 项记 SKIP 并在结尾提示)
Run: `py -3.12 -m pytest -q`(101 passed)+ `pnpm build && pnpm test && pnpm oxlint`(绿/6 passed/0 errors)。

- [x] **Step 5: 浏览器走查留给用户**(同 M4 模式):审计页(登录 admin 账号)/导出按钮/OCR 标记/Alt+Enter/文档数列。清进程(后端/worker/前端 dev)。

- [x] **Step 6: 收尾提交与交接**

```bash
git add backend/scripts/m5_acceptance.py backend/eval_sets
git commit -m "chore: m5 acceptance script and eval set"
git commit --allow-empty -m "chore: m5 complete - acceptance verified"
```

推送 main;计划文件勾选同步;记忆更新(M5 完成状态 + M6 交接:LDAP 仍开放、RAGAS faithfulness 类指标、MinerU 本地化选项、评估集维护约定)。

---

## 计划自审记录(写完即检)

1. **Spec 覆盖**:§1 图三节点+tag 过滤+checkpointer=T3/T4;§2 OCR 全链路(决策/客户端/图片/迁移/白名单)=T5;§3 审计表/API/挂点/导出=T1/T2/T6/T7;§4 评估集=T8;§5 Minor 九项:make_chat_llm 单例与 rerank 防御=T3、节流/Alt+Enter/导出按钮=T10、文档数=T6+T11、高亮/审计页=T9、轮询=T11、oxlint=T11;checkpointer=T4。§6 验收=T12。无遗漏。
2. **占位符扫描**:T5 Step 0 的 API 契约核对是显式验证步骤(代码已给全,核对不符改常量),非 TBD;T12 Step 3 为步骤级清单+脚本骨架(M4 同模式),实现含完整断言列项。无 "TODO/类似 Task N"。
3. **类型一致性**:`maybe_ocr(path, ext, ocr_mode, primary)` 定义与 T5 pipeline 调用一致;`audit(db, username, action, target, detail, ip)` 六处挂点一致;`route_after_grade`/`transform_node`/`rewrite_node(state, llm)`/`grade_node(state, llm)` 在 T3 测试与 graph.py 接线一致;前端 `listAuditLogs(params)→{total,items}` 与 T2 API 返回一致;`upload(..., ocr)` 与后端 Form 字段名 `ocr` 一致;`export(id): Promise<Blob>` 与 responseType blob 一致。
4. **计数一致性**(按测试函数):70 → T1+2=72 → T2+2=74 → T3+10=84 → T4+2=86 → T5+8=94 → T6+2=96 → T7+1=97 → T8+4=101;前端 4 → T10+2=6。(T5 计 8 个测试函数、T3 计 10 个,与上文列表一致。)
5. **风险预置**:astream_events 的 tags 过滤对 FakeListChatModel 的兼容由 T4 第一个测试直接验证(不成立则按 spec §7 退化方案改 ev name/run_id 区分);MinerU API 字段漂移由 T5 Step 0 + MockTransport 双保险;`("grade","generate")` 条件边断言给了降级路径(不许改弱其他断言);conftest 三 env 必须先于 app import(T3 Step 1 置顶)。

---

## M5 执行记录(2026-09-16,验收后固化)

### 结果
- 计数:后端 **103 passed**(70→103,计划预算 101,图测试实增 11 项非 10);前端 build 绿 + vitest **6 passed**(4→6);**oxlint 0 warnings 0 errors**(三处存量旧错清偿)。
- 无头验收 **19 PASS / 0 FAIL / 2 SKIP**:`.venv\Scripts\python scripts\m5_acceptance.py`——docx 真流水线 done、零文本层 PDF `ocr=off` 实测 failed、**多轮指代改写真调生效**(ask#2"它的负责人是谁"→答案含"张三丰",agentic 默认开)、SSE 契约不变、审计 10 类动作全覆盖+筛选分页+403、导出 markdown 含引用附录+owner-only、评估 CLI 3 题 hit@k/MRR=1.0、doc_count 聚合。OCR 真调 2 项 SKIP(MINERU_API_TOKEN 未提供;决策矩阵由 MockTransport 单测覆盖,token 填入后可单跑验收脚本补验)。
- 浏览器走查留给用户(审计页/导出按钮/OCR 标记/Alt+Enter/文档数列)。

### 偏差与修正(实现者对计划的增量)
1. **运行命令**:计划通篇 `py -3.12` 实为全局解释器(无 pytest);实际全部用 `backend\.venv\Scripts\python.exe`。
2. **MinerU v4 契约修正**(T5 Step 0 核对官方文档):不存在 `/api/v4/file/upload`——本地文件走 `POST /api/v4/file-urls/batch`(files[].name/is_ocr)拿预签名 URL → **PUT 字节(不带 Content-Type/Authorization)** → `GET /api/v4/extract-results/batch/{batch_id}` 轮询 `extract_result[0].state/full_zip_url`。客户端与 MockTransport 测试均按实测契约编写。
3. **存量适配**:①`test_graph_always_has_rerank_node` 断言 `("rerank","generate")` → `("rerank","grade")`(M5 拓扑插入 grade,rerank 恒在拓扑的意图不变);②T2 审计计数测试被登录/注册挂点污染 → 改为按 seeded action 断言 + `total>=2`;③doc_count 测试改在建空库后取列表。
4. **测试桩陷阱**:httpx.Client 打桩若在函数内捕获"原构造器",同测试二次打桩会嵌套旧桩(429 错误串进 timeout 用例)——改为模块级 `_REAL_CLIENT` 捕获。
5. T4 实现时漏配 `CHECKPOINTER_ENABLED`(属该任务自身步骤,补上后绿)。
6. throttle.spec 的 `vi.fn()` 需泛型参数(oxlint 规则),与 auth.store.spec 三旧错同批修复。
7. T1 的迁移对开发库已执行(alembic head=a1b2c3d4e5f6);验收数据:M5验收库(id 5)含 m5事实文档.docx(done)与 m5空白页.pdf(failed),eval_sets/5.json 为验收产物。
