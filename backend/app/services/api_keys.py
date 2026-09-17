# backend/app/services/api_keys.py
"""M9:api key 生成与解析(prefix 索引定位 + 常数时间哈希比较)。"""
import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiKey

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
