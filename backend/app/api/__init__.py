from fastapi import APIRouter

api_router = APIRouter()


@api_router.get("/health")
async def health() -> dict:
    return {"status": "ok"}
