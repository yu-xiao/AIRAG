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
    # M5:checkpointer 恢复接线(M3 裁决 1 的可选增强;测试进程关)
    CHECKPOINTER_ENABLED: bool = True
    # M5 OCR:MinerU 云 API(token 空 = OCR 整体关闭,行为同 M4)
    OCR_THIN_CHARS_PER_PAGE: int = 50
    MINERU_API_TOKEN: str = ""
    MINERU_BASE_URL: str = "https://mineru.net"
    # M6 审计留存:超过天数自动/手动清理;0 = 禁用
    AUDIT_RETENTION_DAYS: int = 180
    # M6 多跳兜底:检索不足且重检仍不足(或零命中)时拆子问题再检索
    MULTI_HOP_ENABLED: bool = True
    MULTI_HOP_MAX_SUBQ: int = 3
    RETRIEVAL_TOP_K: int = 8
    # M7:rerank 相关度阈值,默认禁用;智谱 rerank relevance_score 实测全量饱和
    # 0.92~1.0(零命中/正常分布重叠,无可行阈值);换用有真实分数分布的 rerank
    # 供应商时手动开启(env),作为纵深防御。
    RETRIEVAL_MIN_SCORE: float = 0.0
    ZHIPU_API_KEY: str = ""
    ZHIPU_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4"
    EMBED_PROVIDER: str = "zhipu"  # zhipu|fake
    EMBED_MODEL: str = "embedding-3"
    EMBED_DIMS: int = 1024
    UPLOAD_DIR: str = "storage/uploads"  # 相对 backend 运行目录
    # M9 Agent 对外开放
    AGENT_API_ENABLED: bool = True
    AGENT_MAX_KEYS_PER_USER: int = 10
    AGENT_RATE_LIMIT_PER_MIN: int = 60
    # M10:每 key 每日 ask token 配额(Redis 按日计数,0=禁用)
    AGENT_ASK_DAILY_TOKENS: int = 200_000
    MAX_UPLOAD_MB: int = 20


settings = Settings()
