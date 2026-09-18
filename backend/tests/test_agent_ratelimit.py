# backend/tests/test_agent_ratelimit.py
"""M9 Task5:限流窗口逻辑(FakeRedis)+ 429 集成。"""
import pytest

from app.core.config import settings
from app.services import agent_ratelimit
from app.services.agent_ratelimit import allow


class FakeRedis:
    def __init__(self, fail=False):
        self.z: dict[str, dict[str, float]] = {}
        self.kv: dict[str, int] = {}
        self.fail = fail

    async def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    async def zremrangebyscore(self, key, lo, hi):
        await self._check()
        d = self.z.setdefault(key, {})
        self.z[key] = {m: s for m, s in d.items() if s > hi}

    async def zcard(self, key):
        await self._check()
        return len(self.z.get(key, {}))

    async def zadd(self, key, mapping):
        await self._check()
        self.z.setdefault(key, {}).update(mapping)

    async def zrange(self, key, start, end, withscores=False):
        await self._check()
        items = sorted(self.z.get(key, {}).items(), key=lambda kv: kv[1])
        return items[start: end + 1]

    async def expire(self, key, ttl):
        return True

    async def get(self, key):
        await self._check()
        return self.kv.get(key)

    async def incrby(self, key, amount):
        await self._check()
        self.kv[key] = int(self.kv.get(key, 0)) + amount
        return self.kv[key]


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(agent_ratelimit, "get_redis", lambda: r)
    return r


async def test_under_limit_passes(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 3)
    for _ in range(3):
        ok, _ = await allow(1)
        assert ok


async def test_over_limit_denies_with_retry(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 1)
    assert (await allow(2))[0] is True
    ok, retry = await allow(2)
    assert ok is False and 1 <= retry <= 60


async def test_limit_disabled(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 0)
    for _ in range(5):
        assert (await allow(3))[0] is True


async def test_redis_failure_degrades_open(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 1)
    monkeypatch.setattr(agent_ratelimit, "get_redis", lambda: FakeRedis(fail=True))
    assert (await allow(4))[0] is True


async def test_ident_isolated(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 1)
    assert (await allow(101))[0] is True
    assert (await allow(102))[0] is True


async def test_rl_key_shape(fake_redis, monkeypatch):
    """小项③:redis key 对齐 M9 spec §E 的 agent_rl:{key_id}。"""
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 5)
    await allow(9)
    assert "agent_rl:9" in fake_redis.z


# ---- M10:每 key 每日 token 配额 ----

def test_quota_key_shape():
    import re

    rkey, ttl = agent_ratelimit._quota_key(7)
    assert re.fullmatch(r"agent_tq:7:\d{8}", rkey)
    assert 1 <= ttl <= 86401  # 到次日零点(+1 余量)


async def test_quota_under_limit_passes(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_ASK_DAILY_TOKENS", 100)
    await agent_ratelimit.quota_consume(1, 60)
    ok, retry = await agent_ratelimit.quota_check(1)
    assert ok is True and retry == 0


async def test_quota_exhausted_blocks_with_retry(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_ASK_DAILY_TOKENS", 100)
    await agent_ratelimit.quota_consume(2, 100)
    ok, retry = await agent_ratelimit.quota_check(2)
    assert ok is False and 1 <= retry <= 86400


async def test_quota_disabled(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_ASK_DAILY_TOKENS", 0)
    await agent_ratelimit.quota_consume(3, 999999)
    assert (await agent_ratelimit.quota_check(3)) == (True, 0)


async def test_quota_redis_failure_degrades_open(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_ASK_DAILY_TOKENS", 1)
    monkeypatch.setattr(agent_ratelimit, "get_redis",
                        lambda: FakeRedis(fail=True))
    assert (await agent_ratelimit.quota_check(4))[0] is True


async def test_quota_consume_failure_silent(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_ASK_DAILY_TOKENS", 100)
    monkeypatch.setattr(agent_ratelimit, "get_redis",
                        lambda: FakeRedis(fail=True))
    await agent_ratelimit.quota_consume(5, 50)  # 不得抛


async def test_api_429(client, auth_headers, monkeypatch):
    # 集成:第 2 次(配额 1)触发 429;JWT 不限流
    # tests/ 含 __init__.py(pytest 以 tests.xxx 导入,backend 才在 sys.path),
    # 须带包前缀导入兄弟测试模块,裸模块名会 ModuleNotFoundError。
    from tests.test_agent_api import _create_kb, _create_key

    kb_id = await _create_kb(client, auth_headers, "限流库")
    key = await _create_key(client, auth_headers)
    r = FakeRedis()
    monkeypatch.setattr(agent_ratelimit, "get_redis", lambda: r)
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 1)

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        return []

    monkeypatch.setattr("app.services.agent_facade.hybrid_search", fake_hybrid)
    hdr = {"Authorization": f"Bearer {key}"}
    resp1 = await client.post("/api/agent/search",
                              json={"kb_ids": [kb_id], "query": "q"}, headers=hdr)
    resp2 = await client.post("/api/agent/search",
                              json={"kb_ids": [kb_id], "query": "q"}, headers=hdr)
    assert resp1.status_code == 200
    assert resp2.status_code == 429
    detail = resp2.json()["detail"]
    assert detail["code"] == "rate_limited" and detail["retry_after"] >= 1
    assert int(resp2.headers["Retry-After"]) >= 1
