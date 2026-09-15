from fastapi import APIRouter

from app.api.auth import router as auth_router
from app.api.documents import router as documents_router
from app.api.kbs import router as kbs_router

api_router = APIRouter()


@api_router.get("/health")
async def health() -> dict:
    return {"status": "ok"}

api_router.include_router(auth_router)
api_router.include_router(kbs_router)
api_router.include_router(documents_router)
