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
        # 挂载于根(fastmcp 官方集成法):/api 等已注册路由优先,兜底路径
        # 落到 MCP 鉴权中间件;完整 MCP 端点为 POST /mcp。
        app.mount("/", mcp_asgi)
    return app


logger.add("logs/app.log", rotation="10 MB", retention=5, enqueue=True)

app = create_app()
