# AIRag 企业知识库实施计划(主计划:M1 详细任务 + M2~M5 路线图)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按定版设计文档从零搭建企业内部 RAG 知识库(文档上传 → 解析入库 → 混合检索 → LangGraph 流式问答 + 引用溯源),本文件含全项目执行协议、里程碑路线图,以及 M1(骨架/存储/认证)的全部可执行任务。

**Architecture:** FastAPI 异步后端 + PostgreSQL 16/pgvector 一库承载元数据/权限/向量/全文;Celery 做文档流水线;LangGraph 只编排问答链路;Vue3 + Element Plus 前端经 REST+SSE 交互。开发机原生运行(无 Docker);生产用 Docker Compose(容器化属后续部署里程碑)。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.0 async / Alembic / PyJWT / bcrypt / pgvector / Celery+Redis(M2)/ LangGraph(M3)/ Vue3 + TS + Vite + Element Plus + Pinia + axios / pytest / Vitest。

**Spec:** `docs/AIRag-AI知识库需求设计方案.md`(2026-09-14 选型定版版)——本计划由该文档推导,执行者必须同时阅读两份文件。

## Global Constraints

- 开发机 Windows,CMD shell;所有命令必须 CMD 兼容(`rmdir /s /q`、反斜杠路径)。**开发环境原生运行,不依赖 Docker/WSL**:PostgreSQL 16 原生安装 + pgvector 手工编译(一次性,见下方『环境准备』);venv 必须用 `py -3.12 -m venv .venv` 创建(PATH 上默认 python 是 3.11)。
- 版本下限:Python 3.12+、Node 20+、pnpm 9+;依赖小版本在脚手架任务中取当期最新稳定版。
- 端口约定:后端 8000、前端 5173、PG 原生 localhost:5432(若 5432 被本机其他 PG 占用,安装时改 5433 并同步 config.py 默认值)。Redis(M2)的 Windows 运行方案届时另定,M1 不涉及。
- 数据库名:`airag`(运行)/ `airag_test`(测试);PG 镜像 `pgvector/pgvector:pg16`;**zhparser 延后到 M3**(M1/M2 只建 tsv 列,用默认 `simple` 分词,不建中文分词配置)。
- API 统一前缀 `/api`;认证用 Bearer JWT(HS256,默认 60 分钟);密码哈希用 bcrypt(直接用 `bcrypt` 库,**不用 passlib**——规避其与 bcrypt≥4.1 的兼容问题,与设计文档功能等价)。
- M1 注册用户角色一律 `role='admin'`(bootstrap 简化;M4 做 RBAC 时收紧)。
- `.env` 永不入库;每新增一个环境变量必须同步 `.env.example`。
- 提交纪律:conventional commits(`feat:`/`test:`/`chore:`),每个绿灯测试后必须 commit。
- 测试数据库连接串:`postgresql+asyncpg://airag:airag_dev_password@localhost:5432/airag_test`(角色与密码在『环境准备』统一创建)。
- 文档结构与命名遵循 Spec §8(backend/app/..., frontend/src/...)。

## 环境准备(一次性,人工执行;启动 Task 前完成)

1. **安装 PostgreSQL 16(EDB 安装器)**:官网 https://www.enterprisedb.com/downloads/postgres-postgresql-downloads ,默认目录 `C:\Program Files\PostgreSQL\16`,记住 postgres 超级用户密码,端口保持 5432。
2. **安装 Visual Studio 2022 Build Tools**(编译 pgvector 需要 MSVC):官网或 winget 安装,工作负载勾选"使用 C++ 的桌面开发"(含 MSVC v143 + Windows SDK)。
3. **编译安装 pgvector**(在"×64 Native Tools Command Prompt for VS 2022"专用命令行中执行;本机 GitHub 克隆偶发中断,失败直接重试):

   ```cmd
   set PGROOT=C:\Program Files\PostgreSQL\16
   cd /d E:\Projects
   git clone https://github.com/pgvector/pgvector.git
   cd pgvector
   git checkout <最新 release tag>
   nmake /F Makefile.win
   nmake /F Makefile.win install
   ```

4. **建角色建库开扩展**(普通 CMD;提示时输入 postgres 密码):

   ```cmd
   "C:\Program Files\PostgreSQL\16\bin\psql" -U postgres -h localhost -c "CREATE ROLE airag LOGIN PASSWORD 'airag_dev_password';"
   "C:\Program Files\PostgreSQL\16\bin\psql" -U postgres -h localhost -c "CREATE DATABASE airag OWNER airag;"
   "C:\Program Files\PostgreSQL\16\bin\psql" -U postgres -h localhost -c "CREATE DATABASE airag_test OWNER airag;"
   "C:\Program Files\PostgreSQL\16\bin\psql" -U airag -d airag -h localhost -c "CREATE EXTENSION IF NOT EXISTS vector;"
   "C:\Program Files\PostgreSQL\16\bin\psql" -U airag -d airag_test -h localhost -c "CREATE EXTENSION IF NOT EXISTS vector;"
   ```

5. **验证就绪**:

   ```cmd
   "C:\Program Files\PostgreSQL\16\bin\psql" -U airag -d airag -h localhost -c "SELECT extname FROM pg_extension WHERE extname='vector';"
   ```

   返回一行 `vector` 即环境就绪(airag_test 库同样执行一次)。

## 执行协议(跨会话长任务怎么自动接力)

1. 执行会话开头读两份文件:Spec(设计文档)+ 本计划;然后从**第一个未勾选的步骤**继续,不重做已完成任务。
2. 逐任务、逐步骤执行,完成一步勾一步(`- [ ]` → `- [x]`)并保存文件。
3. TDD 纪律:先写失败测试、亲眼看它失败、再写最小实现、看它通过。**禁止跳过红灯步骤,禁止删除或弱化测试**。
4. 任何步骤失败:使用 superpowers:systematic-debugging 找根因,修复后重跑该步骤;不允许带病推进。
5. 每个任务结束有 commit 步骤,必须执行。
6. 里程碑交接:M1 的 Task 12 会生成 M2 的详细计划文件(本目录下 `*-m2-*.md`)并把路线图指向它;此后新会话以该文件为执行入口。M2→M3→M4→M5 同理。
7. 执行方式二选一(由用户在启动执行时指定):superpowers:subagent-driven-development(推荐,每任务派新子代理+两段评审)或 superpowers:executing-plans(本会话内分批执行+检查点)。
8. GitHub 同步:远程仓库 `https://github.com/yu-xiao/AIRAG.git`(2026-09-14 用户提供)。每个里程碑完成、分支合并回 main 后推送一次 `git push origin main`;M1 的首次推送见 Task 12 Step 6。凭据走系统级 Git Credential Manager——首次推送会弹浏览器登录,用户完成一次即缓存。本机访问 GitHub 偶发网络抖动,推送失败先重试再排查。
9. **环境未就绪时的并行路径**:若『环境准备』尚未完成,可先执行不依赖数据库的任务:Task 1 → Task 2(Step 1~2 及 venv/依赖安装;Step 3 的 psql 验证推迟到环境就绪后补做)→ Task 10 → Task 11(前端按 API 契约编程,store 测试已 mock,不需后端运行)。**Task 3 起必须等数据库就绪**——TDD 红绿循环需要真实 PG,不允许跳过测试先行堆码。

## 里程碑路线图(M2~M5 概要,进入时生成详细计划)

| 里程碑 | 范围(详见 Spec §9) | 验收标准 | 详细计划 |
|---|---|---|---|
| **M1 骨架与存储层** | 脚手架 + 全部数据模型/Alembic + JWT 注册登录 + 前端登录闭环(原生运行) | 本地三件套(PG 服务+后端+前端)起,能注册登录进首页 | **本文件 Task 1~12** |
| **M2 文档流水线** | 上传 API+状态机;PDF/docx/xlsx Parser 插件;切块;Embedding Provider(智谱);Celery 流水线+重试(届时定 Windows 的 Redis 方案) | 三种格式上传后状态流转到 done,chunks 可查 | M1 完成后生成 |
| **M3 检索与问答** | 混合检索(向量+zhparser 全文)+RRF;Rerank 开关;LangGraph 三节点图+PostgresSaver+断连语义;SSE 问答 API+引用;前端知识库/文档/对话三页面 | 端到端:上传→提问→流式回答+引用,3~5 份真实文档验收 | M2 完成后生成 |
| **M4 企业化完善** | RBAC(viewer/editor/admin)+权限界面;失败重试/重新解析;问答历史;Rerank 开关界面 | 权限隔离生效,检索质量达标 | M3 完成后生成 |
| **M5 增强(按需)** | MinerU/OCR;RAGAS 评估集;导出/审计;agentic 扩展(图上加节点) | 复杂文档解析可用 | M4 完成后生成 |

**已知开放问题(Spec §11,不阻塞 M1):** LDAP/SSO 对接(影响 M4)、生产机规格(影响 pgvector 调优)、智谱 API key(M2 前必须申请)。

## M1 文件结构总览(本里程碑创建的文件)

```
backend/
  pyproject.toml                      # 依赖与 pytest 配置
  start_dev.bat                       # 迁移+热重载一键启动
  app/main.py                         # create_app + CORS + loguru + 路由挂载
  app/core/config.py                  # Settings(pydantic-settings)
  app/core/security.py                # bcrypt 哈希 + JWT 签发/校验
  app/core/deps.py                    # get_current_user
  app/db/session.py                   # engine + get_db
  app/models/{base,user,knowledge_base,document,chat}.py + __init__.py
  app/schemas/auth.py                 # RegisterIn/UserOut/TokenOut
  app/api/{__init__,auth}.py          # api_router 聚合
  alembic/{env.py,versions/} + alembic.ini
  tests/{conftest.py,test_health.py,test_auth.py}
frontend/
  vite.config.ts                      # @ 别名 + /api 代理
  src/main.ts                         # Pinia/Router/ElementPlus
  src/api/{http.ts,auth.ts}
  src/stores/auth.ts
  src/router/index.ts                 # 路由 + 登录守卫
  src/layouts/MainLayout.vue
  src/pages/{LoginPage.vue,HomePage.vue}
  tests/unit/auth.store.spec.ts       (create-vue 的 vitest 目录结构)
.env.example / .gitignore / README.md(生产用 docker-compose.yml/Dockerfile 推迟到部署里程碑再创建)
```

---

### Task 1: 仓库初始化与项目骨架

**Files:**
- Create: `.gitignore`, `.env.example`, `README.md`, `docs/`(已存在,不覆盖)
- Create: 空目录 `backend/app/{core,models,schemas,api,services,workers}`、`backend/alembic/versions/`、`backend/tests/`

**Interfaces:**
- Produces: git 仓库(main 分支)、根目录 `.env.example`(后续任务的 env 模板源头)

- [ ] **Step 1: 初始化 git 仓库并建目录**

```cmd
cd /d E:\Projects\AIRag
git init -b main
mkdir backend\app\core backend\app\models backend\app\schemas backend\app\api backend\app\services backend\app\workers backend\alembic\versions backend\tests
```

- [ ] **Step 2: 写 `.gitignore`**

```gitignore
.venv/
__pycache__/
*.pyc
.pytest_cache/
.env
logs/
node_modules/
dist/
pnpm-debug.log*
```

- [ ] **Step 3: 写 `.env.example`**

```env
JWT_SECRET=please-change-me-32-chars-minimum
JWT_EXPIRE_MINUTES=60
ZHIPU_API_KEY=
```

(端口/DATABASE_URL 由 config.py 默认值固定为 localhost:5432,不入 env,减少 M1 配置面。)

- [ ] **Step 4: 写 `README.md` 骨架**

```markdown
# AIRag 企业知识库

设计文档:docs/AIRag-AI知识库需求设计方案.md
实施计划:docs/superpowers/plans/2026-09-14-airag-implementation-plan.md

## 开发启动(M1 起,原生运行,无需 Docker)

copy .env.example .env
cd backend
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
start_dev.bat   # 迁移 + 热重载,后端 http://localhost:8000/docs
cd ..\frontend && pnpm install && pnpm dev   # 前端 http://localhost:5173
```

- [ ] **Step 5: 首次提交并验证**

```cmd
git add -A
git commit -m "chore: init repo skeleton with gitignore/env template/readme"
git log --oneline
```

Expected: 输出 1 条提交记录。

---

### Task 2: 后端依赖与配置模块

**Files:**
- Create: `backend/pyproject.toml`, `backend/app/core/__init__.py`(空), `backend/app/core/config.py`, 其余 `__init__.py`

**Interfaces:**
- Produces: `app.core.config.settings`(字段见代码,后续所有模块从它读配置);数据库按『环境准备』节已就绪

- [ ] **Step 1: 写 `backend/pyproject.toml`**

```toml
[project]
name = "airag-backend"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "sqlalchemy[asyncio]>=2.0.30",
    "asyncpg>=0.29",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "alembic>=1.13",
    "pgvector>=0.3",
    "pyjwt>=2.8",
    "bcrypt>=4.1",
    "loguru>=0.7",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "httpx>=0.27"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["app*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 2: 写 `backend/app/core/config.py` 并补齐各层 `__init__.py`**

`config.py`:

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    DATABASE_URL: str = (
        "postgresql+asyncpg://airag:airag_dev_password@localhost:5432/airag"
    )
    TEST_DATABASE_URL: str = (
        "postgresql+asyncpg://airag:airag_dev_password@localhost:5432/airag_test"
    )
    REDIS_URL: str = "redis://localhost:6379/0"  # M2 才使用,Windows 运行方案届时定
    JWT_SECRET: str = "dev-secret-change-me"
    JWT_EXPIRE_MINUTES: int = 60


settings = Settings()
```

CMD 建立 `__init__.py`:

```cmd
cd /d E:\Projects\AIRag\backend
type nul > app\__init__.py & type nul > app\core\__init__.py & type nul > app\models\__init__.py & type nul > app\schemas\__init__.py & type nul > app\api\__init__.py & type nul > app\services\__init__.py & type nul > app\workers\__init__.py & type nul > tests\__init__.py
```

- [ ] **Step 3: 建 venv、装依赖,验证数据库连通**

```cmd
cd /d E:\Projects\AIRag\backend
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
cd /d E:\Projects\AIRag
copy .env.example .env
```

(必须用 `py -3.12`:PATH 上默认 python 是 3.11,直接 `python -m venv` 会违反 requires-python>=3.12。)

确认 PG Windows 服务在运行(默认服务名 `postgresql-x64-16`)且『环境准备』已完成,然后验证连通与扩展:

```cmd
"C:\Program Files\PostgreSQL\16\bin\psql" -U airag -d airag -h localhost -c "SELECT extname FROM pg_extension WHERE extname='vector';"
```

Expected: 返回一行 `vector`;对 `-d airag_test` 再执行一次,同样返回 `vector`。

- [ ] **Step 4: 提交**

```cmd
git add -A
git commit -m "chore: backend deps and settings with native pg connection"
```

---

### Task 3: FastAPI 入口与 /health(测试基建先行)

**Files:**
- Create: `backend/app/main.py`, `backend/tests/conftest.py`, `backend/tests/test_health.py`

**Interfaces:**
- Produces: `app.main:app`(FastAPI 实例);测试夹具 `client`(httpx AsyncClient,已注入依赖覆盖);路由挂载点 `app.api.api_router`
- Consumes: Task 2 的 settings

- [ ] **Step 1: 写失败测试 `tests/test_health.py`**

```python
async def test_health(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 2: 写 `tests/conftest.py`**

```python
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import get_db
from app.main import app
from app.core.config import settings
from app.models import *  # noqa: F401,F403 — 确保所有模型注册进 metadata
from app.models.base import Base

engine = create_async_engine(settings.TEST_DATABASE_URL)
TestSession = async_sessionmaker(engine, expire_on_commit=False)

CLEANUP_ORDER = [
    "messages",
    "conversations",
    "chunks",
    "documents",
    "kb_permissions",
    "knowledge_bases",
    "users",
]


@pytest_asyncio.fixture(scope="session", autouse=True)
async def prepare_db():
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables():
    yield
    async with engine.begin() as conn:
        for table in CLEANUP_ORDER:
            await conn.execute(text(f"DELETE FROM {table}"))


@pytest_asyncio.fixture
async def db_session():
    async with TestSession() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_session):
    async def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
```

注:conftest 引用了尚不存在的 `app.db.session` 与 `app.main`——这正是 Step 2 期望失败的原因。

- [ ] **Step 3: 运行确认失败**

```cmd
cd /d E:\Projects\AIRag\backend
.venv\Scripts\python -m pytest tests\test_health.py -v
```

Expected: FAIL/ERROR(`ModuleNotFoundError: app.main` 或类似)。

- [ ] **Step 4: 最小实现 `app/db/session.py` 与 `app/main.py`**

`app/db/session.py`:

```python
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
```

(同时 `type nul > app\db\__init__.py`)

`app/api/__init__.py`(路由聚合点):

```python
from fastapi import APIRouter

api_router = APIRouter()


@api_router.get("/health")
async def health() -> dict:
    return {"status": "ok"}
```

`app/main.py`:

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.api import api_router


def create_app() -> FastAPI:
    app = FastAPI(title="AIRag API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix="/api")
    return app


logger.add("logs/app.log", rotation="10 MB", retention=5, enqueue=True)

app = create_app()
```

- [ ] **Step 5: 运行确认通过**

```cmd
.venv\Scripts\python -m pytest tests\test_health.py -v
```

Expected: `1 passed`(conftest 的 drop_all/create_all 会在 airag_test 上建空表集——此刻 metadata 为空,不报错即可)。

- [ ] **Step 6: 提交**

```cmd
git add -A
git commit -m "feat: fastapi app skeleton with /api/health and async test harness"
```

---

### Task 4: 全量业务数据模型(Spec §5 七张表)

**Files:**
- Create: `backend/app/models/base.py`, `user.py`, `knowledge_base.py`, `document.py`, `chat.py`;改 `app/models/__init__.py`

**Interfaces:**
- Produces: `Base`( declarative_base,含 TSVector 自定义类型)、`User`、`KnowledgeBase`、`KbPermission`、`Document`、`Chunk`、`Conversation`、`Message`;字段以代码为准,后续任务/M2/M3 直接复用
- Consumes: pgvector 的 `Vector` 类型

- [ ] **Step 1: 写失败测试 `tests/test_models.py`**

```python
from sqlalchemy import inspect

from app.models.base import Base


async def test_all_spec_tables_exist(prepare_db):
    names = set(inspect(Base.metadata).tables.keys())
    expected = {
        "users",
        "knowledge_bases",
        "kb_permissions",
        "documents",
        "chunks",
        "conversations",
        "messages",
    }
    assert expected <= names
```

- [ ] **Step 2: 运行确认失败**

```cmd
.venv\Scripts\python -m pytest tests\test_models.py -v
```

Expected: FAIL(集合为空/缺表)。

- [ ] **Step 3: 实现模型**

`app/models/base.py`:

```python
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import UserDefinedType


class TSVector(UserDefinedType):
    def get_col_spec(self) -> str:
        return "TSVECTOR"


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

`app/models/user.py`:

```python
from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
```

`app/models/knowledge_base.py`:

```python
from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class KnowledgeBase(Base, TimestampMixin):
    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    embed_provider: Mapped[str] = mapped_column(String(32), default="zhipu")
    embed_model: Mapped[str] = mapped_column(String(64), default="embedding-3")

    permissions: Mapped[list["KbPermission"]] = relationship(back_populates="kb")


class KbPermission(Base):
    __tablename__ = "kb_permissions"
    __table_args__ = (UniqueConstraint("kb_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    perm: Mapped[str] = mapped_column(String(16), default="viewer")  # viewer|editor

    kb: Mapped[KnowledgeBase] = relationship(back_populates="permissions")
```

`app/models/document.py`:

```python
from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, TSVector


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    filename: Mapped[str] = mapped_column(String(256))
    file_path: Mapped[str] = mapped_column(String(512))
    mime: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    # pending|parsing|chunking|embedding|done|failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    error_msg: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    page_no: Mapped[int | None] = mapped_column(Integer)
    char_len: Mapped[int] = mapped_column(Integer)
    embedding = mapped_column(Vector(1024))  # 嵌入前为 NULL
    tsv = mapped_column(TSVector)  # M1 仅建列,M3 换 zhparser 配置并生成
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
```

`app/models/chat.py`:

```python
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kb_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer))
    title: Mapped[str] = mapped_column(String(128), default="新对话")


class Message(Base, TimestampMixin):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user|assistant
    content: Mapped[str] = mapped_column(Text)
    citations = mapped_column(JSONB)  # [{document_id,page_no,excerpt}]
```

`app/models/__init__.py`:

```python
from app.models.base import Base
from app.models.user import User
from app.models.knowledge_base import KnowledgeBase, KbPermission
from app.models.document import Chunk, Document
from app.models.chat import Conversation, Message

__all__ = [
    "Base",
    "User",
    "KnowledgeBase",
    "KbPermission",
    "Document",
    "Chunk",
    "Conversation",
    "Message",
]
```

- [ ] **Step 4: 运行确认通过**

```cmd
.venv\Scripts\python -m pytest tests\test_models.py tests\test_health.py -v
```

Expected: `2 passed`(conftest 的 session 级 prepare_db 每次会话首跑都会 drop_all/create_all 重建全部表,无需手动清库)。

- [ ] **Step 5: 提交**

```cmd
git add -A
git commit -m "feat: full domain models for users/kb/documents/chunks/conversations"
```

---

### Task 5: Alembic(async)接入与初始迁移

**Files:**
- Create: `backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/script.py.mako`(模板生成), `backend/alembic/versions/xxx_init_all_tables.py`

**Interfaces:**
- Produces: `alembic upgrade head` 可在任意环境建全量表(含 vector 扩展);M2+ 的迁移在此基础上累加

- [ ] **Step 1: 初始化 alembic async 模板**

```cmd
cd /d E:\Projects\AIRag\backend
.venv\Scripts\python -m alembic init -t async alembic
```

注:Task 1 已预建 `alembic\versions\` 空目录,init 遇到已存在目录会报错,先删掉这两层空目录再执行:`rmdir alembic\versions && rmdir alembic`。

- [ ] **Step 2: 改 `alembic/env.py`(替换模板的 placeholder 段)**

在文件顶部 `from alembic import context` 之后加入 imports:

```python
import app.models  # noqa: F401 — 注册全部模型
from app.core.config import settings
from app.models.base import Base
```

找到 `config = context.config` 行,在其**之后**插入:

```python
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
```

并将文件中 `target_metadata = None` 改为:

```python
target_metadata = Base.metadata
```

- [ ] **Step 3: 生成并修补初始迁移**

```cmd
.venv\Scripts\python -m alembic revision --autogenerate -m "init all tables"
```

打开生成的 `alembic\versions\*_init_all_tables.py`,在 `def upgrade()` 的**第一行**(`op.create_table` 之前)插入:

```python
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
```

- [ ] **Step 4: 对 airag_test 库执行迁移并验证往返**

说明:env.py 读的是 `settings.DATABASE_URL`(默认指向 localhost:5432 的 `airag` 运行库)。本步骤验证用测试库,临时覆盖环境变量:

```cmd
set DATABASE_URL=postgresql+asyncpg://airag:airag_dev_password@localhost:5432/airag_test
.venv\Scripts\python -m alembic upgrade head
.venv\Scripts\python -m alembic current
.venv\Scripts\python -m alembic downgrade base
.venv\Scripts\python -m alembic upgrade head
set DATABASE_URL=
```

Expected: `current` 显示 init_all_tables head;downgrade/upgrade 往返无错。

```cmd
"C:\Program Files\PostgreSQL\16\bin\psql" -U airag -d airag_test -h localhost -c "\dt"
```

Expected: 列出 7 张业务表 + alembic_version。

- [ ] **Step 5: 提交**

```cmd
git add -A
git commit -m "feat: alembic async setup with initial migration for all tables"
```

---

### Task 6: 密码/JWT 安全模块 + 注册接口

**Files:**
- Create: `backend/app/core/security.py`, `backend/app/schemas/auth.py`, `backend/app/api/auth.py`;改 `app/api/__init__.py`
- Test: `backend/tests/test_auth.py`

**Interfaces:**
- Produces: `hash_password(password: str) -> str`、`verify_password(password: str, password_hash: str) -> bool`、`create_access_token(user_id: int) -> str`、`decode_access_token(token: str) -> dict | None`;`POST /api/auth/register`(201→UserOut,409 重名,422 校验);Schema:`UserOut{id,username,role,is_active}`
- Consumes: Task 4 的 `User`、Task 3 的 `get_db`

- [ ] **Step 1: 写失败测试 `tests/test_auth.py`**

```python
import uuid


def _username() -> str:
    return f"user_{uuid.uuid4().hex[:8]}"


async def test_register_success(client):
    resp = await client.post(
        "/api/auth/register", json={"username": _username(), "password": "secret123"}
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] > 0
    assert data["role"] == "admin"
    assert "password_hash" not in data


async def test_register_duplicate_username(client):
    name = _username()
    await client.post(
        "/api/auth/register", json={"username": name, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/register", json={"username": name, "password": "secret123"}
    )
    assert resp.status_code == 409


async def test_register_password_too_short(client):
    resp = await client.post(
        "/api/auth/register", json={"username": _username(), "password": "123"}
    )
    assert resp.status_code == 422
```

- [ ] **Step 2: 运行确认失败**

```cmd
.venv\Scripts\python -m pytest tests\test_auth.py -v
```

Expected: FAIL(404 Not Found,路由不存在)。

- [ ] **Step 3: 实现三个文件**

`app/core/security.py`:

```python
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_access_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc)
        + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
```

`app/schemas/auth.py`:

```python
from pydantic import BaseModel, Field


class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    password: str = Field(min_length=8, max_length=64)


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool

    model_config = {"from_attributes": True}
```

`app/api/auth.py`:

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.session import get_db
from app.models import User
from app.schemas.auth import RegisterIn, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=201)
async def register(payload: RegisterIn, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == payload.username))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="username already exists")
    user = User(
        username=payload.username, password_hash=hash_password(payload.password)
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user
```

`app/api/__init__.py` 追加路由挂载(health 路由保持不变):

```python
from app.api.auth import router as auth_router

api_router.include_router(auth_router)
```

- [ ] **Step 4: 运行确认通过**

```cmd
.venv\Scripts\python -m pytest tests\test_auth.py -v
```

Expected: `3 passed`。

- [ ] **Step 5: 提交**

```cmd
git add -A
git commit -m "feat: bcrypt+jwt security module and register endpoint"
```

---

### Task 7: 登录接口

**Files:**
- Modify: `backend/app/schemas/auth.py`(加 TokenOut)、`backend/app/api/auth.py`(加 login)
- Test: `backend/tests/test_auth.py`(追加)

**Interfaces:**
- Produces: `POST /api/auth/login` → 200 `{"access_token": "...", "token_type": "bearer"}`;错误凭证 401
- Consumes: Task 6 的 `verify_password`/`create_access_token`

- [ ] **Step 1: 追加失败测试**

```python
async def test_login_success(client):
    name = _username()
    await client.post(
        "/api/auth/register", json={"username": name, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": name, "password": "secret123"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


async def test_login_wrong_password(client):
    name = _username()
    await client.post(
        "/api/auth/register", json={"username": name, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": name, "password": "wrongpass1"}
    )
    assert resp.status_code == 401
```

- [ ] **Step 2: 运行确认失败**

```cmd
.venv\Scripts\python -m pytest tests\test_auth.py -v
```

Expected: 新增 2 条 FAIL。

- [ ] **Step 3: 实现**

`schemas/auth.py` 追加:

```python
class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
```

`api/auth.py` 追加(并 `from app.schemas.auth import LoginIn, TokenOut`,同时 import `verify_password, create_access_token`):

```python
@router.post("/login", response_model=TokenOut)
async def login(payload: LoginIn, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="user disabled")
    return TokenOut(access_token=create_access_token(user.id))
```

- [ ] **Step 4: 运行确认通过**

```cmd
.venv\Scripts\python -m pytest tests\test_auth.py -v
```

Expected: `5 passed`。

- [ ] **Step 5: 提交**

```cmd
git add -A
git commit -m "feat: login endpoint issuing jwt access token"
```

---

### Task 8: get_current_user 依赖与 /auth/me

**Files:**
- Create: `backend/app/core/deps.py`;Modify: `backend/app/api/auth.py`
- Test: `backend/tests/test_auth.py`(追加)

**Interfaces:**
- Produces: `get_current_user`(AsyncSession 依赖,返回 `User`,无效/过期 token → 401);`GET /api/auth/me` → UserOut。**所有后续受保护路由都复用此依赖**
- Consumes: Task 6/7 的 token 体系

- [ ] **Step 1: 追加失败测试**

```python
async def _register_and_login(client, username: str) -> str:
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    return resp.json()["access_token"]


async def test_me_without_token(client):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


async def test_me_with_token(client):
    name = _username()
    token = await _register_and_login(client, name)
    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["username"] == name


async def test_me_with_garbage_token(client):
    resp = await client.get(
        "/api/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert resp.status_code == 401
```

- [ ] **Step 2: 运行确认失败**

```cmd
.venv\Scripts\python -m pytest tests\test_auth.py -v
```

Expected: 新增 3 条 FAIL(404)。

- [ ] **Step 3: 实现**

`app/core/deps.py`:

```python
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import get_db
from app.models import User

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    payload = decode_access_token(creds.credentials)
    if payload is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    user = await db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="user not found or disabled")
    return user
```

`api/auth.py` 追加(imports 补 `Depends`、`get_current_user`):

```python
@router.get("/me", response_model=UserOut)
async def me(current: User = Depends(get_current_user)):
    return current
```

- [ ] **Step 4: 运行确认通过**

```cmd
.venv\Scripts\python -m pytest -v
```

Expected: 全部 `10 passed`(health 1 + models 1 + auth 8:注册 3 + 登录 2 + me 3)。

- [ ] **Step 5: 提交**

```cmd
git add -A
git commit -m "feat: jwt auth dependency and /auth/me endpoint"
```

---

### Task 9: 后端本地启动脚本

**Files:**
- Create: `backend/start_dev.bat`;Modify: `README.md`(开发启动一节与脚本一致)

**Interfaces:**
- Produces: 一键开发启动:`start_dev.bat` 完成迁移+热重载;http://localhost:8000/api/health 可访问

- [ ] **Step 1: 写 `backend/start_dev.bat`**

```bat
@echo off
REM AIRag 后端开发启动:迁移 + 热重载
cd /d %~dp0
.venv\Scripts\python -m alembic upgrade head
.venv\Scripts\python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

同时核对 README 的"开发启动"一节与此脚本行为一致(不一致则改 README)。

- [ ] **Step 2: 启动并验证**

新开一个 CMD 窗口运行 `backend\start_dev.bat`,另开一个窗口验证:

```cmd
curl http://localhost:8000/api/health
```

Expected: `{"status":"ok"}`。再验证迁移已在运行库执行:

```cmd
"C:\Program Files\PostgreSQL\16\bin\psql" -U airag -d airag -h localhost -c "SELECT count(*) FROM alembic_version;"
```

Expected: count=1。验证后回到启动窗口 Ctrl+C 停掉 uvicorn。

- [ ] **Step 3: 提交**

```cmd
git add -A
git commit -m "chore: native dev start script with auto-migration"
```

---

### Task 10: 前端脚手架(Vite+Vue3+TS+Router+Pinia+Vitest+Element Plus)

**Files:**
- Create: `frontend/`(create-vue 生成)+ 依赖安装

**Interfaces:**
- Produces: 可 `pnpm build`/`pnpm test` 的 Vue3 工程后续任务的宿主

- [ ] **Step 1: 生成工程**

```cmd
cd /d E:\Projects\AIRag
pnpm create vue@latest frontend -- --ts --router --pinia --vitest --eslint-with-prettier
```

若进入交互式提问,按此清单选择:TypeScript=Yes、JSX=No、Router=Yes、Pinia=Yes、Vitest=Yes、E2E=No、ESLint+Prettier=Yes、其余默认。若目录已存在询问,选忽略/合并。

- [ ] **Step 2: 装依赖**

```cmd
cd frontend
pnpm install
pnpm add element-plus @element-plus/icons-vue axios
```

- [ ] **Step 3: 接入 Element Plus,重写 `src/main.ts`**

```ts
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus)

app.mount('#app')
```

(全量引入是 M1 的刻意简化;unplugin 按需加载优化留给 M3 页面增多时再做。)

- [ ] **Step 4: 验证构建与默认测试**

```cmd
pnpm build
pnpm test -- --run
```

Expected: build 成功;vitest 通过(create-vue 自带示例测试,若报 "no test files" 属正常,Task 11 会补)。

- [ ] **Step 5: 提交**

```cmd
cd /d E:\Projects\AIRag
git add -A
git commit -m "chore: vue3+vite+ts scaffold with element-plus/pinia/router/vitest"
```

---

### Task 11: HTTP 封装、auth store、登录页与路由守卫

**Files:**
- Create: `frontend/src/api/http.ts`, `frontend/src/api/auth.ts`, `frontend/src/stores/auth.ts`, `frontend/src/pages/LoginPage.vue`, `frontend/src/pages/HomePage.vue`, `frontend/src/layouts/MainLayout.vue`
- Modify: `frontend/src/App.vue`, `frontend/src/router/index.ts`, `frontend/vite.config.ts`
- Test: `frontend/tests/unit/auth.store.spec.ts`(或 create-vue 的 `src/**/__tests__` 约定,以工程实际为准)

**Interfaces:**
- Consumes: Task 6~8 的 `/api/auth/*`
- Produces: `authApi.login/register/me`;`useAuthStore()`(`{token, user, isLoggedIn, login(u,p), fetchUser(), clear(), logout()}`);localStorage 键 `airag_token`;vite 代理 `/api`→8000。M3 页面直接复用 http.ts 与守卫

- [ ] **Step 1: 写失败的 store 测试 `tests/unit/auth.store.spec.ts`**

```ts
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAuthStore } from '@/stores/auth'

vi.mock('@/api/auth', () => ({
  authApi: {
    login: vi.fn().mockResolvedValue({ access_token: 'jwt-token', token_type: 'bearer' }),
    register: vi.fn().mockResolvedValue({ id: 1, username: 'alice', role: 'admin', is_active: true }),
    me: vi.fn(),
  },
}))

describe('auth store', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  it('login stores token in state and localStorage', async () => {
    const store = useAuthStore()
    await store.login('alice', 'secret123')
    expect(store.token).toBe('jwt-token')
    expect(localStorage.getItem('airag_token')).toBe('jwt-token')
    expect(store.isLoggedIn).toBe(true)
  })

  it('logout clears token', async () => {
    const store = useAuthStore()
    await store.login('alice', 'secret123')
    store.logout()
    expect(store.token).toBeNull()
    expect(localStorage.getItem('airag_token')).toBeNull()
    expect(store.isLoggedIn).toBe(false)
  })
})
```

- [ ] **Step 2: 运行确认失败**

```cmd
cd /d E:\Projects\AIRag\frontend
pnpm test -- --run
```

Expected: FAIL(`@/stores/auth` 不存在)。

- [ ] **Step 3: 实现 API 层与 store**

`src/api/http.ts`:

```ts
import axios from 'axios'
import router from '@/router'
import { useAuthStore } from '@/stores/auth'

const http = axios.create({ baseURL: '/api' })

http.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.token) {
    config.headers.Authorization = `Bearer ${auth.token}`
  }
  return config
})

http.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      useAuthStore().clear()
      router.push({ name: 'login' })
    }
    return Promise.reject(err)
  },
)

export default http
```

`src/api/auth.ts`:

```ts
import http from './http'

export interface Credentials {
  username: string
  password: string
}

export interface TokenResponse {
  access_token: string
  token_type: string
}

export interface UserResponse {
  id: number
  username: string
  role: string
  is_active: boolean
}

export const authApi = {
  async register(payload: Credentials): Promise<UserResponse> {
    const { data } = await http.post<UserResponse>('/auth/register', payload)
    return data
  },
  async login(payload: Credentials): Promise<TokenResponse> {
    const { data } = await http.post<TokenResponse>('/auth/login', payload)
    return data
  },
  async me(): Promise<UserResponse> {
    const { data } = await http.get<UserResponse>('/auth/me')
    return data
  },
}
```

`src/stores/auth.ts`:

```ts
import { defineStore } from 'pinia'
import { authApi, type UserResponse } from '@/api/auth'

const TOKEN_KEY = 'airag_token'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem(TOKEN_KEY),
    user: null as UserResponse | null,
  }),
  getters: {
    isLoggedIn: (state) => !!state.token,
  },
  actions: {
    async login(username: string, password: string) {
      const { access_token } = await authApi.login({ username, password })
      this.token = access_token
      localStorage.setItem(TOKEN_KEY, access_token)
    },
    async fetchUser() {
      this.user = await authApi.me()
    },
    clear() {
      this.token = null
      this.user = null
      localStorage.removeItem(TOKEN_KEY)
    },
    logout() {
      this.clear()
    },
  },
})
```

- [ ] **Step 4: 运行确认通过**

```cmd
pnpm test -- --run
```

Expected: auth store 2 条 PASS(连同自带示例)。

- [ ] **Step 5: 实现页面、路由、代理**

`src/router/index.ts`(整体替换):

```ts
import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/pages/LoginPage.vue') },
    {
      path: '/',
      component: () => import('@/layouts/MainLayout.vue'),
      children: [{ path: '', name: 'home', component: () => import('@/pages/HomePage.vue') }],
    },
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.name !== 'login' && !auth.isLoggedIn) {
    return { name: 'login' }
  }
})

export default router
```

`src/App.vue`(整体替换):

```vue
<template>
  <router-view />
</template>
```

`src/pages/LoginPage.vue`:

```vue
<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { authApi } from '@/api/auth'

const router = useRouter()
const auth = useAuthStore()
const formRef = ref<FormInstance>()
const loading = ref(false)
const isRegister = ref(false)
const form = reactive({ username: '', password: '' })

const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 8, message: '密码至少 8 位', trigger: 'blur' },
  ],
}

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    if (isRegister.value) {
      await authApi.register({ ...form })
    }
    await auth.login(form.username, form.password)
    ElMessage.success(isRegister.value ? '注册并登录成功' : '登录成功')
    router.push({ name: 'home' })
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '操作失败,请检查用户名密码')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <el-card class="login-card">
      <h2>AIRag 知识库</h2>
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="用户名" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" show-password placeholder="密码" />
        </el-form-item>
        <el-button type="primary" :loading="loading" style="width: 100%" @click="submit">
          {{ isRegister ? '注册并登录' : '登录' }}
        </el-button>
        <el-button link style="width: 100%; margin-top: 8px" @click="isRegister = !isRegister">
          {{ isRegister ? '已有账号?去登录' : '没有账号?注册一个' }}
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  background: #f5f7fa;
}
.login-card {
  width: 360px;
}
h2 {
  text-align: center;
  margin-bottom: 16px;
}
</style>
```

`src/layouts/MainLayout.vue`:

```vue
<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

function onLogout() {
  auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <el-container style="height: 100vh">
    <el-aside width="200px">
      <el-menu default-active="home" router>
        <el-menu-item index="home">首页</el-menu-item>
        <el-menu-item index="kb" disabled>知识库(M3)</el-menu-item>
        <el-menu-item index="chat" disabled>对话(M3)</el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header style="display: flex; justify-content: flex-end; align-items: center">
        <el-button link @click="onLogout">退出登录</el-button>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>
```

`src/pages/HomePage.vue`:

```vue
<template>
  <el-empty description="M1 完成:骨架与认证已就绪。知识库与对话页面将在 M3 上线。" />
</template>
```

`vite.config.ts`(保留 create-vue 生成的 alias,追加 proxy):

```ts
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
})
```

- [ ] **Step 6: 构建验证**

```cmd
pnpm build
pnpm test -- --run
```

Expected: build 成功、测试全绿。

- [ ] **Step 7: 提交**

```cmd
cd /d E:\Projects\AIRag
git add -A
git commit -m "feat: frontend auth loop (axios+pinia store+login page+router guard)"
```

---

### Task 12: M1 端到端验收、M2 计划交接

**Files:**
- Modify: `README.md`(开发启动核对);Create: M2 计划文件(writing-plans 产出)

**Interfaces:**
- Produces: 本地三件套(PG 服务 + 后端 + 前端)可稳定启动;M1 验收清单全过;M2 详细计划文件,路线图表指向它

- [ ] **Step 1: 核对 README 开发启动指南**

确认 `README.md` 的"开发启动"三步(PG Windows 服务 → `backend\start_dev.bat` → `frontend` 下 `pnpm dev`)与实际操作一致,不一致则修正。

- [ ] **Step 2: 起全栈并验收**

按 README 三步启动:确认 PG 服务运行(服务名 `postgresql-x64-16`,可用 `net start | findstr postgres` 检查)→ 新窗口运行 `backend\start_dev.bat` → 新窗口 `cd frontend && pnpm dev`。

浏览器打开 http://localhost:5173 ,人工核对清单(逐项勾选):

- [ ] 未登录访问 `/` 被守卫重定向到 `/login`
- [ ] "没有账号?注册一个" → 输入用户名(≥3 位字母数字下划线)+ 密码(≥8 位)→ 注册并登录成功,跳转首页
- [ ] 退出登录回到登录页;用刚注册的账号重新登录成功
- [ ] 浏览器 DevTools → Application → Local Storage 有 `airag_token`
- [ ] http://localhost:8000/docs Swagger 可访问,`/api/auth/me` 用 token 调试返回用户

- [ ] **Step 3: 全量回归**

```cmd
cd /d E:\Projects\AIRag\backend
.venv\Scripts\python -m pytest -v
cd /d E:\Projects\AIRag\frontend
pnpm test -- --run && pnpm build
```

Expected: 后端 10 passed、前端全绿、构建成功。

- [ ] **Step 4: 提交 M1 完成态**

```cmd
cd /d E:\Projects\AIRag
git add -A
git commit -m "chore: m1 complete - full stack acceptance verified"
```

- [ ] **Step 5: M2 计划交接(长任务接力点)**

使用 superpowers:writing-plans 技能,基于 Spec §7/§9-M2 与当前代码,生成 `docs/superpowers/plans/2026-09-14-airag-m2-document-pipeline.md`(上传 API+状态机、Parser 插件 pdf/docx/xlsx、切块、智谱 Embedding Provider、Celery 流水线+worker 服务、集成测试)。生成本文剩余勾选:

- [ ] M2 计划文件已生成并提交
- [ ] 本文件路线图表中 M2 行的"详细计划"已更新为该文件路径

- [ ] **Step 6: 推送 GitHub(M1 首次)**

```cmd
cd /d E:\Projects\AIRag
git remote add origin https://github.com/yu-xiao/AIRAG.git
git ls-remote origin
```

若 ls-remote 显示远端已有自动生成的初始提交(如 README):先 `git pull origin main --rebase --allow-unrelated-histories` 再推;远端为空则直接推。首次推送会弹出 Git Credential Manager 浏览器登录,由用户完成一次授权。

```cmd
git push -u origin main
git push -u origin m1-skeleton-auth
```

Expected: 两个分支推送成功(失败多为网络抖动,重试)。

---

## 计划自审记录(writing-plans Self-Review)

1. **Spec 覆盖**:M1 范围(脚手架/数据模型+迁移/JWT 注册登录/本地全栈起/能登录)→ Task 1~12 全覆盖;M2~M5 属后续计划,路线图已声明。
2. **占位符扫描**:无 TBD/TODO;所有代码步骤均给出完整代码或精确修改说明。
3. **类型一致性**:`UserOut{ id, username, role, is_active }` 与前端 `UserResponse` 字段一致;`TokenResponse{ access_token, token_type }` 与 `TokenOut` 一致;`get_db`/`client`/`db_session` 在 conftest 与各任务引用一致;localStorage 键 `airag_token` 前后端约定一致。
4. **修订(2026-09-14,去 Docker 化)**:应用户决策,开发环境改为原生 Windows(PG 16 原生安装 + pgvector 手工编译,见『环境准备』);Task 2/9/12 重写为原生验证;docker-compose/Dockerfile 推迟到部署里程碑;端口改为 5432。
