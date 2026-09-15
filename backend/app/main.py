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
