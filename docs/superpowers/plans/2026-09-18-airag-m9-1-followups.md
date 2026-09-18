# AIRag M9.1 收尾批次 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐 M9 移交的三件事:①MCP 真客户端走查(M9 唯一未做的验收步)②admin 建密钥可指定绑定账号 ③成员授权用户改远程搜索下拉。

**Architecture:** ②③共用新的账号搜索端点 `GET /api/users`(登录即可调,只返 id+username);admin 发 key 走新 `POST /api/admin/keys`(配额/归属按目标用户,审计 detail 记 by/to);个人发 key 逻辑抽到 `services/api_keys.issue_api_key` 供两面复用。前端只动 KeysPage(admin 绑定账号下拉)与 KbPage(授权改 el-select 远程搜索+滚动分页),grant 接口不变。

**Tech Stack:** 现有栈零新增依赖;走查用 `@modelcontextprotocol/inspector` --cli(npx,dev-time)与 fastmcp 自带 `Client`(backend venv 已装)做双真客户端验证。

**Spec:** 本计划自带拍板纪要(2026-09-18 用户已拍板,记录于项目记忆 airag-m9-agent-api-shipped);无独立 spec 文档。

## Global Constraints

- Windows:后端测试一律 `backend` 目录下 `.venv\Scripts\python -m pytest ...`;前端在 `frontend` 目录 `npx vitest run <file>`;服务端口 **8001**(start_dev.bat)。
- 直接在 main 分支实施(项目 M1~M9 惯例),每任务完成即 commit(conventional commits:`feat(users):`/`feat(admin):`/`feat(frontend):`/`test:`/`docs:` 前缀)。
- 错误码约定:403 `admin role required`(现有 require_admin);409 `"api key limit reached"`;404 `"user not found"`;400 `"target user is disabled"`;均与 M9 401/403/429 风格一致。
- 审计:admin 发 key 落 `key_create`,detail `{"by": <admin>, "to": <目标>, "name": ...}`;个人发 key 保持 detail `{"name": ...}` 不变。
- 回归红线:`test_auth_keys_api.py`、`test_admin_users.py`、`scripts/m9_acceptance.py`(16/16)既有断言不得破坏。
- grant 接口不变:仍按 username(`PUT /kbs/{id}/permissions`),前端只换输入控件。
- `GET /api/users`、`POST /api/admin/keys` 与 Agent 开关 `AGENT_API_ENABLED` 无关(它们服务 Web 管理面),无条件挂载。

---

### Task 1: 账号搜索端点 GET /api/users + /api/admin/users 加 q/limit/offset

**Files:**
- Create: `backend/app/api/users.py`
- Create: `backend/app/schemas/users.py`
- Modify: `backend/app/api/__init__.py`(挂 users_router)
- Modify: `backend/app/api/admin.py:15-21`(list_users 加参数)
- Test: `backend/tests/test_users_api.py`

**Interfaces:**
- Consumes: 现有 `get_current_user`/`require_admin`、`User` 模型
- Produces(Task 3/4 前端依赖,签名固定):
  - `GET /api/users?q=&limit=&offset=` —— 登录即可调;只返 `UserBriefOut`(`id`,`username` 两字段);过滤 `is_active=False`;`q` 为 username ILIKE 子串;按 username 升序;`limit` 默认 20(1~50),`offset` 默认 0
  - `GET /api/admin/users` 增加可选 `q`/`limit`(1~100,默认不限制)/`offset`,不带参数时行为与现状完全一致(全量、按 id 升序)
  - schemas:`UserBriefOut(id: int, username: str)`(from_attributes)

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_users_api.py
"""M9.1:GET /api/users 账号搜索(登录即可调,仅 id+username)+ /api/admin/users q/limit/offset。"""
from sqlalchemy import text


async def _mk_user(client, username):
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )


async def _promote_self(client, auth_headers, db_session):
    me = await client.get("/api/auth/me", headers=auth_headers)
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"), {"i": me.json()["id"]}
    )
    await db_session.commit()
    return me.json()


async def test_users_requires_auth(client):
    resp = await client.get("/api/users")
    assert resp.status_code == 401


async def test_users_allowed_for_plain_viewer(client):
    await _mk_user(client, "m91_viewer1")
    login = await client.post(
        "/api/auth/login", json={"username": "m91_viewer1", "password": "secret123"}
    )
    resp = await client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert resp.status_code == 200


async def test_users_brief_only(client, auth_headers):
    await _mk_user(client, "m91_alice")
    resp = await client.get("/api/users", headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()
    assert items and set(items[0]) == {"id", "username"}
    assert "m91_alice" in [i["username"] for i in items]


async def test_users_q_filter(client, auth_headers):
    for name in ("m91_bob", "m91_bobby", "m91_carol"):
        await _mk_user(client, name)
    resp = await client.get("/api/users", params={"q": "m91_bob"},
                            headers=auth_headers)
    names = [i["username"] for i in resp.json()]
    assert names == ["m91_bob", "m91_bobby"]


async def test_users_excludes_disabled(client, auth_headers, db_session):
    await _mk_user(client, "m91_off")
    await db_session.execute(
        text("UPDATE users SET is_active = false WHERE username = 'm91_off'")
    )
    await db_session.commit()
    resp = await client.get("/api/users", params={"q": "m91_off"},
                            headers=auth_headers)
    assert resp.json() == []


async def test_users_pagination(client, auth_headers):
    for i in range(3):
        await _mk_user(client, f"m91_pg{i:02d}")
    page1 = await client.get("/api/users", params={"limit": 2, "offset": 0},
                             headers=auth_headers)
    page2 = await client.get("/api/users", params={"limit": 2, "offset": 2},
                             headers=auth_headers)
    assert len(page1.json()) == 2
    n1 = {i["username"] for i in page1.json()}
    n2 = {i["username"] for i in page2.json()}
    assert n1 & n2 == set()


async def test_admin_users_q_limit_offset(client, auth_headers, db_session):
    await _promote_self(client, auth_headers, db_session)
    for i in range(3):
        await _mk_user(client, f"m91_aq{i}")
    resp = await client.get("/api/admin/users", params={"q": "m91_aq"},
                            headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 3
    resp = await client.get(
        "/api/admin/users", params={"q": "m91_aq", "limit": 2, "offset": 2},
        headers=auth_headers,
    )
    assert len(resp.json()) == 1


async def test_admin_users_no_params_returns_all(client, auth_headers, db_session):
    await _promote_self(client, auth_headers, db_session)
    resp = await client.get("/api/admin/users", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) >= 1  # 至少含自己;role/is_active 字段仍在
```

- [ ] **Step 2: 跑测试确认失败**

Run(backend 目录):`.venv\Scripts\python -m pytest tests/test_users_api.py -v`
Expected: FAIL(`/api/users` 404;admin q 用例拿到全量)

- [ ] **Step 3: 实现 schema + 路由**

```python
# backend/app/schemas/users.py
from pydantic import BaseModel


class UserBriefOut(BaseModel):
    """M9.1:账号搜索条目(刻意只含 id+username,不泄角色/状态给普通成员)。"""

    id: int
    username: str

    model_config = {"from_attributes": True}
```

```python
# backend/app/api/users.py
"""M9.1:账号搜索(成员授权下拉、admin 密钥绑定下拉共用)。登录即可调。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models import User
from app.schemas.users import UserBriefOut

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserBriefOut])
async def search_users(
    q: str = Query("", max_length=64),
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    where = [User.is_active == True]  # noqa: E712
    kw = q.strip()
    if kw:
        where.append(User.username.ilike(f"%{kw}%"))
    rows = (
        await db.execute(
            select(User)
            .where(*where)
            .order_by(User.username)
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return rows
```

`api/__init__.py` 追加(imports 区 + include 区,与现有同款):

```python
from app.api.users import router as users_router
# ...
api_router.include_router(users_router)
```

`admin.py` 的 `list_users` 全文替换为:

```python
@router.get("/users", response_model=list[AdminUserOut])
async def list_users(
    q: str | None = None,
    limit: int | None = Query(None, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User).order_by(User.id)
    if q and q.strip():
        stmt = stmt.where(User.username.ilike(f"%{q.strip()}%"))
    stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = await db.execute(stmt)
    return list(rows.scalars().all())
```

(`admin.py` 顶部 import 补 `Query`。)

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run:`.venv\Scripts\python -m pytest tests/test_users_api.py -v && .venv\Scripts\python -m pytest -q`
Expected: 新增 8 PASS;全量 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/users.py backend/app/schemas/users.py backend/app/api/__init__.py backend/app/api/admin.py backend/tests/test_users_api.py
git commit -m "feat(users): account search endpoint and admin user list filters"
```

---

### Task 2: issue_api_key 抽取 + POST /api/admin/keys(admin 指定绑定账号)

**Files:**
- Modify: `backend/app/services/api_keys.py`(追加 `issue_api_key`)
- Modify: `backend/app/api/auth.py:71-114`(create_api_key 改用 helper)
- Modify: `backend/app/api/admin.py`(追加 POST /keys)
- Modify: `backend/app/schemas/admin.py`(追加 AdminKeyCreateIn)
- Test: `backend/tests/test_admin_keys.py`

**Interfaces:**
- Consumes: Task 1 无依赖;现有 `generate_api_key/ApiKey`、`audit`、`require_admin`、`ApiKeyCreatedOut`
- Produces(Task 3 前端依赖):
  - `services/api_keys.issue_api_key(db, user: User, name: str, expires_in_days: int | None) -> tuple[ApiKey, str]` —— 配额检查(超限抛 `KeyQuotaExceeded`)+ 生成落库(flush 不 commit);返回 (行, 明文)
  - `POST /api/admin/keys` body `{"user_id": int, "name": 1..64, "expires_in_days": null(1..3650)}` → 201 `ApiKeyCreatedOut`(含一次性明文);404 user not found / 400 target user is disabled / 409 api key limit reached;配额按**目标用户**活跃 key 计

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_admin_keys.py
"""M9.1:POST /api/admin/keys —— admin 为指定账号发 key;配额按目标用户;审计 by/to。"""
import json

from sqlalchemy import select, text

from app.core.config import settings
from app.models import ApiKey, AuditLog


async def _promote_self(client, auth_headers, db_session):
    me = await client.get("/api/auth/me", headers=auth_headers)
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": me.json()["id"]},
    )
    await db_session.commit()
    return me.json()["username"]


async def _mk_user(client, username):
    r = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    return r.json()["id"]


async def test_admin_creates_key_for_other(client, auth_headers, db_session):
    admin_name = await _promote_self(client, auth_headers, db_session)
    tid = await _mk_user(client, "m91_target1")
    resp = await client.post(
        "/api/admin/keys",
        json={"user_id": tid, "name": "给同事的key", "expires_in_days": 30},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["key"].startswith("airag_") and body["name"] == "给同事的key"
    row = await db_session.get(ApiKey, body["id"])
    assert row.user_id == tid
    assert row.key_hash != body["key"]  # 库里只有哈希
    assert row.expires_at is not None


async def test_admin_key_audit_by_to(client, auth_headers, db_session):
    admin_name = await _promote_self(client, auth_headers, db_session)
    tid = await _mk_user(client, "m91_target2")
    await client.post(
        "/api/admin/keys",
        json={"user_id": tid, "name": "审计key"},
        headers=auth_headers,
    )
    db_session.expire_all()
    rows = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "key_create")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].username == admin_name  # actor 列即 by
    detail = json.loads(rows[0].detail)
    assert detail["by"] == admin_name and detail["to"] == "m91_target2"
    assert detail["name"] == "审计key"


async def test_quota_counts_target_user(client, auth_headers, db_session, monkeypatch):
    await _promote_self(client, auth_headers, db_session)
    tid = await _mk_user(client, "m91_quota")
    monkeypatch.setattr(settings, "AGENT_MAX_KEYS_PER_USER", 1)
    r1 = await client.post("/api/admin/keys",
                           json={"user_id": tid, "name": "1"}, headers=auth_headers)
    r2 = await client.post("/api/admin/keys",
                           json={"user_id": tid, "name": "2"}, headers=auth_headers)
    assert r1.status_code == 201
    assert r2.status_code == 409
    assert r2.json()["detail"] == "api key limit reached"


async def test_non_admin_403(client, auth_headers):
    tid = await _mk_user(client, "m91_v")
    resp = await client.post(
        "/api/admin/keys", json={"user_id": tid, "name": "x"}, headers=auth_headers
    )
    assert resp.status_code == 403


async def test_missing_and_disabled_target(client, auth_headers, db_session):
    await _promote_self(client, auth_headers, db_session)
    resp = await client.post(
        "/api/admin/keys", json={"user_id": 999999, "name": "x"},
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "user not found"

    tid = await _mk_user(client, "m91_off")
    await db_session.execute(
        text("UPDATE users SET is_active = false WHERE id = :i"), {"i": tid}
    )
    await db_session.commit()
    resp = await client.post(
        "/api/admin/keys", json={"user_id": tid, "name": "x"}, headers=auth_headers
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "target user is disabled"


async def test_user_id_required_422(client, auth_headers, db_session):
    await _promote_self(client, auth_headers, db_session)
    resp = await client.post(
        "/api/admin/keys", json={"name": "x"}, headers=auth_headers
    )
    assert resp.status_code == 422
```

- [ ] **Step 2: 跑测试确认失败**

Run:`.venv\Scripts\python -m pytest tests/test_admin_keys.py -v`
Expected: FAIL(404 Not Found,`/api/admin/keys` 不存在)

- [ ] **Step 3: 实现 helper + 端点**

`services/api_keys.py` 追加(顶部 imports 补 `from datetime import datetime, timedelta, timezone`、`from sqlalchemy import func, select`、`from app.core.config import settings`、`from app.models import User`;若引发循环导入则改为函数内 `from app.core.config import settings`):

```python
class KeyQuotaExceeded(Exception):
    """目标用户活跃 key 数已达 AGENT_MAX_KEYS_PER_USER(调用方转 409)。"""


async def issue_api_key(
    db: AsyncSession, user: User, name: str, expires_in_days: int | None
) -> tuple[ApiKey, str]:
    """配额检查 + 生成落库(仅 flush,不 commit);返回 (ApiKey 行, 明文)。

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
    raw, prefix, digest = generate_api_key()
    key = ApiKey(
        user_id=user.id,
        name=name,
        key_prefix=prefix,
        key_hash=digest,
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

`api/auth.py` 的 `create_api_key` 中段替换(配额检查+生成段改用 helper;顶部 import 改 `from app.services.api_keys import generate_api_key` → `from app.services.api_keys import KeyQuotaExceeded, issue_api_key`,并删去不再用的 `datetime/timedelta/timezone/func` 若无其他引用):

```python
    try:
        key, raw = await issue_api_key(db, current, payload.name,
                                       payload.expires_in_days)
    except KeyQuotaExceeded:
        raise HTTPException(status_code=409, detail="api key limit reached")
    await audit(db, current.username, "key_create", f"apikey:{key.id}",
                {"name": payload.name})
    await db.commit()
    await db.refresh(key)
```

(返回 `ApiKeyCreatedOut` 手工构造段保持不变。)

`schemas/admin.py` 追加:

```python
from pydantic import Field


class AdminKeyCreateIn(BaseModel):
    user_id: int
    name: str = Field(min_length=1, max_length=64)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
```

`api/admin.py` 追加端点(imports 补 `from datetime import datetime` 不需要;补 `from app.schemas.admin import AdminKeyCreateIn`、`from app.schemas.auth import ApiKeyCreatedOut`、`from app.services.api_keys import KeyQuotaExceeded, issue_api_key`):

```python
# ---- M9.1:admin 为指定账号发 API 密钥(配额按目标用户;审计记 by/to) ----
@router.post("/keys", response_model=ApiKeyCreatedOut, status_code=201)
async def admin_create_api_key(
    payload: AdminKeyCreateIn,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    target = await db.get(User, payload.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if not target.is_active:
        raise HTTPException(status_code=400, detail="target user is disabled")
    try:
        key, raw = await issue_api_key(db, target, payload.name,
                                       payload.expires_in_days)
    except KeyQuotaExceeded:
        raise HTTPException(status_code=409, detail="api key limit reached")
    await audit(
        db, current.username, "key_create", f"apikey:{key.id}",
        {"by": current.username, "to": target.username, "name": payload.name},
    )
    await db.commit()
    await db.refresh(key)
    return ApiKeyCreatedOut(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        is_active=key.is_active,
        expires_at=key.expires_at,
        last_used_at=key.last_used_at,
        created_at=key.created_at,
        key=raw,
    )
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run:`.venv\Scripts\python -m pytest tests/test_admin_keys.py tests/test_auth_keys_api.py -v && .venv\Scripts\python -m pytest -q`
Expected: 新增 6 PASS;`test_auth_keys_api.py` 5 PASS 不变(重构回归);全量 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/api_keys.py backend/app/api/auth.py backend/app/api/admin.py backend/app/schemas/admin.py backend/tests/test_admin_keys.py
git commit -m "feat(admin): admin-issued api keys bound to arbitrary users"
```

---

### Task 3: 前端——usersApi + 密钥页 admin 绑定账号下拉

**Files:**
- Create: `frontend/src/api/users.ts`
- Modify: `frontend/src/api/keys.ts`(追加 createAdmin)
- Modify: `frontend/src/pages/KeysPage.vue`(admin 条件「绑定账号」远程下拉)
- Test: `frontend/src/pages/__tests__/KeysPage.spec.ts`(改造 + 新用例)

**Interfaces:**
- Consumes: Task 1 `GET /api/users`;Task 2 `POST /api/admin/keys`;现有 `useAuthStore`
- Produces:
  - `usersApi.search(q: string, offset = 0, limit = 20): Promise<UserBrief[]>`,`UserBrief { id: number; username: string }`
  - `keysApi.createAdmin(payload: { user_id: number; name: string; expires_in_days?: number | null }): Promise<ApiKeyCreated>`
  - KeysPage:admin 打开创建对话框多一个「绑定账号」el-select(远程搜索,空=默认绑定自己);非 admin 界面与现状完全一致

- [ ] **Step 1: 写失败测试(改造 KeysPage.spec.ts)**

文件头新增 mock(放在现有 `vi.mock('@/api/keys', ...)` 旁):

```typescript
import { ElSelect } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { usersApi } from '@/api/users'

vi.mock('@/stores/auth', () => ({
  useAuthStore: vi.fn(() => ({ user: { id: 1, username: 'ed', role: 'editor' } })),
}))
vi.mock('@/api/users', () => ({
  usersApi: { search: vi.fn() },
}))
```

`vi.mock('@/api/keys', ...)` 工厂追加 `createAdmin: vi.fn()`;`beforeEach` 追加:

```typescript
vi.mocked(usersApi.search).mockReset()
vi.mocked(keysApi.createAdmin).mockReset()
```

在 describe 尾部追加用例:

```typescript
it('admin flow: bind key to searched account via createAdmin', async () => {
  vi.mocked(useAuthStore).mockReturnValueOnce({
    user: { id: 1, username: 'boss', role: 'admin' },
  } as never)
  vi.mocked(usersApi.search).mockResolvedValue([{ id: 7, username: 'alice' }])
  vi.mocked(keysApi.createAdmin).mockResolvedValue({
    ...items[0]!, id: 11, key: 'airag_BoundOneTime9',
  })
  const w = mountPage()
  await flushPromises()
  await findBtn(w, '创建密钥').trigger('click')
  await flushPromises()
  expect(w.text()).toContain('绑定账号')
  // 直接调组件的 remote-method(绕开 jsdom 输入事件)
  const sel = w.findComponent(ElSelect)
  await (sel.props('remoteMethod') as (q: string) => void)('ali')
  await flushPromises()
  expect(usersApi.search).toHaveBeenCalledWith('ali', 0, 20)
  await sel.vm.$emit('update:modelValue', 7)
  await flushPromises()
  await w.find('input[placeholder="请输入密钥名称"]').setValue('代管key')
  await findBtn(w, '创建').trigger('click')
  await flushPromises()
  expect(keysApi.createAdmin).toHaveBeenCalledWith({
    user_id: 7, name: '代管key', expires_in_days: null,
  })
  expect(keysApi.create).not.toHaveBeenCalled()
  await vi.waitFor(() => {
    expect(w.text()).toContain('airag_BoundOneTime9')
    expect(w.text()).toContain('alice')  // 已绑定账号提示
  })
})

it('non-admin create dialog has no account binding', async () => {
  const w = mountPage()  // 默认 mock 为 editor
  await flushPromises()
  await findBtn(w, '创建密钥').trigger('click')
  await flushPromises()
  expect(w.text()).not.toContain('绑定账号')
  expect(w.findComponent(ElSelect).exists()).toBe(false)
})
```

- [ ] **Step 2: 跑测试确认失败**

Run(frontend 目录):`npx vitest run src/pages/__tests__/KeysPage.spec.ts`
Expected: 新用例 FAIL(无 `@/api/users`、页面无绑定账号)

- [ ] **Step 3: 实现 api 模块 + 页面**

```typescript
// frontend/src/api/users.ts
import http from './http'

/** M9.1:账号搜索条目(仅 id+username;后端过滤停用) */
export interface UserBrief {
  id: number
  username: string
}

export const usersApi = {
  async search(q: string, offset = 0, limit = 20): Promise<UserBrief[]> {
    const { data } = await http.get<UserBrief[]>('/users', {
      params: { q, offset, limit },
    })
    return data
  },
}
```

`api/keys.ts` 在 `create` 后追加:

```typescript
export interface AdminKeyCreatePayload {
  user_id: number
  name: string
  expires_in_days?: number | null
}

// adminApi 段(keysApi 对象内)
  /** M9.1:admin 为指定账号创建密钥(权限继承目标账号) */
  async createAdmin(payload: AdminKeyCreatePayload): Promise<ApiKeyCreated> {
    const { data } = await http.post<ApiKeyCreated>('/admin/keys', payload)
    return data
  },
```

`KeysPage.vue` script 改动(顶部 import 补 `computed`、`useAuthStore`、`usersApi`/`UserBrief`):

```typescript
const auth = useAuthStore()
const isAdmin = computed(() => auth.user?.role === 'admin')

// M9.1:admin 创建时可指定绑定账号(空 = 默认绑定自己)
const form = reactive({
  name: '',
  expires: 'permanent' as 'permanent' | '7' | '30' | '90',
  userId: null as number | null,
})
const userOptions = ref<UserBrief[]>([])
const userSearching = ref(false)
const boundUsername = ref<string | null>(null)

async function searchBindUsers(q: string) {
  userSearching.value = true
  try {
    const found = await usersApi.search(q.trim(), 0, 20)
    // 保留已选项,避免远程搜索刷新后 el-select label 丢成裸 id
    const sel = userOptions.value.find((u) => u.id === form.userId)
    userOptions.value =
      sel && !found.some((u) => u.id === sel.id) ? [sel, ...found] : found
  } catch {
    ElMessage.error('账号搜索失败')
  } finally {
    userSearching.value = false
  }
}
```

`submit` 的创建段替换为(if 分支内 TS 才能收窄 `form.userId` 为 number):

```typescript
    const payload = {
      name: form.name,
      expires_in_days:
        form.expires === 'permanent' ? null : Number(form.expires),
    }
    if (isAdmin.value && form.userId != null) {
      created.value = await keysApi.createAdmin({ ...payload, user_id: form.userId })
      boundUsername.value =
        userOptions.value.find((u) => u.id === form.userId)?.username ?? null
    } else {
      created.value = await keysApi.create(payload)
      boundUsername.value = null
    }
    dialogVisible.value = false
    form.name = ''
    form.expires = 'permanent'
    form.userId = null
```

新增打开函数(重置绑定态,防上次取消后残留),并把页头按钮改为 `@click="openCreate"`:

```typescript
function openCreate() {
  form.name = ''
  form.expires = 'permanent'
  form.userId = null
  userOptions.value = []
  boundUsername.value = null
  formRef.value?.resetFields()
  dialogVisible.value = true
}
```

对话框模板(名称 form-item 之后、有效期之前插入):

```html
        <el-form-item v-if="isAdmin" label="绑定账号">
          <el-select
            v-model="form.userId"
            filterable
            remote
            clearable
            :remote-method="searchBindUsers"
            :loading="userSearching"
            placeholder="默认绑定当前账号"
            no-data-text="输入用户名搜索"
          >
            <el-option v-for="u in userOptions" :key="u.id" :label="u.username" :value="u.id" />
          </el-select>
        </el-form-item>
```

一次性明文对话框 `key-box` 下追加提示:

```html
      <p v-if="boundUsername" class="bound-note">
        该密钥已绑定账号:{{ boundUsername }}(权限与该账号一致,不显示在你的密钥列表)
      </p>
```

(样式:`​.bound-note { margin-top: 8px; font-size: 13px; color: var(--el-text-color-secondary); }`,写入时去掉零宽转义。)

- [ ] **Step 4: 跑测试确认通过 + 类型检查**

Run:`npx vitest run src/pages/__tests__/KeysPage.spec.ts && npm run type-check`
Expected: 5 PASS(存量 3 + 新 2);type-check 无错误

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/users.ts frontend/src/api/keys.ts frontend/src/pages/KeysPage.vue frontend/src/pages/__tests__/KeysPage.spec.ts
git commit -m "feat(frontend): bind admin-created api key to searchable account"
```

---

### Task 4: 前端——成员授权用户改远程搜索下拉(滚动分页)

**Files:**
- Modify: `frontend/src/pages/KbPage.vue`(成员对话框 grant-row)
- Test: `frontend/src/pages/__tests__/KbPage.spec.ts`(新增 describe)

**Interfaces:**
- Consumes: Task 3 `usersApi.search`;现有 `kbApi.grant(id, {username, perm})`(不变)
- Produces: 成员对话框的授权输入从纯文本框变为 el-select(远程搜索 + 下拉滚动加载更多);提交仍按 username

- [ ] **Step 1: 写失败测试(改造 KbPage.spec.ts)**

文件头追加:

```typescript
import { ElSelect } from 'element-plus'
import { usersApi } from '@/api/users'
import type { KbMember } from '@/api/kb'

vi.mock('@/api/users', () => ({
  usersApi: { search: vi.fn() },
}))
```

`vi.mock('@/api/kb', ...)` 工厂追加 `members: vi.fn()`、`grant: vi.fn()`。文件尾追加:

```typescript
describe('KbPage member grant remote dropdown', () => {
  const owned: KbItem = {
    id: 5, name: '成员库', description: null, doc_count: 0,
    my_perm: 'owner', created_at: '2026-09-18T10:00:00',
  }

  it('searches users remotely and grants by username', async () => {
    vi.mocked(kbApi.list).mockResolvedValue([owned])
    vi.mocked(kbApi.members).mockResolvedValue([])
    vi.mocked(kbApi.grant).mockResolvedValue({
      user_id: 2, username: 'alice', perm: 'viewer',
    } as KbMember)
    vi.mocked(usersApi.search).mockResolvedValue([{ id: 2, username: 'alice' }])
    const w = mountPage()
    await flushPromises()
    await w.findAll('button').find((b) => b.text().trim() === '成员')!.trigger('click')
    await flushPromises()
    const userSel = w
      .findAllComponents(ElSelect)
      .find((s) => s.props('placeholder') === '输入用户名搜索')!
    await (userSel.props('remoteMethod') as (q: string) => void)('ali')
    await flushPromises()
    expect(usersApi.search).toHaveBeenCalledWith('ali', 0, 20)
    await userSel.vm.$emit('update:modelValue', 'alice')
    await flushPromises()
    await w.findAll('button').find((b) => b.text().trim() === '添加')!.trigger('click')
    await flushPromises()
    expect(kbApi.grant).toHaveBeenCalledWith(5, { username: 'alice', perm: 'viewer' })
  })
})
```

注:`KbItem`/`KbMember` 字段以 `frontend/src/api/kb.ts` 实际定义为准,缺字段照实补齐。

- [ ] **Step 2: 跑测试确认失败**

Run:`npx vitest run src/pages/__tests__/KbPage.spec.ts`
Expected: 新 describe FAIL(找不到 placeholder「输入用户名搜索」的 select)

- [ ] **Step 3: 实现 KbPage 授权下拉**

script 顶部 import 补 `nextTick`、`usersApi`/`UserBrief`。成员管理段追加:

```typescript
// ---- M9.1:授权对象改为远程搜索下拉(滚动分页) ----
const USER_PAGE_SIZE = 20
const grantUsers = ref<UserBrief[]>([])
const grantSearching = ref(false)
const grantQuery = ref('')
const grantOffset = ref(0)
const grantHasMore = ref(false)

async function fetchGrantUsers(append: boolean) {
  grantSearching.value = true
  try {
    const found = await usersApi.search(
      grantQuery.value,
      append ? grantOffset.value : 0,
      USER_PAGE_SIZE,
    )
    grantOffset.value = append ? grantOffset.value + USER_PAGE_SIZE : USER_PAGE_SIZE
    grantHasMore.value = found.length === USER_PAGE_SIZE
    if (append) {
      const seen = new Set(grantUsers.value.map((u) => u.id))
      grantUsers.value = [...grantUsers.value, ...found.filter((u) => !seen.has(u.id))]
    } else {
      grantUsers.value = found
    }
  } catch {
    ElMessage.error('用户搜索失败')
  } finally {
    grantSearching.value = false
  }
}

function searchGrantUsers(q: string) {
  grantQuery.value = q.trim()
  grantOffset.value = 0
  fetchGrantUsers(false)
}

function onGrantScroll(e: Event) {
  const el = e.target as HTMLElement
  if (
    grantHasMore.value &&
    !grantSearching.value &&
    el.scrollTop + el.clientHeight >= el.scrollHeight - 40
  ) {
    fetchGrantUsers(true)
  }
}

function onGrantDropdown(open: boolean) {
  const wrap = () =>
    document.querySelector('.grant-user-dd .el-scrollbar__wrap') as HTMLElement | null
  if (open) {
    grantQuery.value = ''
    grantOffset.value = 0
    fetchGrantUsers(false)
    nextTick(() => {
      const el = wrap()
      el?.removeEventListener('scroll', onGrantScroll)
      el?.addEventListener('scroll', onGrantScroll)
    })
  } else {
    wrap()?.removeEventListener('scroll', onGrantScroll)
  }
}
```

`openMembers` 内追加重置:`grantUsers.value = []`、`grantOffset.value = 0`、`grantHasMore.value = false`。

模板 grant-row 中 `el-input`(placeholder 用户名)整块替换为:

```html
        <el-select
          v-model="grantForm.username"
          class="grant-input"
          filterable
          remote
          clearable
          :remote-method="searchGrantUsers"
          :loading="grantSearching"
          placeholder="输入用户名搜索"
          no-data-text="未找到用户"
          popper-class="grant-user-dd"
          @visible-change="onGrantDropdown"
        >
          <el-option v-for="u in grantUsers" :key="u.id" :label="u.username" :value="u.username" />
        </el-select>
```

- [ ] **Step 4: 跑测试确认通过 + 类型检查 + 全量前端**

Run:`npx vitest run src/pages/__tests__/KbPage.spec.ts && npm run type-check && npx vitest run`
Expected: 新 describe PASS;存量不破;type-check 无错误;全量 PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/KbPage.vue frontend/src/pages/__tests__/KbPage.spec.ts
git commit -m "feat(frontend): member grant picks user via remote search dropdown"
```

---

### Task 5: MCP 真客户端走查 + 全量回归收口

**Files:**
- Create: `backend/scripts/m91_mcp_walkthrough.py`
- Modify: `README.md`(M9 章节补 admin 代发密钥一句)
- Test: 无新单测;走查脚本 + 三项全量回归为验收

**Interfaces:**
- Consumes: Task 1/2/3/4 全部;真栈(start_dev.bat 的 8001 服务;走查账号 admin/secret123)
- Produces:走查 PASS 证据(两工具真调 + 无权库 ToolError)、全量回归绿灯、README/记忆更新

**背景:** 拍板原文是「claude mcp add 接 /mcp 真调两工具」,但本机无 claude CLI(`where claude` 未命中)。「真客户端」的实质要求是第三方完整 MCP 协议客户端(握手/会话/tools/list/tools/call),故本任务用 **MCP Inspector CLI(官方参考客户端,TypeScript SDK,npx 免装)** 为主、**fastmcp.Client(python 真客户端,venv 已装)** 为兜底与留档;用户可再用自己惯用的客户端(ZCode/Claude/Cursor)复走,interactive 走查仍是用户验收门。

- [ ] **Step 1: 写走查脚本(兜底客户端,可重复执行留档)**

```python
# backend/scripts/m91_mcp_walkthrough.py
"""M9.1:MCP 真客户端走查(fastmcp.Client,真 streamable-http 客户端)。

用法(start_dev.bat 起服务后,backend 目录):
    .venv\\Scripts\\python scripts\\m91_mcp_walkthrough.py <airag_key> [query]

覆盖:initialize 握手、tools/list、list_knowledge_bases、search_knowledge_base
真调、无权库 ToolError 文案。退出码 1 = 走查失败。
"""
import asyncio
import json
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

BASE = "http://127.0.0.1:8001/mcp"
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if not cond else ""))


def _text(result) -> str:
    try:
        return result.content[0].text if result.content else ""
    except Exception:
        return str(result)


async def main():
    if len(sys.argv) < 2:
        print("usage: m91_mcp_walkthrough.py <airag_key> [query]")
        raise SystemExit(2)
    key, query = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "知识库")
    transport = StreamableHttpTransport(
        url=BASE, headers={"Authorization": f"Bearer {key}"}
    )
    async with Client(transport) as c:
        tools = await c.list_tools()
        names = [t.name for t in tools.tools]
        check("tools/list 两工具", "list_knowledge_bases" in names
              and "search_knowledge_base" in names, str(names))

        r1 = await c.call_tool("list_knowledge_bases", {})
        kbs = json.loads(_text(r1))["items"]
        check("list_knowledge_bases 非空", len(kbs) > 0, _text(r1)[:200])
        kb_id = kbs[0]["id"]

        r2 = await c.call_tool(
            "search_knowledge_base", {"kb_ids": [kb_id], "query": query, "top_k": 5}
        )
        body = json.loads(_text(r2))
        check("search_knowledge_base 返回结构",
              {"hits", "total", "elapsed_ms"} <= set(body), _text(r2)[:200])

        try:
            r3 = await c.call_tool(
                "search_knowledge_base", {"kb_ids": [999999], "query": "x"}
            )
            check("无权库 ToolError 文案", "kb_forbidden" in _text(r3), _text(r3)[:200])
        except Exception as e:  # fastmcp 客户端把 tool 错误抛成异常
            check("无权库 ToolError 文案", "kb_forbidden" in str(e), str(e)[:200])
    print(f"\nM9.1 MCP WALKTHROUGH: {sum(RESULTS)}/{len(RESULTS)} PASS")
    raise SystemExit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: 起真栈 + 准备密钥**

Run:`start_dev.bat`(或确认 8001 已在跑,`curl -s http://127.0.0.1:8001/api/health`)。
用 admin 走查账号登录拿 JWT,再用 **Task 2 的新端点**为自己发一把 key(dogfood):

```bash
curl -s -X POST http://127.0.0.1:8001/api/auth/login -H "Content-Type: application/json" -d "{\"username\":\"admin\",\"password\":\"secret123\"}"
curl -s -X POST http://127.0.0.1:8001/api/admin/keys -H "Authorization: Bearer <ADMIN_JWT>" -H "Content-Type: application/json" -d "{\"user_id\":<ADMIN_UID>,\"name\":\"m91-mcp-walkthrough\",\"expires_in_days\":1}"
```

- [ ] **Step 3: 主走查——MCP Inspector CLI(官方参考客户端)**

```bash
npx -y @modelcontextprotocol/inspector --cli http://127.0.0.1:8001/mcp --transport http --header "Authorization: Bearer <airag_key>" --method tools/list
npx -y @modelcontextprotocol/inspector --cli http://127.0.0.1:8001/mcp --transport http --header "Authorization: Bearer <airag_key>" --method tools/call --tool-name list_knowledge_bases
npx -y @modelcontextprotocol/inspector --cli http://127.0.0.1:8001/mcp --transport http --header "Authorization: Bearer <airag_key>" --method tools/call --tool-name search_knowledge_base --tool-arg "{\"kb_ids\":[<KB_ID>],\"query\":\"<真实业务问题>\",\"top_k\":5}"
```

Expected: 三条各返回两工具清单/库列表/检索命中(真检索,非 mock)。inspector CLI 参数名以 `npx @modelcontextprotocol/inspector --help` 实测为准(如 header 旗标名不同按 help 修正);若 npx 拉包失败(GitHub/npm 网络抖动,重试一次),跳到 Step 4 兜底并在执行记录注明。

- [ ] **Step 4: 兜底走查——fastmcp.Client 脚本**

Run(backend 目录):`.venv\Scripts\python scripts\m91_mcp_walkthrough.py <airag_key> <真实业务问题>`
Expected: `M9.1 MCP WALKTHROUGH: 4/4 PASS`

- [ ] **Step 5: 审计与前端浏览器走查(用户门)**

- 查 `audit_logs` 新增 `agent.list_kbs` / `agent.search`(client=mcp,key_name=m91-mcp-walkthrough)。
- 用户浏览器走查:admin 在密钥页为其他账号建 key(绑定账号下拉)、成员授权远程下拉;非 admin 界面不变。走查通过才算 M9.1 收官。

- [ ] **Step 6: 全量回归 + README + 收口**

Run(backend):`.venv\Scripts\python -m pytest -q`
Run(frontend):`npx vitest run`
Run(验收):`.venv\Scripts\python scripts/m9_acceptance.py`(重构后必须仍 16/16)
README《外部 Agent 接入(M9)》「1. 创建密钥」段追加一句:管理员可在密钥页将密钥绑定到任意账号(权限继承该账号;审计记录 by/to)。

```bash
git add backend/scripts/m91_mcp_walkthrough.py README.md
git commit -m "docs(agent): m9.1 walkthrough script and admin key note"
```

最后:更新项目记忆(M9.1 收官、走查结论、M10 候选不变)、`git push origin main`。

---

## 任务依赖图

```
T1 用户搜索端点 ─► T3 前端密钥页(admin 绑定下拉)
T2 admin 发 key ─► T3(共用 usersApi;后端先行)
T1 ─► T4 成员授权远程下拉
T1+T2+T3+T4 ─► T5 走查 + 回归收口(依赖全部)
```

## 风险与执行注意

- **el-select 远程交互在 jsdom 的抖动**:测试一律直接调 `props('remoteMethod')` 并 `vm.$emit('update:modelValue')`,不模拟真实输入/点击(沿用 M9「IAB 点击抖动绕行」经验)。
- **`services/api_keys.py` 新增 settings import 的循环依赖**:`app.core.config` 不回引 services,理论安全;若实测报错改函数内延迟 import。
- **inspector CLI 旗标名随版本漂移**:以 `--help` 实测为准;npx 网络抖动重试一次,再不行走 fastmcp.Client 兜底,不影响验收结论。
- **admin 代发的 key 不在 admin 自己列表**:GET /api/auth/keys 仍只列本人;一次性明文对话框已提示绑定账号,勿擅自加"全量密钥列表"(未拍板,YAGNI)。
- **test_users_api 的分页用例**:库内用户数会随同批用例注册变化,断言只用「页内 2 条」与「两页不相交」,不断言总数。
- Windows:后端命令一律 `.venv\Scripts\python`;勿裸 uvicorn 起服务(沿用 start_dev.bat)。

## 执行记录(2026-09-18)

- T1~T4 全部完成:后端 `test_users_api.py` 8P + `test_admin_keys.py` 6P,全量 196P;前端 KeysPage 5P / KbPage 3P,全量 22P,type-check 干净。commit:6426d43 / 2125ce6 / 2a04b52 / b212a39。
- **MCP 真客户端走查通过**(M9 唯一未做的验收步):本机无 claude CLI,按计划用官方参考客户端 + fastmcp.Client 双真客户端完成——
  - MCP Inspector(`npx @modelcontextprotocol/inspector --cli`,TypeScript SDK):initialize 握手 + Bearer 鉴权通过,tools/list 列出两工具,`list_knowledge_bases` 返回 7 库,`search_knowledge_base`(kb_ids=[2],"年假有多少天")真混合检索 8 命中、top1 即年假制度条款;无权库返回 `isError: true` + `kb_forbidden, denied_kb_ids=[999999]`。勘误:v1 CLI 的 `--tool-arg` 要 key=value 形态(`"kb_ids=[2]" "query=..."`),不吃 JSON 串。
  - `scripts/m91_mcp_walkthrough.py`(fastmcp.Client 真客户端):4/4 PASS。勘误:fastmcp 2.14.7 的 `list_tools()` 直接返回 list(无 `.tools`),脚本已做兼容。
  - 走查密钥经新端点 `POST /api/admin/keys` 下发(dogfood);审计 `agent.search/agent.list_kbs` 落库 client=mcp,key_create 审计 `{"by","to","name"}` 与个人面 `{"name"}` 区分正确。
- `scripts/m9_acceptance.py` 重构后复跑:**16/16 PASS**(issue_api_key 抽取无回归)。
- 环境备注:走查时发现旧后端(系统 Python 起、reloader 已死、跑旧代码)占着 8001,已按 start_dev.bat 惯例用 venv 重启;.venv uvicorn 的 reloader 进程在 wmic 里显示为基座解释器路径(D:\Python\python.exe),以日志 `Started reloader process [pid]` 为准,勿误杀。
- 剩余用户走查门:浏览器走 admin 代发密钥(绑定账号下拉)与成员授权远程下拉;可用走查密钥(2026-09-19 自动过期)接自己的 MCP 客户端复走。
