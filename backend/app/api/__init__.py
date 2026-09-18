from fastapi import APIRouter

from app.api.admin import router as admin_router
from app.api.agent import router as agent_router
from app.api.ask import router as ask_router
from app.api.auth import router as auth_router
from app.api.conversations import router as conversations_router
from app.api.documents import router as documents_router
from app.api.kbs import router as kbs_router
from app.api.users import router as users_router
from app.core.config import settings


def build_api_router() -> APIRouter:
    """装配 /api 路由;抽成函数使 AGENT_API_ENABLED 在 create_app 期读取,
    冒烟测试可 monkeypatch settings 后重建应用验证 404 语义。"""
    router = APIRouter()

    @router.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    router.include_router(auth_router)
    router.include_router(kbs_router)
    router.include_router(users_router)
    router.include_router(documents_router)
    router.include_router(conversations_router)
    router.include_router(ask_router)
    router.include_router(admin_router)
    # M9:agent REST 面为进程启动期开关(无运行时切换)
    if settings.AGENT_API_ENABLED:
        router.include_router(agent_router)
    return router
