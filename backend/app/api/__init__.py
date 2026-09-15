from fastapi import APIRouter

from app.api.auth import router as auth_router

api_router = APIRouter()


@api_router.get("/health")
async def health() -> dict:
    return {"status": "ok"}

api_router.include_router(auth_router)
