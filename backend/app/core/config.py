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
    CHAT_MODEL: str = "glm-5.3-flash"
    CHAT_TEMPERATURE: float = 0.3
    CHAT_MAX_TOKENS: int = 2048
    RERANK_ENABLED: bool = False
    RERANK_MODEL: str = "rerank"  # 智谱文本重排序模型编码,文档枚举仅此一个
    # M5 agentic:查询改写 / CRAG 检索自评(env 全局默认开,关闭即节点直通)
    AGENTIC_REWRITE_ENABLED: bool = True
    AGENTIC_CRAG_ENABLED: bool = True
    RETRIEVAL_TOP_K: int = 8
    ZHIPU_API_KEY: str = ""
    ZHIPU_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4"
    EMBED_PROVIDER: str = "zhipu"  # zhipu|fake
    EMBED_MODEL: str = "embedding-3"
    EMBED_DIMS: int = 1024
    UPLOAD_DIR: str = "storage/uploads"  # 相对 backend 运行目录
    MAX_UPLOAD_MB: int = 20


settings = Settings()
