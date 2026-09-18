# AIRag M10 实施计划:Agent ask 工具(非流式 RAG)与 M9 收尾

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 外部 Agent 通过 REST `POST /api/agent/ask` 与 MCP `ask_knowledge_base` 获得与 Web 端同质量的 RAG 问答(答案+引用+拒答),按 key 每日 token 配额计费防护;清偿 M9 六笔终审小项。

**Architecture:** 一核两面延续 M9——`agent_facade.agent_ask` 复用完整问答图(`build_graph(checkpointer=None)` 模块级单例,非流式 `ainvoke`),`TokenMeter` LangChain callback 累计全部 LLM 调用 token;`agent_ratelimit.py` 扩展每日 token 配额(Redis `agent_tq:{key_id}:{yyyymmdd}`,软上限);REST/MCP 均为薄壳。

**Tech Stack:** FastAPI + fastmcp 2.14.7 + langgraph 1.x + langchain-core(callbacks)+ Redis(asyncio)+ pytest(anyio)。

**Spec:** `docs/superpowers/specs/2026-09-18-airag-m10-agent-ask-design.md`(本计划从 spec 出发,执行者须先读 spec)

## Global Constraints

- Python 一律用 `backend\.venv\Scripts\python.exe`(下文 `.venv\Scripts\python` 均指此);pytest 在 `backend` 目录跑。
- conftest 已钉死:`AGENTIC_REWRITE_ENABLED/AGENTIC_CRAG_ENABLED/CHECKPOINTER/MULTI_HOP/RERANK_ENABLED=false`、`EMBED_PROVIDER=fake`、`MINERU_API_TOKEN=""`——图在测试里只走 generate 一次 LLM 调用。
- 测试导入兄弟测试模块必须带包前缀:`from tests.test_agent_api import _create_kb`(裸模块名会 ModuleNotFoundError)。
- `FakeRedis` 须共享同一实例(monkeypatch `agent_ratelimit.get_redis`),否则窗口/配额永不累计。
- 401 detail 是字符串,403/429 detail 是 dict(既有约定,新端点沿用)。
- 禁止前端改动;提交信息沿用 `feat(scope)/fix/test/docs` 风格。
- Redis 连接串以 `settings.REDIS_URL` 为准(本机有密码,含在 URL 里)。
- Windows CMD;git 仓库在 `E:\Projects\AIRag`,分支 main。

---

### Task 1: 每日 token 配额核心(agent_ratelimit 扩展 + 配置)

**Files:**
- Modify: `backend/app/core/config.py`(AGENT_RATE_LIMIT_PER_MIN 行后加一项)
- Modify: `backend/app/services/agent_ratelimit.py`(新增 quota 三函数)
- Test: `backend/tests/test_agent_ratelimit.py`(FakeRedis 扩 kv + 6 个新用例)

**Interfaces:**
- Consumes: 既有 `get_redis()`、`settings`。
- Produces(Task 3/4 依赖): `quota_check(key_id: int) -> tuple[bool, int]`、`quota_consume(key_id: int, tokens: int) -> None`、`settings.AGENT_ASK_DAILY_TOKENS: int`(默认 200000,0=禁用)、模块内 `_quota_key(key_id: int) -> tuple[str, int]`。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_agent_ratelimit.py` 的 `FakeRedis.__init__` 中加 `self.kv: dict[str, int] = {}`,并在类尾(zrange 之后)追加三个方法:

```python
    async def get(self, key):
        await self._check()
        return self.kv.get(key)

    async def incrby(self, key, amount):
        await self._check()
        self.kv[key] = int(self.kv.get(key, 0)) + amount
        return self.kv[key]
```

文件末尾(`test_api_429` 之前)追加用例:

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_agent_ratelimit.py -v -k quota`
Expected: FAIL,`AttributeError: ... has no attribute 'quota_check'`(或 `_quota_key`)。

- [ ] **Step 3: 实现配额函数**

`backend/app/core/config.py` 在 `AGENT_RATE_LIMIT_PER_MIN: int = 60` 之后加:

```python
    # M10:每 key 每日 ask token 配额(Redis 按日计数,0=禁用)
    AGENT_ASK_DAILY_TOKENS: int = 200_000
```

`backend/app/services/agent_ratelimit.py` 顶部 import 区加 `import datetime as _dt`,文件末尾(`allow` 之后)追加:

```python
# ---- M10:每 key 每日 token 配额(spec C 节) ----

def _quota_key(key_id: int) -> tuple[str, int]:
    """(redis key, 到次日零点秒数);日界按服务器本地时区。"""
    now = _dt.datetime.now()
    tomorrow = (now + _dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return (f"agent_tq:{key_id}:{now:%Y%m%d}",
            max(1, int((tomorrow - now).total_seconds()) + 1))


async def quota_check(key_id: int) -> tuple[bool, int]:
    """ask 前置检查;返回 (是否放行, retry_after 秒)。
    禁用(<=0)或 Redis 异常均放行(可用性优先,同 allow)。"""
    if settings.AGENT_ASK_DAILY_TOKENS <= 0:
        return True, 0
    rkey, retry = _quota_key(key_id)
    try:
        used = int(await get_redis().get(rkey) or 0)
        if used >= settings.AGENT_ASK_DAILY_TOKENS:
            return False, retry
        return True, 0
    except Exception:
        return True, 0


async def quota_consume(key_id: int, tokens: int) -> None:
    """ask 完成后累计;软上限(check 前置,单次可小幅越限,spec 明示)。"""
    if settings.AGENT_ASK_DAILY_TOKENS <= 0:
        return
    rkey, ttl = _quota_key(key_id)
    try:
        r = get_redis()
        await r.incrby(rkey, tokens)
        await r.expire(rkey, ttl)
    except Exception:
        pass
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_agent_ratelimit.py -v`
Expected: 全 PASS(既有用例 + 新增 6 个)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/core/config.py backend/app/services/agent_ratelimit.py backend/tests/test_agent_ratelimit.py
git commit -m "feat(agent): per-key daily token quota core (redis, soft limit)"
```

---

### Task 2: facade.agent_ask + TokenMeter + denied 去重

**Files:**
- Modify: `backend/app/services/agent_facade.py`(抽 `_permitted_kb_ids`、加 TokenMeter/AskOutcome/agent_ask/图单例)
- Test: `backend/tests/test_agent_ask.py`(新建)

**Interfaces:**
- Consumes: `build_graph(checkpointer=None)`(chat_graph.graph,Task 内惰性导入避免循环)、`SearchHit`、既有 `AgentKbDenied`。
- Produces(Task 3/4 依赖):
  - `agent_ask(db: AsyncSession, user: User, kb_ids: list[int], query: str, rerank: bool) -> AskOutcome`
  - `AskOutcome(answer: str, citations: list[dict], refused: bool, tokens_used: int, elapsed_ms: int)`
  - `TokenMeter`(callback,属性 `total: int`)
  - 模块级 `_ask_graph = None` + `_get_ask_graph()`(**测试 monkeypatch `agent_facade._ask_graph` 注入假 LLM 图**)
  - `_permitted_kb_ids(db, user, kb_ids) -> list[int]`(去重归一;agent_search 同步改用,小项⑥)

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_agent_ask.py`:

```python
# backend/tests/test_agent_ask.py
"""M10 Task2:facade.agent_ask(图复用/计量/去重拒权)。"""
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import select as sa_select

from app.models import User
from app.services import agent_facade
from app.services.chat_graph.graph import build_graph
from app.services.retrieval.searcher import SearchHit


class _UsageChatModel(BaseChatModel):
    """唯一响应带 usage_metadata,验证 TokenMeter 正向计量。"""

    @property
    def _llm_type(self) -> str:
        return "usage-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content="计量答案",
            usage_metadata={"input_tokens": 10, "output_tokens": 5,
                            "total_tokens": 15}))])


async def _me(client, auth_headers, db_session) -> User:
    me = (await client.get("/api/auth/me", headers=auth_headers)).json()
    return (await db_session.execute(
        sa_select(User).where(User.id == me["id"]))).scalars().one()


def _patch_graph(monkeypatch, llm, kb_id: int):
    from app.services.chat_graph import nodes

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        return [SearchHit(chunk_id=1, document_id=10, kb_id=kb_id,
                          filename="a.pdf", page_no=1, content="八千五百米",
                          score=0.9, source="both")]

    monkeypatch.setattr(nodes, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(agent_facade, "_ask_graph",
                        build_graph(llm=llm, checkpointer=None))


async def test_agent_ask_outcome_fields(client, auth_headers, db_session,
                                        monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    kb = await client.post("/api/kbs", json={"name": "ask库"},
                           headers=auth_headers)
    kb_id = kb.json()["id"]
    user = await _me(client, auth_headers, db_session)
    _patch_graph(
        monkeypatch,
        FakeListChatModel(responses=["巡航升限为八千五百米。"]), kb_id)

    out = await agent_facade.agent_ask(db_session, user, [kb_id, kb_id],
                                       "升限?", False)
    assert out.answer == "巡航升限为八千五百米。"
    assert out.refused is False
    assert out.tokens_used == 0  # FakeListChatModel 无 usage,按 0(spec B)
    assert out.citations and out.citations[0]["filename"] == "a.pdf"
    assert isinstance(out.elapsed_ms, int)


async def test_agent_ask_tokens_metered(client, auth_headers, db_session,
                                        monkeypatch):
    kb = await client.post("/api/kbs", json={"name": "计量库"},
                           headers=auth_headers)
    _patch_graph(monkeypatch, _UsageChatModel(), kb.json()["id"])
    user = await _me(client, auth_headers, db_session)
    out = await agent_facade.agent_ask(db_session, user, [kb.json()["id"]],
                                       "q", False)
    assert out.tokens_used == 15


async def test_agent_ask_denied_dedup(client, auth_headers, db_session):
    kb = await client.post("/api/kbs", json={"name": "拒权库"},
                           headers=auth_headers)
    kb_id = kb.json()["id"]
    user = await _me(client, auth_headers, db_session)
    with pytest.raises(agent_facade.AgentKbDenied) as ei:
        await agent_facade.agent_ask(db_session, user,
                                     [99999, kb_id, 99999], "q", False)
    assert ei.value.denied_kb_ids == [99999]  # 去重后唯一
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_agent_ask.py -v`
Expected: FAIL,`AttributeError: module 'app.services.agent_facade' has no attribute 'agent_ask'`。

- [ ] **Step 3: 实现 facade**

`backend/app/services/agent_facade.py`:

顶部 import 区改为(新增 `logger`、`BaseCallbackHandler`;`time` 已有):

```python
import asyncio
import time
from dataclasses import dataclass

from langchain_core.callbacks import BaseCallbackHandler
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
```

`AgentKbDenied` 类之后新增共享权限过滤(并把 `agent_search` 内联块替换为调用它):

```python
async def _permitted_kb_ids(
    db: AsyncSession, user: User, kb_ids: list[int]
) -> list[int]:
    """去重归一 + 逐库权限校验;无权限/不存在统一抛 AgentKbDenied
    (denied_kb_ids 不区分两者,不泄露存在性;重复 id 去重后不再出现)。"""
    kb_ids = list(dict.fromkeys(kb_ids))
    rows = (
        await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids))
        )
    ).scalars().all()
    by_id = {kb.id: kb for kb in rows}
    denied = [
        kb_id for kb_id in kb_ids
        if kb_id not in by_id
        or await get_kb_perm(db, user, by_id[kb_id]) is None
    ]
    if denied:
        raise AgentKbDenied(denied)
    return kb_ids
```

`agent_search` 中删除从 `rows = (` 到 `raise AgentKbDenied(denied)` 的整段,替换为:

```python
    kb_ids = await _permitted_kb_ids(db, user, kb_ids)
```

文件末尾追加:

```python
# ---- M10:ask(非流式 RAG,复用完整问答图) ----

class TokenMeter(BaseCallbackHandler):
    """累计一次 ask 内全部 LLM 调用的 total_tokens(spec B)。
    取不到 usage 按 0(FakeListChatModel 等测试替身即 0,不阻断)。"""

    def __init__(self):
        self.total = 0

    def on_llm_end(self, response, **kwargs):
        try:
            got = 0
            for gens in response.generations:
                for g in gens:
                    um = getattr(getattr(g, "message", None),
                                 "usage_metadata", None)
                    if um:
                        got += int(um.get("total_tokens") or 0)
            if not got:  # 老形态兜底,避免与 message 双计
                tu = (getattr(response, "llm_output", None) or {}).get(
                    "token_usage")
                if tu:
                    got = int(tu.get("total_tokens") or 0)
            self.total += got
        except Exception:
            logger.warning("token meter: usage unreadable, counted as 0")


_ask_graph = None


def _get_ask_graph():
    """checkpointer=None 的无状态单轮图,模块级单例(编译一次);
    测试 monkeypatch 模块属性 _ask_graph 注入假 LLM 图。"""
    global _ask_graph
    if _ask_graph is None:
        from app.services.chat_graph.graph import build_graph

        _ask_graph = build_graph(checkpointer=None)
    return _ask_graph


@dataclass
class AskOutcome:
    answer: str
    citations: list[dict]
    refused: bool
    tokens_used: int
    elapsed_ms: int


async def agent_ask(
    db: AsyncSession,
    user: User,
    kb_ids: list[int],
    query: str,
    rerank: bool,
) -> AskOutcome:
    """权限过滤 → 完整问答图(非流式、单轮、无 checkpointer)→ 终态直出。
    图运行可达 90s:权限过滤后 commit 归还连接,避免长占 asyncpg 池
    (顺带持久化 principal 解析写入的 last_used_at,幂等无害)。"""
    kb_ids = await _permitted_kb_ids(db, user, kb_ids)
    await db.commit()
    meter = TokenMeter()
    start = time.perf_counter()
    final = await _get_ask_graph().ainvoke(
        {"question": query, "kb_ids": kb_ids, "rerank": rerank, "history": []},
        config={"callbacks": [meter]},
    )
    return AskOutcome(
        answer=final.get("answer") or "",
        citations=final.get("citations") or [],
        refused=bool(final.get("refused")),
        tokens_used=meter.total,
        elapsed_ms=int((time.perf_counter() - start) * 1000),
    )
```

- [ ] **Step 4: 跑测试确认通过(含存量检索回归)**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_agent_ask.py tests/test_agent_api.py -v`
Expected: 全 PASS(agent_search 重构后存量用例不回归)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/agent_facade.py backend/tests/test_agent_ask.py
git commit -m "feat(agent): facade agent_ask reuses full qa graph, token meter, dedup kb perms"
```

---

### Task 3: REST POST /api/agent/ask + Retry-After 头 + last_used_at 断言

**Files:**
- Modify: `backend/app/schemas/agent.py`(AgentAskIn/AgentAskOut)
- Modify: `backend/app/api/agent.py`(新端点 + `_check_rate` 加头 + `_check_quota`)
- Test: `backend/tests/test_agent_api.py`(追加);`backend/tests/test_agent_ratelimit.py`(429 断言补头)

**Interfaces:**
- Consumes: Task 1 的 `quota_check/quota_consume`、Task 2 的 `agent_facade.agent_ask/AskOutcome/AgentKbDenied`。
- Produces: REST 端点 `POST /api/agent/ask`;schema `AgentAskIn/AgentAskOut`;429 响应带 `Retry-After` 头(小项①)。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_agent_api.py` 末尾追加(文件顶部已有 `select`/`text`/`ApiKey`/`AuditLog` 导入):

```python
# ---- M10:POST /api/agent/ask ----

def _ask_graph_patch(monkeypatch, response_text: str):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services import agent_facade
    from app.services.chat_graph import nodes
    from app.services.chat_graph.graph import build_graph

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        return _fake_hits()

    monkeypatch.setattr(nodes, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(agent_facade, "_ask_graph",
                        build_graph(llm=FakeListChatModel(responses=[response_text]),
                                    checkpointer=None))


async def test_ask_ok(client, auth_headers, monkeypatch):
    kb_id = await _create_kb(client, auth_headers, "问答库")
    key = await _create_key(client, auth_headers)
    _ask_graph_patch(monkeypatch, "巡航升限为八千五百米。")
    resp = await client.post(
        "/api/agent/ask",
        json={"kb_ids": [kb_id], "query": "升限?"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "巡航升限为八千五百米。"
    assert body["refused"] is False and body["tokens_used"] == 0
    assert body["citations"] and isinstance(body["elapsed_ms"], int)


async def test_ask_refused_is_200(client, auth_headers, monkeypatch):
    from app.services.chat_graph.nodes import REFUSAL_PHRASE

    kb_id = await _create_kb(client, auth_headers, "拒答库")
    key = await _create_key(client, auth_headers)
    _ask_graph_patch(monkeypatch, REFUSAL_PHRASE)
    resp = await client.post(
        "/api/agent/ask",
        json={"kb_ids": [kb_id], "query": "无关问题"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200 and resp.json()["refused"] is True


async def test_ask_denied_dedup(client, auth_headers):
    kb_id = await _create_kb(client, auth_headers, "去重库")
    key = await _create_key(client, auth_headers)
    resp = await client.post(
        "/api/agent/ask",
        json={"kb_ids": [kb_id, kb_id, 99999, 99999], "query": "q"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["denied_kb_ids"] == [99999]


async def test_ask_validation_422(client, auth_headers):
    key = await _create_key(client, auth_headers)
    resp = await client.post(
        "/api/agent/ask", json={"kb_ids": [], "query": ""},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422


async def test_ask_quota_429_with_retry_after_header(
    client, auth_headers, monkeypatch
):
    from app.core.config import settings
    from app.services import agent_ratelimit
    from tests.test_agent_ratelimit import FakeRedis

    kb_id = await _create_kb(client, auth_headers, "配额库")
    key = await _create_key(client, auth_headers)
    key_id = (await client.get("/api/auth/keys", headers=auth_headers)
              ).json()[0]["id"]
    fake_redis = FakeRedis()
    monkeypatch.setattr(agent_ratelimit, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(settings, "AGENT_ASK_DAILY_TOKENS", 100)
    await agent_ratelimit.quota_consume(key_id, 100)  # 烧穿

    resp = await client.post(
        "/api/agent/ask",
        json={"kb_ids": [kb_id], "query": "q"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["code"] == "quota_exhausted" and detail["retry_after"] >= 1
    assert int(resp.headers["Retry-After"]) >= 1


async def test_ask_audit_written(client, auth_headers, db_session,
                                 monkeypatch):
    kb_id = await _create_kb(client, auth_headers, "审计ask库")
    key = await _create_key(client, auth_headers)
    _ask_graph_patch(monkeypatch, "正常答案")
    await client.post(
        "/api/agent/ask",
        json={"kb_ids": [kb_id], "query": "q"},
        headers={"Authorization": f"Bearer {key}"},
    )
    db_session.expire_all()  # AsyncSession.expire_all 为同步方法,不可 await
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "agent.ask")
    )).scalars().all()
    assert len(rows) == 1
    d = rows[0].detail
    assert d["client"] == "rest" and d["refused"] is False \
        and "tokens" in d and "elapsed_ms" in d


async def test_key_last_used_at_persisted(client, auth_headers):
    """小项⑤:key 调用后 last_used_at 落库(REST 面经端点 commit 持久化)。"""
    key = await _create_key(client, auth_headers)
    await client.get("/api/agent/kbs",
                     headers={"Authorization": f"Bearer {key}"})
    rows = (await client.get("/api/auth/keys", headers=auth_headers)).json()
    assert rows[0]["last_used_at"] is not None
```

同时在 `backend/tests/test_agent_ratelimit.py` 的 `test_api_429` 末尾补一行断言(小项① REST 限流头):

```python
    assert int(resp2.headers["Retry-After"]) >= 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_agent_api.py -v -k ask`
Expected: 新用例 FAIL(404,路由不存在);`test_key_last_used_at_persisted` 可能已 PASS(存量行为,属正常)。

- [ ] **Step 3: 实现 schema 与端点**

`backend/app/schemas/agent.py` 末尾追加:

```python
class AgentAskIn(BaseModel):
    kb_ids: list[int] = Field(min_length=1, max_length=5)
    query: str = Field(min_length=1, max_length=500)
    rerank: bool = False


class AgentAskOut(BaseModel):
    answer: str
    citations: list[dict]
    refused: bool
    tokens_used: int
    elapsed_ms: int
```

`backend/app/api/agent.py`:

import 区改为:

```python
from app.schemas.agent import (
    AgentAskIn,
    AgentAskOut,
    AgentHitOut,
    AgentKbListOut,
    AgentKbOut,
    AgentSearchIn,
    AgentSearchOut,
)
from app.services import agent_facade
from app.services.agent_ratelimit import (
    allow as rate_allow,
    quota_check,
    quota_consume,
)
```

`_check_rate` 的 429 分支加头:

```python
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "retry_after": retry_after},
            headers={"Retry-After": str(retry_after)},
        )
```

`_check_rate` 之后新增:

```python
async def _check_quota(principal: Principal) -> None:
    """ask 前置配额检查(仅 api_key);429 附 Retry-After 头。"""
    if principal.kind != "api_key" or principal.key_id is None:
        return
    ok, retry_after = await quota_check(principal.key_id)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail={"code": "quota_exhausted", "retry_after": retry_after},
            headers={"Retry-After": str(retry_after)},
        )
```

文件末尾追加端点:

```python
@router.post("/ask", response_model=AgentAskOut)
async def agent_ask(
    payload: AgentAskIn,
    request: Request,
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    await _check_rate(principal)
    await _check_quota(principal)
    try:
        outcome = await agent_facade.agent_ask(
            db, principal.user, payload.kb_ids, payload.query, payload.rerank,
        )
    except agent_facade.AgentKbDenied as e:
        raise HTTPException(
            status_code=403,
            detail={"code": "kb_forbidden", "denied_kb_ids": e.denied_kb_ids},
        )
    if principal.kind == "api_key" and principal.key_id is not None:
        await quota_consume(principal.key_id, outcome.tokens_used)
    await audit(
        db, principal.user.username, "agent.ask", "agent",
        {"client": "rest", "key_name": principal.key_name,
         "kb_ids": payload.kb_ids, "query": payload.query[:200],
         "refused": outcome.refused, "tokens": outcome.tokens_used,
         "elapsed_ms": outcome.elapsed_ms},
        ip=_ip(request),
    )
    await db.commit()
    return AgentAskOut(
        answer=outcome.answer, citations=outcome.citations,
        refused=outcome.refused, tokens_used=outcome.tokens_used,
        elapsed_ms=outcome.elapsed_ms,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_agent_api.py tests/test_agent_ratelimit.py tests/test_agent_ask.py -v`
Expected: 全 PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/schemas/agent.py backend/app/api/agent.py backend/tests/test_agent_api.py backend/tests/test_agent_ratelimit.py
git commit -m "feat(agent): REST /api/agent/ask with quota gate, Retry-After headers, audit"
```

---

### Task 4: MCP ask_knowledge_base 工具 + docstring 同源 + 中间件 429 头

**Files:**
- Modify: `backend/app/mcp_server.py`(新工具、`_send_json` 头参数、search docstring f-string)
- Test: `backend/tests/test_mcp.py`(追加 4 个用例 + 改 1 个既有断言)

**Interfaces:**
- Consumes: Task 1 `quota_check/quota_consume`、Task 2 `agent_facade.agent_ask`。
- Produces: MCP 工具 `ask_knowledge_base(kb_ids: list[int], query: str, rerank: bool = False) -> dict`(键:answer/citations/refused/tokens_used/elapsed_ms);`_send_json(send, status, body, extra_headers=())`;search 工具 docstring 默认值同源(小项④)。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_mcp.py` 追加(msg_id 从 10 起避免与既有冲突):

```python
# ---- M10:ask_knowledge_base ----

async def test_mcp_ask_tool(mcp_client, auth_headers, monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services import agent_facade
    from app.services.chat_graph import nodes
    from app.services.chat_graph.graph import build_graph
    from tests.test_agent_api import _create_kb, _create_key

    kb_id = await _create_kb(mcp_client, auth_headers, "MCP问答库")
    key = await _create_key(mcp_client, auth_headers)

    async def fake_hybrid(db, kb_ids, query, top_k=20):
        from app.services.retrieval.searcher import SearchHit
        return [SearchHit(chunk_id=1, document_id=10, kb_id=kb_id,
                          filename="a.pdf", page_no=1, content="八千五百米",
                          score=0.9, source="both")]

    monkeypatch.setattr(nodes, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(agent_facade, "_ask_graph",
                        build_graph(llm=FakeListChatModel(
                            responses=["巡航升限为八千五百米。"]),
                            checkpointer=None))
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    resp = await mcp_client.post(
        "/mcp",
        json=_rpc("tools/call",
                  {"name": "ask_knowledge_base",
                   "arguments": {"kb_ids": [kb_id], "query": "升限?"}}, 10),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    body = _tool_result(resp.json())
    assert body["answer"] == "巡航升限为八千五百米。"
    assert body["refused"] is False and body["tokens_used"] == 0
    assert {"citations", "elapsed_ms"} <= set(body)


async def test_mcp_ask_quota_toolerror(mcp_client, auth_headers, monkeypatch):
    from app.core.config import settings
    from app.services import agent_ratelimit
    from tests.test_agent_api import _create_kb, _create_key
    from tests.test_agent_ratelimit import FakeRedis

    kb_id = await _create_kb(mcp_client, auth_headers, "MCP配额库")
    key = await _create_key(mcp_client, auth_headers)
    key_id = (await mcp_client.get("/api/auth/keys",
                                   headers=auth_headers)).json()[0]["id"]
    fake_redis = FakeRedis()
    monkeypatch.setattr(agent_ratelimit, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(settings, "AGENT_ASK_DAILY_TOKENS", 100)
    await agent_ratelimit.quota_consume(key_id, 100)

    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    resp = await mcp_client.post(
        "/mcp",
        json=_rpc("tools/call",
                  {"name": "ask_knowledge_base",
                   "arguments": {"kb_ids": [kb_id], "query": "q"}}, 11),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    text = resp.json()["result"]["content"][0]["text"]
    assert "quota_exhausted" in text and "retry_after" in text


async def test_mcp_429_retry_after_header(mcp_client, auth_headers,
                                          monkeypatch):
    from app.core.config import settings
    from app.services import agent_ratelimit
    from tests.test_agent_api import _create_key
    from tests.test_agent_ratelimit import FakeRedis

    key = await _create_key(mcp_client, auth_headers)
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 1)
    fake_redis = FakeRedis()
    monkeypatch.setattr(agent_ratelimit, "get_redis", lambda: fake_redis)
    hdr = {"Authorization": f"Bearer {key}", "Accept": ACCEPT}
    assert (await mcp_client.post("/mcp", json=INIT, headers=hdr)).status_code == 200
    resp = await mcp_client.post("/mcp", json=INIT, headers=hdr)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1


async def test_mcp_key_last_used_at_updated(mcp_client, auth_headers,
                                            db_session):
    """小项⑤:MCP 面经中间件显式 commit,last_used_at 须落库。"""
    from sqlalchemy import select

    from app.models import ApiKey
    from tests.test_agent_api import _create_key

    key = await _create_key(mcp_client, auth_headers)
    hdr = {"Authorization": f"Bearer {key}"}
    sid = await _init(mcp_client, hdr)
    await mcp_client.post(
        "/mcp", json=_rpc("tools/list", {}, 12),
        headers={"Accept": ACCEPT, **hdr, "mcp-session-id": sid},
    )
    db_session.expire_all()  # AsyncSession.expire_all 为同步方法,不可 await
    row = (await db_session.execute(select(ApiKey))).scalars().one()
    assert row.last_used_at is not None
```

既有 `test_mcp_initialize_and_tools_list` 的断言改为含三工具:

```python
    names = [t["name"] for t in resp.json()["result"]["tools"]]
    assert "list_knowledge_bases" in names
    assert "search_knowledge_base" in names
    assert "ask_knowledge_base" in names
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_mcp.py -v`
Expected: 新用例 FAIL(`ask_knowledge_base` 不在 tools/list / unknown tool);tools/list 断言 FAIL。

- [ ] **Step 3: 实现 mcp_server 改动**

`backend/app/mcp_server.py`:

import 区 `from app.services.agent_ratelimit import allow as rate_allow` 改为:

```python
from app.services.agent_ratelimit import (
    allow as rate_allow,
    quota_check,
    quota_consume,
)
```

search 工具整体替换为 f-string docstring 版(小项④;签名默认值本就取 settings,消除硬编码"默认 8"漂移):

```python
_TOP_K_DEFAULT = settings.RETRIEVAL_TOP_K


@mcp.tool
async def search_knowledge_base(
    kb_ids: list[int], query: str,
    top_k: int = settings.RETRIEVAL_TOP_K, rerank: bool = False,
) -> dict:
    f"""在指定知识库中混合检索(向量 + 关键词,RRF 融合)。

    Args:
        kb_ids: 知识库 id 列表(1~5 个),须为当前密钥有权访问的库。
        query: 检索问题,1~500 字。
        top_k: 命中条数上限,1~20,默认 {_TOP_K_DEFAULT}。
        rerank: 是否启用 rerank 重排(服务端未配置 rerank 时忽略)。

    Returns:
        {{"hits": [{{chunk_id, document_id, kb_id, filename, page_no,
        content, score, source}}], "total", "elapsed_ms"}}
    """
    p = _principal()
    if not 1 <= len(kb_ids) <= 5:
        raise ToolError("kb_ids must contain 1~5 ids")
    if not 1 <= len(query) <= 500:
        raise ToolError("query must be 1~500 chars")
    if not 1 <= top_k <= 20:
        raise ToolError("top_k must be 1~20")
    async with SessionLocal() as db:
        try:
            outcome = await agent_facade.agent_search(
                db, p.user, kb_ids, query, top_k, rerank,
            )
        except agent_facade.AgentKbDenied as e:
            raise ToolError(f"kb_forbidden, denied_kb_ids={e.denied_kb_ids}")
        await audit(db, p.user.username, "agent.search", "agent",
                    {"client": "mcp", "key_name": p.key_name, "kb_ids": kb_ids,
                     "query": query[:200], "hit_count": len(outcome.hits)},
                    ip=current_client_ip.get())
        await db.commit()
        return {"hits": [asdict(h) for h in outcome.hits],
                "total": len(outcome.hits),
                "elapsed_ms": outcome.elapsed_ms}
```

search 工具之后新增 ask 工具:

```python
@mcp.tool
async def ask_knowledge_base(
    kb_ids: list[int], query: str, rerank: bool = False,
) -> dict:
    """基于知识库内容直接生成回答(单轮、非流式、带引用)。

    与 search_knowledge_base 的区别:本工具返回服务端生成的完整答案与
    引用编号(质量与 AIRag Web 端一致,含查询改写/检索自评/多跳兜底),
    而非检索片段。

    Args:
        kb_ids: 知识库 id 列表(1~5 个),须为当前密钥有权访问的库。
        query: 问题,1~500 字。单轮无上下文,追问请携带完整问题。
        rerank: 是否启用 rerank 重排(服务端未配置 rerank 时忽略)。

    Returns:
        {"answer", "citations": [{number, chunk_id, document_id,
        filename, page_no, excerpt}], "refused", "tokens_used",
        "elapsed_ms"}。refused=true 表示知识库中未找到相关内容。
        内部多步 LLM 调用,耗时可达 40~90 秒,客户端超时请设充足
        (如 Claude Code 的 MCP_TIMEOUT)。
    """
    p = _principal()
    if not 1 <= len(kb_ids) <= 5:
        raise ToolError("kb_ids must contain 1~5 ids")
    if not 1 <= len(query) <= 500:
        raise ToolError("query must be 1~500 chars")
    if p.kind == "api_key" and p.key_id is not None:
        ok, retry_after = await quota_check(p.key_id)
        if not ok:
            raise ToolError(f"quota_exhausted, retry_after={retry_after}s")
    async with SessionLocal() as db:
        try:
            outcome = await agent_facade.agent_ask(
                db, p.user, kb_ids, query, rerank,
            )
        except agent_facade.AgentKbDenied as e:
            raise ToolError(f"kb_forbidden, denied_kb_ids={e.denied_kb_ids}")
        if p.kind == "api_key" and p.key_id is not None:
            await quota_consume(p.key_id, outcome.tokens_used)
        await audit(db, p.user.username, "agent.ask", "agent",
                    {"client": "mcp", "key_name": p.key_name,
                     "kb_ids": kb_ids, "query": query[:200],
                     "refused": outcome.refused,
                     "tokens": outcome.tokens_used,
                     "elapsed_ms": outcome.elapsed_ms},
                    ip=current_client_ip.get())
        await db.commit()
        return {"answer": outcome.answer, "citations": outcome.citations,
                "refused": outcome.refused,
                "tokens_used": outcome.tokens_used,
                "elapsed_ms": outcome.elapsed_ms}
```

`_send_json` 加头参数,`AgentAuthMiddleware.__call__` 的 429 分支传头:

```python
async def _send_json(send, status: int, body: dict,
                     extra_headers: tuple = ()) -> None:
    payload = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            *extra_headers]})
    await send({"type": "http.response.body", "body": payload})
```

中间件 429 分支(小项① MCP 面):

```python
            if not ok:
                await _send_json(
                    send, 429,
                    {"detail": {"code": "rate_limited", "retry_after": retry_after}},
                    extra_headers=((b"retry-after",
                                    str(retry_after).encode()),),
                )
                return
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_mcp.py -v`
Expected: 全 PASS(含改后的 tools/list 断言)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/mcp_server.py backend/tests/test_mcp.py
git commit -m "feat(agent): MCP ask_knowledge_base tool, docstring config-sourced, 429 Retry-After"
```

---

### Task 5: AGENT_API_ENABLED=false 冒烟(build_api_router 重构)

**Files:**
- Modify: `backend/app/api/__init__.py`(路由装配抽成函数)
- Modify: `backend/app/main.py`(create_app 内现装配)
- Test: `backend/tests/test_agent_api.py`(追加冒烟用例)

**Interfaces:**
- Consumes: `settings.AGENT_API_ENABLED`、`create_app()`。
- Produces: `build_api_router() -> APIRouter`(app/api/__init__.py;模块级 `api_router = build_api_router()` 保留,存量导入不受影响)。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_agent_api.py` 末尾追加:

```python
async def test_agent_api_disabled_smoke(monkeypatch):
    """小项②:总开关关闭后 agent REST 与 /mcp 全 404,常规路由不受影响。"""
    from httpx import ASGITransport, AsyncClient

    from app.core.config import settings
    from app.main import create_app

    monkeypatch.setattr(settings, "AGENT_API_ENABLED", False)
    app2 = create_app()
    async with AsyncClient(transport=ASGITransport(app=app2),
                           base_url="http://t") as c:
        assert (await c.get("/api/health")).status_code == 200
        assert (await c.get("/api/agent/kbs")).status_code == 404
        assert (await c.post("/api/agent/ask", json={})).status_code == 404
        assert (await c.post("/mcp", json={})).status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_agent_api.py::test_agent_api_disabled_smoke -v`
Expected: FAIL——monkeypatch 生效但模块级路由早已装配(app2 仍含 agent 路由与挂载),`/api/agent/kbs` 返回 401 而非 404。

- [ ] **Step 3: 重构路由装配**

`backend/app/api/__init__.py` 整体替换为:

```python
from fastapi import APIRouter

from app.api.admin import router as admin_router
from app.api.agent import router as agent_router
from app.api.ask import router as ask_router
from app.api.auth import router as auth_router
from app.api.conversations import router as conversations_router
from app.api.documents import router as documents_router
from app.api.kbs import router as kbs_router
from app.api.users import router as users_router
from app.core.config import settings


def build_api_router() -> APIRouter:
    """装配 /api 路由;抽成函数使 AGENT_API_ENABLED 在 create_app 期读取,
    冒烟测试可 monkeypatch settings 后重建应用验证 404 语义。"""
    router = APIRouter()

    @router.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    router.include_router(auth_router)
    router.include_router(kbs_router)
    router.include_router(users_router)
    router.include_router(documents_router)
    router.include_router(conversations_router)
    router.include_router(ask_router)
    router.include_router(admin_router)
    # M9:agent REST 面为进程启动期开关(无运行时切换)
    if settings.AGENT_API_ENABLED:
        router.include_router(agent_router)
    return router


api_router = build_api_router()
```

`backend/app/main.py` 中 `from app.api import api_router` 改为 `from app.api import build_api_router`,`create_app` 内 `app.include_router(api_router, prefix="/api")` 改为 `app.include_router(build_api_router(), prefix="/api")`。

- [ ] **Step 4: 跑测试确认通过(全量回归路由层)**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_agent_api.py tests/test_health.py tests/test_auth.py -v`
Expected: 全 PASS(存量路由不受重构影响)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/__init__.py backend/app/main.py backend/tests/test_agent_api.py
git commit -m "refactor(api): build_api_router factory enables agent-off smoke test"
```

---

### Task 6: 限流 redis key 命名对齐 spec

**Files:**
- Modify: `backend/app/services/agent_ratelimit.py`(allow 收 int key_id)
- Modify: `backend/app/api/agent.py`、`backend/app/mcp_server.py`(调用点去 `key:` 前缀)
- Test: `backend/tests/test_agent_ratelimit.py`(既有用例参数改 int + 新增形状断言)

**Interfaces:**
- Consumes: —
- Produces: `allow(key_id: int) -> tuple[bool, int]`(redis key `agent_rl:{key_id}`,对齐 M9 spec §E;Task 3/4 的调用点随本任务同步改)。

- [ ] **Step 1: 改测试(失败先行)**

`backend/tests/test_agent_ratelimit.py`:

- `allow` 的调用统一改 int:`allow("key:1")→allow(1)`、`allow("key:2")→allow(2)`、`allow("key:3")→allow(3)`、`allow("key:4")→allow(4)`;
- `test_ident_isolated` 的 `allow("key:a")/allow("key:b")` 改 `allow(101)/allow(102)`;
- 新增:

```python
async def test_rl_key_shape(fake_redis, monkeypatch):
    """小项③:redis key 对齐 M9 spec §E 的 agent_rl:{key_id}。"""
    monkeypatch.setattr(settings, "AGENT_RATE_LIMIT_PER_MIN", 5)
    await allow(9)
    assert "agent_rl:9" in fake_redis.z
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_agent_ratelimit.py -v`
Expected: `test_rl_key_shape` FAIL(实际 key 为 `agent_rl:key:9`)。

- [ ] **Step 3: 实现**

`backend/app/services/agent_ratelimit.py` 的 `allow` 签名与首行改:

```python
async def allow(key_id: int) -> tuple[bool, int]:
    """返回 (是否放行, retry_after 秒);redis key=agent_rl:{key_id}(M9 spec §E)。"""
```

(函数体内 `rkey = f"agent_rl:{ident}"` 改为 `rkey = f"agent_rl:{key_id}"`;其余不动。)

调用点:`backend/app/api/agent.py` `_check_rate` 内 `rate_allow(f"key:{principal.key_id}")` 改 `rate_allow(principal.key_id)`;`backend/app/mcp_server.py` 中间件内 `rate_allow(f"key:{principal.key_id}")` 改 `rate_allow(principal.key_id)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_agent_ratelimit.py tests/test_agent_api.py tests/test_mcp.py -v`
Expected: 全 PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/agent_ratelimit.py backend/app/api/agent.py backend/app/mcp_server.py backend/tests/test_agent_ratelimit.py
git commit -m "refactor(agent): rate-limit redis key aligns spec agent_rl:{key_id}"
```

---

### Task 7: 无头验收脚本 m10_acceptance.py

**Files:**
- Create: `backend/scripts/m10_acceptance.py`
- 不新增 pytest 用例(脚本即验收;跑在真栈上)

**Interfaces:**
- Consumes: 已上线的 REST `/api/agent/ask`、`/api/auth/keys`、`/api/kbs`、Redis(直写配额 key)、PG(审计核对)。
- Produces: 可重复验收命令 `cd backend && .venv\Scripts\python scripts\m10_acceptance.py`(前置:start_dev.bat 起真栈、Redis 可用;退出码 1=失败)。

- [ ] **Step 1: 写脚本**

新建 `backend/scripts/m10_acceptance.py`(结构复刻 m9_acceptance:health 门→建用户/库/文档→场景→清库):

```python
"""M10 无头验收脚本(真栈:http://127.0.0.1:8001 + worker + Redis)。

用法(backend 目录,项目 venv,start_dev.bat 已起服务):
    .venv\\Scripts\\python scripts\\m10_acceptance.py

覆盖:ask 正常(答案+引用)/ ask 拒答(refused)/ 无权库 403 /
配额烧穿 429+Retry-After 头 / 审计 agent.ask 落库。
真实 LLM(智谱),答案断言宽松(内容或拒答语义)。
"""
import asyncio
import datetime as dt
import sys
import time
import uuid

import httpx
import pymupdf as fitz

BASE = "http://127.0.0.1:8001"
API = f"{BASE}/api"
TIMEOUT = httpx.Timeout(180.0)  # ask 全链路 40~90s/次
RESULTS = {"pass": [], "fail": [], "skip": []}

FACT = "青鸾号高空气船的巡航升限为八千五百米"
FACT_Q = "青鸾号高空气船的巡航升限是多少米?"
OFF_Q = "《红楼梦》的作者是谁?"
SUFFIX = uuid.uuid4().hex[:6]
PDF_MIME = "application/pdf"


def check(name, cond, detail=""):
    (RESULTS["pass"] if cond else RESULTS["fail"]).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f"  {detail}" if detail and not cond else ""))


def skip(name, why):
    RESULTS["skip"].append(name)
    print(f"SKIP {name}  ({why})")


def summary_and_exit():
    total = sum(len(v) for v in RESULTS.values())
    print(f"\nM10 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
    if RESULTS["fail"]:
        print("FAILED:", *RESULTS["fail"], sep="\n  - ")
        sys.exit(1)


def _nullpool_sessionmaker():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import settings

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def promote_roles(usernames: dict[str, str]) -> None:
    from sqlalchemy import text

    engine, maker = _nullpool_sessionmaker()
    try:
        async with maker() as s:
            for username, role in usernames.items():
                await s.execute(
                    text("UPDATE users SET role = :r WHERE username = :u"),
                    {"r": role, "u": username},
                )
            await s.commit()
    finally:
        await engine.dispose()


async def cleanup(user_ids: list[int], kb_ids: list[int],
                  usernames: list[str]) -> None:
    from sqlalchemy import text

    engine, maker = _nullpool_sessionmaker()
    try:
        async with maker() as s:
            for uid in user_ids:
                await s.execute(
                    text("DELETE FROM api_keys WHERE user_id = :u"), {"u": uid})
            for kid in kb_ids:
                await s.execute(
                    text("DELETE FROM chunks WHERE kb_id = :k"), {"k": kid})
                await s.execute(
                    text("DELETE FROM documents WHERE kb_id = :k"), {"k": kid})
                await s.execute(
                    text("DELETE FROM kb_permissions WHERE kb_id = :k"),
                    {"k": kid})
                await s.execute(
                    text("DELETE FROM knowledge_bases WHERE id = :k"), {"k": kid})
            for name in usernames:
                await s.execute(
                    text("DELETE FROM users WHERE username = :n"), {"n": name})
            await s.commit()
    finally:
        await engine.dispose()


async def make_user(c: httpx.AsyncClient, role="editor") -> tuple[str, int]:
    name = f"m10_{SUFFIX}_{role}"
    r = await c.post(f"{API}/auth/register",
                     json={"username": name, "password": "secret123"})
    r.raise_for_status()
    await promote_roles({name: role})
    return name, r.json()["id"]


async def login(c: httpx.AsyncClient, username: str) -> dict:
    r = await c.post(f"{API}/auth/login",
                     json={"username": username, "password": "secret123"})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def create_key(c: httpx.AsyncClient, jwt: dict, name: str) -> dict:
    r = await c.post(f"{API}/auth/keys",
                     json={"name": name, "expires_in_days": 1}, headers=jwt)
    r.raise_for_status()
    return r.json()


def make_pdf_bytes(title: str, paragraphs: list[str]) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    y = 96
    page.insert_text((72, y), title, fontname="china-s", fontsize=16)
    for p in paragraphs:
        y += 28
        page.insert_text((72, y), p, fontname="china-s", fontsize=12)
    return doc.tobytes()


async def wait_doc_done(c: httpx.AsyncClient, jwt: dict, kb_id: int,
                        doc_id: int | None, timeout_s=120) -> dict:
    if doc_id is None:
        return {"status": "upload_failed"}
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = await c.get(f"{API}/kbs/{kb_id}/documents", headers=jwt)
        r.raise_for_status()
        doc = next((d for d in r.json() if d["id"] == doc_id), None)
        if doc and doc["status"] in ("done", "failed"):
            return doc
        await asyncio.sleep(2)
    return {"status": "timeout", "error_msg": "poll timeout"}


def quota_redis_key(key_id: int) -> str:
    return f"agent_tq:{key_id}:{dt.datetime.now():%Y%m%d}"


async def main():
    user_ids: list[int] = []
    usernames: list[str] = []
    kb_ids: list[int] = []
    touched_redis: list[str] = []

    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            health = await c.get(f"{API}/health")
            health.raise_for_status()
        except Exception as e:
            skip("M10 acceptance(全部场景)", f"服务未启动({e});先跑 start_dev.bat")
            summary_and_exit()
            return

        try:
            # ---- 1. 主用户 + 建库 + 文档 ----
            owner_name, owner_id = await make_user(c, role="editor")
            user_ids.append(owner_id)
            usernames.append(owner_name)
            owner = await login(c, owner_name)

            r = await c.post(f"{API}/kbs",
                             json={"name": f"M10验收库{SUFFIX}"}, headers=owner)
            kb_id = r.json().get("id")
            kb_ids.append(kb_id)
            check("kb created 201", r.status_code == 201 and kb_id > 0,
                  r.text[:200])

            up = await c.post(
                f"{API}/kbs/{kb_id}/documents",
                files={"file": ("m10青鸾号.pdf", make_pdf_bytes(
                    "青鸾号高空气船简介",
                    [FACT, "青鸾号以氦气提供升力。",
                     "青鸾号机身采用轻质碳纤维复合材料制造,可在高空持续巡航数十小时。"]),
                    PDF_MIME)},
                data={"ocr": "auto"}, headers=owner,
            )
            doc = await wait_doc_done(c, owner, kb_id, up.json().get("id"))
            check("pdf upload & processed done",
                  up.status_code == 201 and doc["status"] == "done"
                  and doc["chunk_count"] > 0,
                  f"up={up.status_code} doc={doc}")

            created = await create_key(c, owner, "验收key")
            raw_key, key_id = created["key"], created["id"]
            key_hdr = {"Authorization": f"Bearer {raw_key}"}

            # ---- 2. ask 正常:答案 + 引用 + elapsed ----
            r = await c.post(f"{API}/agent/ask",
                             json={"kb_ids": [kb_id], "query": FACT_Q},
                             headers=key_hdr)
            body = r.json() if r.status_code == 200 else {}
            check("ask 200 answer cites fact",
                  r.status_code == 200
                  and ("八千五百" in body.get("answer", "")
                       or "8500" in body.get("answer", ""))
                  and body.get("refused") is False
                  and len(body.get("citations", [])) >= 1
                  and isinstance(body.get("tokens_used"), int)
                  and isinstance(body.get("elapsed_ms"), int),
                  r.text[:300])

            # ---- 3. ask 拒答:库内只有飞艇资料 ----
            r = await c.post(f"{API}/agent/ask",
                             json={"kb_ids": [kb_id], "query": OFF_Q},
                             headers=key_hdr)
            body = r.json() if r.status_code == 200 else {}
            check("ask refused is 200",
                  r.status_code == 200 and body.get("refused") is True,
                  r.text[:300])

            # ---- 4. 无权库 403(他人 key) ----
            other_name, other_id = await make_user(c, role="viewer")
            user_ids.append(other_id)
            usernames.append(other_name)
            other = await login(c, other_name)
            other_key = (await create_key(c, other, "他人key"))["key"]
            r = await c.post(f"{API}/agent/ask",
                             json={"kb_ids": [kb_id], "query": FACT_Q},
                             headers={"Authorization": f"Bearer {other_key}"})
            detail = r.json().get("detail", {}) if r.status_code == 403 else {}
            check("403 kb_forbidden + denied_kb_ids",
                  r.status_code == 403 and detail.get("code") == "kb_forbidden"
                  and kb_id in detail.get("denied_kb_ids", []), r.text[:200])

            # ---- 5. 配额烧穿:直写 redis 到量 → 429 + Retry-After 头 ----
            from redis import asyncio as aioredis

            from app.core.config import settings as app_settings

            full = await create_key(c, owner, "配额key")
            rkey = quota_redis_key(full["id"])
            redis = aioredis.from_url(app_settings.REDIS_URL,
                                      decode_responses=True)
            try:
                await redis.set(rkey, 999_999_999)
                touched_redis.append(rkey)
                r = await c.post(
                    f"{API}/agent/ask",
                    json={"kb_ids": [kb_id], "query": FACT_Q},
                    headers={"Authorization": f"Bearer {full['key']}"})
                detail = r.json().get("detail", {}) if r.status_code == 429 else {}
                check("quota 429 + Retry-After header",
                      r.status_code == 429
                      and detail.get("code") == "quota_exhausted"
                      and int(r.headers.get("Retry-After", 0)) >= 1,
                      f"{r.status_code} {r.text[:200]}")
            finally:
                for rk in touched_redis:
                    await redis.delete(rk)
                await redis.aclose()

            # ---- 6. 审计 agent.ask 落库 ----
            from sqlalchemy import text

            engine, maker = _nullpool_sessionmaker()
            try:
                async with maker() as s:
                    n = (await s.execute(text(
                        "SELECT count(*) FROM audit_logs "
                        "WHERE action = 'agent.ask'"))).scalar()
            finally:
                await engine.dispose()
            check("agent.ask audit rows", (n or 0) >= 2, f"rows={n}")
        finally:
            try:
                await cleanup(user_ids, kb_ids, usernames)
                print(f"cleanup done: users={usernames} kbs={kb_ids}")
            except Exception as e:
                print(f"cleanup FAILED(需手工清理): {e}")

    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: 语法自检**

Run: `cd backend && .venv\Scripts\python -c "import ast; ast.parse(open('scripts/m10_acceptance.py', encoding='utf-8').read())"`
Expected: 无输出(语法 OK)。

- [ ] **Step 3: 真栈跑验收(服务在跑且 Redis 可用)**

Run: `cd backend && .venv\Scripts\python scripts\m10_acceptance.py`
Expected: `M10 ACCEPTANCE: 5/5 PASS`(ask 正常/拒答/403/配额 429/审计)。服务未起则 SKIP 全量并先起服务再复跑。

- [ ] **Step 4: 提交**

```bash
git add backend/scripts/m10_acceptance.py
git commit -m "test(agent): m10 acceptance script (ask happy/refusal/403/quota/audit)"
```

---

### Task 8: MCP 走查扩展 + README/.env.example + 全量回归

**Files:**
- Modify: `backend/scripts/m91_mcp_walkthrough.py`(加 ask 两步)
- Modify: `README.md`(接入指南更新)
- Modify: `.env.example`(根目录,加 AGENT_ASK_DAILY_TOKENS)

**Interfaces:**
- Consumes: 已上线的 MCP `ask_knowledge_base`。
- Produces: 文档与示例更新;全量绿基线。

- [ ] **Step 1: 扩展走查脚本**

`backend/scripts/m91_mcp_walkthrough.py`:

docstring 覆盖行改为:

```
覆盖:initialize 握手、tools/list、list_knowledge_bases、search_knowledge_base
真调、ask_knowledge_base 真调(答案或拒答)、无权库 ToolError 文案。
退出码 1 = 走查失败。
```

`tools/list` 断言后(`check("tools/list 两工具"...)` 之后)新增:

```python
        check("tools/list 含 ask 工具", "ask_knowledge_base" in names, str(names))
```

`search` 返回结构 check 之后、无权库 try 之前插入:

```python
        r4 = await c.call_tool(
            "ask_knowledge_base", {"kb_ids": [kb_id], "query": query}
        )
        body4 = json.loads(_text(r4))
        check("ask_knowledge_base 返回结构",
              {"answer", "citations", "refused", "tokens_used",
               "elapsed_ms"} <= set(body4), _text(r4)[:200])
        check("ask 答案非空或拒答",
              bool(body4["answer"]) or body4["refused"] is True,
              _text(r4)[:200])
```

- [ ] **Step 2: README 与 .env.example**

`README.md` 外部 Agent 节:

- 标题 `## 外部 Agent 接入(M9)` → `## 外部 Agent 接入(M9/M10)`;
- 首段改为:

```markdown
知识库可通过 API Key 只读开放给外部 Agent(知识库发现 + 检索 + RAG 问答)。密钥在「API 密钥」页面创建,权限与创建者账号一致,可随时吊销。
```

- MCP 可用工具行改为:

```markdown
可用工具:`list_knowledge_bases` / `search_knowledge_base` / `ask_knowledge_base`(直接生成答案+引用,单轮无上下文,内部多步 LLM 耗时 40~90 秒,客户端超时请设充足,如 Claude Code 的 `MCP_TIMEOUT`)。
```

- REST curl 示例末尾追加:

```bash
# RAG 问答(非流式,返回 answer/citations/refused/tokens_used;40~90s)
curl -X POST -H "Authorization: Bearer airag_xxxx" -H "Content-Type: application/json" \
  -d '{"kb_ids":[1],"query":"退货流程"}' \
  http://127.0.0.1:8001/api/agent/ask
```

- 「限流与审计」节末尾追加一行:

```markdown
ask 按 key 每日 token 配额 `AGENT_ASK_DAILY_TOKENS`(默认 200000,0=禁用),超限 429 附 `Retry-After` 头(到次日零点)。
```

`.env.example`(仓库根)在 `AGENT_RATE_LIMIT_PER_MIN=60` 行后加:

```
AGENT_ASK_DAILY_TOKENS=200000
```

- [ ] **Step 3: 全量后端回归**

Run: `cd backend && .venv\Scripts\python -m pytest -q`
Expected: 全 PASS(M9.1 基线 196 + 本计划新增约 22,以实际数目为准,0 fail)。

- [ ] **Step 4: MCP 真客户端走查(真栈 + 真实智谱)**

Run(用现有或新建 key):`cd backend && .venv\Scripts\python scripts\m91_mcp_walkthrough.py <airag_key> 知识库`
Expected: 全 PASS(含 ask 两步)。

- [ ] **Step 5: 提交**

```bash
git add backend/scripts/m91_mcp_walkthrough.py README.md .env.example
git commit -m "docs(agent): ask tool in agent guide, env example, walkthrough extended"
```

---

## 执行记录(执行时回填)

(此节由执行者按任务完成情况回填:偏差、裁决、验收结果。)
