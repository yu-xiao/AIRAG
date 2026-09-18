# backend/app/services/api_keys.py
"""M9:api key 生成与解析(prefix 索引定位 + 常数时间哈希比较)。"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import ApiKey, User

KEY_HEADER = "airag_"


class KeyRejected(Exception):
    """解析失败;code ∈ invalid_key|key_revoked|key_expired(即 401 detail)。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def generate_api_key() -> tuple[str, str, str]:
    """返回 (明文 key, key_prefix, key_sha256_hex);明文只在创建响应出现一次。"""
    key = f"{KEY_HEADER}{secrets.token_urlsafe(24)}"
    return key, key[:14], hash_api_key(key)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class KeyQuotaExceeded(Exception):
    """目标用户活跃 key 数已达 AGENT_MAX_KEYS_PER_USER(调用方转 409)。"""


async def issue_api_key(
    db: AsyncSession, user: User, name: str, expires_in_days: int | None
) -> tuple[ApiKey, str]:
    """配额检查 + 生成落库(仅 flush,不 commit);返回 (ApiKey 行, 明文)。

    审计与 commit 由调用方负责(个人面/admin 面 detail 不同)。
    """
    count = (
        await db.execute(
            select(func.count()).select_from(ApiKey).where(
                ApiKey.user_id == user.id, ApiKey.is_active == True  # noqa: E712
            )
        )
    ).scalar_one()
    if count >= settings.AGENT_MAX_KEYS_PER_USER:
        raise KeyQuotaExceeded()
    raw, prefix, digest = generate_api_key()
    key = ApiKey(
        user_id=user.id,
        name=name,
        key_prefix=prefix,
        key_hash=digest,
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=expires_in_days)
            if expires_in_days
            else None
        ),
    )
    db.add(key)
    await db.flush()
    return key, raw


async def resolve_api_key(db: AsyncSession, raw: str) -> ApiKey:
    row = (
        await db.execute(select(ApiKey).where(ApiKey.key_prefix == raw[:14]))
    ).scalar_one_or_none()
    if row is None or not hmac.compare_digest(hash_api_key(raw), row.key_hash):
        raise KeyRejected("invalid_key")
    if not row.is_active:
        raise KeyRejected("key_revoked")
    if row.expires_at is not None and row.expires_at <= datetime.now(timezone.utc):
        raise KeyRejected("key_expired")
    return row
