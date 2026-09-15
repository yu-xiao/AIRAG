# AIRag M4 企业化完善实施计划(RBAC/重试/历史/Rerank 开关)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 权限隔离生效:三级角色(viewer/editor/admin)+ KB 成员授权收紧所有入口(修复 M3-R4 遗留"登录即可检索任意 KB");失败文档重新解析;会话删除;Rerank 请求级开关(前端可切)。验收:双用户权限矩阵脚本化走查 + 存量 70 用例全绿。

**Architecture:** 复用 M1-M3 全部基座。新增 `app/core/perms.py` 单点权限判定(`get_kb_perm`/`has_perm`),kbs/documents/ask 三处 API 入口接同一判定;`kb_permissions` 表(M1 已建、M3 未用)真正启用;用户管理走 `/api/admin/users`(admin only)。Rerank 从编译期拓扑改为请求级开关:图拓扑恒含 rerank 直通节点,`AskIn.rerank` 流入 state。**零数据库迁移**(role 列/kb_permissions 表均已存在,role 无 server_default)。

**Tech Stack(新增):** 前端 dompurify(流式渲染消毒,M3 代码注释已承诺 M4)。后端零新依赖。

**Spec:** `docs/AIRag-AI知识库需求设计方案.md` §1.2(无权限不可见不可检索)/§9-M4;前置:`2026-09-15-airag-m3-retrieval-chat.md` 末尾《M4 交接附录》(本计划已消化,裁决见下)。

## 开工裁决(控制器,2026-09-15,吸收交接附录)

| # | 裁决 | 理由与代价 |
|---|---|---|
| R1 | **角色语义钉死**:admin=隐式全部 KB owner + 用户管理;editor=可建库(成为 owner)、可上传被授 editor 权的库;viewer=只读使用者(可问答被授 viewer+ 权的库,不能建库/上传)。**注册默认 viewer**(修掉 M1 遗留 default="admin" 隐患) | 三级与 spec §9-M4 一一对应;现有开发库用户已是 admin 不受影响;存量测试经 conftest 升级 editor 后不改语义 |
| R2 | **权限语义**:KB 级权限 = owner(admin 全局隐式/库主)> kb_permissions(editor/viewer)> 无。直接访问端点对不可见库统一 **404**(不泄露存在性);`/ask` 对 kb_ids 任一不可见 **403**(kb id 来自用户自己请求,无泄露);上传/重解析需 editor+ 否则 403 | spec"不可见、不可检索"字面落地;403/404 语义写进测试 |
| R3 | **成员按用户名授权**(GrantIn{username, perm}),不是 user_id | 企业用户互知用户名;admin 可从用户管理页查 id 但普通 owner 不知道;目标不存在 404、目标是库主 400(库主不入表天然全权) |
| R4 | **重解析 = 删旧 chunks + 状态回 pending + 重新入队**,对 failed/done/pending 均可,处理中(parsing/chunking/embedding)409 防重入 | 与 M2 流水线幂等设计一致(按 document_id 重跑全链);spec §7"支持重新处理" |
| R5 | **Rerank 请求级开关**:图拓扑恒为 retrieve→rerank→generate(rerank_node 无 provider 或未开时直通返回 {});`AskIn.rerank: bool=False` 流入 state;RERANK_ENABLED 环境变量语义收窄为"provider 可用性" | M3 编译期拓扑无法逐请求切换;直通节点开销为零(一次 dict 判断);前端开关 localStorage 记忆 |
| R6 | **问答历史完善 = 会话删除**(DELETE 级联 messages)+ 前端删除按钮;不做重命名/导出 | M3 已有列表+历史加载;spec 未细化,按最小闭环 |
| R7 | **M3 承诺的 M4 加固批**:DOMPurify(markdown 渲染消毒)+ 页面卸载中止 SSE(useChatStream 暴露 abort) | ChatPage.vue:17 注释明示"DOMPurify 排期在 M4";交接附录 Minor 清单对应项。其余 Minor(轮询闪烁/kb 高亮/markdown 全量重渲等)继续延后 M5 |
| R8 | **LDAP/SSO 开放问题未收到输入**,M4 维持本地账号体系 | spec §11 开放问题;不阻塞 RBAC 本体;记入 M5 交接 |
| R9 | **alembic 零迁移**(已核验:users.role/kb_permissions 均在 init 迁移 e6ad26c40163,role 无 server_default,模型 default 是 Python 侧) | 避免无谓迁移;若执行中发现运行库缺表以 `alembic upgrade head` 兜底 |

## Global Constraints(承前,新增 M4 专属)

- M1 执行协议全部继续有效:**DLP 双检**(findstr TSD-Header 且 numstat 无 `-	-`)、显式路径暂存、conventional commits、TDD 红绿、py -3.12、测试进程 conftest 强制 test 库+fake 嵌入。
- **计数基线:后端 46 passed 0 skipped(2026-09-15 实测,比 M3 计划的 43 多 3 个 T8 加固用例,以实际为准);前端 3 passed。**Expected 计数按测试函数数。
- 端口/进程:后端 8001/前端 5173/PG 5432/Redis 6379(.env 带密码 REDIS_URL,**勿动 Redis 服务**);真模型 ZHIPU key 可用,EMBED_PROVIDER=zhipu。
- 权限语义(R2)与角色语义(R1)是全计划钉死契约,测试断言状态码必须与之一致。
- 存量用例适配**只允许**两种:T1 的 conftest auth_headers 升 editor、T4 的 test_ask 建 KB 播种;其余存量用例语义不得改动。
- 前端在页面任务完成前不得破坏现有三页面;每任务 `pnpm build` + `pnpm test` 全绿才可提交。

## 环境事实(已核验,2026-09-15)

- 后端 `pytest -q` = **46 passed**;前端 vitest = 3 passed。
- `User.role` 存在(String16,模型 default="admin"——R1 改 "viewer");`UserOut` 已含 role 字段(schemas/auth.py);`/auth/me` 已返回 role。
- `KbPermission` 模型+表已存在(viewer|editor,UniqueConstraint(kb_id,user_id)),从未被 API 使用。
- kbs/documents/ask 三组 API 当前**无任何权限过滤**(M3-R4 从简);conversations 已按 user 域隔离。
- `build_graph` 拓扑由 settings.RERANK_ENABLED 编译期决定;`rerank_node` 已有 `get_reranker() is None` 直通分支。
- conftest:`auth_headers` 注册随机用户并登录(改后需升 editor);`celery_eager` 强制 eager+propagates=False;表按 CLEANUP_ORDER 逐测试清空。
- test_ask 现发 kb_ids=[1] 且不建 KB——ask 加权限校验后必须播种(见 T4)。
- 前端 `api/auth.ts` 的 UserResponse 需核验是否含 role(执行 T8 时确认,缺则补)。
- 运行库 airag 有 M3 验收残留(KB 2 三文档);存量用户 role 均为 'admin'(旧 default),验收脚本需自建新用户。

---

### Task 0: 依赖与前端类型基线

**Files:** Modify `frontend/package.json`(pnpm add dompurify)、`frontend/src/api/auth.ts`(核验/补 UserResponse.role)

- [x] Step 1: `cd /d E:\Projects\AIRag\frontend && pnpm add dompurify && pnpm add -D @types/dompurify`(若类型已内联于包则跳过 -D 项,以安装输出为准)。
- [x] Step 2: 核验 `src/api/auth.ts` 的 `UserResponse` 是否含 `role: string`;缺则在接口定义中补上(后端 UserOut 自 M1 起就有 role)。
- [x] Step 3: `pnpm build && pnpm test` 全绿(3 passed)。
- [x] Step 4: 提交:`chore: m4 deps (dompurify) and auth type baseline`

### Task 1: 权限判定核心 + 注册默认 viewer

**Files:** Create `backend/app/core/perms.py`;Modify `backend/app/models/user.py`(default viewer)、`backend/tests/conftest.py`(auth_headers 升 editor);Test `backend/tests/test_perms.py`

**Interfaces(Produces):** `get_kb_perm(db: AsyncSession, user: User, kb: KnowledgeBase) -> str | None`(返回 "owner"|"editor"|"viewer"|None;admin 全局 owner);`has_perm(perm: str | None, min_perm: str) -> bool`(owner>editor>viewer);后续 T2/T3/T4/T5 全部消费这两个函数。

- [x] Step 1: 失败测试 `tests/test_perms.py`(4 函数):

```python
async def test_admin_and_owner_are_implicit_owner(db_session):
    from app.core.perms import get_kb_perm
    from app.models import KnowledgeBase, User

    owner = User(username="p_owner", password_hash="x", role="editor")
    admin = User(username="p_admin", password_hash="x", role="admin")
    db_session.add_all([owner, admin])
    await db_session.flush()
    kb = KnowledgeBase(name="权限库", owner_id=owner.id)
    db_session.add(kb)
    await db_session.flush()
    assert await get_kb_perm(db_session, admin, kb) == "owner"
    assert await get_kb_perm(db_session, owner, kb) == "owner"


async def test_granted_and_missing_perm(db_session):
    from app.core.perms import get_kb_perm
    from app.models import KnowledgeBase, KbPermission, User

    owner = User(username="g_owner", password_hash="x", role="editor")
    viewer = User(username="g_viewer", password_hash="x", role="viewer")
    stranger = User(username="g_other", password_hash="x", role="editor")
    db_session.add_all([owner, viewer, stranger])
    await db_session.flush()
    kb = KnowledgeBase(name="授权库", owner_id=owner.id)
    db_session.add(kb)
    await db_session.flush()
    db_session.add(KbPermission(kb_id=kb.id, user_id=viewer.id, perm="viewer"))
    await db_session.flush()
    assert await get_kb_perm(db_session, viewer, kb) == "viewer"
    assert await get_kb_perm(db_session, stranger, kb) is None


def test_has_perm_ranking():
    from app.core.perms import has_perm

    assert has_perm("editor", "viewer")
    assert has_perm("owner", "editor")
    assert not has_perm("viewer", "editor")
    assert not has_perm(None, "viewer")


async def test_register_defaults_to_viewer(client):
    resp = await client.post(
        "/api/auth/register", json={"username": "plain_me", "password": "secret123"}
    )
    assert resp.status_code == 201
    assert resp.json()["role"] == "viewer"
```

- [x] Step 2: RED(`pytest tests\test_perms.py -v` 全 FAIL/ERROR)。
- [x] Step 3: 实现:

`app/core/perms.py`:

```python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeBase, KbPermission, User

RANK = {"viewer": 1, "editor": 2, "owner": 3}


async def get_kb_perm(db: AsyncSession, user: User, kb: KnowledgeBase) -> str | None:
    """KB 级权限:admin 全局隐式 owner;库主 owner;否则查 kb_permissions。"""
    if user.role == "admin" or kb.owner_id == user.id:
        return "owner"
    row = await db.execute(
        select(KbPermission.perm).where(
            KbPermission.kb_id == kb.id, KbPermission.user_id == user.id
        )
    )
    return row.scalar_one_or_none()


def has_perm(perm: str | None, min_perm: str) -> bool:
    return perm is not None and RANK[perm] >= RANK[min_perm]
```

`models/user.py`:role 行改 `default="viewer"`。

`conftest.py` 的 `auth_headers`(register 后追加升级,`text` 已在顶部导入):

```python
    created = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    # M4 起注册默认 viewer;存量用例的建库/上传流按 editor 走
    await db_session.execute(
        text("UPDATE users SET role = 'editor' WHERE id = :i"),
        {"i": created.json()["id"]},
    )
    await db_session.commit()
```

(fixture 签名补 `db_session` 依赖:`async def auth_headers(client, db_session):`。)

- [x] Step 4: GREEN:全量 **50 passed**(46+4)。
- [x] Step 5: 提交:`feat: kb permission core with viewer-default registration`

### Task 2: KB API 权限收紧(可见性过滤/my_perm/viewer 禁建)

**Files:** Modify `backend/app/api/kbs.py`、`backend/app/schemas/kb.py`(KBOut 增 `my_perm: str | None = None`);Test `backend/tests/test_kbs.py` 追加

**Interfaces:** GET /kbs 仅返回可见库(admin 全部;非 admin = own ∪ granted),每项附 my_perm;GET /kbs/{id} 不可见 404;POST /kbs viewer 403。Consumes: T1 的 get_kb_perm/has_perm。

- [x] Step 1: 失败测试(test_kbs.py 追加 4 函数 + 文件级 helper):

```python
async def _register_and_login(client, username):
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}
```

```python
async def test_viewer_cannot_create_kb(client, auth_headers):
    plain = await _register_and_login(client, "plain_viewer1")
    resp = await client.post("/api/kbs", json={"name": "游客库"}, headers=plain)
    assert resp.status_code == 403
    # editor 仍可建(auth_headers 已升 editor)
    ok = await client.post("/api/kbs", json={"name": "编辑库"}, headers=auth_headers)
    assert ok.status_code == 201


async def test_kb_invisible_to_stranger(client, auth_headers):
    mine = await client.post("/api/kbs", json={"name": "私库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    other = await _register_and_login(client, "stranger_ed1")  # 默认 viewer
    listed = await client.get("/api/kbs", headers=other)
    assert all(k["id"] != kb_id for k in listed.json())
    got = await client.get(f"/api/kbs/{kb_id}", headers=other)
    assert got.status_code == 404


async def test_list_returns_my_perm(client, auth_headers, db_session):
    from app.models import KbPermission, KnowledgeBase, User

    mine = await client.post("/api/kbs", json={"name": "权限标注库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    listed = await client.get("/api/kbs", headers=auth_headers)
    row = next(k for k in listed.json() if k["id"] == kb_id)
    assert row["my_perm"] == "owner"

    viewer_headers = await _register_and_login(client, "perm_viewer1")
    reg = await client.get("/api/auth/me", headers=viewer_headers)
    db_session.add(KbPermission(kb_id=kb_id, user_id=reg.json()["id"], perm="viewer"))
    await db_session.commit()
    granted = await client.get("/api/kbs", headers=viewer_headers)
    row2 = next(k for k in granted.json() if k["id"] == kb_id)
    assert row2["my_perm"] == "viewer"


async def test_admin_sees_all_kbs(client, auth_headers, db_session):
    from sqlalchemy import text as _text

    mine = await client.post("/api/kbs", json={"name": "他人库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    me = await client.get("/api/auth/me", headers=auth_headers)
    await db_session.execute(
        _text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": me.json()["id"]},
    )
    await db_session.commit()
    listed = await client.get("/api/kbs", headers=auth_headers)
    row = next(k for k in listed.json() if k["id"] == kb_id)
    assert row["my_perm"] == "owner"
```

- [x] Step 2: RED。
- [x] Step 3: 实现(kbs.py 整体重写):

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.perms import get_kb_perm
from app.db.session import get_db
from app.models import KnowledgeBase, KbPermission, User
from app.schemas.kb import KBIn, KBOut

router = APIRouter(prefix="/kbs", tags=["kbs"])


@router.post("", response_model=KBOut, status_code=201)
async def create_kb(
    payload: KBIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current.role == "viewer":
        raise HTTPException(status_code=403, detail="viewers cannot create knowledge bases")
    kb = KnowledgeBase(
        name=payload.name, description=payload.description, owner_id=current.id
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    out = KBOut.model_validate(kb)
    out.my_perm = "owner"
    return out


@router.get("", response_model=list[KBOut])
async def list_kbs(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current.role == "admin":
        rows = (
            await db.execute(select(KnowledgeBase).order_by(KnowledgeBase.id.desc()))
        ).scalars().all()
        out = []
        for kb in rows:
            item = KBOut.model_validate(kb)
            item.my_perm = "owner"
            out.append(item)
        return out
    rows = (
        await db.execute(
            select(KnowledgeBase)
            .where(
                or_(
                    KnowledgeBase.owner_id == current.id,
                    KnowledgeBase.id.in_(
                        select(KbPermission.kb_id).where(
                            KbPermission.user_id == current.id
                        )
                    ),
                )
            )
            .order_by(KnowledgeBase.id.desc())
        )
    ).scalars().all()
    grants = (
        await db.execute(
            select(KbPermission).where(KbPermission.user_id == current.id)
        )
    ).scalars().all()
    perm_by_kb = {g.kb_id: g.perm for g in grants}
    out = []
    for kb in rows:
        item = KBOut.model_validate(kb)
        item.my_perm = "owner" if kb.owner_id == current.id else perm_by_kb.get(kb.id)
        out.append(item)
    return out


@router.get("/{kb_id}", response_model=KBOut)
async def get_kb(
    kb_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    perm = await get_kb_perm(db, current, kb)
    if perm is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    out = KBOut.model_validate(kb)
    out.my_perm = perm
    return out
```

schemas/kb.py 的 KBOut 追加一行:`my_perm: str | None = None`。

- [x] Step 4: GREEN:全量 **54 passed**(50+4)。
- [x] Step 5: 提交:`feat: kb visibility filtering with my_perm and viewer gate`

### Task 3: documents API 权限 + 重新解析端点

**Files:** Modify `backend/app/api/documents.py`;Test `backend/tests/test_documents.py` 追加

**Interfaces:** 上传:不可见 404 / 可见但 <editor 403;列表/详情/chunks:不可见 404;新增 `POST /documents/{doc_id}/reprocess`(editor+,处理中 409,删旧 chunks 回 pending 重新入队,返回 DocumentOut)。

- [x] Step 1: 失败测试(追加 4 函数;文件顶部补 `from tests_helpers import *` 之类不需要——helper 直接复制 Task 2 的 `_register_and_login` 到本文件):

```python
async def test_upload_permission_matrix(client, auth_headers, db_session):
    from app.models import KbPermission

    mine = await client.post("/api/kbs", json={"name": "上传权限库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    stranger = await _register_and_login(client, "up_stranger1")
    r1 = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("a.pdf", b"x", "application/pdf")}, headers=stranger,
    )
    assert r1.status_code == 404  # 不可见,不泄露

    viewer = await _register_and_login(client, "up_viewer1")
    me = await client.get("/api/auth/me", headers=viewer)
    db_session.add(KbPermission(kb_id=kb_id, user_id=me.json()["id"], perm="viewer"))
    await db_session.commit()
    r2 = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("a.pdf", b"x", "application/pdf")}, headers=viewer,
    )
    assert r2.status_code == 403  # 可见但无 editor

    stranger_list = await client.get(f"/api/kbs/{kb_id}/documents", headers=stranger)
    assert stranger_list.status_code == 404


async def test_reprocess_resets_failed_document(client, auth_headers, db_session, monkeypatch):
    import app.api.documents as docs_mod
    from app.models import Chunk, Document, KnowledgeBase

    me = await client.get("/api/auth/me", headers=auth_headers)
    uid = me.json()["id"]
    kb = KnowledgeBase(name="重解析库", owner_id=uid)
    db_session.add(kb)
    await db_session.flush()
    doc = Document(
        kb_id=kb.id, filename="bad.pdf", file_path="x", mime="application/pdf",
        size=1, sha256="f" * 64, status="failed", error_msg="boom",
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(Chunk(document_id=doc.id, kb_id=kb.id, chunk_index=0,
                         content="旧块", page_no=1, char_len=3, content_hash="old"))
    await db_session.commit()

    calls = []
    monkeypatch.setattr(docs_mod, "process_document",
                        lambda doc_id: calls.append(doc_id))
    resp = await client.post(f"/api/documents/{doc.id}/reprocess", headers=auth_headers)
    assert resp.status_code == 200
    assert calls == [doc.id]
    body = resp.json()
    assert body["status"] == "pending"
    assert body["error_msg"] is None
    from sqlalchemy import select

    left = (await db_session.execute(select(Chunk).where(Chunk.document_id == doc.id))).scalars().all()
    assert left == []


async def test_reprocess_conflict_while_processing(client, auth_headers, db_session):
    from app.models import Document, KnowledgeBase

    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = KnowledgeBase(name="处理中库", owner_id=me.json()["id"])
    db_session.add(kb)
    await db_session.flush()
    doc = Document(kb_id=kb.id, filename="p.pdf", file_path="x", mime="application/pdf",
                   size=1, sha256="e" * 64, status="parsing")
    db_session.add(doc)
    await db_session.commit()
    resp = await client.post(f"/api/documents/{doc.id}/reprocess", headers=auth_headers)
    assert resp.status_code == 409


async def test_reprocess_requires_editor(client, auth_headers, db_session):
    from app.models import Document, KnowledgeBase

    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = KnowledgeBase(name="只读重解析库", owner_id=me.json()["id"])
    db_session.add(kb)
    await db_session.flush()
    doc = Document(kb_id=kb.id, filename="v.pdf", file_path="x", mime="application/pdf",
                   size=1, sha256="d" * 64, status="done")
    db_session.add(doc)
    await db_session.commit()

    viewer = await _register_and_login(client, "rp_viewer1")
    vme = await client.get("/api/auth/me", headers=viewer)
    from app.models import KbPermission

    db_session.add(KbPermission(kb_id=kb.id, user_id=vme.json()["id"], perm="viewer"))
    await db_session.commit()
    resp = await client.post(f"/api/documents/{doc.id}/reprocess", headers=viewer)
    assert resp.status_code == 403
```

- [x] Step 2: RED。
- [x] Step 3: 实现(documents.py):

顶部导入追加:

```python
from sqlalchemy import delete
from app.core.perms import get_kb_perm, has_perm
from app.models import Chunk
```

(Chunk 原在函数内导入,统一到顶部;`from app.models import Document, KnowledgeBase, User` 行合并 Chunk。)

`_get_kb_or_404` 替换为带鉴权版本:

```python
async def _get_visible_kb_or_404(
    db: AsyncSession, current: User, kb_id: int
) -> KnowledgeBase:
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or await get_kb_perm(db, current, kb) is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return kb
```

upload_document:`await _get_kb_or_404(db, kb_id)` 改为:

```python
    kb = await _get_visible_kb_or_404(db, current, kb_id)
    if not has_perm(await get_kb_perm(db, current, kb), "editor"):
        raise HTTPException(status_code=403, detail="editor permission required")
```

list_documents:`await _get_kb_or_404(db, kb_id)` → `await _get_visible_kb_or_404(db, current, kb_id)`。

get_document 与 list_chunks 开头各加(替换原裸 404 检查):

```python
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    if kb is None or await get_kb_perm(db, current, kb) is None:
        raise HTTPException(status_code=404, detail="document not found")
```

(list_chunks 里原函数内 `from app.models import Chunk` 删除,用顶部导入。)

新增端点(挂在 list_chunks 之后):

```python
@router.post("/documents/{doc_id}/reprocess", response_model=DocumentOut)
async def reprocess_document(
    doc_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    if kb is None or await get_kb_perm(db, current, kb) is None:
        raise HTTPException(status_code=404, detail="document not found")
    if not has_perm(await get_kb_perm(db, current, kb), "editor"):
        raise HTTPException(status_code=403, detail="editor permission required")
    if doc.status in ("parsing", "chunking", "embedding"):
        raise HTTPException(status_code=409, detail="document is being processed")
    await db.execute(delete(Chunk).where(Chunk.document_id == doc_id))
    doc.status = "pending"
    doc.error_msg = None
    doc.chunk_count = 0
    doc.page_count = None
    await db.commit()
    await db.refresh(doc)
    process_document.delay(doc_id)
    return doc
```

- [x] Step 4: GREEN:全量 **58 passed**(54+4)。
- [x] Step 5: 提交:`feat: document api permissions and reprocess endpoint`

### Task 4: ask 权限校验 + 会话删除(问答历史完善)

**Files:** Modify `backend/app/api/ask.py`、`backend/app/api/conversations.py`;Test `backend/tests/test_ask.py` 追加 1 + 存量适配、`backend/tests/test_conversations.py` 追加 2

**Interfaces:** POST /api/chat/ask:kb_ids 任一不可见(不存在或无权限)→ 403;DELETE /api/chat/conversations/{id} → 204(级联删 messages,他人会话 404)。

- [x] Step 1: 失败测试:

test_ask.py — 存量 `test_ask_streams_tokens_and_saves` 适配:monkeypatch 段之前建 KB 并用其 id(kb_ids 由 [1] 改为变量):

```python
    kb = await client.post(
        "/api/kbs", json={"name": "问答播种库"}, headers=auth_headers
    )
    kb_id = kb.json()["id"]
```

POST body 改 `"kb_ids": [kb_id]`(其余断言不动;fake_search 的 kb_ids 参数是 stub,无需对齐)。

追加:

```python
async def test_ask_rejects_invisible_kb(client, auth_headers):
    other = await _register_and_login(client, "ask_other1")
    mine = await client.post("/api/kbs", json={"name": "他人私库"}, headers=other)
    kb_id = mine.json()["id"]
    resp = await client.post(
        "/api/chat/ask",
        json={"kb_ids": [kb_id], "question": "看得到吗"},
        headers=auth_headers,
    )
    assert resp.status_code == 403
    missing = await client.post(
        "/api/chat/ask",
        json={"kb_ids": [999999], "question": "不存在的库"},
        headers=auth_headers,
    )
    assert missing.status_code == 403
```

(`_register_and_login` helper 同 Task 2 复制到本文件顶部。)

test_conversations.py 追加:

```python
async def test_delete_own_conversation_cascades(client, auth_headers, db_session):
    from app.models import Message

    created = await client.post(
        "/api/chat/conversations", json={"kb_ids": [1], "name": "待删"},
        headers=auth_headers,
    )
    conv_id = created.json()["id"]
    db_session.add_all([
        Message(conversation_id=conv_id, role="user", content="q"),
        Message(conversation_id=conv_id, role="assistant", content="a"),
    ])
    await db_session.commit()
    resp = await client.delete(f"/api/chat/conversations/{conv_id}", headers=auth_headers)
    assert resp.status_code == 204
    from sqlalchemy import select

    rows = (await db_session.execute(select(Message).where(
        Message.conversation_id == conv_id))).scalars().all()
    assert rows == []
    gone = await client.get(
        f"/api/chat/conversations/{conv_id}/messages", headers=auth_headers
    )
    assert gone.status_code == 404


async def test_delete_stranger_conversation_404(client, auth_headers):
    created = await client.post(
        "/api/chat/conversations", json={"kb_ids": [1], "name": "别人的"},
        headers=auth_headers,
    )
    conv_id = created.json()["id"]
    other = await _register_and_login(client, "del_other1")
    resp = await client.delete(f"/api/chat/conversations/{conv_id}", headers=other)
    assert resp.status_code == 404
```

(`_register_and_login` helper 同样复制到本文件;conversations.py 的 messages 端点对不存在的 conv 现返回 404,保持。)

- [x] Step 2: RED(新用例;注意存量 test_ask 若未先做播种适配会同时红——按 Step 1 一起改)。
- [x] Step 3: 实现:

ask.py — 导入区补 `from fastapi import HTTPException`、`from app.core.perms import get_kb_perm`、`from app.models import KnowledgeBase`(并入现有 models 导入行);`ask` 函数体在 `if payload.conversation_id is None:` 之前插入:

```python
    for kb_id in payload.kb_ids:
        kb = await db.get(KnowledgeBase, kb_id)
        if kb is None or await get_kb_perm(db, current, kb) is None:
            raise HTTPException(
                status_code=403, detail=f"no permission for knowledge base {kb_id}"
            )
```

conversations.py — 导入区补 `from sqlalchemy import delete, select`;追加端点:

```python
@router.delete("/conversations/{conv_id}", status_code=204)
async def delete_conversation(
    conv_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(Conversation, conv_id)
    if conv is None or conv.user_id != current.id:
        raise HTTPException(status_code=404, detail="conversation not found")
    await db.execute(delete(Message).where(Message.conversation_id == conv_id))
    await db.delete(conv)
    await db.commit()
    return None
```

- [x] Step 4: GREEN:全量 **61 passed**(58+3)。
- [x] Step 5: 提交:`feat: ask permission gate and conversation deletion`

### Task 5: KB 成员管理 API(权限管理界面后端)

**Files:** Modify `backend/app/api/kbs.py`、`backend/app/schemas/kb.py`;Test `backend/tests/test_kb_permissions.py`(新建)

**Interfaces:** `GET /kbs/{id}/permissions → [{user_id, username, perm}]`(owner/admin);`PUT /kbs/{id}/permissions {username, perm}` upsert 返回 MemberOut(目标用户 404 / 目标是库主 400);`DELETE /kbs/{id}/permissions?username=x` → 204(无授权行 404)。perm ∈ viewer|editor(Literal 校验)。

- [x] Step 1: 失败测试(4 函数,helper 复制):

```python
async def test_owner_grants_updates_and_lists(client, auth_headers, db_session):
    mine = await client.post("/api/kbs", json={"name": "成员库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    grantee = await _register_and_login(client, "member_ed1")

    put = await client.put(
        f"/api/kbs/{kb_id}/permissions",
        json={"username": "member_ed1", "perm": "viewer"}, headers=auth_headers,
    )
    assert put.status_code == 200
    assert put.json()["perm"] == "viewer"

    put2 = await client.put(
        f"/api/kbs/{kb_id}/permissions",
        json={"username": "member_ed1", "perm": "editor"}, headers=auth_headers,
    )
    assert put2.status_code == 200
    assert put2.json()["perm"] == "editor"

    listed = await client.get(f"/api/kbs/{kb_id}/permissions", headers=auth_headers)
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["username"] == "member_ed1"
    assert rows[0]["perm"] == "editor"

    # 授权后对方可见该库且可上传
    visible = await client.get("/api/kbs", headers=grantee)
    assert any(k["id"] == kb_id for k in visible.json())


async def test_grant_requires_owner(client, auth_headers, db_session):
    from app.models import KbPermission

    mine = await client.post("/api/kbs", json={"name": "越权库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    editor_user = await _register_and_login(client, "grant_ed1")
    me = await client.get("/api/auth/me", headers=editor_user)
    db_session.add(KbPermission(kb_id=kb_id, user_id=me.json()["id"], perm="editor"))
    await db_session.commit()
    resp = await client.put(
        f"/api/kbs/{kb_id}/permissions",
        json={"username": "grant_ed1", "perm": "viewer"}, headers=editor_user,
    )
    assert resp.status_code == 403


async def test_revoke_member(client, auth_headers):
    mine = await client.post("/api/kbs", json={"name": "回收库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    await client.put(
        f"/api/kbs/{kb_id}/permissions",
        json={"username": "revoke_me1", "perm": "viewer"}, headers=auth_headers,
    )
    resp = await client.delete(
        f"/api/kbs/{kb_id}/permissions?username=revoke_me1", headers=auth_headers
    )
    assert resp.status_code == 204
    listed = await client.get(f"/api/kbs/{kb_id}/permissions", headers=auth_headers)
    assert listed.json() == []
    again = await client.delete(
        f"/api/kbs/{kb_id}/permissions?username=revoke_me1", headers=auth_headers
    )
    assert again.status_code == 404


async def test_grant_target_validation(client, auth_headers):
    mine = await client.post("/api/kbs", json={"name": "校验库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    unknown = await client.put(
        f"/api/kbs/{kb_id}/permissions",
        json={"username": "no_such_user_x", "perm": "viewer"}, headers=auth_headers,
    )
    assert unknown.status_code == 404
    me = await client.get("/api/auth/me", headers=auth_headers)
    self_grant = await client.put(
        f"/api/kbs/{kb_id}/permissions",
        json={"username": me.json()["username"], "perm": "viewer"}, headers=auth_headers,
    )
    assert self_grant.status_code == 400  # 库主天然全权,不入表
    bad_perm = await client.put(
        f"/api/kbs/{kb_id}/permissions",
        json={"username": "revoke_me2", "perm": "owner"}, headers=auth_headers,
    )
    assert bad_perm.status_code == 422  # Literal 校验挡掉 owner 注入
```

注意 `test_grant_target_validation` 里 `revoke_me2` 无需真注册(422 在用户查询之前由 pydantic 挡下——若实现先查用户则该断言改 404,**实现必须把 Literal 校验交给 pydantic,顺序天然如此**)。

- [x] Step 2: RED。
- [x] Step 3: 实现:

schemas/kb.py 追加:

```python
from typing import Literal

from pydantic import BaseModel, Field


class MemberOut(BaseModel):
    user_id: int
    username: str
    perm: str


class GrantIn(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    perm: Literal["viewer", "editor"]
```

(文件内已有 BaseModel/Field 导入则合并,不重复。)

kbs.py 追加(导入区补 `from app.schemas.kb import GrantIn, MemberOut` 与 `from app.models import User` 中并入;`select` 已有):

```python
@router.get("/{kb_id}/permissions", response_model=list[MemberOut])
async def list_members(
    kb_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if not has_perm(await get_kb_perm(db, current, kb), "owner"):
        raise HTTPException(status_code=403, detail="owner permission required")
    rows = (
        await db.execute(
            select(KbPermission, User)
            .join(User, User.id == KbPermission.user_id)
            .where(KbPermission.kb_id == kb_id)
            .order_by(KbPermission.id)
        )
    ).all()
    return [
        MemberOut(user_id=p.user_id, username=u.username, perm=p.perm)
        for p, u in rows
    ]


@router.put("/{kb_id}/permissions", response_model=MemberOut)
async def grant_permission(
    kb_id: int,
    payload: GrantIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if not has_perm(await get_kb_perm(db, current, kb), "owner"):
        raise HTTPException(status_code=403, detail="owner permission required")
    target = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if target.id == kb.owner_id:
        raise HTTPException(status_code=400, detail="owner already has full access")
    row = (
        await db.execute(
            select(KbPermission).where(
                KbPermission.kb_id == kb_id, KbPermission.user_id == target.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = KbPermission(kb_id=kb_id, user_id=target.id, perm=payload.perm)
        db.add(row)
    else:
        row.perm = payload.perm
    await db.commit()
    return MemberOut(user_id=target.id, username=target.username, perm=payload.perm)


@router.delete("/{kb_id}/permissions", status_code=204)
async def revoke_permission(
    kb_id: int,
    username: str,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if not has_perm(await get_kb_perm(db, current, kb), "owner"):
        raise HTTPException(status_code=403, detail="owner permission required")
    row = (
        await db.execute(
            select(KbPermission)
            .join(User, User.id == KbPermission.user_id)
            .where(KbPermission.kb_id == kb_id, User.username == username)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="permission not found")
    await db.delete(row)
    await db.commit()
    return None
```

(kbs.py 导入区 `has_perm` 并入 `from app.core.perms import get_kb_perm, has_perm`。)

- [x] Step 4: GREEN:全量 **65 passed**(61+4)。
- [x] Step 5: 提交:`feat: kb member management api (grant/revoke by username)`

### Task 6: 用户管理 API(admin)

**Files:** Modify `backend/app/core/deps.py`(require_admin);Create `backend/app/schemas/admin.py`、`backend/app/api/admin.py`;Modify `backend/app/api/__init__.py`;Test `backend/tests/test_admin_users.py`

**Interfaces:** `require_admin` 依赖(非 admin 403);`GET /api/admin/users → [{id, username, role, is_active}]`;`PATCH /api/admin/users/{id} {role?, is_active?}`(改自己 400,不存在 404)。

- [x] Step 1: 失败测试(3 函数,helper 复制):

```python
async def _promote_to_admin(client, db_session, headers):
    from sqlalchemy import text

    me = await client.get("/api/auth/me", headers=headers)
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"), {"i": me.json()["id"]}
    )
    await db_session.commit()
    return me.json()["id"]


async def test_admin_lists_and_updates_users(client, auth_headers, db_session):
    my_id = await _promote_to_admin(client, db_session, auth_headers)
    target = await _register_and_login(client, "admin_target1")

    listed = await client.get("/api/admin/users", headers=auth_headers)
    assert listed.status_code == 200
    ids = [u["id"] for u in listed.json()]
    assert my_id in ids

    tme = await client.get("/api/auth/me", headers=target)
    tid = tme.json()["id"]
    patched = await client.patch(
        f"/api/admin/users/{tid}",
        json={"role": "editor", "is_active": False}, headers=auth_headers,
    )
    assert patched.status_code == 200
    assert patched.json()["role"] == "editor"
    assert patched.json()["is_active"] is False

    # 被禁用的用户登录被拒
    login = await client.post(
        "/api/auth/login", json={"username": "admin_target1", "password": "secret123"}
    )
    assert login.status_code == 401


async def test_admin_endpoints_forbid_non_admin(client, auth_headers):
    listed = await client.get("/api/admin/users", headers=auth_headers)
    assert listed.status_code == 403


async def test_admin_cannot_modify_self(client, auth_headers, db_session):
    my_id = await _promote_to_admin(client, db_session, auth_headers)
    resp = await client.patch(
        f"/api/admin/users/{my_id}", json={"role": "viewer"}, headers=auth_headers
    )
    assert resp.status_code == 400
```

- [x] Step 2: RED。
- [x] Step 3: 实现:

deps.py 追加:

```python
async def require_admin(current: User = Depends(get_current_user)) -> User:
    if current.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return current
```

schemas/admin.py:

```python
from typing import Literal

from pydantic import BaseModel


class AdminUserOut(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool

    model_config = {"from_attributes": True}


class AdminUserIn(BaseModel):
    role: Literal["viewer", "editor", "admin"] | None = None
    is_active: bool | None = None
```

api/admin.py:

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_admin
from app.db.session import get_db
from app.models import User
from app.schemas.admin import AdminUserIn, AdminUserOut

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=list[AdminUserOut])
async def list_users(
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(select(User).order_by(User.id))
    return list(rows.scalars().all())


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: int,
    payload: AdminUserIn,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if user_id == current.id:
        raise HTTPException(status_code=400, detail="cannot modify self")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    await db.commit()
    await db.refresh(user)
    return user
```

api/__init__.py 挂载 admin_router(照 conversations/ask 的既有写法)。

- [x] Step 4: GREEN:全量 **68 passed**(65+3)。
- [x] Step 5: 提交:`feat: admin user management api`

### Task 7: Rerank 请求级开关

**Files:** Modify `backend/app/services/chat_graph/state.py`(+rerank)、`nodes.py`(rerank_node 判 flag)、`graph.py`(拓扑恒含 rerank)、`backend/app/schemas/chat.py`(AskIn+rerank)、`backend/app/api/ask.py`(init 带 rerank);Test `backend/tests/test_chat_graph.py` 追加 2

**Interfaces:** `AskIn.rerank: bool = False`;state 增 `rerank: bool`;图节点集恒含 "rerank"(即使 RERANK_ENABLED=false);rerank_node 仅当 `reranker is not None and state.get("rerank") and hits` 时精排。

- [x] Step 1: 失败测试(test_chat_graph.py 追加):

```python
async def test_graph_always_has_rerank_node():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services.chat_graph.graph import build_graph

    g = build_graph(llm=FakeListChatModel(responses=["x"]))
    assert "rerank" in g.get_graph().nodes  # 测试进程 RERANK_ENABLED 未设


async def test_rerank_node_respects_state_flag(monkeypatch):
    from app.services.chat_graph import nodes as nodes_mod

    class FakeReranker:
        def rerank(self, query, documents, top_n=8):
            return list(range(len(documents)))[::-1]  # 全量倒序

    monkeypatch.setattr(nodes_mod, "get_reranker", lambda: FakeReranker())
    hits = [
        {"content": "甲", "chunk_id": 1, "document_id": 1, "kb_id": 1,
         "filename": "a.pdf", "page_no": 1, "score": 0.9, "source": "vector"},
        {"content": "乙", "chunk_id": 2, "document_id": 1, "kb_id": 1,
         "filename": "a.pdf", "page_no": 2, "score": 0.8, "source": "keyword"},
    ]
    off = await nodes_mod.rerank_node({"question": "q", "hits": hits})
    assert off == {}  # 未开开关:直通

    on = await nodes_mod.rerank_node({"question": "q", "hits": hits, "rerank": True})
    ids = [h["chunk_id"] for h in on["hits"]]
    assert ids == [2, 1]  # 倒序生效
```

- [x] Step 2: RED。
- [x] Step 3: 实现:

state.py 的 ChatState 追加一行:`rerank: bool`。

nodes.py 的 rerank_node 首行条件改为:

```python
async def rerank_node(state: dict) -> dict:
    reranker = get_reranker()
    if reranker is None or not state.get("hits") or not state.get("rerank"):
        return {}
```

(其余不动。)

graph.py 的 build_graph 边接线段(删除 if/else 分支)改为:

```python
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "rerank")
    g.add_edge("rerank", "generate")
    g.add_edge("generate", END)
```

schemas/chat.py 的 AskIn 追加:`rerank: bool = False`。

ask.py 的 init 改:

```python
    init = {
        "question": payload.question,
        "kb_ids": payload.kb_ids,
        "rerank": payload.rerank,
    }
```

- [x] Step 4: GREEN:全量 **70 passed**(68+2)。
- [x] Step 5: 提交:`feat: per-request rerank flag through graph state`

### Task 8: 前端基建(api 模块/composable 扩展)

**Files:** Modify `frontend/src/api/kb.ts`(my_perm + detail + members/grant/revoke)、`frontend/src/api/documents.ts`(reprocess)、`frontend/src/api/chat.ts`(remove)、`frontend/src/composables/useChatStream.ts`(rerank 参数 + abort);Create `frontend/src/api/admin.ts`

**Interfaces:** KbItem 增 `my_perm?: string | null`;`kbApi.detail(id)`、`kbApi.members(kbId)`、`kbApi.grant(kbId, {username, perm})`、`kbApi.revoke(kbId, username)`;`documentsApi.reprocess(docId)`;`conversationsApi.remove(id)`;`adminApi.listUsers()` / `adminApi.updateUser(id, {role?, is_active?})`;`useChatStream.ask` payload 增 `rerank?: boolean`,返回值增 `abort()`(中止当前流)。

- [x] Step 1: 实现 api 扩展(kb.ts):

```ts
export interface KbItem {
  id: number
  name: string
  description: string | null
  owner_id: number
  embed_provider: string
  embed_model: string
  created_at: string
  my_perm?: string | null
}

export interface KbMember {
  user_id: number
  username: string
  perm: 'viewer' | 'editor'
}

export const kbApi = {
  async list(): Promise<KbItem[]> {
    const { data } = await http.get<KbItem[]>('/kbs')
    return data
  },

  async create(payload: KbIn): Promise<KbItem> {
    const { data } = await http.post<KbItem>('/kbs', payload)
    return data
  },

  async detail(kbId: number): Promise<KbItem> {
    const { data } = await http.get<KbItem>(`/kbs/${kbId}`)
    return data
  },

  async members(kbId: number): Promise<KbMember[]> {
    const { data } = await http.get<KbMember[]>(`/kbs/${kbId}/permissions`)
    return data
  },

  async grant(kbId: number, payload: { username: string; perm: 'viewer' | 'editor' }): Promise<KbMember> {
    const { data } = await http.put<KbMember>(`/kbs/${kbId}/permissions`, payload)
    return data
  },

  async revoke(kbId: number, username: string): Promise<void> {
    await http.delete(`/kbs/${kbId}/permissions`, { params: { username } })
  },
}
```

documents.ts 追加:

```ts
  async reprocess(docId: number): Promise<DocumentItem> {
    const { data } = await http.post<DocumentItem>(`/documents/${docId}/reprocess`)
    return data
  },
```

chat.ts 追加:

```ts
  async remove(conversationId: number): Promise<void> {
    await http.delete(`/chat/conversations/${conversationId}`)
  },
```

admin.ts(新建):

```ts
import http from './http'

export interface AdminUser {
  id: number
  username: string
  role: 'viewer' | 'editor' | 'admin'
  is_active: boolean
}

export const adminApi = {
  async listUsers(): Promise<AdminUser[]> {
    const { data } = await http.get<AdminUser[]>('/admin/users')
    return data
  },

  async updateUser(
    id: number,
    payload: { role?: AdminUser['role']; is_active?: boolean },
  ): Promise<AdminUser> {
    const { data } = await http.patch<AdminUser>(`/admin/users/${id}`, payload)
    return data
  },
}
```

useChatStream.ts:`ask` 的 payload 类型增 `rerank?: boolean`;body 增 `rerank: payload.rerank ?? false`;模块内 `let currentCtrl: AbortController | null` 保存当前控制器,`onerror` 里 `currentCtrl = null`,`ask` 开始时 `currentCtrl = ctrl`,返回对象改 `return { ask, abort }`,`abort()` 实现 `currentCtrl?.abort(); currentCtrl = null`。

- [x] Step 2: `pnpm build && pnpm test` 全绿(3 passed)。
- [x] Step 3: 提交:`feat: frontend api modules for rbac reprocess and rerank`

### Task 9: 前端页面(权限界面/用户管理/重试按钮/Rerank 开关/历史删除/加固)

**Files:** Modify `frontend/src/pages/KbPage.vue`、`frontend/src/pages/DocsPage.vue`、`frontend/src/pages/ChatPage.vue`、`frontend/src/layouts/MainLayout.vue`、`frontend/src/router/index.ts`;Create `frontend/src/pages/UsersPage.vue`

**Interfaces:**
- KbPage:viewer(`auth.user.role === 'viewer'`)隐藏"新建知识库";表格增"我的权限"列(tag:owner=管理员/editor=可编辑/viewer=只读);行操作增"成员"按钮(仅 `row.my_perm === 'owner'` 显示)→ el-dialog 成员管理(表格 username/perm select/移除 + 底部"按用户名添加"输入框+perm 下拉+添加按钮;操作后刷新成员列表与 KB 列表)。
- UsersPage(`/admin/users`):el-table username/role(el-select 行内切换:viewer/editor/admin)/is_active(el-switch)/操作;错误 ElMessage;MainLayout 菜单 `v-if="auth.user?.role === 'admin'"` 才显示"用户管理"。
- DocsPage:onMounted 调 `kbApi.detail(kbId)` 取 `my_perm`;`my_perm` 为 owner/editor 时行操作显示"重新解析"(status 为 failed/done/pending 时可点,处理中禁用);点击调 `documentsApi.reprocess` 成功后恢复轮询刷新。
- ChatPage:工具栏增 `el-switch`("精排重排",v-model `rerankEnabled`,`localStorage.getItem('airag_rerank') === '1'` 初始化,change 时写回);`ask` payload 带 `rerank: rerankEnabled`;会话项 hover 显示删除图标(`@click.stop` 调 `conversationsApi.remove` 后刷新列表,删除当前会话则清空消息区);`render()` 包 `DOMPurify.sanitize(md.render(src))`;`onUnmounted(() => abort())`。

- [x] Step 1: 按上述接口实现五个文件(完整 SFC;成员对话框为独立 el-dialog 内嵌 el-table + 表单行;沿用现有页面代码风格)。
- [x] Step 2: `pnpm build && pnpm test` 全绿;oxlint 若报存量三处旧错可忽略(交接清单已记),**不得新增**报错。
- [x] Step 3: 提交:`feat: rbac ui with member dialog, user admin, reprocess and rerank switch`

### Task 10: M4 端到端验收(真库×脚本化权限矩阵)

- [x] Step 1: 起全栈四进程(PG/Redis 服务确认 → start_dev.bat → start_worker.bat → pnpm dev)。
- [x] Step 2: 无头验收链(curl/PowerShell 脚本,全部断言写进输出):
  1. 注册三个用户:u_admin/u_owner/u_viewer(默认 viewer);SQL 把 u_admin 升 admin(运行库直接 psql UPDATE)。
  2. u_owner 建库 → 上传 M3 验收样例(或新造一份含事实的 docx)→ 轮询 done。
  3. **权限矩阵**:u_viewer GET /kbs 不含该库;GET /kbs/{id} 404;ask kb_ids 403;上传 404。
  4. u_admin(或 u_owner)PUT permissions 授 u_viewer viewer → u_viewer 列表可见(my_perm=viewer)→ ask 200(SSE 正常)→ 上传 403。
  5. 改授 editor → 上传 201;revoke → 列表又不可见。
  6. **重新解析**:上传一个损坏 pdf(b"not a pdf")→ 轮询 failed(error_msg 非空)→ POST reprocess → 200 且状态回 pending →(worker 再失败)failed;全程 u_owner 200、u_viewer 403。
  7. **会话删除**:u_viewer ask 一次建会话 → DELETE 204 → messages 404。
  8. **用户管理**:u_admin GET /admin/users 200;PATCH u_viewer is_active=false → u_viewer 登录 401;PATCH 还原 true。u_owner GET /admin/users 403。
  9. **Rerank 开关**(可选,余额已充):临时 .env `RERANK_ENABLED=true` 重启后端,ask `rerank:true` 走通 SSE 不报错;还原 false。
- [x] Step 3: 后端 70 passed + 前端 build/test 回归;清进程。
- [x] Step 4: 浏览器走查留给用户(同 M1/M3 模式):成员对话框/用户管理页/重试按钮/Rerank 开关/会话删除。无头项全过即 M4 验收 PASS。
- [x] Step 5: 空标记提交:`chore: m4 complete - rbac acceptance verified`

### Task 11(控制器): 收尾

- 勾选同步+提交;全计划终审(重点:R1-R9 落地、403/404 语义一致、ask 权限在会话创建**之前**校验、reprocess 与 eager 测试的交互、SSE 契约未破坏);合并推送;M5 交接附录(LDAP 开放问题、checkpointer 恢复、其余 Minor);记忆更新。

---

## 计划自审记录

1. **Spec 覆盖**:§1.2"无权限不可见不可检索"→T2/T3/T4(404 隐藏+ask 403);§9-M4 四项→RBAC=T1-T6+T9、失败重试=T3+T9、问答历史=T4+T9(删除级联)、Rerank 开关=T7+T9;M3 交接"R4 必须收紧"→R1/R2 全入口清单;交接裁决 1/2/3(SSE 契约/断连/docx page_no)不在 M4 动——ask.py 改动仅加权限校验与 rerank 字段,SSE 帧格式未触碰。
2. **占位符扫描**:T9 页面任务按 M3 T7-9 先例给接口级描述+关键行为(非 TBD);其余任务代码完整。无"TODO/类似 Task N"引用(helper 三处显式复制是有意为之,避免跨文件导入 conftest 的可靠性问题)。
3. **类型一致性**:`get_kb_perm/has_perm` 签名在 T1 定义、T2/T3/T4/T5 消费一致;`MemberOut{user_id, username, perm}` ↔ 前端 `KbMember`;`AskIn.rerank` ↔ useChatStream payload ↔ state.rerank;`my_perm` 后端 KBOut ↔ 前端 KbItem;`AdminUserOut` ↔ `AdminUser`。reprocess 返回 DocumentOut ↔ `documentsApi.reprocess`。
4. **计数一致性**(按测试函数):46→T1+4=50→T2+4=54→T3+4=58→T4+3=61(含存量适配不加数)→T5+4=65→T6+3=68→T7+2=70;前端 3 不变(T8/T9 无新单测)。存量适配两处:T1 conftest、T4 test_ask 播种——均为计划内显式步骤。
5. **风险预置**:T7 拓扑改动影响 M3 存量图测试(rerank 直通不改变 hits 顺序,`test_graph_end_to_end_with_fakes` 断言不变);T3 reprocess 在 eager 模式下 monkeypatch `docs_mod.process_document` 避免真跑流水线;T5 的 422 断言依赖 pydantic 先于业务校验(Literal 在请求体解析阶段执行,天然成立)。

---

## M4 执行记录(2026-09-15,验收后固化)

### 结果
- 计数:后端 **70 passed**(46→70,与计划一致);前端 build 绿 + vitest **4 passed**(基线实为 4,计划写 3,以实际为准)。oxlint 仍为存量 3 错(auth.store.spec.ts),M4 零新增。
- 无头验收脚本 **30/30 PASS**(真栈:PG/Redis/worker/智谱嵌入+LLM):注册默认 viewer、权限矩阵(不可见 404/ask 403/上传 404)、授权后可见可问不可传、editor 授权后可传、revoke 后不可见、坏 pdf→failed→reprocess 200、会话删除级联、admin 用户管理(禁用登录 401/还原/403/自改 400)。
- **Rerank 真调通过**:RERANK_ENABLED=true + AskIn.rerank=true,SSE 序列 token*→citations→done 无 error 帧,答案带引用。**发现并修正:智谱 rerank 模型代码是 `rerank`,不是 M1 占位的 `rerank-3`**(commit 9c885a1)。

### 偏离与修正(实现者对计划的增量)
1. `test_auth.py::test_register_success` 断言 admin→**viewer**(R1 语义变更的必然结果,计划适配清单漏列)。
2. T3 reprocess 测试的 monkeypatch 需带 `.delay` 的假任务对象(计划里 lambda 无 .delay)。
3. T4 `test_ask_rejects_invisible_kb` 的"他人"需先升 editor(默认 viewer 建不了库)。
4. T5 `test_revoke_member` 的目标用户需先注册(计划的测试漏了注册步骤)。
5. T7 `test_graph_always_has_rerank_node` 断言从"节点存在"加强为"边存在"(未接线节点也出现在 get_graph().nodes)。
6. 验收脚本一次 asyncio.run 内完成全部 DB 晋升(模块级 engine 绑定首个事件循环,多次 asyncio.run 复用死循环连接)。

### M5 交接附录(2026-09-15,M5 计划生成时必须消化)
1. **LDAP/SSO 开放问题仍未收到输入**(spec §11):M4 维持本地账号;若要接 LDAP,users 表加 provider/external_id 字段,登录路由加分枝,RBAC 语义不变。
2. **checkpointer 恢复仍是 M5 可选增强**(M3 裁决 1):get_checkpointer 单例已就绪,thread_id=conversation_id 已随 config 传递,接线成本可控;断连语义维持"只保留已落库 user 消息"。
3. **智谱 rerank 模型代码 = `rerank`**(本次实测),top_n 语义为"返回前 n 条",results 按 index 引用原始列表——与 ZhipuRerank.rerank 现有实现吻合。
4. **延后 Minor 清单续存**:轮询 loading 闪烁、kb 高亮子路由、markdown 每帧全量重渲(流式可增量渲染)、Alt/Meta+Enter、oxlint 三处旧错、poll-after-unmount 窄窗、单次 OpenAI 客户端复用、rerank 负下标防御、文档数占位列(KbPage 仍为"—",可由 list_kbs 聚合 documents.count 补齐)。
5. **验收环境事实**:运行库 airag 现有 M4 验收数据(m4a_owner/m4a_viewer/m4a_admin 三用户、KB"M4验收库"docx done + m4bad.pdf failed、会话 8 已删);m4a_admin 为 admin 角色,可直接登浏览器走查用户管理页。存量旧用户(如 M1-M3 验收用户)role 均为 'admin'(旧默认),生产首部署需注意。
6. **浏览器走查留给用户**:成员对话框/用户管理页/重试按钮/Rerank 开关/会话删除(登录 m4a_admin/secret123 或自建账号)。
