import os
import uuid as _uuid
from pathlib import Path as _Path

# M2 全局测试约定:测试进程一律指向测试库 + fake 嵌入(必须在导入 app.* 之前设置)
os.environ["DATABASE_URL"] = (
    "postgresql+asyncpg://airag:airag_dev_password@localhost:5432/airag_test"
)
os.environ["EMBED_PROVIDER"] = "fake"

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
            # Task 4 前模型未注册、表不存在,DELETE 会报 UndefinedTableError;
            # 只清理 Base.metadata 中已注册的表,模型齐全后行为与计划一致。
            if table not in Base.metadata.tables:
                continue
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


@pytest_asyncio.fixture(autouse=True)
async def isolated_upload_dir(tmp_path):
    old = settings.UPLOAD_DIR
    settings.UPLOAD_DIR = str(tmp_path / "uploads")
    _Path(settings.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    yield
    settings.UPLOAD_DIR = old


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
