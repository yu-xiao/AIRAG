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

api_router = APIRouter()


@api_router.get("/health")
async def health() -> dict:
    return {"status": "ok"}

api_router.include_router(auth_router)
api_router.include_router(kbs_router)
api_router.include_router(users_router)
api_router.include_router(documents_router)
api_router.include_router(conversations_router)
api_router.include_router(ask_router)
api_router.include_router(admin_router)
# M9:agent REST 面为进程启动期开关(无运行时切换)
if settings.AGENT_API_ENABLED:
    api_router.include_router(agent_router)
