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


settings = Settings()
