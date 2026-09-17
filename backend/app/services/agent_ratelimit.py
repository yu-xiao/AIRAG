# backend/app/services/agent_ratelimit.py
"""M9:按 key 滑动窗口限流(Redis ZSET);Redis 异常降级放行(可用性优先)。"""
import time
import uuid

from redis import asyncio as aioredis

from app.core.config import settings

WINDOW_SECONDS = 60

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """惰性单例;测试 monkeypatch 此函数注入 fake。"""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


async def allow(ident: str) -> tuple[bool, int]:
    """返回 (是否放行, retry_after 秒)。"""
    limit = settings.AGENT_RATE_LIMIT_PER_MIN
    if limit <= 0:
        return True, 0
    now = time.time()
    rkey = f"agent_rl:{ident}"
    try:
        r = get_redis()
        await r.zremrangebyscore(rkey, 0, now - WINDOW_SECONDS)
        count = await r.zcard(rkey)
        if count >= limit:
            oldest = await r.zrange(rkey, 0, 0, withscores=True)
            retry = 1
            if oldest:
                # int 截断 +1 = 向上取整;夹上限 WINDOW_SECONDS,
                # 防时钟回拨/同刻两次取时(耗时 ≤ 0)算出 61 越界。
                retry = min(
                    WINDOW_SECONDS,
                    max(1, int(WINDOW_SECONDS - (now - oldest[0][1])) + 1),
                )
            return False, retry
        await r.zadd(rkey, {f"{now:.6f}:{uuid.uuid4().hex[:6]}": now})
        await r.expire(rkey, WINDOW_SECONDS * 2)
        return True, 0
    except Exception:
        return True, 0
