# AIRag M9 Agent 对接 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 AIRag 知识库以只读方式开放给外部 Agent:API Key 认证(绑定用户继承 RBAC)+ REST `/api/agent/*` + MCP `/mcp` 双协议面,含审计、限流与前端密钥管理页。

**Architecture:** 同进程"一核两面"——`services/agent_facade.py` 是唯一能力核心(权限过滤→检索→审计),REST 路由与 fastmcp Streamable HTTP 子应用都是薄壳;MCP 鉴权/限流由纯 ASGI 中间件完成并通过 ContextVar 传递主体。现有路由、SSE 问答、前端功能零改动。

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async + pydantic v2(现有);新增 `fastmcp>=2.3,<3`(MCP 服务)、`redis>=5`(限流,celery 已隐含)、dev `asgi-lifespan>=2`(MCP 测试拉起 lifespan);前端 Vue3 + element-plus + vitest(现有)。

**Spec:** `docs/superpowers/specs/2026-09-17-airag-m9-agent-integration-design.md`

## Global Constraints

- Windows 环境:后端测试一律用 `backend` 目录下 `.venv\Scripts\python -m pytest ...`;前端测试 `npx vitest run <file>`(在 `frontend` 目录)。后端服务端口 **8001**(8000 被金蝶 IIS 占用)。
- Python 3.12 / SQLAlchemy 2.0 async / pydantic v2;注释与文档跟随仓库中文风格。
- alembic 当前 head 为 `a7b8c9d0e1f2`(m7_messages_refused);新迁移 `down_revision='a7b8c9d0e1f2'`。
- 错误码约定(全计划统一):401 detail 为字符串 `invalid_key|key_revoked|key_expired`(JWT 沿用现有字符串);403 detail 为 `{"code":"kb_forbidden","denied_kb_ids":[...]}`(库不存在并入);429 detail 为 `{"code":"rate_limited","retry_after":<int秒>}`。
- 一期只读:不开放 ask/文档写操作;不动 SSE 问答与 LLM 链路。
- 每个任务完成即 commit(conventional commits,`feat(agent):`/`test:`/`docs:` 前缀)。
- 测试库由 `tests/conftest.py` 在导入前钉死(`airag_test` + fake embed);conftest 的 `CLEANUP_ORDER` 在本计划 Task 1 中修改,后续任务不得再动。
- `AGENT_API_ENABLED` 为进程启动期开关(路由挂载/import 时判定),无运行时切换。

---

### Task 1: ApiKey 模型 + key 生成/解析服务 + 迁移 + conftest

**Files:**
- Create: `backend/app/models/api_key.py`
- Create: `backend/app/services/api_keys.py`
- Create: `backend/alembic/versions/b9c8d7e6f5a4_m9_api_keys.py`
- Modify: `backend/app/models/__init__.py`
- Modify: `backend/tests/conftest.py:38-47`(CLEANUP_ORDER)
- Test: `backend/tests/test_api_keys_service.py`

**Interfaces:**
- Consumes: `app.models.base.Base/TimestampMixin`、`app.db.session`(测试 fixture `db_session`)
- Produces(Task 2/3 依赖,签名固定):
  - `ApiKey` 模型(表 `api_keys`,列:id/user_id/name/key_prefix/key_hash/is_active/expires_at/last_used_at/created_at)
  - `generate_api_key() -> tuple[str, str, str]` —— (明文 key, key_prefix, sha256 hex)
  - `hash_api_key(key: str) -> str`
  - `resolve_api_key(db: AsyncSession, raw: str) -> ApiKey` —— 失败抛 `KeyRejected`,`KeyRejected.code ∈ {"invalid_key","key_revoked","key_expired"}`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_api_keys_service.py
"""M9 Task1:api key 生成/哈希/解析(resolve)。"""
import pytest
from sqlalchemy import select

from app.models import ApiKey, User
from app.services.api_keys import (
    KeyRejected,
    generate_api_key,
    hash_api_key,
    resolve_api_key,
)


def test_generate_api_key_format():
    raw, prefix, digest = generate_api_key()
    assert raw.startswith("airag_") and len(raw) > 20
    assert prefix == raw[:14] and prefix.startswith("airag_")
    assert digest == hash_api_key(raw) and len(digest) == 64


def test_generate_uniqueness():
    assert generate_api_key()[0] != generate_api_key()[0]


async def test_resolve_roundtrip(db_session):
    user = User(username="k1_owner", password_hash="x", role="viewer")
    db_session.add(user)
    await db_session.flush()
    raw, prefix, digest = generate_api_key()
    db_session.add(
        ApiKey(user_id=user.id, name="t", key_prefix=prefix, key_hash=digest)
    )
    await db_session.commit()

    key = await resolve_api_key(db_session, raw)
    assert key.user_id == user.id and key.name == "t"


async def test_resolve_rejects(db_session):
    user = User(username="k2_owner", password_hash="x", role="viewer")
    db_session.add(user)
    await db_session.flush()
    raw, prefix, digest = generate_api_key()
    revoked_raw, revoked_prefix, revoked_digest = generate_api_key()
    db_session.add_all([
        ApiKey(user_id=user.id, name="bad", key_prefix="airag_bad", key_hash="0" * 64),
        ApiKey(user_id=user.id, name="revoked", key_prefix=revoked_prefix,
               key_hash=revoked_digest, is_active=False),
        ApiKey(user_id=user.id, name="ok", key_prefix=prefix, key_hash=digest),
    ])
    await db_session.commit()

    with pytest.raises(KeyRejected) as e:
        await resolve_api_key(db_session, "airag_nope")
    assert e.value.code == "invalid_key"

    with pytest.raises(KeyRejected) as e:
        await resolve_api_key(db_session, revoked_raw)
    assert e.value.code == "key_revoked"


async def test_resolve_expired(db_session):
    from datetime import datetime, timedelta, timezone

    user = User(username="k3_owner", password_hash="x", role="viewer")
    db_session.add(user)
    await db_session.flush()
    raw, prefix, digest = generate_api_key()
    db_session.add(ApiKey(
        user_id=user.id, name="exp", key_prefix=prefix, key_hash=digest,
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    await db_session.commit()

    with pytest.raises(KeyRejected) as e:
        await resolve_api_key(db_session, raw)
    assert e.value.code == "key_expired"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_api_keys_service.py -v`
Expected: FAIL(`ModuleNotFoundError: app.services.api_keys` / `app.models.ApiKey`)

- [ ] **Step 3: 实现模型与服务**

```python
# backend/app/models/api_key.py
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ApiKey(Base, TimestampMixin):
    """M9:对外 Agent 调用凭证;绑定用户,权限实时继承归属用户。"""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    key_prefix: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(64))  # sha256 hex,不存明文
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

```python
# backend/app/services/api_keys.py
"""M9:api key 生成与解析(prefix 索引定位 + 常数时间哈希比较)。"""
import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiKey

KEY_HEADER = "airag_"


class KeyRejected(Exception):
    """解析失败;code ∈ invalid_key|key_revoked|key_expired(即 401 detail)。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def generate_api_key() -> tuple[str, str, str]:
    """返回 (明文 key, key_prefix, key_sha256_hex);明文只在创建响应出现一次。"""
    key = f"{KEY_HEADER}{secrets.token_urlsafe(24)}"
    return key, key[:14], hash_api_key(key)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def resolve_api_key(db: AsyncSession, raw: str) -> ApiKey:
    row = (
        await db.execute(select(ApiKey).where(ApiKey.key_prefix == raw[:14]))
    ).scalar_one_or_none()
    if row is None or not hmac.compare_digest(hash_api_key(raw), row.key_hash):
        raise KeyRejected("invalid_key")
    if not row.is_active:
        raise KeyRejected("key_revoked")
    if row.expires_at is not None and row.expires_at <= datetime.now(timezone.utc):
        raise KeyRejected("key_expired")
    return row
```

修改 `backend/app/models/__init__.py`(导出 + `__all__` 加 `"ApiKey"`):

```python
from app.models.audit import AuditLog
from app.models.api_key import ApiKey   # 新增(保持字母序放在 audit 后亦可)
# __all__ 列表追加 "ApiKey"
```

修改 `backend/tests/conftest.py` 的 `CLEANUP_ORDER`(api_keys 仅引用 users,删于 users 前):

```python
CLEANUP_ORDER = [
    "audit_logs",
    "messages",
    "conversations",
    "chunks",
    "documents",
    "kb_permissions",
    "knowledge_bases",
    "api_keys",
    "users",
]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_api_keys_service.py -v`
Expected: 5 PASS

- [ ] **Step 5: 写 alembic 迁移并本地验证**

```python
# backend/alembic/versions/b9c8d7e6f5a4_m9_api_keys.py
"""m9 api_keys

Revision ID: b9c8d7e6f5a4
Revises: a7b8c9d0e1f2
Create Date: 2026-09-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b9c8d7e6f5a4"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"),
                  nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False, unique=True, index=True),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("api_keys")
```

Run(backend 目录,真实库):`.venv\Scripts\python -m alembic upgrade head`
Expected: 无报错;再 `.venv\Scripts\python -m alembic current` 显示 `b9c8d7e6f5a4 (head)`。

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/api_key.py backend/app/services/api_keys.py backend/alembic/versions/b9c8d7e6f5a4_m9_api_keys.py backend/app/models/__init__.py backend/tests/conftest.py backend/tests/test_api_keys_service.py
git commit -m "feat(agent): api key model, generator/resolver and migration"
```

---

### Task 2: Key 管理端点(POST/GET/DELETE /api/auth/keys)

**Files:**
- Modify: `backend/app/api/auth.py`(追加 3 个端点)
- Modify: `backend/app/schemas/auth.py`(追加 3 个 schema)
- Modify: `backend/app/core/config.py`(追加 `AGENT_MAX_KEYS_PER_USER`)
- Test: `backend/tests/test_auth_keys_api.py`

**Interfaces:**
- Consumes: Task 1 的 `generate_api_key/ApiKey`;现有 `get_current_user`、`audit()`
- Produces(Task 3/7/8 依赖):
  - `POST /api/auth/keys` body `{"name": str(1..64), "expires_in_days": int|null(1..3650)}` → 201 `ApiKeyCreatedOut`(含一次性 `key` 明文);超配额 409 detail `"api key limit reached"`
  - `GET /api/auth/keys` → `list[ApiKeyOut]`(无明文,按 id 降序)
  - `DELETE /api/auth/keys/{id}` → 204;非本人/不存在 404
  - schemas:`ApiKeyOut(id,name,key_prefix,is_active,expires_at,last_used_at,created_at)`、`ApiKeyCreatedOut(ApiKeyOut + key)`
  - config:`AGENT_MAX_KEYS_PER_USER: int = 10`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_auth_keys_api.py
"""M9 Task2:key 管理端点(JWT-only,创建一次性明文/列表/吊销/配额)。"""
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.models import ApiKey


async def _mk_key(client, headers, name="t", days=None):
    return await client.post(
        "/api/auth/keys",
        json={"name": name, "expires_in_days": days},
        headers=headers,
    )


async def test_create_returns_plaintext_once(client, auth_headers, db_session):
    resp = await _mk_key(client, auth_headers, name="Cursor", days=30)
    assert resp.status_code == 201
    body = resp.json()
    assert body["key"].startswith("airag_")
    assert body["name"] == "Cursor"
    assert body["expires_at"] is not None
    # 库里只有哈希
    from sqlalchemy import select
    row = (await db_session.execute(select(ApiKey))).scalar_one()
    assert row.key_hash != body["key"] and row.key_prefix == body["key"][:14]


async def test_list_has_no_plaintext(client, auth_headers):
    await _mk_key(client, auth_headers, name="a")
    resp = await client.get("/api/auth/keys", headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert "key" not in items[0] and items[0]["key_prefix"].startswith("airag_")


async def test_revoke_then_404_for_foreign_key(client, auth_headers, db_session):
    created = (await _mk_key(client, auth_headers, name="r")).json()
    resp = await client.delete(f"/api/auth/keys/{created['id']}", headers=auth_headers)
    assert resp.status_code == 204
    row = await db_session.get(ApiKey, created["id"])
    assert row.is_active is False

    # 他人 key → 404(不泄露存在性)
    other = await client.post(
        "/api/auth/register",
        json={"username": "keyother1", "password": "secret123"},
    )
    other_login = await client.post(
        "/api/auth/login",
        json={"username": "keyother1", "password": "secret123"},
    )
    other_headers = {"Authorization": f"Bearer {other_login.json()['access_token']}"}
    resp = await client.delete(
        f"/api/auth/keys/{created['id']}", headers=other_headers
    )
    assert resp.status_code == 404


async def test_key_quota_409(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MAX_KEYS_PER_USER", 2)
    assert (await _mk_key(client, auth_headers, name="1")).status_code == 201
    assert (await _mk_key(client, auth_headers, name="2")).status_code == 201
    resp = await _mk_key(client, auth_headers, name="3")
    assert resp.status_code == 409


async def test_api_key_cannot_create_key(client, auth_headers):
    created = (await _mk_key(client, auth_headers, name="nochain")).json()
    resp = await client.post(
        "/api/auth/keys", json={"name": "x"},
        headers={"Authorization": f"Bearer {created['key']}"},
    )
    assert resp.status_code == 401  # get_current_user 只认 JWT
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_auth_keys_api.py -v`
Expected: FAIL(404 Not Found,路由不存在)

- [ ] **Step 3: 实现 schema + 端点**

`schemas/auth.py` 追加(顶部已有 `from datetime import datetime` 则复用,没有就补):

```python
from datetime import datetime
# 追加到文件末尾
class ApiKeyCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)  # None=永久


class ApiKeyOut(BaseModel):
    id: int
    name: str
    key_prefix: str
    is_active: bool
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyCreatedOut(ApiKeyOut):
    key: str  # 唯一一次返回明文
```

`core/config.py` 在 `MAX_UPLOAD_MB` 前追加:

```python
    # M9 Agent 对外开放
    AGENT_MAX_KEYS_PER_USER: int = 10
```

`api/auth.py` 追加(imports 补:`from datetime import datetime, timedelta, timezone`、`from fastapi import Response`、`from sqlalchemy import func, select`、`from app.core.config import settings`、`from app.models import ApiKey`、`from app.schemas.auth import ApiKeyCreateIn, ApiKeyCreatedOut, ApiKeyOut`、`from app.services.api_keys import generate_api_key`):

```python
# ---- M9:API 密钥管理(JWT-only;key 不能创建 key) ----
@router.post("/keys", response_model=ApiKeyCreatedOut, status_code=201)
async def create_api_key(
    payload: ApiKeyCreateIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    count = (
        await db.execute(
            select(func.count()).select_from(ApiKey).where(
                ApiKey.user_id == current.id, ApiKey.is_active == True  # noqa: E712
            )
        )
    ).scalar_one()
    if count >= settings.AGENT_MAX_KEYS_PER_USER:
        raise HTTPException(status_code=409, detail="api key limit reached")
    raw, prefix, digest = generate_api_key()
    key = ApiKey(
        user_id=current.id,
        name=payload.name,
        key_prefix=prefix,
        key_hash=digest,
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days)
            if payload.expires_in_days
            else None
        ),
    )
    db.add(key)
    await db.flush()
    await audit(db, current.username, "key_create", f"apikey:{key.id}",
                {"name": payload.name})
    await db.commit()
    await db.refresh(key)
    return ApiKeyCreatedOut.model_validate(key, update={"key": raw})


@router.get("/keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(ApiKey).where(ApiKey.user_id == current.id)
            .order_by(ApiKey.id.desc())
        )
    ).scalars().all()
    return rows


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    key = await db.get(ApiKey, key_id)
    if key is None or key.user_id != current.id:
        raise HTTPException(status_code=404, detail="api key not found")
    key.is_active = False
    await audit(db, current.username, "key_revoke", f"apikey:{key.id}")
    await db.commit()
    return Response(status_code=204)
```

注:pydantic v2 的 `model_validate(obj, update={...})` 若当前版本不支持,改为手工构造 `ApiKeyCreatedOut(id=key.id, name=key.name, ..., key=raw)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_auth_keys_api.py -v`
Expected: 5 PASS

- [ ] **Step 5: 跑全量回归确认无破坏**

Run: `.venv\Scripts\python -m pytest -q`
Expected: 全部 PASS(存量 + 新增)

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/auth.py backend/app/schemas/auth.py backend/app/core/config.py backend/tests/test_auth_keys_api.py
git commit -m "feat(agent): personal api key management endpoints"
```

---

### Task 3: Principal 依赖 + GET /api/agent/kbs + facade.list_kbs_for

**Files:**
- Modify: `backend/app/core/deps.py`(追加 Principal/current_principal/resolve_bearer_principal/get_agent_principal)
- Create: `backend/app/services/agent_facade.py`(本任务只实现 `list_kbs_for`;Task 4 续写 `agent_search`)
- Create: `backend/app/schemas/agent.py`(本任务只加 KB 相关;Task 4 续写 search 相关)
- Create: `backend/app/api/agent.py`(GET /kbs;Task 4 续写 search)
- Modify: `backend/app/api/__init__.py`(条件挂载 agent_router)
- Test: `backend/tests/test_agent_api.py`

**Interfaces:**
- Consumes: Task 1 `resolve_api_key/KeyRejected`;现有 `get_kb_perm/decode_access_token/bearer_scheme`
- Produces(Task 4/6 依赖,签名固定):
  - `@dataclass Principal: user: User; kind: str; key_id: int|None = None; key_name: str|None = None`(kind ∈ "jwt"|"api_key")
  - `current_principal: ContextVar[Principal | None]`(MCP 中间件写入,Task 6 用)
  - `resolve_bearer_principal(db, raw: str) -> Principal`(401 抛 HTTPException;Task 6 中间件复用)
  - `get_agent_principal`(FastAPI Depends 版)
  - `agent_facade.list_kbs_for(db, user) -> list[KbBrief]`;`@dataclass KbBrief: id:int; name:str; description:str|None; my_perm:str`
  - REST:`GET /api/agent/kbs` → `{"items": [KbBrief...]}`,Header JWT 或 `airag_` key 均可

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_agent_api.py
"""M9 Task3:agent principal 双路径鉴权 + GET /api/agent/kbs 可见性 + 审计。"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.models import ApiKey, AuditLog
from app.services.api_keys import generate_api_key


async def _create_key(client, auth_headers, name="agent-key", days=None) -> str:
    resp = await client.post(
        "/api/auth/keys",
        json={"name": name, "expires_in_days": days},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    return resp.json()["key"]


async def _create_kb(client, auth_headers, name) -> int:
    resp = await client.post("/api/kbs", json={"name": name}, headers=auth_headers)
    assert resp.status_code == 201
    return resp.json()["id"]


async def test_kbs_with_jwt(client, auth_headers):
    kb_id = await _create_kb(client, auth_headers, "JWT可见库")
    resp = await client.get("/api/agent/kbs", headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [i["id"] for i in items] == [kb_id]
    assert items[0]["my_perm"] == "owner"


async def test_kbs_with_api_key_only_permitted(client, auth_headers, db_session):
    mine = await _create_kb(client, auth_headers, "我的库")
    # 他人库(注册默认 viewer,建库会被 403,先 SQL 提权为 editor——conftest 同法)
    await client.post(
        "/api/auth/register", json={"username": "agother1", "password": "secret123"}
    )
    await db_session.execute(
        text("UPDATE users SET role = 'editor' WHERE username = 'agother1'")
    )
    await db_session.commit()
    other_login = await client.post(
        "/api/auth/login", json={"username": "agother1", "password": "secret123"}
    )
    other_headers = {"Authorization": f"Bearer {other_login.json()['access_token']}"}
    await _create_kb(client, other_headers, "别人的库")

    key = await _create_key(client, auth_headers)
    resp = await client.get("/api/agent/kbs",
                            headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    ids = [i["id"] for i in resp.json()["items"]]
    assert mine in ids
    assert resp.json()["items"][0]["name"] == "我的库"

    # 他人 key 看不到我的库
    resp = await client.get("/api/agent/kbs", headers=other_headers)
    names = [i["name"] for i in resp.json()["items"]]
    assert "我的库" not in names


async def test_revoked_key_401(client, auth_headers):
    key = await _create_key(client, auth_headers)
    created = (await client.get("/api/auth/keys", headers=auth_headers)).json()[0]
    await client.delete(f"/api/auth/keys/{created['id']}", headers=auth_headers)
    resp = await client.get("/api/agent/kbs",
                            headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "key_revoked"


async def test_expired_key_401(client, auth_headers, db_session):
    me = await client.get("/api/auth/me", headers=auth_headers)
    uid = me.json()["id"]
    raw, prefix, digest = generate_api_key()
    db_session.add(ApiKey(user_id=uid, name="exp", key_prefix=prefix, key_hash=digest,
                          expires_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    await db_session.commit()
    resp = await client.get("/api/agent/kbs",
                            headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "key_expired"


async def test_disabled_user_key_401(client, auth_headers, db_session):
    me = await client.get("/api/auth/me", headers=auth_headers)
    key = await _create_key(client, auth_headers)
    await db_session.execute(
        text("UPDATE users SET is_active = false WHERE id = :i"),
        {"i": me.json()["id"]},
    )
    await db_session.commit()
    resp = await client.get("/api/agent/kbs",
                            headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 401


async def test_no_auth_401(client):
    resp = await client.get("/api/agent/kbs")
    assert resp.status_code == 401


async def test_audit_written(client, auth_headers, db_session):
    await _create_key(client, auth_headers, name="审计key")
    key = await _create_key(client, auth_headers)
    await client.get("/api/agent/kbs", headers={"Authorization": f"Bearer {key}"})
    db_session.expire_all()  # expire_all 是同步方法,不可 await
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "agent.list_kbs")
    )).scalars().all()
    assert len(rows) == 1 and rows[0].username
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_agent_api.py -v`
Expected: FAIL(404,`/api/agent/kbs` 不存在)

- [ ] **Step 3: 实现依赖 + facade + 路由**

`core/deps.py` 全文替换为(保留现有函数,追加 M9 部分;imports 相应补 `dataclasses.dataclass`、`contextvars.ContextVar`、`datetime/timezone`、`app.services.api_keys`):

```python
# ---- M9:对外 Agent 主体(双路径:JWT 或 airag_ key) ----
@dataclass
class Principal:
    user: User
    kind: str  # jwt | api_key
    key_id: int | None = None
    key_name: str | None = None


# MCP 侧由 ASGI 中间件写入(mcp_server.py),工具函数读取
current_principal: ContextVar[Principal | None] = ContextVar(
    "current_principal", default=None
)


async def resolve_bearer_principal(db: AsyncSession, raw: str) -> Principal:
    """把 Bearer 凭证解析为 Principal;失败抛 401 HTTPException。"""
    if raw.startswith("airag_"):
        try:
            key = await resolve_api_key(db, raw)
        except KeyRejected as e:
            raise HTTPException(status_code=401, detail=e.code)
        user = await db.get(User, key.user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="invalid_key")
        key.last_used_at = datetime.now(timezone.utc)
        return Principal(user=user, kind="api_key", key_id=key.id, key_name=key.name)
    payload = decode_access_token(raw)
    if payload is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    user = await db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="user not found or disabled")
    return Principal(user=user, kind="jwt")


async def get_agent_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    if creds is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return await resolve_bearer_principal(db, creds.credentials)
```

注:`last_used_at` 的写库依赖路由内 commit(agent 路由均会 commit);`resolve_api_key/KeyRejected` 顶部 import 即可,无循环依赖。

```python
# backend/app/services/agent_facade.py
"""M9:Agent 能力核心(唯一业务实现;REST 与 MCP 共用)。"""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.perms import get_kb_perm
from app.models import KnowledgeBase, User


@dataclass
class KbBrief:
    id: int
    name: str
    description: str | None
    my_perm: str


class AgentKbDenied(Exception):
    """任一 kb_id 无权限或不存在;denied_kb_ids 不区分两者(不泄露存在性)。"""

    def __init__(self, denied_kb_ids: list[int]):
        super().__init__(denied_kb_ids)
        self.denied_kb_ids = denied_kb_ids


async def list_kbs_for(db: AsyncSession, user: User) -> list[KbBrief]:
    """与 GET /api/kbs 同语义:admin 全库 owner;否则 自有 ∪ 被授权。"""
    rows = (await db.execute(select(KnowledgeBase))).scalars().all()
    out = []
    for kb in rows:
        perm = await get_kb_perm(db, user, kb)
        if perm is not None:
            out.append(KbBrief(id=kb.id, name=kb.name,
                               description=kb.description, my_perm=perm))
    out.sort(key=lambda x: -x.id)
    return out
```

```python
# backend/app/schemas/agent.py
"""M9:agent 面请求/响应 schema(Task 4 追加 search 部分)。"""
from pydantic import BaseModel


class AgentKbOut(BaseModel):
    id: int
    name: str
    description: str | None
    my_perm: str


class AgentKbListOut(BaseModel):
    items: list[AgentKbOut]
```

```python
# backend/app/api/agent.py
"""M9:REST 面(薄壳;能力全部来自 services/agent_facade)。"""
from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_agent_principal
from app.db.session import get_db
from app.schemas.agent import AgentKbListOut
from app.services import agent_facade
from app.services.audit import audit

router = APIRouter(prefix="/agent", tags=["agent"])


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/kbs", response_model=AgentKbListOut)
async def agent_kbs(
    request: Request,
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    items = await agent_facade.list_kbs_for(db, principal.user)
    await audit(
        db, principal.user.username, "agent.list_kbs", "agent",
        {"client": "rest", "key_name": principal.key_name, "kb_count": len(items)},
        ip=_ip(request),
    )
    await db.commit()
    return AgentKbListOut(items=[AgentKbOut(**asdict(i)) for i in items])
```

(imports 里 `AgentKbOut` 也要从 schemas.agent 引入。)

`api/__init__.py` 追加:

```python
from app.core.config import settings
from app.api.agent import router as agent_router
# ...
if settings.AGENT_API_ENABLED:
    api_router.include_router(agent_router)
```

并在 `core/config.py` 的 M9 段补:`AGENT_API_ENABLED: bool = True`。

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `.venv\Scripts\python -m pytest tests/test_agent_api.py -v && .venv\Scripts\python -m pytest -q`
Expected: 新增 7 PASS;全量 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/deps.py backend/app/services/agent_facade.py backend/app/schemas/agent.py backend/app/api/agent.py backend/app/api/__init__.py backend/app/core/config.py backend/tests/test_agent_api.py
git commit -m "feat(agent): principal auth dep and GET /api/agent/kbs"
```

---

### Task 4: agent_search facade + POST /api/agent/search(含 rerank/阈值路径)

**Files:**
- Modify: `backend/app/services/agent_facade.py`(追加 `agent_search`)
- Modify: `backend/app/schemas/agent.py`(追加 search schema)
- Modify: `backend/app/api/agent.py`(追加 POST /search;限流接线留给 Task 5)
- Test: `backend/tests/test_agent_api.py`(追加 search 用例,文件内新增 class 或直接函数)

**Interfaces:**
- Consumes: Task 3 的 `Principal/get_agent_principal/AgentKbDenied`;现有 `hybrid_search/SearchHit/get_reranker/settings`
- Produces(Task 6 依赖):
  - `@dataclass SearchOutcome: hits: list[SearchHit]; elapsed_ms: int`
  - `agent_facade.agent_search(db, user, kb_ids: list[int], query: str, top_k: int, rerank: bool) -> SearchOutcome`(任一 kb 无权/不存在抛 `AgentKbDenied`)
  - REST:`POST /api/agent/search` body `{"kb_ids":[1..5], "query":1..500字, "top_k":1..20默认RETRIEVAL_TOP_K, "rerank":bool}` → `{"hits":[8个SearchHit字段], "total", "elapsed_ms"}`

- [ ] **Step 1: 写失败测试(追加到 test_agent_api.py)**

```python
# ---- Task4:POST /api/agent/search ----
from app.services.retrieval.searcher import SearchHit  # noqa: E402


def _fake_hits(n=2):
    return [
        SearchHit(chunk_id=i, document_id=i * 10, kb_id=1, filename="a.pdf",
                  page_no=i + 1, content=f"内容{i}", score=0.5 + i * 0.1,
                  source="both")
        for i in range(n)
    ]


async def test_search_ok(client, auth_headers, monkeypatch):
    kb_id = await _create_kb(client, auth_headers, "检索库")
    key = await _create_key(client, auth_headers)

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        hits = _fake_hits()
        for h in hits:
            h.kb_id = kb_id
        return hits

    monkeypatch.setattr("app.services.agent_facade.hybrid_search", fake_hybrid)
    resp = await client.post(
        "/api/agent/search",
        json={"kb_ids": [kb_id], "query": "测试问题", "top_k": 5},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2 and body["elapsed_ms"] >= 0
    hit = body["hits"][0]
    assert set(hit) == {"chunk_id", "document_id", "kb_id", "filename",
                        "page_no", "content", "score", "source"}


async def test_search_denied_includes_nonexistent(client, auth_headers):
    kb_id = await _create_kb(client, auth_headers, "被拒库")
    key = await _create_key(client, auth_headers)
    resp = await client.post(
        "/api/agent/search",
        json={"kb_ids": [kb_id, 99999], "query": "x"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "kb_forbidden" and 99999 in detail["denied_kb_ids"]
    assert kb_id not in detail["denied_kb_ids"]


async def test_search_validation_422(client, auth_headers):
    key = await _create_key(client, auth_headers)
    resp = await client.post(
        "/api/agent/search", json={"kb_ids": [], "query": ""},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422


async def test_search_rerank_applied(client, auth_headers, monkeypatch):
    kb_id = await _create_kb(client, auth_headers, "重排库")
    key = await _create_key(client, auth_headers)

    class FakeReranker:
        def rerank(self, query, documents, top_n=8):
            return [(1, 0.95), (0, 0.10)]  # 原顺序反转

    monkeypatch.setattr("app.services.agent_facade.settings.RERANK_ENABLED", True)
    monkeypatch.setattr("app.services.agent_facade.get_reranker", lambda: FakeReranker())

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        return _fake_hits()

    monkeypatch.setattr("app.services.agent_facade.hybrid_search", fake_hybrid)
    resp = await client.post(
        "/api/agent/search",
        json={"kb_ids": [kb_id], "query": "q", "rerank": True},
        headers={"Authorization": f"Bearer {key}"},
    )
    body = resp.json()
    assert body["hits"][0]["score"] == 0.95 and body["hits"][0]["chunk_id"] == 1


async def test_search_rerank_min_score_gate(client, auth_headers, monkeypatch):
    kb_id = await _create_kb(client, auth_headers, "阈值库")
    key = await _create_key(client, auth_headers)

    class FakeReranker:
        def rerank(self, query, documents, top_n=8):
            return [(0, 0.50)]

    monkeypatch.setattr("app.services.agent_facade.settings.RERANK_ENABLED", True)
    monkeypatch.setattr("app.services.agent_facade.settings.RETRIEVAL_MIN_SCORE", 0.9)
    monkeypatch.setattr("app.services.agent_facade.get_reranker", lambda: FakeReranker())

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        return _fake_hits(1)

    monkeypatch.setattr("app.services.agent_facade.hybrid_search", fake_hybrid)
    resp = await client.post(
        "/api/agent/search",
        json={"kb_ids": [kb_id], "query": "q", "rerank": True},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.json()["total"] == 0
```

注:`monkeypatch.setattr("...settings.RERANK_ENABLED", True)` 这种字符串路径形式对实例属性可用(`agent_facade.settings` 是同一个 Settings 单例)。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_agent_api.py -v -k search`
Expected: FAIL(404 路由不存在)

- [ ] **Step 3: 实现 facade.agent_search + schema + 路由**

`agent_facade.py` 追加(imports 补 `asyncio/time`、`SearchHit/hybrid_search`、`get_reranker`、`settings`):

```python
@dataclass
class SearchOutcome:
    hits: list[SearchHit]
    elapsed_ms: int


async def agent_search(
    db: AsyncSession,
    user: User,
    kb_ids: list[int],
    query: str,
    top_k: int,
    rerank: bool,
) -> SearchOutcome:
    """权限过滤 → hybrid_search → 可选 rerank(与 rerank_node 同阈值语义)。"""
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
    ]
    if denied:
        raise AgentKbDenied(denied)

    start = time.perf_counter()
    hits = await hybrid_search(db, kb_ids, query, top_k=top_k)
    reranker = get_reranker() if rerank else None
    if reranker is not None and hits:
        scored = await asyncio.to_thread(
            reranker.rerank, query, [h.content for h in hits], top_k,
        )
        ranked = sorted(scored, key=lambda p: p[1], reverse=True)[:top_k]
        out: list[SearchHit] = []
        for idx, rel in ranked:
            if not 0 <= idx < len(hits):
                continue
            if settings.RETRIEVAL_MIN_SCORE > 0 and rel < settings.RETRIEVAL_MIN_SCORE:
                continue
            h = hits[idx]
            out.append(SearchHit(
                chunk_id=h.chunk_id, document_id=h.document_id, kb_id=h.kb_id,
                filename=h.filename, page_no=h.page_no, content=h.content,
                score=round(float(rel), 6), source=h.source,
            ))
        hits = out
    return SearchOutcome(hits=hits,
                         elapsed_ms=int((time.perf_counter() - start) * 1000))
```

`schemas/agent.py` 追加:

```python
from pydantic import Field

from app.core.config import settings


class AgentSearchIn(BaseModel):
    kb_ids: list[int] = Field(min_length=1, max_length=5)
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default_factory=lambda: settings.RETRIEVAL_TOP_K, ge=1, le=20)
    rerank: bool = False


class AgentHitOut(BaseModel):
    chunk_id: int
    document_id: int
    kb_id: int
    filename: str
    page_no: int | None
    content: str
    score: float
    source: str


class AgentSearchOut(BaseModel):
    hits: list[AgentHitOut]
    total: int
    elapsed_ms: int
```

`api/agent.py` 追加:

```python
from fastapi import HTTPException

from app.schemas.agent import AgentHitOut, AgentSearchIn, AgentSearchOut


@router.post("/search", response_model=AgentSearchOut)
async def agent_search(
    payload: AgentSearchIn,
    request: Request,
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    try:
        outcome = await agent_facade.agent_search(
            db, principal.user, payload.kb_ids, payload.query,
            payload.top_k, payload.rerank,
        )
    except agent_facade.AgentKbDenied as e:
        raise HTTPException(
            status_code=403,
            detail={"code": "kb_forbidden", "denied_kb_ids": e.denied_kb_ids},
        )
    await audit(
        db, principal.user.username, "agent.search", "agent",
        {"client": "rest", "key_name": principal.key_name,
         "kb_ids": payload.kb_ids, "query": payload.query[:200],
         "hit_count": len(outcome.hits)},
        ip=_ip(request),
    )
    await db.commit()
    return AgentSearchOut(
        hits=[AgentHitOut(**asdict(h)) for h in outcome.hits],
        total=len(outcome.hits),
        elapsed_ms=outcome.elapsed_ms,
    )
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `.venv\Scripts\python -m pytest tests/test_agent_api.py -v && .venv\Scripts\python -m pytest -q`
Expected: 新增 5 PASS;全量 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_facade.py backend/app/schemas/agent.py backend/app/api/agent.py backend/tests/test_agent_api.py
git commit -m "feat(agent): hybrid search facade and POST /api/agent/search"
```

---

### Task 5: Redis 滑动窗口限流 + 接入 agent 路由(429)

**Files:**
- Create: `backend/app/services/agent_ratelimit.py`
- Modify: `backend/app/api/agent.py`(`_check_rate` 接入两个端点)
- Modify: `backend/pyproject.toml`(dependencies 加 `"redis>=5"`)
- Test: `backend/tests/test_agent_ratelimit.py`

**Interfaces:**
- Consumes: Task 3/4 的 agent 路由、`Principal`
- Produces(Task 6 依赖):`agent_ratelimit.allow(ident: str) -> tuple[bool, int]`(False 时第二项为 retry_after 秒;`AGENT_RATE_LIMIT_PER_MIN<=0` 或 Redis 异常时放行);测试可 monkeypatch `app.services.agent_ratelimit.get_redis`
- config:`AGENT_RATE_LIMIT_PER_MIN: int = 60`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_agent_ratelimit.py
"""M9 Task5:限流窗口逻辑(FakeRedis)+ 429 集成。"""
import pytest

from app.core.config import settings
from app.services import agent_ratelimit
from app.services.agent_ratelimit import allow


class FakeRedis:
    def __init__(self, fail=False):
        self.z: dict[str, dict[str, float]] = {}
        self.fail = fail

    async def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    async def zremrangebyscore(self, key, lo, hi):
        await self._check()
        d = self.z.setdefault(key, {})
        self.z[key] = {m: s for m, s in d.items() if s > hi}

    async def zcard(self, key):
        await self._check()
        return len(self.z.get(key, {}))

    async def zadd(self, key, mapping):
        await self._check()
        self.z.setdefault(key, {}).update(mapping)

    async def zrange(self, key, start, end, withscores=False):
        await self._check()
        items = sorted(self.z.get(key, {}).items(), key=lambda kv: kv[1])
        return items[start: end + 1]

    async def expire(self, key, ttl):
        return True


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(agent_ratelimit, "get_redis", lambda: r)
    return r


async def test_under_limit_passes(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 3)
    for _ in range(3):
        ok, _ = await allow("key:1")
        assert ok


async def test_over_limit_denies_with_retry(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 1)
    assert (await allow("key:2"))[0] is True
    ok, retry = await allow("key:2")
    assert ok is False and 1 <= retry <= 60


async def test_limit_disabled(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 0)
    for _ in range(5):
        assert (await allow("key:3"))[0] is True


async def test_redis_failure_degrades_open(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 1)
    monkeypatch.setattr(agent_ratelimit, "get_redis", lambda: FakeRedis(fail=True))
    assert (await allow("key:4"))[0] is True


async def test_ident_isolated(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 1)
    assert (await allow("key:a"))[0] is True
    assert (await allow("key:b"))[0] is True


async def test_api_429(client, auth_headers, monkeypatch):
    # 集成:第 2 次(配额 1)触发 429;JWT 不限流
    from test_agent_api import _create_kb, _create_key

    kb_id = await _create_kb(client, auth_headers, "限流库")
    key = await _create_key(client, auth_headers)
    r = FakeRedis()
    monkeypatch.setattr(agent_ratelimit, "get_redis", lambda: r)
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 1)

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        return []

    monkeypatch.setattr("app.services.agent_facade.hybrid_search", fake_hybrid)
    hdr = {"Authorization": f"Bearer {key}"}
    resp1 = await client.post("/api/agent/search",
                              json={"kb_ids": [kb_id], "query": "q"}, headers=hdr)
    resp2 = await client.post("/api/agent/search",
                              json={"kb_ids": [kb_id], "query": "q"}, headers=hdr)
    assert resp1.status_code == 200
    assert resp2.status_code == 429
    detail = resp2.json()["detail"]
    assert detail["code"] == "rate_limited" and detail["retry_after"] >= 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_agent_ratelimit.py -v`
Expected: FAIL(ModuleNotFoundError: agent_ratelimit)

- [ ] **Step 3: 实现限流并接线**

```python
# backend/app/services/agent_ratelimit.py
"""M9:按 key 滑动窗口限流(Redis ZSET);Redis 异常降级放行(可用性优先)。"""
import time
import uuid

from redis import asyncio as aioredis

from app.core.config import settings

WINDOW_SECONDS = 60

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """惰性单例;测试 monkeypatch 此函数注入 fake。"""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


async def allow(ident: str) -> tuple[bool, int]:
    """返回 (是否放行, retry_after 秒)。"""
    limit = settings.AGENT_RATE_LIMIT_PER_MIN
    if limit <= 0:
        return True, 0
    now = time.time()
    rkey = f"agent_rl:{ident}"
    try:
        r = get_redis()
        await r.zremrangebyscore(rkey, 0, now - WINDOW_SECONDS)
        count = await r.zcard(rkey)
        if count >= limit:
            oldest = await r.zrange(rkey, 0, 0, withscores=True)
            retry = 1
            if oldest:
                retry = max(1, int(WINDOW_SECONDS - (now - oldest[0][1])) + 1)
            return False, retry
        await r.zadd(rkey, {f"{now:.6f}:{uuid.uuid4().hex[:6]}": now})
        await r.expire(rkey, WINDOW_SECONDS * 2)
        return True, 0
    except Exception:
        return True, 0
```

`api/agent.py` 顶部与两个端点开头接入(在 `list_kbs_for`/`agent_search` 调用之前):

```python
from app.services.agent_ratelimit import allow as rate_allow


async def _check_rate(principal: Principal) -> None:
    """仅对 API Key 生效;JWT(人工调试)不限流。"""
    if principal.kind != "api_key" or principal.key_id is None:
        return
    ok, retry_after = await rate_allow(f"key:{principal.key_id}")
    if not ok:
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "retry_after": retry_after},
        )
```

并在 `agent_kbs` 与 `agent_search` 端点体第一行调用 `await _check_rate(principal)`。

`core/config.py` M9 段补:`AGENT_RATE_LIMIT_PER_MIN: int = 60`。

`pyproject.toml` dependencies 追加 `"redis>=5",`,然后安装:
Run: `.venv\Scripts\python -m pip install "redis>=5"`

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `.venv\Scripts\python -m pytest tests/test_agent_ratelimit.py tests/test_agent_api.py -v && .venv\Scripts\python -m pytest -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_ratelimit.py backend/app/api/agent.py backend/app/core/config.py backend/pyproject.toml backend/tests/test_agent_ratelimit.py
git commit -m "feat(agent): redis sliding-window rate limit on agent endpoints"
```

---

### Task 6: MCP 面(/mcp,fastmcp Streamable HTTP + ASGI 鉴权中间件)

**Files:**
- Create: `backend/app/mcp_server.py`
- Modify: `backend/app/main.py`(挂载 + lifespan 组合)
- Modify: `backend/pyproject.toml`(dependencies 加 `"fastmcp>=2.3,<3"`;dev 加 `"asgi-lifespan>=2"`)
- Test: `backend/tests/test_mcp.py`

**Interfaces:**
- Consumes: Task 3 `current_principal/resolve_bearer_principal`;Task 4 `agent_facade.list_kbs_for/agent_search`;Task 5 `rate_allow`
- Produces:
  - `build_mcp_asgi_app()`(main.py 用;内部 `FastMCP("AIRag")` + `http_app(path="/", transport="streamable-http")` 包 `AgentAuthMiddleware`)
  - MCP tools:`list_knowledge_bases()`、`search_knowledge_base(kb_ids, query, top_k=RETRIEVAL_TOP_K, rerank=False)`(参数约束与 REST 相同;无权库返回 ToolError `kb_forbidden, denied_kb_ids=[...]`)
  - 端点 `POST /mcp`(Streamable HTTP);无/坏凭证 → HTTP 401;超限 → 429
  - `AGENT_API_ENABLED=false` 时 `/mcp` 不挂载、`/api/agent/*` 不注册

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_mcp.py
"""M9 Task6:MCP 面(原始 JSON-RPC over httpx ASGI + lifespan)。"""
import json

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.main import app

ACCEPT = "application/json, text/event-stream"
INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "t", "version": "0"}},
}


@pytest_asyncio.fixture
async def mcp_client(client):
    """client fixture 已 override get_db;再包 lifespan 以启动 mcp session manager。"""
    async with LifespanManager(app):
        yield client


def _rpc(method: str, params=None, msg_id=1):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


async def _init(c, headers):
    resp = await c.post("/mcp", json=INIT,
                        headers={**headers, "Accept": ACCEPT})
    assert resp.status_code == 200
    sid = resp.headers.get("mcp-session-id")
    await c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                 headers={**headers, "Accept": ACCEPT,
                          **({"mcp-session-id": sid} if sid else {})})
    return sid


def _tool_result(resp_json) -> dict:
    text = resp_json["result"]["content"][0]["text"]
    return json.loads(text)


async def test_mcp_401_without_key(mcp_client):
    resp = await mcp_client.post("/mcp", json=INIT,
                                 headers={"Accept": ACCEPT})
    assert resp.status_code == 401


async def test_mcp_401_bad_key(mcp_client):
    resp = await mcp_client.post(
        "/mcp", json=INIT,
        headers={"Accept": ACCEPT, "Authorization": "Bearer airag_bogus"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_key"


async def test_mcp_initialize_and_tools_list(mcp_client, auth_headers):
    from test_agent_api import _create_key

    key = await _create_key(mcp_client, auth_headers)
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    assert sid  # fastmcp streamable http 会下发会话 id
    resp = await mcp_client.post(
        "/mcp", json=_rpc("tools/list", {}, 2),
        headers={"Accept": ACCEPT, "Authorization": f"Bearer {key}",
                 "mcp-session-id": sid},
    )
    names = [t["name"] for t in resp.json()["result"]["tools"]]
    assert "list_knowledge_bases" in names and "search_knowledge_base" in names


async def test_mcp_tool_list_kbs(mcp_client, auth_headers):
    from test_agent_api import _create_kb, _create_key

    kb_id = await _create_kb(mcp_client, auth_headers, "MCP可见库")
    key = await _create_key(mcp_client, auth_headers)
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    resp = await mcp_client.post(
        "/mcp",
        json=_rpc("tools/call",
                  {"name": "list_knowledge_bases", "arguments": {}}, 3),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    body = _tool_result(resp.json())
    assert kb_id in [i["id"] for i in body["items"]]


async def test_mcp_tool_search(mcp_client, auth_headers, monkeypatch):
    from test_agent_api import _create_kb, _create_key
    from app.services.retrieval.searcher import SearchHit

    kb_id = await _create_kb(mcp_client, auth_headers, "MCP检索库")
    key = await _create_key(mcp_client, auth_headers)

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        return [SearchHit(chunk_id=1, document_id=10, kb_id=kb_id,
                          filename="a.pdf", page_no=1, content="命中",
                          score=0.9, source="both")]

    monkeypatch.setattr("app.services.agent_facade.hybrid_search", fake_hybrid)
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    resp = await mcp_client.post(
        "/mcp",
        json=_rpc("tools/call",
                  {"name": "search_knowledge_base",
                   "arguments": {"kb_ids": [kb_id], "query": "命中?"}}, 4),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    body = _tool_result(resp.json())
    assert body["total"] == 1 and "命中" in body["hits"][0]["content"]


async def test_mcp_tool_denied_kb(mcp_client, auth_headers):
    from test_agent_api import _create_key

    key = await _create_key(mcp_client, auth_headers)
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    resp = await mcp_client.post(
        "/mcp",
        json=_rpc("tools/call",
                  {"name": "search_knowledge_base",
                   "arguments": {"kb_ids": [99999], "query": "q"}}, 5),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    text = resp.json()["result"]["content"][0]["text"]
    assert "kb_forbidden" in text


async def test_mcp_rate_limited_429(mcp_client, auth_headers, monkeypatch):
    from test_agent_api import _create_key
    from app.core.config import settings
    from app.services import agent_ratelimit
    from tests.test_agent_ratelimit import FakeRedis

    key = await _create_key(mcp_client, auth_headers)
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 1)
    monkeypatch.setattr(agent_ratelimit, "get_redis", lambda: FakeRedis())
    hdr = {"Authorization": f"Bearer {key}", "Accept": ACCEPT}
    assert (await mcp_client.post("/mcp", json=INIT, headers=hdr)).status_code == 200
    resp = await mcp_client.post("/mcp", json=INIT, headers=hdr)
    assert resp.status_code == 429
    assert resp.json()["detail"]["code"] == "rate_limited"
```

注:`/mcp` 的 MCP 测试走的是独立于 `get_db` override 的 `SessionLocal` 会话(facade 内),测试数据经 REST/fixture 写入并 commit,因此可见;这是有意设计,与生产一致。

- [ ] **Step 2: 安装依赖并确认测试失败**

Run: `.venv\Scripts\python -m pip install "fastmcp>=2.3,<3" "asgi-lifespan>=2"`,并把两个依赖写进 `pyproject.toml`(dependencies:`"fastmcp>=2.3,<3"`;optional-dependencies.dev:`"asgi-lifespan>=2"`)。
Run: `.venv\Scripts\python -m pytest tests/test_mcp.py -v`
Expected: FAIL(404,`/mcp` 不存在)

- [ ] **Step 3: 实现 mcp_server + main 挂载**

```python
# backend/app/mcp_server.py
"""M9:MCP 面(Streamable HTTP)。鉴权/限流在 AgentAuthMiddleware(纯 ASGI),
主体经 core.deps.current_principal 传入;工具与 REST 共用 agent_facade。"""
import json
from dataclasses import asdict

from fastapi import HTTPException
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from app.core.config import settings
from app.core.deps import Principal, current_principal, resolve_bearer_principal
from app.db.session import SessionLocal
from app.services import agent_facade
from app.services.agent_ratelimit import allow as rate_allow
from app.services.audit import audit

mcp = FastMCP(name="AIRag")


def _principal() -> Principal:
    p = current_principal.get()
    if p is None:  # 中间件已拦截,此为防御
        raise ToolError("unauthorized: missing or invalid API key")
    return p


@mcp.tool
async def list_knowledge_bases() -> dict:
    """列出当前 API Key 归属用户有权访问的知识库。

    返回 {"items": [{"id", "name", "description", "my_perm"}]},
    my_perm ∈ viewer|editor|owner。检索前先调用本工具确认可用知识库 id。
    """
    p = _principal()
    async with SessionLocal() as db:
        items = await agent_facade.list_kbs_for(db, p.user)
        await audit(db, p.user.username, "agent.list_kbs", "agent",
                    {"client": "mcp", "key_name": p.key_name,
                     "kb_count": len(items)})
        await db.commit()
        return {"items": [asdict(i) for i in items]}


@mcp.tool
async def search_knowledge_base(
    kb_ids: list[int], query: str,
    top_k: int = settings.RETRIEVAL_TOP_K, rerank: bool = False,
) -> dict:
    """在指定知识库中混合检索(向量 + 关键词,RRF 融合)。

    Args:
        kb_ids: 知识库 id 列表(1~5 个),须为当前密钥有权访问的库。
        query: 检索问题,1~500 字。
        top_k: 命中条数上限,1~20,默认 8。
        rerank: 是否启用 rerank 重排(服务端未配置 rerank 时忽略)。

    Returns:
        {"hits": [{chunk_id, document_id, kb_id, filename, page_no,
        content, score, source}], "total", "elapsed_ms"}
    """
    p = _principal()
    if not 1 <= len(kb_ids) <= 5:
        raise ToolError("kb_ids must contain 1~5 ids")
    if not 1 <= len(query) <= 500:
        raise ToolError("query must be 1~500 chars")
    if not 1 <= top_k <= 20:
        raise ToolError("top_k must be 1~20")
    async with SessionLocal() as db:
        try:
            outcome = await agent_facade.agent_search(
                db, p.user, kb_ids, query, top_k, rerank,
            )
        except agent_facade.AgentKbDenied as e:
            raise ToolError(f"kb_forbidden, denied_kb_ids={e.denied_kb_ids}")
        await audit(db, p.user.username, "agent.search", "agent",
                    {"client": "mcp", "key_name": p.key_name, "kb_ids": kb_ids,
                     "query": query[:200], "hit_count": len(outcome.hits)})
        await db.commit()
        return {"hits": [asdict(h) for h in outcome.hits],
                "total": len(outcome.hits),
                "elapsed_ms": outcome.elapsed_ms}


class AgentAuthMiddleware:
    """纯 ASGI:解析 Bearer(airag_ key 或 JWT)→ current_principal;401/429 短路。"""

    def __init__(self, app):
        self.app = app

    @property
    def lifespan(self):
        # fastmcp http_app 的 lifespan 需在宿主启动期运行(session manager)
        return self.app.lifespan

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        authz = headers.get("authorization", "")
        principal = None
        if authz.lower().startswith("bearer "):
            raw = authz[7:].strip()
            async with SessionLocal() as db:
                try:
                    principal = await resolve_bearer_principal(db, raw)
                    await db.commit()  # last_used_at(MCP 工具另开会话)
                except HTTPException:
                    principal = None
        if principal is None:
            await _send_json(send, 401, {"detail": "not authenticated"})
            return
        if principal.kind == "api_key":
            ok, retry_after = await rate_allow(f"key:{principal.key_id}")
            if not ok:
                await _send_json(send, 429, {"detail": {
                    "code": "rate_limited", "retry_after": retry_after}})
                return
        token = current_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            current_principal.reset(token)


async def _send_json(send, status: int, body: dict) -> None:
    payload = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": payload})


def build_mcp_asgi_app():
    """path="/" 使挂载后完整端点恰为 /mcp(app.mount("/mcp", ...))。"""
    return AgentAuthMiddleware(
        mcp.http_app(path="/", transport="streamable-http")
    )
```

注:401 细分 code(`invalid_key` 等)在 `resolve_bearer_principal` 的 HTTPException 里;中间件如需透传 detail,把 `except HTTPException` 分支改为读取 `e.status_code/e.detail` 后 `_send_json(send, e.status_code, {"detail": e.detail})`(推荐,测试 `test_mcp_401_bad_key` 断言依赖它):

```python
                try:
                    principal = await resolve_bearer_principal(db, raw)
                    await db.commit()
                except HTTPException as e:
                    await _send_json(send, e.status_code, {"detail": e.detail})
                    return
```

(用这个版本,删掉上面 `principal = None` 的宽松分支。)

`main.py` 全文替换:

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.api import api_router
from app.core.config import settings


def create_app() -> FastAPI:
    mcp_asgi = None
    lifespan = None
    if settings.AGENT_API_ENABLED:
        from app.mcp_server import build_mcp_asgi_app

        mcp_asgi = build_mcp_asgi_app()
        # fastmcp session manager 依赖宿主 lifespan(fastmcp 官方 FastAPI 集成法)
        lifespan = lambda app: mcp_asgi.lifespan(app)  # noqa: E731
    app = FastAPI(title="AIRag API", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix="/api")
    if mcp_asgi is not None:
        app.mount("/mcp", mcp_asgi)
    return app


logger.add("logs/app.log", rotation="10 MB", retention=5, enqueue=True)

app = create_app()
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `.venv\Scripts\python -m pytest tests/test_mcp.py -v && .venv\Scripts\python -m pytest -q`
Expected: MCP 7 PASS;全量 PASS(注意:全量跑时 `client` fixture 不带 lifespan,MCP 用例自带 `mcp_client`,互不影响)

- [ ] **Step 5: 手工冒烟(可选但推荐,start_dev.bat 起 8001 后)**

```bash
curl -s -X POST http://127.0.0.1:8001/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{},\"clientInfo\":{\"name\":\"curl\",\"version\":\"0\"}}}"
```
Expected: 401 `{"detail":"not authenticated"}`;带 `-H "Authorization: Bearer airag_..."` 后返回 initialize result。

- [ ] **Step 6: Commit**

```bash
git add backend/app/mcp_server.py backend/app/main.py backend/pyproject.toml backend/tests/test_mcp.py
git commit -m "feat(agent): MCP streamable-http face at /mcp with ASGI auth"
```

---

### Task 7: 前端——API 密钥管理页

**Files:**
- Create: `frontend/src/api/keys.ts`
- Create: `frontend/src/pages/KeysPage.vue`
- Modify: `frontend/src/router/index.ts`(新增路由)
- Modify: `frontend/src/layouts/MainLayout.vue`(导航项 + Key 图标 import)
- Test: `frontend/src/pages/__tests__/KeysPage.spec.ts`

**Interfaces:**
- Consumes: Task 2 的三个端点;现有 `http/PageHeader/element-plus/tokens 主题`
- Produces:页面路由 `/keys`(name `keys`,title "API 密钥");`keysApi.list/create/revoke` 供页面与测试使用

- [ ] **Step 1: 写失败测试**

```typescript
// frontend/src/pages/__tests__/KeysPage.spec.ts
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus from 'element-plus'
import KeysPage from '@/pages/KeysPage.vue'
import { keysApi, type ApiKeyItem } from '@/api/keys'

vi.mock('@/api/keys', () => ({
  keysApi: {
    list: vi.fn<() => Promise<ApiKeyItem[]>>(),
    create: vi.fn(),
    revoke: vi.fn(),
  },
}))

const items: ApiKeyItem[] = [
  {
    id: 1, name: 'Cursor 工作机', key_prefix: 'airag_AbCdEf', is_active: true,
    expires_at: null, last_used_at: null, created_at: '2026-09-17T10:00:00',
  },
  {
    id: 2, name: '旧密钥', key_prefix: 'airag_XyZwVu', is_active: false,
    expires_at: null, last_used_at: null, created_at: '2026-09-16T10:00:00',
  },
]

const mountPage = () => mount(KeysPage, { global: { plugins: [ElementPlus] } })
// 精确匹配:页面同时存在「创建密钥」与对话框内「创建」,includes 会撞错按钮
const findBtn = (w: ReturnType<typeof mount>, text: string) =>
  w.findAll('button').find((b) => b.text().trim() === text)!

describe('KeysPage', () => {
  beforeEach(() => {
    vi.mocked(keysApi.list).mockResolvedValue(items)
    vi.mocked(keysApi.create).mockReset()
    vi.mocked(keysApi.revoke).mockReset()
  })

  it('renders key list with status tags', async () => {
    const w = mountPage()
    await flushPromises()
    expect(w.text()).toContain('Cursor 工作机')
    expect(w.text()).toContain('airag_AbCdEf')
    expect(w.text()).toContain('已吊销')
  })

  it('create flow shows one-time plaintext key', async () => {
    vi.mocked(keysApi.create).mockResolvedValue({
      ...items[0], id: 9, key: 'airag_OneTimeSecret123',
    })
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '创建密钥').trigger('click')
    await flushPromises()
    const input = w.find('input[placeholder="请输入密钥名称"]')
    await input.setValue('新密钥')
    await findBtn(w, '创建').trigger('click')
    await flushPromises()
    expect(keysApi.create).toHaveBeenCalledWith({
      name: '新密钥',
      expires_in_days: null,
    })
    await vi.waitFor(() => {
      expect(w.text()).toContain('airag_OneTimeSecret123')
    })
  })

  it('revoke asks confirm then calls api', async () => {
    vi.mocked(keysApi.revoke).mockResolvedValue(undefined)
    const w = mountPage()
    await flushPromises()
    // 第 1 行是活跃密钥 → 吊销按钮
    await findBtn(w, '吊销').trigger('click')
    await flushPromises()
    expect(keysApi.revoke).not.toHaveBeenCalled() // 等待确认框
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run(frontend 目录): `npx vitest run src/pages/__tests__/KeysPage.spec.ts`
Expected: FAIL(找不到 `@/pages/KeysPage.vue`、`@/api/keys`)

- [ ] **Step 3: 实现 api 模块 + 页面 + 路由 + 导航**

```typescript
// frontend/src/api/keys.ts
import http from './http'

/** API 密钥条目(列表;永不包含明文) */
export interface ApiKeyItem {
  id: number
  name: string
  key_prefix: string
  is_active: boolean
  expires_at: string | null
  last_used_at: string | null
  created_at: string
}

export interface ApiKeyCreatePayload {
  name: string
  expires_in_days?: number | null
}

/** 创建响应:唯一一次携带明文 key */
export interface ApiKeyCreated extends ApiKeyItem {
  key: string
}

export const keysApi = {
  async list(): Promise<ApiKeyItem[]> {
    const { data } = await http.get<ApiKeyItem[]>('/auth/keys')
    return data
  },
  async create(payload: ApiKeyCreatePayload): Promise<ApiKeyCreated> {
    const { data } = await http.post<ApiKeyCreated>('/auth/keys', payload)
    return data
  },
  async revoke(id: number): Promise<void> {
    await http.delete(`/auth/keys/${id}`)
  },
}
```

```vue
<!-- frontend/src/pages/KeysPage.vue -->
<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import PageHeader from '@/components/PageHeader.vue'
import { keysApi, type ApiKeyCreated, type ApiKeyItem } from '@/api/keys'

const loading = ref(false)
const items = ref<ApiKeyItem[]>([])

const dialogVisible = ref(false)
const submitting = ref(false)
const formRef = ref<FormInstance>()
const form = reactive({ name: '', expires: 'permanent' as 'permanent' | '7' | '30' | '90' })
const rules: FormRules = {
  name: [
    { required: true, message: '请输入密钥名称', trigger: 'blur' },
    { max: 64, message: '最长 64 字符', trigger: 'blur' },
  ],
}

const created = ref<ApiKeyCreated | null>(null)

const errMsg = (e: unknown, fallback: string) =>
  (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? fallback

async function load() {
  loading.value = true
  try {
    items.value = await keysApi.list()
  } catch (e) {
    ElMessage.error(errMsg(e, '密钥列表加载失败'))
  } finally {
    loading.value = false
  }
}

async function submit(formEl: FormInstance | undefined) {
  if (!formEl) return
  await formEl.validate()
  submitting.value = true
  try {
    created.value = await keysApi.create({
      name: form.name,
      expires_in_days:
        form.expires === 'permanent' ? null : Number(form.expires),
    })
    dialogVisible.value = false
    form.name = ''
    form.expires = 'permanent'
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e, '创建失败'))
  } finally {
    submitting.value = false
  }
}

async function revoke(row: ApiKeyItem) {
  try {
    await ElMessageBox.confirm(
      `确定吊销「${row.name}」?使用该密钥的 Agent 将立即失去访问权。`,
      '吊销密钥',
      { type: 'warning', confirmButtonText: '吊销', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await keysApi.revoke(row.id)
    ElMessage.success('已吊销')
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e, '吊销失败'))
  }
}

async function copyKey() {
  if (!created.value) return
  await navigator.clipboard.writeText(created.value.key)
  ElMessage.success('已复制到剪贴板')
}

const fmt = (s: string | null) => (s ? new Date(s).toLocaleString() : '—')

onMounted(load)
</script>

<template>
  <div class="page">
    <PageHeader title="API 密钥" description="供外部 Agent(MCP / REST)访问知识库的凭证,权限与你当前账号一致。">
      <template #actions>
        <el-button type="primary" @click="dialogVisible = true">创建密钥</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never" class="card">
      <el-table v-loading="loading" :data="items">
        <el-table-column prop="name" label="名称" min-width="140" />
        <el-table-column prop="key_prefix" label="前缀" min-width="140" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
              {{ row.is_active ? '活跃' : '已吊销' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="过期时间" min-width="160">
          <template #default="{ row }">{{ fmt(row.expires_at) }}</template>
        </el-table-column>
        <el-table-column label="最后使用" min-width="160">
          <template #default="{ row }">{{ fmt(row.last_used_at) }}</template>
        </el-table-column>
        <el-table-column label="创建时间" min-width="160">
          <template #default="{ row }">{{ fmt(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button v-if="row.is_active" link type="danger" @click="revoke(row)">
              吊销
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有密钥,创建一把给外部 Agent 使用" />
        </template>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" title="创建 API 密钥" width="480px">
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" maxlength="64" placeholder="请输入密钥名称" />
        </el-form-item>
        <el-form-item label="有效期">
          <el-radio-group v-model="form.expires">
            <el-radio value="7">7 天</el-radio>
            <el-radio value="30">30 天</el-radio>
            <el-radio value="90">90 天</el-radio>
            <el-radio value="permanent">永久</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit(formRef)">
          创建
        </el-button>
      </template>
    </el-dialog>

    <el-dialog :model-value="created !== null" title="密钥已创建" width="520px"
               :close-on-click-modal="false" @close="created = null">
      <el-alert type="warning" :closable="false" show-icon
                title="明文密钥仅展示这一次,关闭后无法再次查看。" class="alert" />
      <el-input :model-value="created?.key ?? ''" readonly class="key-box">
        <template #append>
          <el-button @click="copyKey">复制</el-button>
        </template>
      </el-input>
      <template #footer>
        <el-button type="primary" @click="created = null">我已保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--app-spacing-4, 16px);
}
.card {
  border-radius: var(--app-radius, 12px);
  border: 1px solid var(--app-card-border, #e5e7eb);
  background: var(--app-card-bg, #fff);
}
.alert {
  margin-bottom: 12px;
}
.key-box :deep(input) {
  font-family: monospace;
}
</style>
```

`router/index.ts` 在 `chat` 路由对象后追加:

```typescript
      {
        path: 'keys',
        name: 'keys',
        component: () => import('@/pages/KeysPage.vue'),
        meta: { title: 'API 密钥' },
      },
```

`MainLayout.vue`:图标 import 行加 `Key`;在"对话"菜单项后追加:

```html
      <el-menu-item index="/keys">
        <el-icon><Key /></el-icon>
        <span>API 密钥</span>
      </el-menu-item>
```

- [ ] **Step 4: 跑测试确认通过 + 类型检查**

Run: `npx vitest run src/pages/__tests__/KeysPage.spec.ts && npm run type-check`
Expected: 3 PASS;type-check 无错误

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/keys.ts frontend/src/pages/KeysPage.vue frontend/src/router/index.ts frontend/src/layouts/MainLayout.vue frontend/src/pages/__tests__/KeysPage.spec.ts
git commit -m "feat(frontend): api key management page with one-time reveal"
```

---

### Task 8: 配置样例、README 接入指南、无头验收脚本、全量收口

**Files:**
- Modify: `.env.example`(M9 三个键)
- Modify: `README.md`(追加《外部 Agent 接入(M9)》)
- Create: `backend/scripts/m9_acceptance.py`
- Test: 无新单测;以脚本 + 全量回归验收

**Interfaces:**
- Consumes: 前述全部任务;真实栈(start_dev.bat 的 8001 服务 + worker + Redis)
- Produces:验收报告(PASS/FAIL 汇总,退出码);文档

- [ ] **Step 1: .env.example 追加**

```ini
# M9 Agent 对外开放(REST /api/agent/* + MCP /mcp)
AGENT_API_ENABLED=true
AGENT_RATE_LIMIT_PER_MIN=60
AGENT_MAX_KEYS_PER_USER=10
```

- [ ] **Step 2: README 追加接入指南章节**

在 README.md 末尾追加(先读现有结构,标题层级与现有一致):

```markdown
## 外部 Agent 接入(M9)

知识库可通过 API Key 只读开放给外部 Agent(检索 + 知识库发现)。密钥在「API 密钥」页面创建,权限与创建者账号一致,可随时吊销。

### 1. 创建密钥
登录 Web → 左侧「API 密钥」→ 创建(明文只显示一次)。

### 2. MCP 客户端(Claude Code / Cursor / ZCode 等)
​```bash
claude mcp add --transport http airag http://<host>:8001/mcp --header "Authorization: Bearer airag_xxxx"
​```
可用工具:`list_knowledge_bases` / `search_knowledge_base`。

### 3. REST 客户端(Dify / Coze / 内部系统)
OpenAPI 文档:`http://<host>:8001/openapi.json`(tag `agent`)。
​```bash
# 知识库发现
curl -H "Authorization: Bearer airag_xxxx" http://127.0.0.1:8001/api/agent/kbs
# 混合检索
curl -X POST -H "Authorization: Bearer airag_xxxx" -H "Content-Type: application/json" \
  -d '{"kb_ids":[1],"query":"退货流程","top_k":8}' \
  http://127.0.0.1:8001/api/agent/search
​```

### 限流与审计
每密钥每分钟 `AGENT_RATE_LIMIT_PER_MIN`(默认 60)次;所有调用记入审计日志(action `agent.*`)。紧急关闭:`AGENT_API_ENABLED=false` 后重启。
```

(实际写入时去掉 ​ 转义符,正常三反引号。)

- [ ] **Step 3: 写无头验收脚本**

仿照 `scripts/m8_acceptance.py` 的 check/skip/summary 骨架与 `promote_roles`(一次性 NullPool 引擎直接 SQL,见 m8 脚本同款实现)写 `backend/scripts/m9_acceptance.py`:

```python
"""M9 无头验收脚本(真栈:http://127.0.0.1:8001 + worker + Redis)。

用法(backend 目录,项目 venv):
    .venv\\Scripts\\python scripts\\m9_acceptance.py

覆盖:key 生命周期(创建/列表/吊销)、agent REST(kbs 可见性/search 真检索/
403/401)、限流 429、MCP initialize(带/不带 key)。前置:start_dev.bat
已起服务(含 worker),Redis 可用。收尾清 DB(api_keys/chunks/documents/
kb_permissions/knowledge_bases/users FK 顺序)。
"""
import asyncio
import json
import time
import uuid

import httpx

BASE = "http://127.0.0.1:8001"
API = f"{BASE}/api"
TIMEOUT = httpx.Timeout(120.0)
RESULTS = {"pass": [], "fail": [], "skip": []}

ACCEPT = "application/json, text/event-stream"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "m9acc", "version": "0"}}}

FACT = "青鸾号高空气船的巡航升限为八千五百米"
FACT_Q = "青鸾号高空气船的巡航升限是多少米?"
SUFFIX = uuid.uuid4().hex[:6]


def check(name, cond, detail=""):
    (RESULTS["pass"] if cond else RESULTS["fail"]).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))


def summary_and_exit():
    total = sum(len(v) for v in RESULTS.values())
    print(f"\nM9 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
    if RESULTS["fail"]:
        print("FAILED:", *RESULTS["fail"], sep="\n  - ")
        raise SystemExit(1)


async def make_user(c: httpx.AsyncClient, role="editor"):
    name = f"m9_{SUFFIX}_{role}"
    r = await c.post(f"{API}/auth/register",
                     json={"username": name, "password": "secret123"})
    r.raise_for_status()
    # 提权(直连 DB 或走 admin 接口;此处复用 m8 的 NullPool SQL 方式)
    return name, r.json()["id"]


async def main():
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            health = await c.get(f"{API}/health")
            health.raise_for_status()
        except Exception as e:
            print(f"SKIP: 服务未启动({e});先跑 start_dev.bat")
            return

        # 1. 主用户 + 建库 + 上传文档 + 等待解析完成(同 m8 轮询 status=done)
        #    用户名唯一化:main_user = await make_user(c)
        #    login → jwt;POST /api/kbs 建库 "M9验收库";POST 文本文件(FACT 内容);
        #    GET /api/kbs/{id}/documents 轮询 status == "done"(上限 120s)。
        # 2. POST /api/auth/keys {"name":"验收key","expires_in_days":1} → key
        # 3. GET /api/agent/kbs (Bearer key) → items 含验收库
        # 4. POST /api/agent/search {"kb_ids":[id],"query":FACT_Q} → hits>=1 且
        #    任一 hit content 含 "八千五百米";elapsed_ms 字段存在
        # 5. 无 Authorization → 401;伪造 key → 401 detail=invalid_key
        # 6. 他人(第二个 make_user)+ 其 key 检索该库 → 403 kb_forbidden
        # 7. 限流:AGENT_RATE_LIMIT_PER_MIN 默认 60 → 连发 61 次 /api/agent/kbs,
        #    统计出现 429(detail.code == rate_limited)
        # 8. MCP:POST /mcp initialize 无凭证 → 401;带 key → 200 且含 serverInfo
        # 9. 吊销 key(DELETE /api/auth/keys/{id})→ /api/agent/kbs 401 key_revoked
        # 10. 清理:直连 DB 删 api_keys/chunks/documents/kb_permissions/
        #     knowledge_bases/本次 users(FK 顺序,m8 promote_roles 同款 NullPool 引擎)
        ...
    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
```

注:骨架中带编号注释的 10 步为**实现要求**,写脚本时逐步落实为真实代码(轮询、断言、清理),不得留 `...` 提交;`promote_roles`/清库直接照抄 `m8_acceptance.py` 的 NullPool SQL 惯例。

- [ ] **Step 4: 全量回归 + 验收执行**

Run(backend):`.venv\Scripts\python -m pytest -q`
Run(frontend):`npx vitest run`
Run(验收,需先 start_dev.bat 且 Redis/worker 在跑):`.venv\Scripts\python scripts\m9_acceptance.py`
Expected: 三项全绿;验收输出 `M9 ACCEPTANCE: N/N PASS`

- [ ] **Step 5: 用户走查(spec 验收门)**

由用户在真客户端走一遍(执行者准备好密钥后交给用户):Claude Code/ZCode `claude mcp add --transport http airag http://127.0.0.1:8001/mcp --header "Authorization: Bearer airag_xxx"` → 调 `list_knowledge_bases`、`search_knowledge_base`;前端「API 密钥」页创建/吊销一轮。走查通过才算 M9 收官。

- [ ] **Step 6: Commit**

```bash
git add .env.example README.md backend/scripts/m9_acceptance.py
git commit -m "docs(agent): agent integration guide, env sample and m9 acceptance script"
```

---

## 任务依赖图

```
T1 模型/迁移 ─► T2 管理端点 ─► T3 Principal+/agent/kbs ─► T4 /agent/search ─► T6 MCP(依赖 T3/T4/T5)
                                                    └► T5 限流 ──────────────┘
T7 前端(依赖 T2 的端点,可与 T4-T6 并行)
T8 文档/验收(最后)
```

## 风险与执行注意

- **fastmcp 版本面**:仅用 `FastMCP/@mcp.tool/http_app(path,transport)` 三个 API + 官方 FastAPI 挂载法;若 `http_app` 参数名随版本变化(如 `stateless_http`),以 `pip show fastmcp` 版本对照官方文档调整,**不得**把协议库引进 facade。中间件 `lifespan` property 依赖 fastmcp http_app 返回值带 `.lifespan`(官方文档保证);若安装版本 AttributeError,退回 `self.app.router.lifespan_context`。
- **MCP 测试必须带 lifespan**(`asgi_lifespan`),否则 fastmcp session manager 未启动会 500/挂起。
- **`last_used_at` 持久化依赖路由 commit**;新增 agent 端点必须 `await db.commit()`(审计同理)。
- **Windows**:一切后端命令走 `.venv\Scripts\python`;服务 8001;不要用裸 uvicorn 起服务(Proactor/checkpointer 问题,M9 不新增该风险)。
- Task 2 的 `model_validate(..., update=)` 若报错,按任务内备注改为手工构造。
