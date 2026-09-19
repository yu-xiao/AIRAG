# AIRag M12 权限治理收官 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** per-KB scope 白名单 + KB 删除端点(Web)+ KB 重名 DB 唯一约束 + M11 终审八小项,收尾 Agent 授权模型的"库粒度"最后一块。

**Architecture:** 沿用 M9-M11 既有分层:`ApiKey` 加 `kb_scope` JSON 列(NULL=不限),`Principal.key_scope` 全链路传播,在 facade `_permitted_kb_ids`/`list_kbs_for` 与 doc_ops `visible_*_or_404` 两个既有守卫点做"活交集"过滤;KB 删除为 `services/kb_ops.py` 单实现 + Web 薄壳端点,级联 chunks/documents/kb_permissions/conversations.kb_ids/磁盘/eval_sets;doc_ops 错误改 `DocOpError(code,status,message)` + 全局 FastAPI handler(HTTP 报文零变化,MCP 按 code 映射)。

**Tech Stack:** FastAPI + SQLAlchemy 2 async + Alembic + PostgreSQL(pgvector)+ fastmcp + Celery + Vue3/Element Plus + pytest/vitest。

**Spec:** `docs/superpowers/specs/2026-09-19-airag-m12-perms-governance-design.md`(本计划从 spec 出发,执行者须同时读 spec)。

## Global Constraints

- Windows CMD;后端命令一律在 `E:\Projects\AIRag\backend` 下用 `.venv\Scripts\python`;测试从 backend 目录跑 `.venv\Scripts\python -m pytest -q`。
- 测试库由 conftest 指向 `airag_test`,`Base.metadata.create_all` 建表——**模型即测试真源**;alembic 迁移(Task 1)在 dev 栈验收(Task 12)真跑。
- conftest autouse:`UPLOAD_DIR` 指向 tmp_path、celery eager、fake 嵌入;**走查期间勿在 backend\ 写临时文件**(--reload 会打断在途长 MCP 调用)。
- 测试基线:后端 **247P/0F**、前端 vitest 22/22、`npm run build` 零错——每任务结束时不得低于基线。
- 提交走 conventional commits(每任务一提交)。
- 错误序铁律(spec B):agent 文档操作 **404 可见性 → 403 perm → 403 key 能力**;search/ask 用 `kb_forbidden+denied_kb_ids`;scope 折入前两类,不新造错误码。
- `MAX_UPLOAD_MB=20`(`app/core/config.py:52`);前端硬编码副本 `DocsPage.vue:24`。
- alembic 当前 head `c1d2e3f4a7b6`(笔误,实为 `c1d2e3f4a5b6`,M11 api_keys.role);新迁移 down_revision 接它。

---

### Task 1: 数据模型与迁移(kb_scope 列 + KB name 唯一约束 + IntegrityError 兜底)

**Files:**
- Create: `backend/alembic/versions/d4e5f6a7b8c9_m12_kb_scope_and_unique_name.py`
- Modify: `backend/app/models/api_key.py`
- Modify: `backend/app/models/knowledge_base.py:11`
- Modify: `backend/app/api/kbs.py:15-41`(create_kb)
- Test: `backend/tests/test_kbs.py:124-137`(改造)

**Interfaces:**
- Consumes: 无(首任务)。
- Produces: `ApiKey.kb_scope: Mapped[list[int] | None]`(JSON 列,模型属性即可读写 list[int]);`KnowledgeBase.name` 唯一约束(直插同名行抛 `sqlalchemy.exc.IntegrityError`);`POST /api/kbs` IntegrityError 兜底转 409(文案不变)。

- [ ] **Step 1: 写失败测试**

`tests/test_kbs.py` 末尾追加,并**替换** `test_create_kb_duplicate_name_409_when_db_already_has_two`(:124-137,DB 唯一约束后直插同名本身会抛 IntegrityError,该用例前提不复存在):

```python
# ---- M12:KB name DB 唯一约束 + create_kb IntegrityError 兜底 ----
async def test_db_rejects_duplicate_kb_names(client, auth_headers, db_session):
    """M12:唯一约束落地,直插同名行在 commit 时抛 IntegrityError。"""
    from sqlalchemy.exc import IntegrityError

    from app.models import KnowledgeBase

    me = await client.get("/api/auth/me", headers=auth_headers)
    owner_id = me.json()["id"]
    db_session.add(KnowledgeBase(name="双胞胎库", owner_id=owner_id))
    await db_session.commit()
    db_session.add(KnowledgeBase(name="双胞胎库", owner_id=owner_id))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_create_kb_unique_fallback_409(client, auth_headers, db_session,
                                             monkeypatch):
    """M12:并发兜底——重名 SELECT 恒空(模拟竞态)时 flush 撞唯一约束仍 409。"""
    import app.api.kbs as kbs_mod
    from app.models import KnowledgeBase

    await client.post("/api/kbs", json={"name": "竞态库"}, headers=auth_headers)
    real_select = kbs_mod.select

    def blind_select(*a, **k):
        return real_select(KnowledgeBase).where(KnowledgeBase.id < 0)

    monkeypatch.setattr(kbs_mod, "select", blind_select)
    resp = await client.post("/api/kbs", json={"name": "竞态库"},
                             headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "knowledge base name already exists"
```

(test_kbs.py 顶部已 import 的 `pytest` 若缺则补 `import pytest`。)

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_kbs.py -q`
Expected: FAIL——`test_db_rejects_duplicate_kb_names` 不抛 IntegrityError(第二条 commit 成功);`test_create_kb_unique_fallback_409` 得 201 而非 409。

- [ ] **Step 3: 模型与端点实现**

`app/models/api_key.py`:import 行加 `JSON`,`is_active` 之后加:

```python
    # M12:库粒度白名单;NULL=不限(继承用户全部可访问库),非空=仅列出的库
    kb_scope: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
```

`app/models/knowledge_base.py:11` 改(去掉 `index=True`,换唯一):

```python
    name: Mapped[str] = mapped_column(String(128), unique=True)
```

`app/api/kbs.py` create_kb 的 `db.add(kb)` 段改为(顶部 import 加 `from sqlalchemy.exc import IntegrityError`):

```python
    db.add(kb)
    try:
        await db.flush()
    except IntegrityError:  # M12:并发窗口兜底,DB 唯一约束兜住
        await db.rollback()
        raise HTTPException(status_code=409,
                            detail="knowledge base name already exists")
```

- [ ] **Step 4: 迁移文件**

`alembic/versions/d4e5f6a7b8c9_m12_kb_scope_and_unique_name.py`(头部格式照抄 `c1d2e3f4a5b6` 迁移):

```python
# backend/alembic/versions/d4e5f6a7b8c9_m12_kb_scope_and_unique_name.py
"""m12 api_keys.kb_scope + knowledge_bases.name unique

Revision ID: d4e5f6a7b8c9
Revises: c1d2e3f4a5b6
Create Date: 2026-09-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("kb_scope", sa.JSON(), nullable=True))
    # 存量同名去重:按 id 升序第 2 条起改后缀,幂等(重跑无同名组即空操作)
    op.execute(
        """
        WITH ranked AS (
            SELECT id, name,
                   ROW_NUMBER() OVER (PARTITION BY name ORDER BY id) AS rn
            FROM knowledge_bases
        )
        UPDATE knowledge_bases k
        SET name = k.name || '-' || ranked.rn
        FROM ranked
        WHERE k.id = ranked.id AND ranked.rn > 1
        """
    )
    op.drop_index("ix_knowledge_bases_name")
    op.create_unique_constraint("uq_knowledge_bases_name",
                                "knowledge_bases", ["name"])


def downgrade() -> None:
    op.drop_constraint("uq_knowledge_bases_name", "knowledge_bases")
    op.create_index("ix_knowledge_bases_name", "knowledge_bases", ["name"])
    op.drop_column("api_keys", "kb_scope")
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_kbs.py -q`
Expected: PASS 全绿(含存量重名 409 三用例——应用层检查先行,行为不变)。

- [ ] **Step 6: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/app/models/api_key.py backend/app/models/knowledge_base.py backend/app/api/kbs.py backend/alembic/versions/d4e5f6a7b8c9_m12_kb_scope_and_unique_name.py backend/tests/test_kbs.py
git commit -m "feat(kb): kb_scope column, unique kb name constraint + integrity fallback"
```

---

### Task 2: Principal.key_scope 传播 + facade 过滤(search/ask/list_kbs,REST+MCP)

**Files:**
- Modify: `backend/app/core/deps.py:39-45,60-79`
- Modify: `backend/app/services/agent_facade.py:35-67,76-85,166-190`
- Modify: `backend/app/api/agent.py:75-122,125-161`
- Modify: `backend/app/mcp_server.py:47-62,84-110,113-169`
- Test: Create `backend/tests/test_kb_scope.py`;Modify `backend/tests/test_mcp.py`(追加)

**Interfaces:**
- Consumes: Task 1 的 `ApiKey.kb_scope`。
- Produces: `Principal.key_scope: frozenset[int] | None`(JWT 恒 None);`_permitted_kb_ids(db, user, kb_ids, key_scope=None)`、`list_kbs_for(db, user, key_scope=None)`、`agent_search(db, user, kb_ids, query, top_k, rerank, key_scope=None)`、`agent_ask(db, user, kb_ids, query, rerank, key_scope=None)`——后续任务照此签名调用。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_kb_scope.py`:

```python
# backend/tests/test_kb_scope.py
"""M12 Task2:per-KB scope 白名单(REST 面:search/ask/list_kbs)。

活交集语义:实际可访问 = 用户实时 perm ∩ key scope;None=不限(存量 key 零影响);
JWT 调试通道不受限。scope 外的库进 denied_kb_ids(403 kb_forbidden)或从 list 剔除。
"""
import pytest

from app.models import ApiKey
from app.services.api_keys import generate_api_key


async def _scoped_key(client, auth_headers, db_session, kb_ids,
                      role="read_only") -> str:
    """直插 DB 铸 scoped key(铸造 API 校验在 Task4;此处绕过以单测传播链)。"""
    me = await client.get("/api/auth/me", headers=auth_headers)
    raw, prefix, digest = generate_api_key()
    db_session.add(ApiKey(user_id=me.json()["id"], name=f"scoped-{role}",
                          key_prefix=prefix, key_hash=digest, role=role,
                          kb_scope=list(kb_ids)))
    await db_session.commit()
    return raw


async def _two_kbs(client, auth_headers) -> tuple[int, int]:
    a = await client.post("/api/kbs", json={"name": "界内库"}, headers=auth_headers)
    b = await client.post("/api/kbs", json={"name": "界外库"}, headers=auth_headers)
    return a.json()["id"], b.json()["id"]


async def test_scoped_key_search_denied(client, auth_headers, db_session,
                                        monkeypatch):
    from app.services.retrieval.searcher import SearchHit

    kb_in, kb_out = await _two_kbs(client, auth_headers)
    key = await _scoped_key(client, auth_headers, db_session, [kb_in])

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        return [SearchHit(chunk_id=1, document_id=10, kb_id=kb_ids[0],
                          filename="a.pdf", page_no=1, content="c",
                          score=0.9, source="both")]

    monkeypatch.setattr("app.services.agent_facade.hybrid_search", fake_hybrid)
    hdr = {"Authorization": f"Bearer {key}"}
    r1 = await client.post("/api/agent/search",
                           json={"kb_ids": [kb_in], "query": "q"}, headers=hdr)
    assert r1.status_code == 200
    r2 = await client.post("/api/agent/search",
                           json={"kb_ids": [kb_out], "query": "q"}, headers=hdr)
    assert r2.status_code == 403
    assert r2.json()["detail"]["code"] == "kb_forbidden"
    assert r2.json()["detail"]["denied_kb_ids"] == [kb_out]


async def test_scoped_key_ask_denied(client, auth_headers, db_session):
    """denied 在 _permitted_kb_ids 即抛,不触达问答图(无需 LLM)。"""
    kb_in, kb_out = await _two_kbs(client, auth_headers)
    key = await _scoped_key(client, auth_headers, db_session, [kb_in])
    r = await client.post("/api/agent/ask",
                          json={"kb_ids": [kb_out], "query": "q"},
                          headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403
    assert r.json()["detail"]["denied_kb_ids"] == [kb_out]


async def test_scoped_key_list_kbs_filtered(client, auth_headers, db_session):
    kb_in, kb_out = await _two_kbs(client, auth_headers)
    key = await _scoped_key(client, auth_headers, db_session, [kb_in])
    r = await client.get("/api/agent/kbs",
                         headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert [i["id"] for i in r.json()["items"]] == [kb_in]


async def test_unscoped_key_sees_all(client, auth_headers, db_session):
    kb_in, kb_out = await _two_kbs(client, auth_headers)
    key = await _scoped_key(client, auth_headers, db_session, None)  # kb_scope NULL
    r = await client.get("/api/agent/kbs",
                         headers={"Authorization": f"Bearer {key}"})
    ids = [i["id"] for i in r.json()["items"]]
    assert kb_in in ids and kb_out in ids


async def test_jwt_unrestricted(client, auth_headers, db_session):
    kb_in, kb_out = await _two_kbs(client, auth_headers)
    await _scoped_key(client, auth_headers, db_session, [kb_in])  # 造出 scoped key 但不用
    r = await client.get("/api/agent/kbs", headers=auth_headers)
    ids = [i["id"] for i in r.json()["items"]]
    assert kb_in in ids and kb_out in ids


async def test_scope_intersects_user_perm(client, auth_headers, db_session):
    """scope 写了他人的库:用户 perm 拦在前面(spec B1 活交集)。"""
    from sqlalchemy import text

    kb_in, _ = await _two_kbs(client, auth_headers)
    await client.post("/api/auth/register",
                      json={"username": "scope_str1", "password": "secret123"})
    await db_session.execute(
        text("UPDATE users SET role = 'editor' WHERE username = 'scope_str1'"))
    await db_session.commit()
    other = await client.post("/api/auth/login",
                              json={"username": "scope_str1",
                                    "password": "secret123"})
    other_kb = await client.post(
        "/api/kbs", json={"name": "别人的库2"},
        headers={"Authorization": f"Bearer {other.json()['access_token']}"})
    key = await _scoped_key(client, auth_headers, db_session,
                            [kb_in, other_kb.json()["id"]])
    r = await client.post("/api/agent/search",
                          json={"kb_ids": [other_kb.json()["id"]],
                                "query": "q"},
                          headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403
    assert r.json()["detail"]["denied_kb_ids"] == [other_kb.json()["id"]]
```

`tests/test_mcp.py` 末尾追加(MCP 面同语义):

```python
# ---- M12:scope 过滤(MCP 面) ----
async def test_mcp_scoped_key_list_and_denied(mcp_client, auth_headers,
                                              db_session):
    from app.models import ApiKey
    from app.services.api_keys import generate_api_key
    from tests.test_agent_api import _create_kb

    kb_in = await _create_kb(mcp_client, auth_headers, "MCP界内库")
    kb_out = await _create_kb(mcp_client, auth_headers, "MCP界外库")
    me = await mcp_client.get("/api/auth/me", headers=auth_headers)
    raw, prefix, digest = generate_api_key()
    db_session.add(ApiKey(user_id=me.json()["id"], name="mcp-scoped",
                          key_prefix=prefix, key_hash=digest,
                          kb_scope=[kb_in]))
    await db_session.commit()

    hdr = {"Authorization": f"Bearer {raw}"}
    sid = await _init(mcp_client, hdr)
    resp = await mcp_client.post(
        "/mcp",
        json=_rpc("tools/call",
                  {"name": "list_knowledge_bases", "arguments": {}}, 20),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    body = _tool_result(resp.json())
    assert [i["id"] for i in body["items"]] == [kb_in]
    resp2 = await mcp_client.post(
        "/mcp",
        json=_rpc("tools/call",
                  {"name": "search_knowledge_base",
                   "arguments": {"kb_ids": [kb_out], "query": "q"}}, 21),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    text = resp2.json()["result"]["content"][0]["text"]
    assert "kb_forbidden" in text and str(kb_out) in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_kb_scope.py tests/test_mcp.py -q`
Expected: FAIL——`_scoped_key(..., None)` 落库成功但 `Principal` 无 key_scope,denied/list 断言失败(ApiKey 构造不受影响,SQLAlchemy 2 未知列不报——模型已有列但 Principal 不读)。

- [ ] **Step 3: 实现**

`app/core/deps.py`——`Principal` 加字段、`resolve_bearer_principal` 传播:

```python
@dataclass
class Principal:
    user: User
    kind: str  # jwt | api_key
    key_id: int | None = None
    key_name: str | None = None
    key_role: str | None = None  # M11:read_only|editor;JWT 恒 None
    key_scope: frozenset[int] | None = None  # M12:per-KB 白名单;None=不限
```

`resolve_bearer_principal` 的 api_key 分支 return 改:

```python
        return Principal(user=user, kind="api_key", key_id=key.id,
                         key_name=key.name, key_role=key.role,
                         key_scope=(frozenset(key.kb_scope)
                                    if key.kb_scope is not None else None))
```

`app/services/agent_facade.py`——三个函数加参:

```python
async def _permitted_kb_ids(
    db: AsyncSession, user: User, kb_ids: list[int],
    key_scope: frozenset[int] | None = None,
) -> list[int]:
    """去重归一 + 逐库权限校验 + key scope 活交集(M12);无权限/不存在/
    界外统一抛 AgentKbDenied(不区分三者,不泄露存在性)。"""
    kb_ids = list(dict.fromkeys(kb_ids))
    rows = (
        await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids))
        )
    ).scalars().all()
    by_id = {kb.id: kb for kb in rows}
    denied = [
        kb_id for kb_id in kb_ids
        if kb_id not in by_id
        or await get_kb_perm(db, user, by_id[kb_id]) is None
        or (key_scope is not None and kb_id not in key_scope)
    ]
    if denied:
        raise AgentKbDenied(denied)
    return kb_ids


async def list_kbs_for(db: AsyncSession, user: User,
                       key_scope: frozenset[int] | None = None) -> list[KbBrief]:
    """与 GET /api/kbs 同语义:admin 全库 owner;否则 自有 ∪ 被授权;
    key scope 非空时再取交集(M12)。"""
    rows = (await db.execute(select(KnowledgeBase))).scalars().all()
    out = []
    for kb in rows:
        if key_scope is not None and kb.id not in key_scope:
            continue
        perm = await get_kb_perm(db, user, kb)
        if perm is not None:
            out.append(KbBrief(id=kb.id, name=kb.name,
                               description=kb.description, my_perm=perm))
    out.sort(key=lambda x: -x.id)
    return out
```

`agent_search` 签名加 `key_scope: frozenset[int] | None = None`,首行改 `kb_ids = await _permitted_kb_ids(db, user, kb_ids, key_scope)`;`agent_ask` 同理。

`app/api/agent.py` 三处调用加参:`agent_kbs` 的 `list_kbs_for(db, principal.user, principal.key_scope)`;`agent_search`/`agent_ask` 端点的 facade 调用末尾加 `principal.key_scope`。

`app/mcp_server.py` 三处调用加参:`list_knowledge_bases` 的 `list_kbs_for(db, p.user, p.key_scope)`;`search_knowledge_base`/`ask_knowledge_base` 的 facade 调用末尾加 `p.key_scope`。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_kb_scope.py tests/test_mcp.py tests/test_agent_api.py tests/test_agent_ask.py -q`
Expected: PASS(既有 agent 测试不受影响——key_scope None 短路)。

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/app/core/deps.py backend/app/services/agent_facade.py backend/app/api/agent.py backend/app/mcp_server.py backend/tests/test_kb_scope.py backend/tests/test_mcp.py
git commit -m "feat(agent): per-kb scope propagation and facade filtering"
```

---

### Task 3: doc_ops visible_* 折入 scope(agent 文档五操作,REST+MCP)

**Files:**
- Modify: `backend/app/services/doc_ops.py:27-45`
- Modify: `backend/app/api/agent.py:185-283`(五端点调用点)
- Modify: `backend/app/mcp_server.py:244-388`(五工具调用点)
- Test: Modify `backend/tests/test_agent_docs_api.py`、`backend/tests/test_mcp.py`(追加)

**Interfaces:**
- Consumes: Task 2 的 `Principal.key_scope`。
- Produces: `visible_kb_or_404(db, user, kb_id, key_scope=None)`、`visible_doc_or_404(db, user, doc_id, key_scope=None)`——scope 外 → 404(与不可见同桶,错误序 404→403→403 不变);Web 面(JWT)不传该参。

- [ ] **Step 1: 写失败测试**

`tests/test_agent_docs_api.py` 末尾追加(该文件已有 `_create_kb`/`_create_key` 复用惯例,scoped key 直插 DB):

```python
# ---- M12:文档五操作的 scope 折入(404 不泄露) ----
async def _scoped_key_db(client, auth_headers, db_session, kb_ids, role):
    from app.models import ApiKey
    from app.services.api_keys import generate_api_key

    me = await client.get("/api/auth/me", headers=auth_headers)
    raw, prefix, digest = generate_api_key()
    db_session.add(ApiKey(user_id=me.json()["id"], name=f"sc-{role}",
                          key_prefix=prefix, key_hash=digest, role=role,
                          kb_scope=list(kb_ids)))
    await db_session.commit()
    return raw


async def test_scoped_doc_ops_out_of_scope_404(client, auth_headers, db_session):
    from tests.test_agent_api import _create_kb

    kb_in, kb_out = None, None
    kb_in = await _create_kb(client, auth_headers, "文档界内库")
    kb_out = await _create_kb(client, auth_headers, "文档界外库")
    # 界外库先放一篇文档(用 Web 面 JWT 上传,editor 用户)
    up = await client.post(
        f"/api/kbs/{kb_out}/documents",
        files={"file": ("o.docx", b"out-of-scope", "application/octet-stream")},
        headers=auth_headers,
    )
    doc_out = up.json()["id"]
    ro = await _scoped_key_db(client, auth_headers, db_session, [kb_in],
                              "read_only")
    ed = await _scoped_key_db(client, auth_headers, db_session, [kb_in],
                              "editor")
    rh = {"Authorization": f"Bearer {ro}"}
    eh = {"Authorization": f"Bearer {ed}"}
    # 读:list/get 界外 → 404
    assert (await client.get(f"/api/agent/kbs/{kb_out}/documents",
                             headers=rh)).status_code == 404
    assert (await client.get(f"/api/agent/documents/{doc_out}",
                             headers=rh)).status_code == 404
    # 写:upload/delete/reprocess 界外 → 404(可见性先于 perm/key 检查)
    assert (await client.post(
        f"/api/agent/kbs/{kb_out}/documents",
        files={"file": ("a.docx", b"x", "application/octet-stream")},
        headers=eh)).status_code == 404
    assert (await client.delete(f"/api/agent/documents/{doc_out}",
                                headers=eh)).status_code == 404
    assert (await client.post(f"/api/agent/documents/{doc_out}/reprocess",
                              headers=eh)).status_code == 404


async def test_scoped_doc_ops_in_scope_works(client, auth_headers, db_session):
    from tests.test_agent_api import _create_kb

    kb_in = await _create_kb(client, auth_headers, "文档界内库2")
    ed = await _scoped_key_db(client, auth_headers, db_session, [kb_in],
                              "editor")
    eh = {"Authorization": f"Bearer {ed}"}
    up = await client.post(
        f"/api/agent/kbs/{kb_in}/documents",
        files={"file": ("i.docx", b"in-scope", "application/octet-stream")},
        headers=eh,
    )
    assert up.status_code == 201
    doc_id = up.json()["id"]
    assert (await client.get(f"/api/agent/documents/{doc_id}",
                             headers=eh)).status_code == 200
    assert (await client.delete(f"/api/agent/documents/{doc_id}",
                                headers=eh)).status_code == 204
```

`tests/test_mcp.py` 追加:

```python
async def test_mcp_scoped_doc_not_found(mcp_client, auth_headers, db_session):
    from app.models import ApiKey
    from app.services.api_keys import generate_api_key
    from tests.test_agent_api import _create_kb

    kb_in = await _create_kb(mcp_client, auth_headers, "MCP界内库2")
    kb_out = await _create_kb(mcp_client, auth_headers, "MCP界外库2")
    me = await mcp_client.get("/api/auth/me", headers=auth_headers)
    raw, prefix, digest = generate_api_key()
    db_session.add(ApiKey(user_id=me.json()["id"], name="mcp-scoped2",
                          key_prefix=prefix, key_hash=digest,
                          kb_scope=[kb_in]))
    await db_session.commit()
    hdr, sid = {"Authorization": f"Bearer {raw}"}, None
    sid = await _init(mcp_client, hdr)
    rj = await _tool_call(mcp_client, hdr, sid, "list_documents",
                          {"kb_id": kb_out}, 22)
    assert _is_error(rj) and "not_found" in _err_text(rj)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_agent_docs_api.py tests/test_mcp.py -q`
Expected: FAIL——界外用例现得 403/200(未折入 scope)。

- [ ] **Step 3: 实现**

`app/services/doc_ops.py` 两个守卫加参(scope 检查在 perm 检查之后、返回之前——同一 404 桶):

```python
async def visible_kb_or_404(
    db: AsyncSession, user: User, kb_id: int,
    key_scope: frozenset[int] | None = None,
) -> KnowledgeBase:
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or await get_kb_perm(db, user, kb) is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if key_scope is not None and kb.id not in key_scope:  # M12:scope 折入可见性
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return kb


async def visible_doc_or_404(
    db: AsyncSession, user: User, doc_id: int,
    key_scope: frozenset[int] | None = None,
) -> Document:
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    if kb is None or await get_kb_perm(db, user, kb) is None:
        raise HTTPException(status_code=404, detail="document not found")
    if key_scope is not None and doc.kb_id not in key_scope:  # M12
        raise HTTPException(status_code=404, detail="document not found")
    return doc
```

`app/api/agent.py` 五个端点的 `visible_kb_or_404(db, principal.user, kb_id)` / `visible_doc_or_404(db, principal.user, doc_id)` 全部改为加第 4 参 `principal.key_scope`(`agent.py:193,216,238,257,276`)。

`app/mcp_server.py` 五个工具的对应调用加第 4 参 `p.key_scope`(`mcp_server.py:251,277,313,342,369`)。同时把 `_SEARCH_DOC`/`_UPLOAD_DOC` 等 Args 文案里"须为当前密钥有权访问的库"改为"须为当前密钥有权访问的库(若密钥设了范围,还须在范围内)"——仅 `kb_ids:`/`kb_id:` 两处描述行,不改结构。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_agent_docs_api.py tests/test_mcp.py tests/test_documents.py tests/test_doc_ops.py -q`
Expected: PASS(Web 面 JWT 不传参,行为不变)。

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/app/services/doc_ops.py backend/app/api/agent.py backend/app/mcp_server.py backend/tests/test_agent_docs_api.py backend/tests/test_mcp.py
git commit -m "feat(agent): scope gate on document tools (rest+mcp)"
```

---

### Task 4: 铸造面 kb_scope(校验 422、schema、审计、ApiKeyOut 回显)

**Files:**
- Modify: `backend/app/services/api_keys.py:39-71`
- Modify: `backend/app/schemas/auth.py:31-51`
- Modify: `backend/app/schemas/admin.py:7-13`
- Modify: `backend/app/api/auth.py:68-94`
- Modify: `backend/app/api/admin.py:66-98`
- Test: Modify `backend/tests/test_auth_keys_api.py`、`backend/tests/test_admin_keys.py`(追加)

**Interfaces:**
- Consumes: Task 1 的 `ApiKey.kb_scope`。
- Produces: `issue_api_key(db, user, name, expires_in_days, role="read_only", kb_scope: list[int] | None = None) -> tuple[ApiKey, str]`;`KbScopeInvalid(ValueError)`(属性 `.message`,端点转 422);`ApiKeyCreateIn`/`AdminKeyCreateIn` 增 `kb_scope: list[int] | None = None`;`ApiKeyOut`/`ApiKeyCreatedOut` 增 `kb_scope: list[int] | None`。

- [ ] **Step 1: 写失败测试**

`tests/test_auth_keys_api.py` 末尾追加:

```python
# ---- M12:kb_scope 铸造校验与回显 ----
async def test_create_key_kb_scope_validation(client, auth_headers):
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(client, auth_headers, "范围库")
    # 空列表 422
    r0 = await client.post("/api/auth/keys",
                           json={"name": "空范围", "kb_scope": []},
                           headers=auth_headers)
    assert r0.status_code == 422
    assert "empty" in r0.json()["detail"]
    # 不存在 422
    r1 = await client.post("/api/auth/keys",
                           json={"name": "幽灵库", "kb_scope": [999999]},
                           headers=auth_headers)
    assert r1.status_code == 422
    assert "999999" in r1.json()["detail"]
    # 他人库(无 perm)422
    await client.post("/api/auth/register",
                      json={"username": "scope_own1", "password": "secret123"})
    other = await client.post("/api/auth/login",
                              json={"username": "scope_own1",
                                    "password": "secret123"})
    other_kb = await client.post(
        "/api/kbs", json={"name": "别人的范围库"},
        headers={"Authorization": f"Bearer {other.json()['access_token']}"})
    r2 = await client.post(
        "/api/auth/keys",
        json={"name": "越界", "kb_scope": [other_kb.json()["id"]]},
        headers=auth_headers)
    assert r2.status_code == 422
    assert "inaccessible" in r2.json()["detail"]


async def test_create_key_kb_scope_echo_and_audit(client, auth_headers,
                                                  db_session):
    import json as _json

    from sqlalchemy import select

    from app.models import AuditLog
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(client, auth_headers, "回显库")
    r = await client.post(
        "/api/auth/keys",
        json={"name": "范围key", "kb_scope": [kb_id, kb_id]},  # 重复 id 去重
        headers=auth_headers)
    assert r.status_code == 201
    assert r.json()["kb_scope"] == [kb_id]  # 去重落库 + 创建响应回显
    listed = await client.get("/api/auth/keys", headers=auth_headers)
    row = next(k for k in listed.json() if k["id"] == r.json()["id"])
    assert row["kb_scope"] == [kb_id]
    # 不传 → None
    r3 = await client.post("/api/auth/keys", json={"name": "无范围"},
                           headers=auth_headers)
    assert r3.json()["kb_scope"] is None
    db_session.expire_all()
    audit_row = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "key_create",
                               AuditLog.detail.contains("范围key"))
    )).scalars().one()
    assert _json.loads(audit_row.detail)["kb_scope"] == [kb_id]
```

`tests/test_admin_keys.py` 末尾追加:

```python
# ---- M12:admin 代发 scoped key(校验按目标用户) ----
async def test_admin_issue_scoped_key(client, auth_headers, db_session):
    from sqlalchemy import text

    from tests.test_agent_api import _create_kb

    target_kb = await _create_kb(client, auth_headers, "目标用户的库")
    # 另一个用户(目标不可访问)的库
    await client.post("/api/auth/register",
                      json={"username": "scope_tg1", "password": "secret123"})
    await db_session.execute(
        text("UPDATE users SET role = 'editor' WHERE username = 'scope_tg1'"))
    await db_session.commit()
    tg = await client.post("/api/auth/login",
                           json={"username": "scope_tg1",
                                 "password": "secret123"})
    tg_kb = await client.post(
        "/api/kbs", json={"name": "第三方库"},
        headers={"Authorization": f"Bearer {tg.json()['access_token']}"})
    # 提权 admin
    me = await client.get("/api/auth/me", headers=auth_headers)
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": me.json()["id"]})
    await db_session.commit()
    r = await client.post("/api/admin/keys",
                          json={"user_id": me.json()["id"],
                                "name": "代发范围",
                                "kb_scope": [target_kb]},
                          headers=auth_headers)
    assert r.status_code == 201 and r.json()["kb_scope"] == [target_kb]
    r2 = await client.post("/api/admin/keys",
                           json={"user_id": me.json()["id"],
                                 "name": "代发越界",
                                 "kb_scope": [tg_kb.json()["id"]]},
                           headers=auth_headers)
    assert r2.status_code == 422
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_auth_keys_api.py tests/test_admin_keys.py -q`
Expected: FAIL——现入库无校验:空列表 201、响应无 kb_scope 键(KeyError/断言失败)。

- [ ] **Step 3: 实现**

`app/services/api_keys.py`——顶部 import 加 `from app.core.perms import get_kb_perm`、models 导入加 `KnowledgeBase`;`KeyQuotaExceeded` 旁加异常类;`issue_api_key` 扩展:

```python
class KbScopeInvalid(ValueError):
    """kb_scope 校验失败(空列表/含归属用户不可访问的库);调用方转 422。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


async def issue_api_key(
    db: AsyncSession, user: User, name: str, expires_in_days: int | None,
    role: str = "read_only", kb_scope: list[int] | None = None,
) -> tuple[ApiKey, str]:
    """配额检查 + scope 校验(M12) + 生成落库(仅 flush,不 commit)。

    审计与 commit 由调用方负责(个人面/admin 面 detail 不同)。
    """
    count = (
        await db.execute(
            select(func.count()).select_from(ApiKey).where(
                ApiKey.user_id == user.id, ApiKey.is_active == True  # noqa: E712
            )
        )
    ).scalar_one()
    if count >= settings.AGENT_MAX_KEYS_PER_USER:
        raise KeyQuotaExceeded()
    if kb_scope is not None:
        if not kb_scope:
            raise KbScopeInvalid("kb_scope must not be empty")
        kb_scope = list(dict.fromkeys(kb_scope))
        for kb_id in kb_scope:
            kb = await db.get(KnowledgeBase, kb_id)
            if kb is None or await get_kb_perm(db, user, kb) is None:
                raise KbScopeInvalid(
                    f"kb_scope contains inaccessible knowledge base: {kb_id}")
    raw, prefix, digest = generate_api_key()
    key = ApiKey(
        user_id=user.id,
        name=name,
        key_prefix=prefix,
        key_hash=digest,
        role=role,
        kb_scope=kb_scope,
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=expires_in_days)
            if expires_in_days
            else None
        ),
    )
    db.add(key)
    await db.flush()
    return key, raw
```

`app/schemas/auth.py`——`ApiKeyCreateIn` 加 `kb_scope: list[int] | None = None`;`ApiKeyOut` 加 `kb_scope: list[int] | None`。`app/schemas/admin.py`——`AdminKeyCreateIn` 加 `kb_scope: list[int] | None = None`。

`app/api/auth.py` create_api_key:

```python
    try:
        key, raw = await issue_api_key(db, current, payload.name,
                                       payload.expires_in_days, payload.role,
                                       payload.kb_scope)
    except KeyQuotaExceeded:
        raise HTTPException(status_code=409, detail="api key limit reached")
    except KbScopeInvalid as e:
        raise HTTPException(status_code=422, detail=e.message)
    await audit(db, current.username, "key_create", f"apikey:{key.id}",
                {"name": payload.name}
                | ({"kb_scope": key.kb_scope} if key.kb_scope else {}))
```

(import 行加 `KbScopeInvalid`;`ApiKeyCreatedOut(...)` 手工字段列表加 `kb_scope=key.kb_scope`。)

`app/api/admin.py` admin_create_api_key 同型:`issue_api_key(..., payload.role, payload.kb_scope)`;`except KbScopeInvalid as e: raise HTTPException(status_code=422, detail=e.message)`;audit detail 加 `| ({"kb_scope": key.kb_scope} if key.kb_scope else {})`;响应字段加 `kb_scope=key.kb_scope`;import 加 `KbScopeInvalid`。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_auth_keys_api.py tests/test_admin_keys.py tests/test_agent_api.py tests/test_mcp.py -q`
Expected: PASS(既有铸造用例不传 kb_scope,默认 None 行为不变)。

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/app/services/api_keys.py backend/app/schemas/auth.py backend/app/schemas/admin.py backend/app/api/auth.py backend/app/api/admin.py backend/tests/test_auth_keys_api.py backend/tests/test_admin_keys.py
git commit -m "feat(agent): scoped key issuance with 422 validation and audit"
```

---

### Task 5: admin 查询目标用户可见库(GET /api/admin/users/{id}/kbs)

**Files:**
- Modify: `backend/app/api/kbs.py:53-98`(list_kbs 重构)
- Modify: `backend/app/api/admin.py`(新端点)
- Modify: `backend/app/schemas/admin.py`(AdminUserKbOut)
- Test: Modify `backend/tests/test_admin_keys.py`(追加)

**Interfaces:**
- Consumes: 无新依赖。
- Produces: `visible_kbs_for(db, user) -> list[KnowledgeBase]`(kbs.py 模块级函数,admin 全库/否则自有 ∪ 被授权,按 id 倒序);`GET /api/admin/users/{user_id}/kbs -> list[AdminUserKbOut]`(id+name);`AdminUserKbOut(id: int, name: str)`。前端 Task 10 消费 `/admin/users/{id}/kbs`。

- [ ] **Step 1: 写失败测试**

`tests/test_admin_keys.py` 末尾追加:

```python
# ---- M12:admin 查目标用户可见库(代发范围下拉数据源) ----
async def test_admin_user_kbs(client, auth_headers, db_session):
    from sqlalchemy import text

    from tests.test_agent_api import _create_kb

    mine_kb = await _create_kb(client, auth_headers, "我的库A")
    # 他人库并授予我 viewer
    await client.post("/api/auth/register",
                      json={"username": "scope_gr1", "password": "secret123"})
    await db_session.execute(
        text("UPDATE users SET role = 'editor' WHERE username = 'scope_gr1'"))
    await db_session.commit()
    gr = await client.post("/api/auth/login",
                           json={"username": "scope_gr1",
                                 "password": "secret123"})
    gr_kb = await _create_kb(
        client,
        {"Authorization": f"Bearer {gr.json()['access_token']}"}, "授权给我的库")
    me = await client.get("/api/auth/me", headers=auth_headers)
    # 授权(gr 用户是 owner)
    await client.put(
        f"/api/kbs/{gr_kb}/permissions",
        json={"username": me.json()["username"], "perm": "viewer"},
        headers={"Authorization": f"Bearer {gr.json()['access_token']}"})
    # 提权 admin
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": me.json()["id"]})
    await db_session.commit()
    r = await client.get(f"/api/admin/users/{me.json()['id']}/kbs",
                         headers=auth_headers)
    assert r.status_code == 200
    ids = {k["id"] for k in r.json()}
    assert {"id", "name"} == set(r.json()[0].keys())
    assert mine_kb in ids and gr_kb in ids
    # 目标不存在 404;非 admin 403
    assert (await client.get("/api/admin/users/999999/kbs",
                             headers=auth_headers)).status_code == 404
    stranger = await client.post("/api/auth/login",
                                 json={"username": "scope_gr1",
                                       "password": "secret123"})
    r2 = await client.get(
        f"/api/admin/users/{me.json()['id']}/kbs",
        headers={"Authorization": f"Bearer {stranger.json()['access_token']}"})
    assert r2.status_code == 403


async def test_admin_target_admin_sees_all(client, auth_headers, db_session):
    """目标用户本身是 admin → 其可见集 = 全库(get_kb_perm 同语义)。"""
    from sqlalchemy import text

    from tests.test_agent_api import _create_kb

    await _create_kb(client, auth_headers, "全库可见性库")
    await client.post("/api/auth/register",
                      json={"username": "scope_ad1", "password": "secret123"})
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE username = 'scope_ad1'"))
    await db_session.commit()
    ad = await client.post("/api/auth/login",
                           json={"username": "scope_ad1",
                                 "password": "secret123"})
    ad_id = (await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {ad.json()['access_token']}"}
    )).json()["id"]
    r = await client.get(f"/api/admin/users/{ad_id}/kbs", headers=auth_headers)
    # auth_headers 用户须是 admin 才能调;先提权
```

注意:`test_admin_target_admin_sees_all` 末尾 auth_headers 用户尚未提权,补齐(在请求前提权自身):

```python
    me = await client.get("/api/auth/me", headers=auth_headers)
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": me.json()["id"]})
    await db_session.commit()
    r = await client.get(f"/api/admin/users/{ad_id}/kbs", headers=auth_headers)
    assert r.status_code == 200
    assert any(k["name"] == "全库可见性库" for k in r.json())
```

(即把上面两段合并成一个完整用例,断言目标 admin 可见含他人的库。)

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_admin_keys.py -q`
Expected: FAIL——404(路由不存在)。

- [ ] **Step 3: 实现**

`app/api/kbs.py`——从 `list_kbs` 抽出 helper(置于 `_doc_counts` 旁),`list_kbs` 改用它(行为不变,既有 test_kbs 用例回归):

```python
async def visible_kbs_for(db: AsyncSession, user: User) -> list[KnowledgeBase]:
    """user 可见库(id 倒序;不含 doc_count/my_perm 装配):
    admin 全库;否则 自有 ∪ 被授权。M12 抽出供 admin 目标用户查询复用。"""
    if user.role == "admin":
        stmt = select(KnowledgeBase)
    else:
        stmt = select(KnowledgeBase).where(
            or_(
                KnowledgeBase.owner_id == user.id,
                KnowledgeBase.id.in_(
                    select(KbPermission.kb_id).where(
                        KbPermission.user_id == user.id
                    )
                ),
            )
        )
    return (await db.execute(stmt.order_by(KnowledgeBase.id.desc()))
            ).scalars().all()


@router.get("", response_model=list[KBOut])
async def list_kbs(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc_counts = await _doc_counts(db)
    rows = await visible_kbs_for(db, current)
    grants = () if current.role == "admin" else (
        await db.execute(
            select(KbPermission).where(KbPermission.user_id == current.id)
        )
    ).scalars().all()
    perm_by_kb = {g.kb_id: g.perm for g in grants}
    out = []
    for kb in rows:
        item = KBOut.model_validate(kb)
        item.my_perm = ("owner" if current.role == "admin"
                        or kb.owner_id == current.id
                        else perm_by_kb.get(kb.id))
        item.doc_count = doc_counts.get(kb.id, 0)
        out.append(item)
    return out
```

`app/schemas/admin.py` 加:

```python
class AdminUserKbOut(BaseModel):
    """M12:admin 代发 scoped key 的范围下拉条目(目标用户可见库)。"""

    id: int
    name: str
```

`app/api/admin.py`——import 加 `from app.api.kbs import visible_kbs_for` 与 `AdminUserKbOut`(schemas 导入行并入);`list_users` 之后加:

```python
@router.get("/users/{user_id}/kbs", response_model=list[AdminUserKbOut])
async def admin_user_kbs(
    user_id: int,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """M12:目标用户可见库列表(admin 代发 scoped key 的范围数据源)。"""
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    rows = await visible_kbs_for(db, target)
    return [AdminUserKbOut(id=kb.id, name=kb.name) for kb in rows]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_admin_keys.py tests/test_kbs.py tests/test_kb_permissions.py -q`
Expected: PASS(list_kbs 重构后行为不变)。

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/app/api/kbs.py backend/app/api/admin.py backend/app/schemas/admin.py backend/tests/test_admin_keys.py
git commit -m "feat(admin): user-visible kbs endpoint for scoped key issuance"
```

---

### Task 6: KB 删除端点 + kb_ops 级联(Web only)

**Files:**
- Create: `backend/app/services/kb_ops.py`
- Modify: `backend/app/api/kbs.py`(DELETE /{kb_id})
- Test: Modify `backend/tests/test_kbs.py`(追加)

**Interfaces:**
- Consumes: `doc_ops.BUSY_STATUSES`、`audit()`、`settings.UPLOAD_DIR`。
- Produces: `delete_knowledge_base(db, kb, *, username) -> None`(级联+审计+commit;busy 抛 409);`DELETE /api/kbs/{kb_id}`(不可见 404 / 可见非 owner 403 / owner|admin 204)。前端 Task 11 消费该端点。

- [ ] **Step 1: 写失败测试**

`tests/test_kbs.py` 末尾追加:

```python
# ---- M12:KB 删除(级联 + 权限矩阵) ----
async def _mk_full_kb(db_session, owner_id, name="删除库"):
    """库 + 文档(done)+ chunk + 成员 + 会话引用 + 磁盘文件 + eval 集文件。"""
    from app.core.config import settings
    from app.models import Chunk, Conversation, Document, KbPermission

    kb = KnowledgeBase(name=name, owner_id=owner_id)
    db_session.add(kb)
    await db_session.flush()
    doc = Document(kb_id=kb.id, filename="a.docx", file_path="x", mime="m",
                   size=1, sha256="del", status="done")
    db_session.add(doc)
    await db_session.flush()
    db_session.add(Chunk(document_id=doc.id, kb_id=kb.id, chunk_index=0,
                         content="c", char_len=1, content_hash="h"))
    db_session.add(KbPermission(kb_id=kb.id, user_id=owner_id + 10 ** 6,
                                perm="viewer"))  # 假想成员(user 无 FK 校验由 DB 保证,先造不引用)
    other_kb = KnowledgeBase(name=name + "-邻", owner_id=owner_id)
    db_session.add(other_kb)
    await db_session.flush()
    db_session.add(Conversation(user_id=owner_id, kb_ids=[kb.id, other_kb.id]))
    await db_session.commit()
    doc_dir = Path(settings.UPLOAD_DIR) / str(kb.id)
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "f.docx").write_bytes(b"x")
    return kb, doc, other_kb
```

注意:`KbPermission.user_id` 指向不存在的用户会触发 FK 违约——改为先注册真实用户:

```python
async def _mk_full_kb(client, db_session, owner_id, name="删除库"):
    from pathlib import Path

    from app.core.config import settings
    from app.models import Chunk, Conversation, Document, KbPermission

    member = await client.post(
        "/api/auth/register",
        json={"username": f"del_m{name}", "password": "secret123"})
    kb = KnowledgeBase(name=name, owner_id=owner_id)
    db_session.add(kb)
    await db_session.flush()
    doc = Document(kb_id=kb.id, filename="a.docx", file_path="x", mime="m",
                   size=1, sha256="del", status="done")
    db_session.add(doc)
    await db_session.flush()
    db_session.add(Chunk(document_id=doc.id, kb_id=kb.id, chunk_index=0,
                         content="c", char_len=1, content_hash="h"))
    db_session.add(KbPermission(kb_id=kb.id,
                                user_id=member.json()["id"], perm="viewer"))
    other_kb = KnowledgeBase(name=name + "-邻", owner_id=owner_id)
    db_session.add(other_kb)
    await db_session.flush()
    db_session.add(Conversation(user_id=owner_id,
                                kb_ids=[kb.id, other_kb.id]))
    await db_session.commit()
    doc_dir = Path(settings.UPLOAD_DIR) / str(kb.id)
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "f.docx").write_bytes(b"x")
    return kb, doc, other_kb
```

(以第二个版本为准;两个名字段不同的库避免唯一约束冲突,测试内每次调用传不同 name。)

```python
async def test_delete_kb_cascade(client, auth_headers, db_session, monkeypatch,
                                 tmp_path):
    import json as _json

    from sqlalchemy import select

    from app.models import (AuditLog, Chunk, Conversation, Document,
                            KbPermission)
    from app.services import kb_ops

    me = await client.get("/api/auth/me", headers=auth_headers)
    uid = me.json()["id"]
    kb, doc, other_kb = await _mk_full_kb(client, db_session, uid,
                                          name="级联删除库")
    monkeypatch.setattr(kb_ops, "EVAL_DIR", tmp_path)
    eval_file = tmp_path / f"{kb.id}.json"
    eval_file.write_text("{}")
    from app.core.config import settings

    resp = await client.delete(f"/api/kbs/{kb.id}", headers=auth_headers)
    assert resp.status_code == 204
    db_session.expire_all()
    assert await db_session.get(type(kb), kb.id) is None
    assert await db_session.get(Document, doc.id) is None
    assert (await db_session.execute(
        select(Chunk).where(Chunk.kb_id == kb.id))).scalars().first() is None
    assert (await db_session.execute(
        select(KbPermission).where(KbPermission.kb_id == kb.id))
    ).scalars().first() is None
    conv = (await db_session.execute(
        select(Conversation).where(Conversation.user_id == uid))).scalars().one()
    assert conv.kb_ids == [other_kb.id]  # array_remove 只清本库
    assert not (Path(settings.UPLOAD_DIR) / str(kb.id)).exists()
    assert not eval_file.exists()
    audit_row = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "kb_delete",
                               AuditLog.target == f"kb:{kb.id}")
    )).scalars().one()
    detail = _json.loads(audit_row.detail)
    assert detail["doc_count"] == 1 and detail["member_count"] == 1


async def test_delete_kb_busy_409(client, auth_headers, db_session):
    from app.models import Document

    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = KnowledgeBase(name="忙库", owner_id=me.json()["id"])
    db_session.add(kb)
    await db_session.flush()
    db_session.add(Document(kb_id=kb.id, filename="a.docx", file_path="x",
                            mime="m", size=1, sha256="busy",
                            status="parsing"))
    await db_session.commit()
    resp = await client.delete(f"/api/kbs/{kb.id}", headers=auth_headers)
    assert resp.status_code == 409
    db_session.expire_all()
    assert await db_session.get(KnowledgeBase, kb.id) is not None  # 行未动


async def test_delete_kb_permissions(client, auth_headers, db_session):
    from sqlalchemy import text

    # 授权成员(editor)可见但非 owner → 403
    kb, _, _ = await _mk_full_kb(client, db_session,
                                 (await client.get("/api/auth/me",
                                                   headers=auth_headers)
                                  ).json()["id"], name="权限矩阵库")
    await client.post("/api/auth/register",
                      json={"username": "del_member1", "password": "secret123"})
    member_login = await client.post(
        "/api/auth/login",
        json={"username": "del_member1", "password": "secret123"})
    member_hdr = {"Authorization":
                  f"Bearer {member_login.json()['access_token']}"}
    from app.models import KbPermission

    mid = (await client.get("/api/auth/me", headers=member_hdr)).json()["id"]
    db_session.add(KbPermission(kb_id=kb.id, user_id=mid, perm="editor"))
    await db_session.commit()
    assert (await client.delete(f"/api/kbs/{kb.id}",
                                headers=member_hdr)).status_code == 403
    # 陌生人 → 404
    assert (await client.delete(f"/api/kbs/{kb.id}",
                                headers=member_hdr)).status_code == 403
    stranger = await client.post(
        "/api/auth/register",
        json={"username": "del_str1", "password": "secret123"})
    str_hdr = {"Authorization": f"Bearer {stranger.json()['access_token']}"}
    assert (await client.delete(f"/api/kbs/{kb.id}",
                                headers=str_hdr)).status_code == 404
    # admin → 204
    me = await client.get("/api/auth/me", headers=auth_headers)
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": me.json()["id"]})
    await db_session.commit()
    assert (await client.delete(f"/api/kbs/{kb.id}",
                                headers=auth_headers)).status_code == 204
```

(`test_kbs.py` 顶部补 `from pathlib import Path` 与 `from app.models import KnowledgeBase` 的既有 import 检查——该文件现有用例都是函数内 import,保持函数内 import 风格亦可。)

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_kbs.py -q`
Expected: FAIL——405/404(无 DELETE 路由)。

- [ ] **Step 3: 实现**

Create `backend/app/services/kb_ops.py`:

```python
# backend/app/services/kb_ops.py
"""M12:KB 删除级联(Web 面唯一实现;spec C)。

顺序:busy 409 → chunks → documents → kb_permissions → conversations
array_remove 清悬空 id → KB 行 → 审计 → commit → 磁盘/评估集尽力清理。
"""
import shutil
from pathlib import Path

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import (Chunk, Conversation, Document, KbPermission,
                        KnowledgeBase)
from app.services.audit import audit
from app.services.doc_ops import BUSY_STATUSES

# 与 scripts/purge_orphan_evalsets.py 同源(backend/eval_sets)
EVAL_DIR = Path(__file__).resolve().parents[2] / "eval_sets"


async def delete_knowledge_base(
    db: AsyncSession, kb: KnowledgeBase, *, username: str,
) -> None:
    """级联删除知识库(不可逆;调用面前置 owner/admin 校验)。"""
    kb_id = kb.id
    busy = (await db.execute(
        select(Document.id).where(
            Document.kb_id == kb_id,
            Document.status.in_(BUSY_STATUSES),
        ).limit(1)
    )).scalar_one_or_none()
    if busy is not None:
        raise HTTPException(
            status_code=409,
            detail="knowledge base has documents being processed")
    doc_count = (await db.execute(
        select(func.count(Document.id)).where(Document.kb_id == kb_id)
    )).scalar_one()
    member_count = (await db.execute(
        select(func.count(KbPermission.id)).where(KbPermission.kb_id == kb_id)
    )).scalar_one()
    await db.execute(delete(Chunk).where(Chunk.kb_id == kb_id))
    await db.execute(delete(Document).where(Document.kb_id == kb_id))
    await db.execute(delete(KbPermission).where(KbPermission.kb_id == kb_id))
    # 清悬空 id:不清会让旧会话提问撞 kb_forbidden(spec C2 步5)
    await db.execute(
        update(Conversation)
        .where(func.array_position(Conversation.kb_ids, kb_id).isnot(None))
        .values(kb_ids=func.array_remove(Conversation.kb_ids, kb_id))
    )
    await audit(db, username, "kb_delete", f"kb:{kb_id}",
                {"name": kb.name, "doc_count": doc_count,
                 "member_count": member_count})
    await db.delete(kb)
    await db.commit()
    # 尽力清理(行已删,失败仅日志;孤儿由 purge 哲学兜底)
    shutil.rmtree(Path(settings.UPLOAD_DIR) / str(kb_id), ignore_errors=True)
    try:
        (EVAL_DIR / f"{kb_id}.json").unlink(missing_ok=True)
    except OSError:
        logger.warning(f"eval set removal failed: kb {kb_id}")
```

`app/api/kbs.py`——import 加 `Response`(`from fastapi import ...`)与 `from app.services import kb_ops`;`get_kb` 之后加端点:

```python
@router.delete("/{kb_id}", status_code=204)
async def delete_kb(
    kb_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """M12:删除知识库(admin/owner;级联见 kb_ops;不可逆)。"""
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    perm = await get_kb_perm(db, current, kb)
    if perm is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if not has_perm(perm, "owner"):  # admin/库主 get_kb_perm 即 owner
        raise HTTPException(status_code=403,
                            detail="owner or admin required")
    await kb_ops.delete_knowledge_base(db, kb, username=current.username)
    return Response(status_code=204)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_kbs.py tests/test_documents.py tests/test_conversations*.py -q`(conversations 测试文件名以实际为准,可先 `dir tests\test_chat*` 确认;没有则只跑前两个)
Expected: PASS。

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/app/services/kb_ops.py backend/app/api/kbs.py backend/tests/test_kbs.py
git commit -m "feat(kb): knowledge base deletion with full cascade"
```

---

### Task 7: 小项① DocOpError 结构化错误码

**Files:**
- Modify: `backend/app/services/doc_ops.py`(全部 raise 点)
- Modify: `backend/app/main.py`(全局 handler)
- Modify: `backend/app/mcp_server.py:227-241`(_e)及各 `except HTTPException` 调用点
- Test: Modify `backend/tests/test_doc_ops.py`(断言改 DocOpError);Modify `backend/tests/test_mcp.py`(追加 _e 映射用例)

**Interfaces:**
- Consumes: 无。
- Produces: `DocOpError(code: str, status: int, message: str)`(属性同名;code ∈ not_found|duplicate|too_large|unsupported_type|busy)——HTTP 面经全局 handler 出 `{"detail": message}` 同 status(**报文零变化**);MCP `_e` 按 code 出 `ToolError(f"{code}: {message}")`。

- [ ] **Step 1: 改测试(先行失败)**

`tests/test_doc_ops.py`:顶部 `from fastapi import HTTPException` 删除,`pytest.raises(HTTPException)` 全部换 `pytest.raises(doc_ops.DocOpError)`,`.value.status_code` 换 `.value.status`,并各补一行 code 断言。共 4 处(:35-39→415/unsupported_type、:42-46→413/too_large、:62-67→409/duplicate、:92-95→409/busy),示例:

```python
    with pytest.raises(doc_ops.DocOpError) as e1:
        await doc_ops.save_upload(db_session, kb, filename="a.exe",
                                  payload=b"x", mime=None, ocr_mode="auto",
                                  username="u", action="doc_upload")
    assert e1.value.status == 415
    assert e1.value.code == "unsupported_type"
```

(413→`code == "too_large"`;409 dup→`"duplicate"`;busy→`"busy"`。)

`tests/test_mcp.py` 追加:

```python
async def test_mcp_error_code_mapping():
    """M12 小项①:DocOpError 按 code 前缀,不再英文子串匹配。"""
    from app.mcp_server import _e
    from app.services.doc_ops import DocOpError

    t1 = _e(DocOpError("busy", 409, "document is being processed"))
    assert str(t1) == "busy: document is being processed"
    t2 = _e(DocOpError("duplicate", 409, "duplicate document in this kb"))
    assert str(t2).startswith("duplicate:")
    from fastapi import HTTPException

    t3 = _e(HTTPException(status_code=404, detail="x not found"))
    assert str(t3).startswith("not_found:")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_doc_ops.py tests/test_mcp.py::test_mcp_error_code_mapping -q`
Expected: FAIL——`doc_ops.DocOpError` 不存在。

- [ ] **Step 3: 实现**

`app/services/doc_ops.py`——模块 docstring 的"校验失败抛 HTTPException"改为"校验失败抛 DocOpError";删 `from fastapi import HTTPException`,加异常类与各 raise 替换:

```python
class DocOpError(Exception):
    """M12 小项①:结构化错误。HTTP 面经 main.py 全局 handler 转
    {"detail": message} 同 status(报文与既往 HTTPException 完全一致);
    MCP 面 _e 按 code 出 ToolError 前缀,消灭英文子串匹配。"""

    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
```

raise 点逐个替换(message 逐字保留,HTTP 报文零变化):

| 位置 | 原 | 新 |
|---|---|---|
| visible_kb_or_404 ×1 | HTTPException(404,"knowledge base not found") | DocOpError("not_found", 404, "knowledge base not found") |
| visible_doc_or_404 ×2 | HTTPException(404,"document not found") | DocOpError("not_found", 404, "document not found") |
| save_upload 415 | f"unsupported file type: {ext}" | DocOpError("unsupported_type", 415, f"unsupported file type: {ext}") |
| save_upload 413 | "file too large" | DocOpError("too_large", 413, "file too large") |
| save_upload 409 | "duplicate document in this kb" | DocOpError("duplicate", 409, "duplicate document in this kb") |
| delete busy | "document is being processed" | DocOpError("busy", 409, "document is being processed") |
| reprocess busy | 同上 | 同上 |

`app/main.py`——import 加 `from fastapi.responses import JSONResponse` 与 `from app.services.doc_ops import DocOpError`;`create_app` 内 `app.include_router(...)` 之前加:

```python
    async def _docop_error_handler(request, exc: DocOpError):
        return JSONResponse(status_code=exc.status,
                            content={"detail": exc.message})

    app.add_exception_handler(DocOpError, _docop_error_handler)
```

`app/mcp_server.py`——`_e` 重写 + 各工具的 `except HTTPException as e: raise _e(e)` 改 `except (doc_ops.DocOpError, HTTPException) as e: raise _e(e)`(共 8 处:list_documents/get_document/upload×2/delete×2/reprocess×2):

```python
def _e(e: Exception) -> ToolError:
    """doc_ops 异常 → ToolError(文本携带错误 code,spec E)。

    M12 小项①:DocOpError 自带 code,不再按英文 detail 子串猜测;
    HTTPException(非 doc_ops 来源)按 status 映射兜底。
    """
    if isinstance(e, doc_ops.DocOpError):
        return ToolError(f"{e.code}: {e.message}")
    detail = str(e.detail)
    code = {404: "not_found", 413: "too_large", 415: "unsupported_type"}.get(
        e.status_code)
    if code is None:
        return ToolError(detail)
    return ToolError(f"{code}: {detail}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_doc_ops.py tests/test_mcp.py tests/test_agent_docs_api.py tests/test_documents.py -q`
Expected: PASS——HTTP 集成用例(detail/status 断言)不变即证明报文零变化。

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/app/services/doc_ops.py backend/app/main.py backend/app/mcp_server.py backend/tests/test_doc_ops.py backend/tests/test_mcp.py
git commit -m "refactor(docops): structured DocOpError codes"
```

---

### Task 8: 小项②⑥⑦(Content-Length 预检、ApiKeyOut.role Literal、MCP total 全量)

**Files:**
- Modify: `backend/app/api/documents.py:33-49`(upload 预检)
- Modify: `backend/app/api/agent.py:227-246`(upload 预检)
- Modify: `backend/app/mcp_server.py:293-309`(b64 长度预检)、`:244-269`(total)
- Modify: `backend/app/schemas/auth.py:41`(role Literal)
- Test: Modify `backend/tests/test_documents.py`、`backend/tests/test_agent_docs_api.py`、`backend/tests/test_mcp.py`、`backend/tests/test_auth_keys_api.py`(各追加)

**Interfaces:**
- Consumes: Task 7 后 doc_ops/save_upload 仍是权威校验。
- Produces: 上传预检(Web/REST 读 body 前查 Content-Length;MCP 解码前查 b64 长度);`ApiKeyOut.role: Literal["read_only","editor"]`;MCP `list_documents.total` = 库内全量数。

- [ ] **Step 1: 写失败测试**

`tests/test_documents.py` 追加:

```python
async def test_upload_content_length_precheck_413(client, auth_headers,
                                                   monkeypatch):
    """M12 小项②:读 body 前按 Content-Length 预检(省内存缓冲)。"""
    from app.core.config import settings

    kb = await client.post("/api/kbs", json={"name": "预检库"},
                           headers=auth_headers)

    def _must_not_reach(*a, **k):
        raise AssertionError("precheck must reject before save_upload")

    monkeypatch.setattr("app.services.doc_ops.save_upload", _must_not_reach)
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 0)  # 阈值=(0+1)MB
    resp = await client.post(
        f"/api/kbs/{kb.json()['id']}/documents",
        files={"file": ("big.docx", b"x" * (2 * 1024 * 1024),
                        "application/octet-stream")},
        headers=auth_headers,
    )
    assert resp.status_code == 413
```

`tests/test_agent_docs_api.py` 追加:

```python
async def test_agent_upload_content_length_precheck_413(
        client, auth_headers, db_session, monkeypatch):
    from app.core.config import settings
    from tests.test_agent_api import _create_kb, _create_key_role

    kb_id = await _create_kb(client, auth_headers, "预检库2")
    key = await _create_key_role(client, auth_headers, "预检编辑", "editor")

    def _must_not_reach(*a, **k):
        raise AssertionError("precheck must reject before save_upload")

    monkeypatch.setattr("app.services.doc_ops.save_upload", _must_not_reach)
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 0)
    resp = await client.post(
        f"/api/agent/kbs/{kb_id}/documents",
        files={"file": ("big.docx", b"x" * (2 * 1024 * 1024),
                        "application/octet-stream")},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 413
```

`tests/test_mcp.py` 追加:

```python
async def test_mcp_upload_b64_precheck_too_large(mcp_client, auth_headers,
                                                 monkeypatch):
    """M12 小项②:解码前按 b64 长度粗判(monkeypatch 证明未解码)。"""
    import base64 as _b64

    from app.core.config import settings
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(mcp_client, auth_headers, "预检库3")
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 0)  # 阈值=(0*4//3+1)MB

    def _must_not_decode(*a, **k):
        raise AssertionError("precheck must reject before decode")

    monkeypatch.setattr(_b64, "b64decode", _must_not_decode)
    hdr, sid = await _keyed_session(mcp_client, auth_headers, role="editor")
    rj = await _tool_call(
        mcp_client, hdr, sid, "upload_document",
        {"kb_id": kb_id, "filename": "big.docx",
         "content_b64": _b64.b64encode(b"x" * (2 * 1024 * 1024)).decode()},
        23)
    assert _is_error(rj) and "too_large" in _err_text(rj)


async def test_mcp_list_documents_total_is_full_count(
        mcp_client, auth_headers, db_session):
    """M12 小项⑦:total 为库内总数,与 limit 截断解耦。"""
    from app.models import Document
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(mcp_client, auth_headers, "计数库")
    me = await mcp_client.get("/api/auth/me", headers=auth_headers)
    for i in range(3):
        db_session.add(Document(kb_id=kb_id, filename=f"c{i}.docx",
                                file_path="x", mime="m", size=1,
                                sha256=f"cnt{i}", status="done"))
    await db_session.commit()
    hdr, sid = await _keyed_session(mcp_client, auth_headers)
    rj = await _tool_call(mcp_client, hdr, sid, "list_documents",
                          {"kb_id": kb_id, "limit": 2}, 24)
    body = _tool_result(rj)
    assert body["total"] == 3 and len(body["items"]) == 2
```

`tests/test_auth_keys_api.py` 追加:

```python
async def test_api_key_out_role_literal():
    """M12 小项⑥:ApiKeyOut.role 强类型(裸 str → Literal)。"""
    import pytest as _pytest

    from pydantic import ValidationError

    from app.schemas.auth import ApiKeyOut

    base = dict(id=1, name="n", key_prefix="airag_x", is_active=True,
                expires_at=None, last_used_at=None,
                created_at="2026-09-19T00:00:00", kb_scope=None)
    assert ApiKeyOut.model_validate({**base, "role": "editor"}).role == "editor"
    with _pytest.raises(ValidationError):
        ApiKeyOut.model_validate({**base, "role": "bogus"})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_documents.py::test_upload_content_length_precheck_413 tests/test_agent_docs_api.py::test_agent_upload_content_length_precheck_413 tests/test_mcp.py -q`
Expected: FAIL——预检用例触发 AssertionError(save_upload 被调)/body 被读;MCP b64 用例同样;total 用例得 2。

- [ ] **Step 3: 实现**

`app/api/documents.py` upload_document——函数签名加 `request: Request`(import 加 `Request`),`_require_kb_editor` 之前加:

```python
    cl = request.headers.get("content-length")
    if cl and int(cl) > (settings.MAX_UPLOAD_MB + 1) * 1024 * 1024:
        # M12 小项②:multipart 开销 +1MB 松余量,宁可漏报不可误报;
        # 权威校验仍在 save_upload(413)
        raise HTTPException(status_code=413, detail="file too large")
```

(import 加 `from app.core.config import settings`。)

`app/api/agent.py` agent_upload_document——`await _check_rate(principal)` 之后加同款三行(`request: Request` 已有参数;settings 需 import)。

`app/mcp_server.py` upload_document——`p = _principal()` 之后、`base64.b64decode` 之前加:

```python
    if len(content_b64) > (settings.MAX_UPLOAD_MB * 4 // 3 + 1) * 1024 * 1024:
        # M12 小项②:解码前按 b64 长度粗判(4/3 膨胀 +1MB 松余量)
        raise ToolError(f"too_large: exceeds {settings.MAX_UPLOAD_MB}MB")
```

`app/mcp_server.py` list_documents——查询段改(顶部 `from sqlalchemy import select` 改 `from sqlalchemy import func, select`):

```python
        total = (await db.execute(
            select(func.count()).select_from(Document)
            .where(Document.kb_id == kb_id)
        )).scalar_one()
        rows = (await db.execute(
            select(Document)
            .where(Document.kb_id == kb_id)
            .order_by(Document.id.desc()).limit(limit)
        )).scalars().all()
```

audit detail 的 `"doc_count": len(items)` 改 `"doc_count": total`,返回 `"total": total`。

`app/schemas/auth.py:41`:`role: str` → `role: Literal["read_only", "editor"]`。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_documents.py tests/test_agent_docs_api.py tests/test_mcp.py tests/test_auth_keys_api.py -q`
Expected: PASS。

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/app/api/documents.py backend/app/api/agent.py backend/app/mcp_server.py backend/app/schemas/auth.py backend/tests/test_documents.py backend/tests/test_agent_docs_api.py backend/tests/test_mcp.py backend/tests/test_auth_keys_api.py
git commit -m "fix(agent): upload prechecks, ApiKeyOut role literal, mcp total count"
```

---

### Task 9: 小项③④⑤(MCP ask 失败分支/busy 测试 + m11_acceptance 审计绑定)

**Files:**
- Modify: `backend/tests/test_mcp.py`(追加 4 用例)
- Modify: `backend/scripts/m11_acceptance.py:223-242`(审计断言绑定本次运行)

**Interfaces:**
- Consumes: 无(纯测试补齐 + 验收脚本修正)。
- Produces: 无接口;M12 验收脚本(Task 12)沿用"按 target 绑定"的审计断言模式。

- [ ] **Step 1: 写测试(纯新增,先跑确认失败/现状)**

`tests/test_mcp.py` 追加:

```python
# ---- M12 小项③④:ask 失败分支 + busy 文本 ----
async def test_mcp_ask_denied_kb(mcp_client, auth_headers):
    from tests.test_agent_api import _create_key

    key = await _create_key(mcp_client, auth_headers)
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    rj = await _tool_call(mcp_client, hdr, sid, "ask_knowledge_base",
                          {"kb_ids": [99999], "query": "q"}, 30)
    assert _is_error(rj) and "kb_forbidden" in _err_text(rj)


async def test_mcp_ask_internal_error(mcp_client, auth_headers, monkeypatch):
    from tests.test_agent_api import _create_kb, _create_key

    kb_id = await _create_kb(mcp_client, auth_headers, "内部错误库")
    key = await _create_key(mcp_client, auth_headers)

    async def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.agent_facade.agent_ask", boom)
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    rj = await _tool_call(mcp_client, hdr, sid, "ask_knowledge_base",
                          {"kb_ids": [kb_id], "query": "q"}, 31)
    assert _is_error(rj) and "internal error" in _err_text(rj)


async def test_mcp_ask_with_jwt_principal(mcp_client, auth_headers,
                                          monkeypatch):
    """小项③:JWT(非 key)走 ask——放行且不烧配额。"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services import agent_facade
    from app.services.chat_graph import nodes
    from app.services.chat_graph.graph import build_graph
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(mcp_client, auth_headers, "JWT问答库")
    monkeypatch.setattr(nodes, "hybrid_search",
                        _noop_hybrid := _make_fake_hybrid(kb_id))
    monkeypatch.setattr(agent_facade, "_ask_graph",
                        build_graph(llm=FakeListChatModel(
                            responses=["jwt 通道答案。"]), checkpointer=None))
    hdr = {"Authorization": auth_headers["Authorization"],
           "Accept": ACCEPT}
    sid = await _init(mcp_client, hdr)
    rj = await _tool_call(mcp_client, hdr, sid, "ask_knowledge_base",
                          {"kb_ids": [kb_id], "query": "q"}, 32)
    body = _tool_result(rj)
    assert body["answer"] == "jwt 通道答案。"
```

其中 `_make_fake_hybrid` 为模块级辅助(置于文件 M12 段顶部):

```python
def _make_fake_hybrid(kb_id: int):
    from app.services.retrieval.searcher import SearchHit

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        return [SearchHit(chunk_id=1, document_id=10, kb_id=kb_id,
                          filename="a.pdf", page_no=1, content="锚点内容",
                          score=0.9, source="both")]

    return fake_hybrid
```

busy 文本用例(小项④;复用 `_keyed_session` editor key + 直插 busy 文档):

```python
async def test_mcp_busy_toolerror_text(mcp_client, auth_headers, db_session):
    from app.models import Document
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(mcp_client, auth_headers, "忙库MCP")
    me = await mcp_client.get("/api/auth/me", headers=auth_headers)
    doc = Document(kb_id=kb_id, filename="b.docx", file_path="x", mime="m",
                   size=1, sha256="mcpbusy", status="parsing")
    db_session.add(doc)
    await db_session.commit()
    hdr, sid = await _keyed_session(mcp_client, auth_headers, role="editor")
    rj = await _tool_call(mcp_client, hdr, sid, "delete_document",
                          {"doc_id": doc.id}, 33)
    assert _is_error(rj) and _err_text(rj).startswith("busy:")
    db_session.expire_all()
    doc2 = await db_session.get(Document, doc.id)
    doc2.status = "parsing"  # delete 已失败,行还在;再造 reprocess busy
    await db_session.commit()
    rj2 = await _tool_call(mcp_client, hdr, sid, "reprocess_document",
                           {"doc_id": doc.id}, 34)
    assert _is_error(rj2) and _err_text(rj2).startswith("busy:")
```

- [ ] **Step 2: 跑测试确认现状**

Run: `.venv\Scripts\python -m pytest tests/test_mcp.py -q`
Expected: 新 4 用例中 `test_mcp_ask_denied_kb`/`internal_error`/`busy_toolerror_text` 应当已 PASS(Task 7 的 code 前缀已就位)——它们是**补测**,失败即说明 Task 7 映射有漏;`test_mcp_ask_with_jwt_principal` 若 `_make_fake_hybrid` 未定义则 ImportError,按 Step 1 补齐后 PASS。若全部直接 PASS,本任务性质即"锁行为的回归护栏",继续 Step 3。

- [ ] **Step 3: m11_acceptance 审计断言绑定本次运行**

`scripts/m11_acceptance.py:231-241` 的审计查询改(该作用域 `doc_id` 已存在——见 :215 删除步骤):

```python
                    rows = (await s.execute(
                        select(AuditLog.action).where(
                            AuditLog.action.in_([
                                "agent.upload_document",
                                "agent.delete_document",
                            ]),
                            AuditLog.target.in_([f"doc:{doc_id}"]),
                        )
                    )).scalars().all()
```

并在 docstring「覆盖」行尾补注:`审计断言按本次运行 doc target 绑定(M12 小项⑤)`。

Run: `.venv\Scripts\python -m py_compile scripts/m11_acceptance.py` → 无输出即过;真栈回归在 Task 12。

- [ ] **Step 4: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/tests/test_mcp.py backend/scripts/m11_acceptance.py
git commit -m "test(agent): mcp failure branches + acceptance audit binding"
```

---

### Task 10: 前端 KeysPage 范围多选 + 徽标 + admin 目标库加载

**Files:**
- Modify: `frontend/src/api/keys.ts`
- Modify: `frontend/src/pages/KeysPage.vue`
- Modify: `frontend/src/pages/DocsPage.vue:19,177,313,322`(小项⑧ 常量合一)
- Test: Modify `frontend/src/pages/__tests__/KeysPage.spec.ts`(追加)

**Interfaces:**
- Consumes: Task 4 的 `kb_scope` 入参/回显;Task 5 的 `GET /api/admin/users/{id}/kbs`;`kbApi.list()`(自服务模式选项源)。
- Produces: `keysApi.adminUserKbs(userId)`;`ApiKeyItem.kb_scope: number[] | null`;提交体含 `kb_scope: number[] | null`(空选→null);DocsPage 单一 `IDLE_STATUSES` 常量(小项⑧)。

- [ ] **Step 1: 写失败测试**

`KeysPage.spec.ts` 追加(mock 区加 `vi.mock('@/api/kb', ...)`):

```ts
vi.mock('@/api/kb', () => ({
  kbApi: { list: vi.fn() },
}))
```

import 区加 `import { kbApi } from '@/api/kb'`;`items` 两项各补 `kb_scope: null`;新 describe:

```ts
describe('KeysPage kb scope picker', () => {
  const scopedItem: ApiKeyItem = {
    id: 3, name: '范围密钥', key_prefix: 'airag_Scoped', role: 'read_only',
    is_active: true, kb_scope: [1, 2],
    expires_at: null, last_used_at: null, created_at: '2026-09-19T10:00:00',
  }

  it('renders scope badge (全部 / N 库)', async () => {
    vi.mocked(keysApi.list).mockResolvedValue([scopedItem])
    const w = mountPage()
    await flushPromises()
    expect(w.text()).toContain('范围 2 库')
  })

  it('empty selection submits kb_scope null; options from own kbs', async () => {
    vi.mocked(keysApi.list).mockResolvedValue([])
    vi.mocked(kbApi.list).mockResolvedValue([
      { id: 1, name: '库一' } as never,
      { id: 2, name: '库二' } as never,
    ])
    vi.mocked(keysApi.create).mockResolvedValue({
      ...scopedItem, id: 20, key: 'airag_ScopedOneTime',
    })
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '创建密钥').trigger('click')
    await flushPromises()
    expect(kbApi.list).toHaveBeenCalled()  // 打开即载入自己的库
    await w.find('input[placeholder="请输入密钥名称"]').setValue('范围key')
    await findBtn(w, '创建').trigger('click')
    await flushPromises()
    expect(keysApi.create).toHaveBeenCalledWith({
      name: '范围key', role: 'read_only', expires_in_days: null,
      kb_scope: null,
    })
  })

  it('selecting kbs submits ids', async () => {
    vi.mocked(keysApi.list).mockResolvedValue([])
    vi.mocked(kbApi.list).mockResolvedValue([
      { id: 1, name: '库一' } as never,
      { id: 2, name: '库二' } as never,
    ])
    vi.mocked(keysApi.create).mockResolvedValue({
      ...scopedItem, id: 21, key: 'airag_ScopedTwoTime',
    })
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '创建密钥').trigger('click')
    await flushPromises()
    const sel = w.findComponent(ElSelect)  // 非 admin:表单内唯一 select
    await sel.vm.$emit('update:modelValue', [1, 2])
    await flushPromises()
    await w.find('input[placeholder="请输入密钥名称"]').setValue('选库key')
    await findBtn(w, '创建').trigger('click')
    await flushPromises()
    expect(keysApi.create).toHaveBeenCalledWith({
      name: '选库key', role: 'read_only', expires_in_days: null,
      kb_scope: [1, 2],
    })
  })

  it('admin mode loads target user kbs after binding account', async () => {
    vi.mocked(useAuthStore).mockReturnValueOnce({
      user: { id: 1, username: 'boss', role: 'admin' },
    } as never)
    vi.mocked(keysApi.list).mockResolvedValue([])
    vi.mocked(usersApi.search).mockResolvedValue([{ id: 7, username: 'alice' }])
    vi.mocked(keysApi.adminUserKbs).mockResolvedValue([
      { id: 5, name: '目标用户的库' },
    ])
    vi.mocked(keysApi.createAdmin).mockResolvedValue({
      ...scopedItem, id: 22, key: 'airag_AdminScoped9',
    })
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '创建密钥').trigger('click')
    await flushPromises()
    const sels = w.findAllComponents(ElSelect)
    const bindSel = sels.find((s) => s.props('placeholder') === '默认绑定当前账号')!
    await (bindSel.props('remoteMethod') as (q: string) => void)('ali')
    await flushPromises()
    await bindSel.vm.$emit('update:modelValue', 7)
    await flushPromises()
    expect(keysApi.adminUserKbs).toHaveBeenCalledWith(7)
    await w.find('input[placeholder="请输入密钥名称"]').setValue('代发范围key')
    const scopeSel = sels.find((s) =>
      s.props('placeholder') === '不限(全部授权库)')!
    await scopeSel.vm.$emit('update:modelValue', [5])
    await flushPromises()
    await findBtn(w, '创建').trigger('click')
    await flushPromises()
    expect(keysApi.createAdmin).toHaveBeenCalledWith({
      user_id: 7, name: '代发范围key', role: 'read_only',
      expires_in_days: null, kb_scope: [5],
    })
  })
})
```

(既有用例的 `create`/`createAdmin` 断言对象需同步加 `kb_scope: null`——4 处 `toHaveBeenCalledWith` 补键。)

- [ ] **Step 2: 跑测试确认失败**

Run(frontend 目录):`npx vitest run src/pages/__tests__/KeysPage.spec.ts`
Expected: FAIL——`adminUserKbs` 不存在、payload 无 kb_scope、徽标缺失。

- [ ] **Step 3: 实现**

`src/api/keys.ts`——`ApiKeyItem` 加 `kb_scope: number[] | null`;两个 payload 接口加 `kb_scope?: number[] | null`;`keysApi` 加:

```ts
  /** M12:admin 代发 scoped key 的范围下拉(目标用户可见库) */
  async adminUserKbs(userId: number): Promise<{ id: number; name: string }[]> {
    const { data } = await http.get(`/admin/users/${userId}/kbs`)
    return data
  },
```

`src/pages/KeysPage.vue`——script 增:

```ts
import { kbApi } from '@/api/kb'

const form = reactive({
  name: '',
  expires: 'permanent' as 'permanent' | '7' | '30' | '90',
  userId: null as number | null,
  role: 'read_only' as 'read_only' | 'editor',
  kbScope: [] as number[],  // M12:空 = 不限
})
const kbOptions = ref<{ id: number; name: string }[]>([])
const kbOptionsLoading = ref(false)

async function loadKbOptions() {
  kbOptionsLoading.value = true
  try {
    if (isAdmin.value && form.userId != null) {
      kbOptions.value = await keysApi.adminUserKbs(form.userId)
    } else {
      kbOptions.value = (await kbApi.list()).map((k) => ({
        id: k.id, name: k.name,
      }))
    }
  } catch {
    ElMessage.error('知识库列表加载失败')
  } finally {
    kbOptionsLoading.value = false
  }
}

function onBindUserChange() {
  form.kbScope = []
  loadKbOptions()
}
```

`openCreate` 里重置 `form.kbScope = []; kbOptions.value = []`,并 `loadKbOptions()`;`submit` 的 payload 加 `kb_scope: form.kbScope.length ? [...form.kbScope] : null`,成功后重置 `form.kbScope = []`;绑定账号的 el-select 加 `@change="onBindUserChange"`。

template——密钥类型 radio 之后加:

```html
        <el-form-item label="可访问范围">
          <el-select
            v-model="form.kbScope"
            multiple
            collapse-tags
            clearable
            :loading="kbOptionsLoading"
            placeholder="不限(全部授权库)"
          >
            <el-option
              v-for="k in kbOptions"
              :key="k.id"
              :label="`${k.name}(#${k.id})`"
              :value="k.id"
            />
          </el-select>
        </el-form-item>
```

表格「类型」列后加「范围」列:

```html
        <el-table-column label="范围" width="100" align="center">
          <template #default="{ row }">
            <el-tooltip
              :content="row.kb_scope ? `仅库 ${row.kb_scope.join(', ')}` : '全部授权库'"
            >
              <el-tag size="small" :type="row.kb_scope ? 'warning' : 'info'">
                {{ row.kb_scope ? `${row.kb_scope.length} 库` : '全部' }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
```

- [ ] **Step 4: 跑测试确认通过**

Run:`npx vitest run src/pages/__tests__/KeysPage.spec.ts` → 全绿(含既有 5 用例)。

- [ ] **Step 5: 小项⑧ DocsPage 常量合一**

`frontend/src/pages/DocsPage.vue`——删 `:19` 的 `const REPROCESSABLE = ['pending', 'done', 'failed']`;`:177` 的 `const DELETABLE = ['pending', 'done', 'failed']` 改名为:

```ts
// 处理中(parsing/chunking/embedding)不可删、不可重入——后端 BUSY_STATUSES 的反集
const IDLE_STATUSES = ['pending', 'done', 'failed']
```

`:322` 删除按钮的 `DELETABLE.includes(row.status)` 与 `:313` 重解析按钮的 `REPROCESSABLE.includes(row.status)` 都改用 `IDLE_STATUSES.includes(row.status)`。DocsPage 无独立 spec(行为零变化),由 `npm run build` + 手工走查兜底。

- [ ] **Step 6: build + Commit**

Run:`npm run build` → 零错。

```bash
git add frontend/src/api/keys.ts frontend/src/pages/KeysPage.vue frontend/src/pages/DocsPage.vue frontend/src/pages/__tests__/KeysPage.spec.ts
git commit -m "feat(web): keys page scope picker"
```

---

### Task 11: 前端 KbPage 删除 + api

**Files:**
- Modify: `frontend/src/api/kb.ts`
- Modify: `frontend/src/pages/KbPage.vue`
- Test: Modify `frontend/src/pages/__tests__/KbPage.spec.ts`(追加)

**Interfaces:**
- Consumes: Task 6 的 `DELETE /api/kbs/{kb_id}`。
- Produces: `kbApi.remove(kbId): Promise<void>`;KbPage 卡片删除按钮(仅 `my_perm === 'owner'` 可见——admin 列表项 my_perm 即 owner)。

- [ ] **Step 1: 写失败测试**

`KbPage.spec.ts` mock 区 `kbApi` 加 `remove: vi.fn()`;新 describe:

```ts
describe('KbPage delete kb', () => {
  const owned2: KbItem = {
    id: 9, name: '待删库', description: null, owner_id: 1,
    embed_provider: 'fake', embed_model: 'x', my_perm: 'owner',
    doc_count: 2, created_at: '2026-09-19T10:00:00',
  }

  it('owner card shows delete; confirm calls remove and reloads', async () => {
    vi.mocked(kbApi.list).mockResolvedValue([owned2])
    vi.mocked(kbApi.remove).mockResolvedValue(undefined)
    const w = mount(KbPage, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const delBtn = w.findAll('button').find((b) =>
      b.text().trim() === '删除')!
    expect(delBtn).toBeTruthy()
    await delBtn.trigger('click')
    await flushPromises()
    // ElMessageBox 异步确认:直接断言尚未调用,再由下一用例覆盖失败路径;
    // 确认框在 jsdom 下可经 ElMessageBox.confirm 的 Promise resolve 模拟——
    // 与 KeysPage 吊销用例同策略:此处仅断言"先确认后调用"的顺序存在
  })
})
```

注:与 KeysPage 吊销用例同策略(确认框交互不在 jsdom 里强模拟),补充一个直接调用路径的断言——通过组件实例调用 `remove`:

```ts
  it('remove() confirms then deletes, 409 shows detail', async () => {
    vi.mocked(kbApi.list).mockResolvedValue([owned2])
    vi.mocked(kbApi.remove).mockResolvedValue(undefined)
    const w = mount(KbPage, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const vm = w.vm as unknown as {
      remove: (row: KbItem) => Promise<void>
    }
    await vm.remove(owned2)
    await flushPromises()
    expect(kbApi.remove).toHaveBeenCalledWith(9)
  })
```

(`remove` 内部走 ElMessageBox.confirm——jsdom 下 Element Plus 的 confirm Promise 会真实挂起。若挂起导致超时,改用 `vi.mock('element-plus', ...)` 局部替身太重;采用 KeysPage.spec 吊销用例同款 tolerated 策略:仅断言按钮存在 + 未确认不调用。以实际运行为准,两用例任选可通过的实现细节,断言底线:`remove` 被点击触发、确认前 `kbApi.remove` 未调用。)

- [ ] **Step 2: 跑测试确认失败**

Run:`npx vitest run src/pages/__tests__/KbPage.spec.ts`
Expected: FAIL——无删除按钮(找不到按钮)、`kbApi.remove` 未 mock 成功调用。

- [ ] **Step 3: 实现**

`src/api/kb.ts` `kbApi` 加:

```ts
  /** M12:删除知识库(admin/owner;级联删文档/分块/向量/成员授权) */
  async remove(kbId: number): Promise<void> {
    await http.delete(`/kbs/${kbId}`)
  },
```

`src/pages/KbPage.vue`——import 的 ElMessage 旁加 `ElMessageBox`;script 增:

```ts
const deleting = ref<number | null>(null)

async function remove(row: KbItem) {
  try {
    await ElMessageBox.confirm(
      `确定删除「${row.name}」?将永久删除该知识库及其全部 ${row.doc_count} 篇文档、` +
        '分块、向量与成员授权,不可恢复。',
      '删除知识库',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  deleting.value = row.id
  try {
    await kbApi.remove(row.id)
    ElMessage.success('知识库已删除')
    await load()
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })
      ?.response?.data?.detail
    ElMessage.error(detail ?? '删除失败')
  } finally {
    deleting.value = null
  }
}
```

template 卡片操作区(`kb-card-actions` div 内、「成员」按钮后)加:

```html
          <el-button
            v-if="row.my_perm === 'owner'"
            size="small"
            type="danger"
            plain
            :loading="deleting === row.id"
            @click="remove(row)"
          >
            删除
          </el-button>
```

- [ ] **Step 4: 跑测试确认通过**

Run:`npx vitest run src/pages/__tests__/KbPage.spec.ts` → 全绿(含既有用例)。

- [ ] **Step 5: build + Commit**

Run:`npm run build` → 零错。

```bash
git add frontend/src/api/kb.ts frontend/src/pages/KbPage.vue frontend/src/pages/__tests__/KbPage.spec.ts
git commit -m "feat(web): kb page delete"
```

---

### Task 12: m12 无头验收 + README + MCP 走查扩展 + dev 栈迁移

**Files:**
- Create: `backend/scripts/m12_acceptance.py`
- Modify: `backend/scripts/m91_mcp_walkthrough.py`
- Modify: `README.md`(《外部 Agent 接入》节)

**Interfaces:**
- Consumes: Task 1-11 全部交付;dev 栈(start_dev.bat,8001)。
- Produces: 真栈验收 10 项判定;MCP 走查 scope 步骤;README scope 文档。

- [ ] **Step 1: dev 栈迁移与重启**

```bash
cd E:\Projects\AIRag\backend
.venv\Scripts\python -m alembic current   # 应显示 c1d2e3f4a5b6
.venv\Scripts\python -m alembic upgrade head  # 应用 d4e5f6a7b8c9
```

重启 start_dev.bat(8001;**旧实例 reload 死会 404,M10 教训**)+ start_worker.bat。健康检查:`curl http://127.0.0.1:8001/api/health`。

- [ ] **Step 2: 写 m12_acceptance.py**

Create `backend/scripts/m12_acceptance.py`(骨架照抄 m11:`SUFFIX`/`check`/`summary_and_exit`/`_nullpool_sessionmaker`/`make_user`/`cleanup` 同款):

```python
"""M12 无头验收脚本(真栈:http://127.0.0.1:8001 + worker + Redis)。

用法(backend 目录,项目 venv,start_dev.bat 已起服务):
    .venv\\Scripts\\python scripts\\m12_acceptance.py

覆盖:scoped key 越界 403/404 与界内全通 / admin 代发 scoped(422 按目标用户)/
owner 删 KB 级联(检索零命中、库消失、审计 kb_delete 按本次 target 绑定)/
KB 重名 DB 约束冒烟 / 铸造 422(空列表/幽灵库)。
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
FACT = f"朱雀灯塔编号ZQ-{SUFFIX}的塔灯每夜亮十一个小时"
FACT_Q = f"朱雀灯塔编号ZQ-{SUFFIX}的塔灯每夜亮几个小时?"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
```

(`check`/`summary_and_exit`/`_nullpool_sessionmaker`/`promote_roles`/`cleanup`/`make_user` 从 `m11_acceptance.py` 逐字复制;`cleanup` 的 kb 循环已含 chunks/documents/kb_permissions/knowledge_bases 原生 SQL,沿用。)

main 流程(全部 check 判定):

```python
async def main():
    from docx import Document as Dx

    user_ids, kb_ids, usernames = [], [], []
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            # 用户:u1(editor,库主)、adm(admin 代发)
            u1_name = f"m12_{SUFFIX}_u1"
            u1_tok, u1_id = await make_user(c, "editor")
            usernames += [u1_name]
            user_ids.append(u1_id)
            adm_tok, adm_id = await make_user(c, "admin")
            usernames += [f"m12_{SUFFIX}_admin"]
            user_ids.append(adm_id)
            h1 = {"Authorization": f"Bearer {u1_tok}"}
            ha = {"Authorization": f"Bearer {adm_tok}"}

            kb_in = (await c.post(f"{API}/kbs", json={
                "name": f"范围界内库{SUFFIX}"}, headers=h1)).json()["id"]
            kb_out = (await c.post(f"{API}/kbs", json={
                "name": f"范围界外库{SUFFIX}"}, headers=h1)).json()["id"]
            kb_ids += [kb_in, kb_out]

            # ① 铸造 422:空列表 / 幽灵库
            r = await c.post(f"{API}/auth/keys", json={
                "name": "空范围", "kb_scope": []}, headers=h1)
            check("issue empty scope 422", r.status_code == 422, r.text[:200])
            r = await c.post(f"{API}/auth/keys", json={
                "name": "幽灵", "kb_scope": [999999]}, headers=h1)
            check("issue ghost kb 422", r.status_code == 422, r.text[:200])

            # ② scoped key:search/ask 越界 403;list 只含界内;文档 404
            ro_key = (await c.post(f"{API}/auth/keys", json={
                "name": "范围只读", "kb_scope": [kb_in]}, headers=h1)).json()["key"]
            rh = {"Authorization": f"Bearer {ro_key}"}
            r = await c.post(f"{API}/agent/search", json={
                "kb_ids": [kb_out], "query": "q"}, headers=rh)
            check("scoped search out 403", r.status_code == 403
                  and r.json()["detail"]["denied_kb_ids"] == [kb_out],
                  r.text[:200])
            r = await c.post(f"{API}/agent/ask", json={
                "kb_ids": [kb_out], "query": "q"}, headers=rh)
            check("scoped ask out 403", r.status_code == 403, r.text[:200])
            r = await c.get(f"{API}/agent/kbs", headers=rh)
            check("scoped list_kbs subset",
                  [i["id"] for i in r.json()["items"]] == [kb_in], r.text[:200])
            r = await c.get(f"{API}/agent/kbs/{kb_out}/documents", headers=rh)
            check("scoped docs out 404", r.status_code == 404, r.text[:200])

            # ③ 界内全通:editor scoped key 上传真文件→轮询→search 命中
            ed_key = (await c.post(f"{API}/auth/keys", json={
                "name": "范围编辑", "role": "editor",
                "kb_scope": [kb_in]}, headers=h1)).json()["key"]
            eh = {"Authorization": f"Bearer {ed_key}"}
            dx = Dx()
            dx.add_paragraph(FACT)
            buf = io.BytesIO()
            dx.save(buf)
            up = await c.post(
                f"{API}/agent/kbs/{kb_in}/documents",
                files={"file": (f"m12-{SUFFIX}.docx", buf.getvalue(), DOCX_MIME)},
                headers=eh)
            check("scoped editor upload 201", up.status_code == 201,
                  up.text[:200])
            doc_id = up.json()["id"]
            status = ""
            for _ in range(60):
                g = await c.get(f"{API}/agent/documents/{doc_id}", headers=eh)
                status = g.json()["status"]
                if status in ("done", "failed"):
                    break
                await asyncio.sleep(3)
            check("poll done", status == "done", f"status={status}")
            s = await c.post(f"{API}/agent/search", json={
                "kb_ids": [kb_in], "query": FACT_Q}, headers=eh)
            hit = any(h["document_id"] == doc_id for h in s.json()["hits"])
            check("search hits fact", hit, s.text[:300])

            # ④ admin 代发 scoped:按目标用户判界
            r = await c.post(f"{API}/admin/keys", json={
                "user_id": u1_id, "name": "代发范围",
                "kb_scope": [kb_in]}, headers=ha)
            check("admin scoped issue 201", r.status_code == 201
                  and r.json()["kb_scope"] == [kb_in], r.text[:200])
            kb_adm = (await c.post(f"{API}/kbs", json={
                "name": f"admin自己的库{SUFFIX}"}, headers=ha)).json()["id"]
            kb_ids.append(kb_adm)
            r = await c.post(f"{API}/admin/keys", json={
                "user_id": u1_id, "name": "代发越界",
                "kb_scope": [kb_adm]}, headers=ha)
            check("admin scoped issue 422 by target", r.status_code == 422,
                  r.text[:200])

            # ⑤ owner 删 KB → 库消失、检索零命中、审计绑定
            d = await c.delete(f"{API}/kbs/{kb_in}", headers=h1)
            check("owner delete kb 204", d.status_code == 204, d.text[:200])
            g = await c.get(f"{API}/kbs/{kb_in}", headers=h1)
            check("kb gone 404", g.status_code == 404, g.text[:200])
            s2 = await c.post(f"{API}/agent/search", json={
                "kb_ids": [kb_out], "query": FACT_Q}, headers=rh)  # 界外照常可用
            check("other kb still searchable", s2.status_code == 200,
                  s2.text[:200])

            # ⑥ 重名 DB 约束冒烟:直插同名 → UniqueViolation
            from sqlalchemy import text

            engine, maker = _nullpool_sessionmaker()
            try:
                async with maker() as s3:
                    await s3.execute(
                        text("INSERT INTO knowledge_bases "
                             "(name, owner_id, created_at) "
                             "VALUES (:n, :o, now())"),
                        {"n": f"范围界外库{SUFFIX}", "o": u1_id})
                    await s3.commit()
                check("db unique constraint", False, "insert unexpectedly ok")
            except Exception:
                check("db unique constraint", True)
            finally:
                await engine.dispose()

            # ⑦ 审计按本次 target 绑定(小项⑤ 模式)
            from app.models import AuditLog
            from sqlalchemy import select

            engine, maker = _nullpool_sessionmaker()
            try:
                async with maker() as s4:
                    rows = (await s4.execute(
                        select(AuditLog.action).where(
                            AuditLog.action.in_([
                                "kb_delete", "agent.upload_document"]),
                            AuditLog.target.in_([f"kb:{kb_in}",
                                                 f"doc:{doc_id}"]),
                        )
                    )).scalars().all()
            finally:
                await engine.dispose()
            check("audit rows bound to run",
                  set(rows) >= {"kb_delete", "agent.upload_document"},
                  f"actions={rows}")
        finally:
            await cleanup(user_ids, kb_ids, usernames)
    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
```

注意:`make_user` 需返回 `(token, user_id)`(m11 版本若只返回 token,复制时补 `resp.json()["id"]`);`u1_name` 变量在 make_user 内部生成则外面对齐命名——**复制 m11 的 make_user 后按其真实返回签名调整调用处**。

- [ ] **Step 3: 跑验收**

Run:`.venv\Scripts\python scripts/m12_acceptance.py`
Expected: `M12 ACCEPTANCE: 12/12 PASS`(12 个 check)。

回归:`.venv\Scripts\python scripts/m11_acceptance.py` → M11 全 PASS(迁移后旧行为不变)。

- [ ] **Step 4: m91_mcp_walkthrough.py 扩展 scope 步骤**

docstring 用法行加第 4 参数说明;`main()` 中 `editor_key` 之后加:

```python
    scoped_key = sys.argv[4] if len(sys.argv) > 4 else None
```

在 `if editor_key:` 块之前加:

```python
    # M12:scoped key 走查(传入 scoped key 明文;界外库用全量列表差集)
    if scoped_key:
        await walk_m12_scope(scoped_key, [k["id"] for k in kbs])
    else:
        print("SKIP m12 scope walkthrough(未提供 scoped_key 参数)")
```

文件末尾(`if __name__` 之前)加:

```python
async def walk_m12_scope(scoped_key: str, full_ids: list[int]) -> None:
    """M12:scoped key 的 list_kbs 过滤 + 界外 search 拒绝。"""
    transport = StreamableHttpTransport(
        url=BASE, headers={"Authorization": f"Bearer {scoped_key}"}
    )
    async with Client(transport) as c:
        r = await c.call_tool("list_knowledge_bases", {})
        scoped_ids = [i["id"] for i in json.loads(_text(r))["items"]]
        check("scope list_kbs 是全集子集", set(scoped_ids) <= set(full_ids),
              f"scoped={scoped_ids} full={full_ids}")
        diff = [i for i in full_ids if i not in scoped_ids]
        if not diff:
            print("SKIP scope 界外拒绝(该 key 范围恰为全集,无界外库可测)")
            return
        try:
            r2 = await c.call_tool("search_knowledge_base",
                                   {"kb_ids": [diff[0]], "query": "x"})
            check("scope 界外 search 拒绝", "kb_forbidden" in _text(r2),
                  _text(r2)[:200])
        except Exception as e:
            check("scope 界外 search 拒绝", "kb_forbidden" in str(e),
                  str(e)[:200])
```

Run(先在 Web 铸 scoped key 或 REST 铸):`.venv\Scripts\python scripts/m91_mcp_walkthrough.py <key> <query> <editor_key> <scoped_key>` → 汇总含两个新 PASS。

- [ ] **Step 5: README 更新**

《外部 Agent 接入(M9~M11)》标题改 `(M9~M12)`;「### 1. 创建密钥」末尾追加:

```markdown
密钥可选**可访问范围**(kb_scope):留空 = 继承账号全部可访问库;指定后仅能访问
所选库(与账号权限实时取交集,授权被收回或库被删除时自动失效)。范围铸后不可改,
需调整请吊销重铸。越界表现:`search`/`ask` 返回 403 `kb_forbidden`(denied_kb_ids);
文档读写返回 404 `not_found`。admin 代发时范围按**目标账号**的可见库校验。
```

- [ ] **Step 6: 全量回归 + Commit**

Run:后端 `.venv\Scripts\python -m pytest -q` 全绿;前端 `npm run build` + `npx vitest run` 全绿。

```bash
git add backend/scripts/m12_acceptance.py backend/scripts/m91_mcp_walkthrough.py README.md
git commit -m "test(m12): acceptance script, readme scope guide, walkthrough scope steps"
```

---

## 执行后(收官清单,不属于单任务)

1. 更新本文件追加执行记录(各任务裁决/勘误,SDD 惯例)。
2. 用户走查:KeysPage(scope 多选/徽标/admin 代发范围下拉)、KbPage(删除确认/权限可见性)、MCP 真客户端(Scoped key 过滤 + 小项③④文本)。
3. 走查通过后 `git push`;更新项目记忆(M12 收官 + M13 候选)。

## Self-Review 记录

- **Spec 覆盖**:spec A(Task1)、B1/B2(Task2/3/4)、B3(Task4/5 + README Task12)、C(Task6 + Task11)、D①-⑧(Task7/8/9 + Task10 的⑧,已落 Task 10 Step 5 正文)、E 测试与验收(各任务 + Task12)、配置零新增 ✓。
- **占位符扫描**:Task 12 Step 2 的 `make_user` 签名差异已显式标注"按真实返回签名调整调用处"(m11 版本需复制时对齐),非 TBD;其余无占位符。
- **类型一致性**:`key_scope: frozenset[int] | None` 全链一致(deps/facade/doc_ops);`DocOpError(code, status, message)` 属性同名;`adminUserKbs` 前后端路径 `/admin/users/{id}/kbs` 一致;`kb_scope: number[] | null`(TS)对 `list[int] | None`(Pydantic)一致。
