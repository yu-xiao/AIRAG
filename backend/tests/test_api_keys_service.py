# backend/tests/test_api_keys_service.py
"""M9 Task1:api key 生成/哈希/解析(resolve)。"""
import pytest
from sqlalchemy import select

from app.models import ApiKey, User
from app.services.api_keys import (
    KeyRejected,
    generate_api_key,
    hash_api_key,
    resolve_api_key,
)


def test_generate_api_key_format():
    raw, prefix, digest = generate_api_key()
    assert raw.startswith("airag_") and len(raw) > 20
    assert prefix == raw[:14] and prefix.startswith("airag_")
    assert digest == hash_api_key(raw) and len(digest) == 64


def test_generate_uniqueness():
    assert generate_api_key()[0] != generate_api_key()[0]


async def test_resolve_roundtrip(db_session):
    user = User(username="k1_owner", password_hash="x", role="viewer")
    db_session.add(user)
    await db_session.flush()
    raw, prefix, digest = generate_api_key()
    db_session.add(
        ApiKey(user_id=user.id, name="t", key_prefix=prefix, key_hash=digest)
    )
    await db_session.commit()

    key = await resolve_api_key(db_session, raw)
    assert key.user_id == user.id and key.name == "t"


async def test_resolve_rejects(db_session):
    user = User(username="k2_owner", password_hash="x", role="viewer")
    db_session.add(user)
    await db_session.flush()
    raw, prefix, digest = generate_api_key()
    revoked_raw, revoked_prefix, revoked_digest = generate_api_key()
    db_session.add_all([
        ApiKey(user_id=user.id, name="bad", key_prefix="airag_bad", key_hash="0" * 64),
        ApiKey(user_id=user.id, name="revoked", key_prefix=revoked_prefix,
               key_hash=revoked_digest, is_active=False),
        ApiKey(user_id=user.id, name="ok", key_prefix=prefix, key_hash=digest),
    ])
    await db_session.commit()

    with pytest.raises(KeyRejected) as e:
        await resolve_api_key(db_session, "airag_nope")
    assert e.value.code == "invalid_key"

    with pytest.raises(KeyRejected) as e:
        await resolve_api_key(db_session, revoked_raw)
    assert e.value.code == "key_revoked"


async def test_resolve_expired(db_session):
    from datetime import datetime, timedelta, timezone

    user = User(username="k3_owner", password_hash="x", role="viewer")
    db_session.add(user)
    await db_session.flush()
    raw, prefix, digest = generate_api_key()
    db_session.add(ApiKey(
        user_id=user.id, name="exp", key_prefix=prefix, key_hash=digest,
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    await db_session.commit()

    with pytest.raises(KeyRejected) as e:
        await resolve_api_key(db_session, raw)
    assert e.value.code == "key_expired"
