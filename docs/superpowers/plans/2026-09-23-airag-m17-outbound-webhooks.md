# AIRag M17 出站集成(通用 webhook 推送)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 五类事件(文档/评估终态、拒答)经 HMAC 签名的通用 webhook at-least-once 推送到外部系统,admin 全局配置端点+测试发送+投递记录页。

**Architecture:** DB 为真相源——事件点与业务同事务落 `webhook_deliveries`(payload 快照),commit 后 nudge Celery 投递任务;beat 60s 兜底扫描;投递引擎 `deliver_one/deliver_due` 状态机(pending/succeeded/retrying/dead,退避 1/5/15/60 分钟,4xx 永久失败);每次投递用随调用新建关闭的 `httpx.AsyncClient`(规避 M15 跨循环毒化)。

**Tech Stack:** FastAPI + SQLAlchemy async + Alembic + Celery(solo worker + 独立 beat)+ httpx;Vue3 + Element Plus + vitest。

**Spec:** `docs/superpowers/specs/2026-09-23-airag-m17-outbound-webhooks-design.md`(先读;本计划从 spec 出发,冲突以 spec 为准)。

## Global Constraints

- 后端测试:工作目录 `E:\Projects\AIRag\backend`,命令 `.venv\Scripts\python -m pytest tests -q`。基线 **348P/0F**,不得回退。
- 前端测试:工作目录 `E:\Projects\AIRag\frontend`,`npm run test:unit -- --run` 基线 **61/61**;`npm run build` 零错。
- Windows CMD:没有 `ls`,用 `dir`。
- conventional commits,每任务一提交,不 push(收官统一)。
- **secret 原文存储但永不回显**:仅 POST 响应与 PUT rotate 响应一次性明文;GET/列表一律 masked(`wh_****` + 尾 4 位)。
- **投递客户端绝不跨调用缓存**:每次 deliver_due/deliver_one 调用内新建 httpx.AsyncClient 并随调用关闭(M15 worker 跨循环毒化教训,见 `eval_runner.py:136-147` 注释)。
- 事件发射与业务同事务(audit() 同哲学,`emit_event` 不自行 commit);nudge 必须在业务 commit 之后。
- 注释中文、简洁、讲约束/为什么;schema/组件镜像后端字段命名。
- 不引新依赖(httpx 已有;测试用 monkeypatch,不引 respx)。

---

### Task 1: 数据模型 + 迁移(webhook 两表)

**Files:**
- Create: `backend/app/models/webhook.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/alembic/versions/a8b9c0d1e2f3_m17_webhook_tables.py`
- Test: `backend/tests/test_webhook_models.py`

**Interfaces:**
- Produces: `WebhookEndpoint`(id/name/url/secret/events JSON/enabled/created_by/description/TimestampMixin)、`WebhookDelivery`(id/endpoint_id FK CASCADE/event_type/event_id/payload JSON/status/attempts/next_attempt_at/response_status/last_error/TimestampMixin);后续任务从 `app.models` 导入这两个名字。
- 迁移:`revision="a8b9c0d1e2f3"`,`down_revision="f6a7b8c9d0e1"`(当前 head,已核 `alembic/versions/` 目录)。

- [ ] **Step 1: 写失败测试(Create test_webhook_models.py)**

```python
# backend/tests/test_webhook_models.py
"""M17 T1:webhook 两表模型与级联。"""
from sqlalchemy import select

from app.models import WebhookDelivery, WebhookEndpoint


async def test_endpoint_delivery_roundtrip_and_cascade(db_session):
    ep = WebhookEndpoint(name="ops", url="http://x/hook",
                         secret="wh_abc123", events=["document.done"],
                         created_by=1)
    db_session.add(ep)
    await db_session.flush()
    d = WebhookDelivery(endpoint_id=ep.id, event_type="document.done",
                        event_id="a" * 32,
                        payload={"event_id": "a" * 32}, status="pending")
    db_session.add(d)
    await db_session.commit()

    rows = (await db_session.execute(select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1 and rows[0].status == "pending"
    assert rows[0].attempts == 0 and rows[0].next_attempt_at is None

    await db_session.delete(ep)  # FK CASCADE
    await db_session.commit()
    assert (await db_session.execute(
        select(WebhookDelivery))).scalars().all() == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_webhook_models.py -q`
Expected: FAIL —— `ImportError: cannot import name 'WebhookEndpoint'`。

- [ ] **Step 3: 实现**

`backend/app/models/webhook.py`(参照 `app/models/eval.py` 风格):

```python
# backend/app/models/webhook.py
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class WebhookEndpoint(Base, TimestampMixin):
    """M17:出站 webhook 端点(admin 全局);secret 原文存储——HMAC 签名
    需要原文,与 ApiKey hash 模式 deliberately 不同,任何读路径不得回显。"""
    __tablename__ = "webhook_endpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    url: Mapped[str] = mapped_column(String(500))
    secret: Mapped[str] = mapped_column(String(64))
    events: Mapped[list | None] = mapped_column(JSON)   # None/空=订阅全部
    enabled: Mapped[bool] = mapped_column(Boolean, default=True,
                                          server_default="true")
    created_by: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(String(200))


class WebhookDelivery(Base, TimestampMixin):
    """单事件×单端点的一次投递;payload 是信封快照(event_id 幂等键)。
    状态机:pending → succeeded | retrying →(到期)succeeded|retrying|dead;
    4xx(非 429)直接 dead(对方拒收,重试无意义)。"""
    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    event_id: Mapped[str] = mapped_column(String(36))
    payload: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    response_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(500))
```

(顶部 `from datetime import datetime`;模型注释密度照 eval.py。)

`models/__init__.py` 加 `from app.models.webhook import WebhookDelivery, WebhookEndpoint` 并进 `__all__`。

迁移 `a8b9c0d1e2f3_m17_webhook_tables.py`:照抄最近迁移(f6a7b8c9d0e1)的文件骨架,`down_revision="f6a7b8c9d0e1"`,upgrade 建两表(列与模型一致,含 unique/默认值/索引:`webhook_endpoints.name` 唯一、`webhook_deliveries.endpoint_id` 与 `status` 建索引),downgrade drop 两表。写完跑 `.venv\Scripts\python -m alembic upgrade head` 应无输出成功(本地 dev 库),`.venv\Scripts\python -m alembic downgrade -1` 再 upgrade 回来各一次验证可逆。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_webhook_models.py -q` → PASS;全量 `tests -q` → **~349P/0F**。

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/webhook.py backend/app/models/__init__.py backend/alembic/versions/a8b9c0d1e2f3_m17_webhook_tables.py backend/tests/test_webhook_models.py
git commit -m "feat(webhook): endpoint and delivery models with migration"
```

---

### Task 2: outbound 服务(emit/签名/投递状态机)+ 配置

**Files:**
- Create: `backend/app/services/outbound.py`
- Modify: `backend/app/core/config.py`(加 2 配置)、`.env.example`(根目录,注意在 `E:\Projects\AIRag\.env.example`)
- Test: `backend/tests/test_outbound.py`

**Interfaces:**
- Consumes: T1 两模型;`settings.WEBHOOK_MAX_ATTEMPTS/WEBHOOK_TIMEOUT_S`
- Produces(后续任务依赖的精确签名):
  - `EVENT_TYPES: tuple[str, ...] = ("document.done", "document.failed", "eval.completed", "eval.failed", "chat.refused")`
  - `BACKOFF_MINUTES: tuple[int, ...] = (1, 5, 15, 60, 60)`
  - `async def emit_event(db, event_type: str, data: dict) -> int`(同事务插 pending 行,返回行数;**不 commit**)
  - `def sign_headers(secret: str, event_type: str, body: str) -> dict[str, str]`(返回 X-AIRag-Event/X-AIRag-Timestamp/X-AIRag-Signature + Content-Type 四头;签名=hex(HMAC-SHA256(secret, f"{ts}.{body}")))
  - `async def deliver_one(db, delivery: WebhookDelivery, client: httpx.AsyncClient | None = None) -> None`(单行投递并落结果,内部逐行 commit;client None 时 `async with httpx.AsyncClient()` 自建)
  - `async def deliver_due(db, client: httpx.AsyncClient | None = None) -> int`(扫 pending + 到期 retrying,每批 50 调 deliver_one,返回处理行数)
  - `def nudge() -> None`(`deliver_pending.delay()` try/except 失败仅告警——beat 兜底)
  - 信封构造:`def _envelope(event_type: str, data: dict) -> dict` = `{"event_id": uuid4().hex, "event_type": ..., "occurred_at": datetime.now(timezone.utc).isoformat(), "data": data}`

- [ ] **Step 1: 写失败测试(Create test_outbound.py,核心用例全给)**

```python
# backend/tests/test_outbound.py
"""M17 T2:emit 展开/信封/签名公式/投递状态机全分支。"""
import hashlib
import hmac as hmac_mod
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.models import WebhookDelivery, WebhookEndpoint
from app.services.outbound import (
    BACKOFF_MINUTES,
    emit_event,
    sign_headers,
)


async def _mk_ep(db_session, events=None, enabled=True, url="http://x/h"):
    ep = WebhookEndpoint(name=f"ep{time.time_ns()}", url=url,
                         secret="wh_s3cret", events=events, enabled=enabled,
                         created_by=1)
    db_session.add(ep)
    await db_session.commit()
    return ep


async def test_emit_expands_only_matching_enabled(db_session):
    """订阅匹配展开;空订阅=全部;禁用排除。"""
    from sqlalchemy import select
    await _mk_ep(db_session, events=["document.done"])
    await _mk_ep(db_session, events=[])                 # 空=订阅全部
    await _mk_ep(db_session, events=["document.failed"], enabled=False)
    n = await emit_event(db_session, "document.failed", {"x": 1})
    await db_session.commit()
    assert n == 1  # 只有「空订阅」端点命中(禁用的排除)
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1
    assert rows[0].payload["event_type"] == "document.failed"
    assert rows[0].payload["data"] == {"x": 1}
    assert len(rows[0].payload["event_id"]) == 32       # uuid4().hex 幂等键
    assert rows[0].status == "pending"


async def test_emit_no_endpoints_zero_rows(db_session):
    """无订阅端点=零行零副作用(系统默认关闭)。"""
    from sqlalchemy import select
    assert await emit_event(db_session, "eval.completed", {}) == 0
    await db_session.commit()
    assert (await db_session.execute(
        select(WebhookDelivery))).scalars().all() == []
```

```python
async def test_emit_envelope_and_persistence(db_session):
    await _mk_ep(db_session, events=["chat.refused"])
    await emit_event(db_session, "chat.refused",
                     {"source": "web", "kb_ids": [1], "question": "q"})
    await db_session.commit()
    row = (await db_session.execute(
        select(WebhookDelivery))).scalars().one()
    assert row.event_type == "chat.refused"
    assert row.payload["data"]["source"] == "web"
    assert row.event_id == row.payload["event_id"]
    assert row.status == "pending" and row.attempts == 0


def test_sign_headers_formula():
    body = json.dumps({"a": 1}, ensure_ascii=False)
    h = sign_headers("wh_s3cret", "document.done", body)
    ts = h["X-AIRag-Timestamp"]
    expected = hmac_mod.new(
        b"wh_s3cret", f"{ts}.{body}".encode(),
        hashlib.sha256).hexdigest()
    assert h["X-AIRag-Signature"] == expected
    assert h["X-AIRag-Event"] == "document.done"
    assert h["Content-Type"] == "application/json; charset=utf-8"
```

投递状态机(fake client 注入;`deliver_one/db` 直接驱动):

```python
class _Resp:
    def __init__(self, code): self.status_code = code


class _FakeClient:
    """记录 POST;按 URL 路径返回预设状态码或抛异常。"""
    def __init__(self, routes): self.routes, self.calls = routes, []
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, **kw):
        self.calls.append((url, kw))
        r = self.routes.get(url.rstrip("/").rsplit("/", 1)[-1])
        if isinstance(r, Exception): raise r
        return _Resp(r)


async def _pending(db_session, url_path, status="pending", attempts=0,
                   nxt=None):
    ep = await _mk_ep(db_session, events=[])
    ep.url = f"http://h/{url_path}"
    await db_session.commit()
    d = WebhookDelivery(endpoint_id=ep.id, event_type="test",
                        event_id="e" * 32, payload={"event_id": "e" * 32},
                        status=status, attempts=attempts, next_attempt_at=nxt)
    db_session.add(d)
    await db_session.commit()
    return d


async def test_deliver_success_2xx(db_session):
    from app.services.outbound import deliver_one
    d = await _pending(db_session, "ok", )
    c = _FakeClient({"ok": 200})
    await deliver_one(db_session, d, client=c)
    db_session.refresh(d)
    assert d.status == "succeeded" and d.response_status == 200
    url, kw = c.calls[0]
    assert kw["content"] == json.dumps(d.payload, ensure_ascii=False)
    assert "X-AIRag-Signature" in kw["headers"]


async def test_deliver_5xx_and_429_retry_with_backoff(db_session):
    from app.services.outbound import deliver_one
    d = await _pending(db_session, "boom")
    await deliver_one(db_session, d, client=_FakeClient({"boom": 500}))
    db_session.refresh(d)
    assert d.status == "retrying" and d.attempts == 1
    assert d.next_attempt_at > datetime.now(timezone.utc)
    assert d.last_error

    d2 = await _pending(db_session, "rate")
    await deliver_one(db_session, d2, client=_FakeClient({"rate": 429}))
    db_session.refresh(d2)
    assert d2.status == "retrying"


async def test_deliver_4xx_permanent_dead(db_session):
    from app.services.outbound import deliver_one
    d = await _pending(db_session, "reject")
    await deliver_one(db_session, d, client=_FakeClient({"reject": 404}))
    db_session.refresh(d)
    assert d.status == "dead" and "permanent" in (d.last_error or "")


async def test_deliver_attempts_exhausted_dead(db_session):
    from app.services.outbound import deliver_one
    d = await _pending(db_session, "boom",
                       status="retrying", attempts=4)  # 第 5 次仍失败
    await deliver_one(db_session, d, client=_FakeClient({"boom": 503}))
    db_session.refresh(d)
    assert d.status == "dead"


async def test_deliver_timeout_exception_retries(db_session):
    from app.services.outbound import deliver_one
    d = await _pending(db_session, "slow")
    await deliver_one(
        db_session, d,
        client=_FakeClient({"slow": httpx.ConnectTimeout("t")}))
    db_session.refresh(d)
    assert d.status == "retrying"


async def test_deliver_due_scans_pending_and_due_retrying(db_session):
    from app.services.outbound import deliver_due
    due = await _pending(db_session, "ok", status="retrying", attempts=1,
                         nxt=datetime.now(timezone.utc) - timedelta(minutes=1))
    future = await _pending(db_session, "ok2", status="retrying", attempts=1,
                            nxt=datetime.now(timezone.utc) + timedelta(hours=1))
    fresh = await _pending(db_session, "ok3")
    c = _FakeClient({"ok": 200, "ok2": 200, "ok3": 200})
    n = await deliver_due(db_session, client=c)
    assert n == 2  # due + fresh;future 不动
    db_session.refresh(future)
    assert future.attempts == 1
```

(禁用端点跳过用例:pending 行 + 其端点 enabled=False → deliver_due 后行保持 pending 且 attempts==0。退避取值用例:attempts=1 后 next_attempt_at-now ≈ BACKOFF_MINUTES[0] 分钟,容差断言即可。)

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_outbound.py -q`
Expected: FAIL —— `ModuleNotFoundError: app.services.outbound`。

- [ ] **Step 3: 实现(services/outbound.py + config + .env.example)**

`config.py` 在 AGENT_* 组后加(M17 注释组):

```python
    # M17:出站 webhook
    WEBHOOK_MAX_ATTEMPTS: int = 5
    WEBHOOK_TIMEOUT_S: int = 10
```

`.env.example` 追加对应两条带中文注释。`outbound.py` 骨架(实现按 spec B/C 节;关键约束):

```python
"""M17:出站事件发射与 webhook 投递引擎。

emit_event 与业务同事务(audit() 同哲学);nudge 须在业务 commit 之后——
早到的投递任务扫不到未提交行,空转无害,beat 60s 兜底补。投递用
httpx.AsyncClient 每次调用新建随调用关闭,绝不模块级缓存——worker
每任务一个新事件循环,跨循环复用客户端即 M15 毒化(NoneType.send)。
"""
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import WebhookDelivery, WebhookEndpoint

EVENT_TYPES = ("document.done", "document.failed", "eval.completed",
               "eval.failed", "chat.refused")
BACKOFF_MINUTES = (1, 5, 15, 60, 60)


def _envelope(event_type: str, data: dict) -> dict: ...


async def emit_event(db: AsyncSession, event_type: str, data: dict) -> int:
    """同事务展开 pending 投递行;返回行数(0=无订阅,零副作用)。"""
    eps = (await db.execute(select(WebhookEndpoint).where(
        WebhookEndpoint.enabled.is_(True)))).scalars().all()
    env = _envelope(event_type, data)
    n = 0
    for ep in eps:
        if ep.events and event_type not in ep.events:
            continue
        db.add(WebhookDelivery(
            endpoint_id=ep.id, event_type=event_type,
            event_id=env["event_id"], payload=env, status="pending"))
        n += 1
    return n


def sign_headers(secret: str, event_type: str, body: str) -> dict[str, str]:
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.{body}".encode(),
                   hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json; charset=utf-8",
            "X-AIRag-Event": event_type, "X-AIRag-Timestamp": ts,
            "X-AIRag-Signature": sig}


async def deliver_one(db, delivery, client=None) -> None:
    """单行投递:复查端点 enabled(禁用→本轮跳过不耗次)→POST→
    逐行 commit 落状态(eval 逐题进度同哲学)。"""
    ...


async def deliver_due(db, client=None) -> int:
    """扫 pending + 到期 retrying(id asc,批 50 循环);返回处理行数。"""
    ...


def nudge() -> None:
    from app.workers.webhook_tasks import deliver_pending
    try:
        deliver_pending.delay()
    except Exception:
        logger.warning("webhook nudge failed; beat scan will cover")
```

deliver_one 内核:attempts+=1 → body=`json.dumps(delivery.payload, ensure_ascii=False)` → headers=sign_headers(ep.secret, ...) → POST(ep.url, content=body, headers=..., timeout=settings.WEBHOOK_TIMEOUT_S);结果分派:2xx→succeeded;429/5xx/异常→attempts≥MAX 则 dead 否则 retrying + next_attempt_at=now+BACKOFF_MINUTES[min(attempts-1, len-1)] 分钟;其余 4xx→dead + last_error=f"permanent {code}"。httpx 异常 `str(e)[:500]` 进 last_error。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_outbound.py -q` → 全 PASS;全量 → **~360P/0F**(348 + T1 1 + T2 约 11)。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/outbound.py backend/app/core/config.py E:/Projects/AIRag/.env.example backend/tests/test_outbound.py
git commit -m "feat(webhook): outbound service - emit, HMAC signing, at-least-once delivery engine"
```

---

### Task 3: 五事件挂点 + commit 后 nudge

**Files:**
- Modify: `backend/app/workers/pipeline.py`(133-136 done 区/44-56 _mark_failed)
- Modify: `backend/app/services/eval_runner.py`(199-212 两终态)
- Modify: `backend/app/api/ask.py`(102-110 SSE 落库块)
- Modify: `backend/app/api/agent.py`(137-159)、`backend/app/mcp_server.py`(146-163)
- Test: `backend/tests/test_webhook_events.py`(新)+ `backend/tests/test_pipeline.py`(扩 2 用例)

**Interfaces:**
- Consumes: T2 `emit_event(db, event_type, data) -> int`、`nudge()`;T1 模型。
- Produces: 无新接口——五挂点行为:终态落库事务内 `emit_event(...)`、commit 后 `if n 行: nudge()`;数据形状按 spec B 节表。

**挂点模式(五处同构,以 pipeline done 为例):**

```python
            doc.error_msg = None
            n = await emit_event(session, "document.done", {
                "document": {"id": document_id, "kb_id": doc.kb_id,
                             "filename": doc.filename,
                             "chunk_count": doc.chunk_count}})
            await session.commit()
            if n:
                nudge()
```

五处落点(emit 在既有 commit **之前**、nudge 在其后;行号为写作时锚点):

1. `pipeline.py _run` 134-135:`document.done`,data=document{id,kb_id,filename,chunk_count}
2. `pipeline.py _mark_failed` 53-54:`document.failed`,data=document{id,kb_id,filename}+error=error_msg[:500]
3. `eval_runner.py run_eval_task` 199-202:`eval.completed`,data=run{id,kb_id,mode,item_count,summary}
4. `eval_runner.py run_eval_task` 209-212(rollback 后):`eval.failed`,data=run{id,kb_id,mode}+error=str(e)[:500]
5. `ask.py` 102-110(s2 块内,audit 之后):`if refused: emit_event(s2, "chat.refused", {"source": "web", "kb_ids": payload.kb_ids, "question": payload.question[:500]})`;commit 后 `if refused and n: nudge()`(n 记 emit 返回值)
6. `agent.py` 151-159:`if outcome.refused: n = await emit_event(db, "chat.refused", {"source": "rest", "kb_ids": payload.kb_ids, "question": payload.query[:500]})` 在既有 `await db.commit()`(159)前;commit 后 `if n: nudge()`
7. `mcp_server.py` 156-163:同 6,`source: "mcp"`、query 变量名对应该文件

(共 7 个落点、5 类事件;import:`from app.services.outbound import emit_event, nudge`。worker 侧文件在函数内延迟 import 亦可,按文件现有 import 风格。)

- [ ] **Step 1: 写失败测试**

`tests/test_webhook_events.py`(eval 两个用例走真任务路径;agent/mcp 用既有假图模式;文档两个用例扩进 test_pipeline.py):

```python
# backend/tests/test_webhook_events.py
"""M17 T3:五类事件在真实终态路径上展开成投递行 + commit 后 nudge。"""
from sqlalchemy import select

from app.models import WebhookDelivery, WebhookEndpoint
from app.workers import pipeline as pipeline_mod
from app.workers.eval_tasks import run_evaluation


async def _subscribed_ep(db_session, events=None):
    ep = WebhookEndpoint(name=f"ev{__import__('time').time_ns()}",
                         url="http://x/h", secret="wh_s",
                         events=events, created_by=1)
    db_session.add(ep)
    await db_session.commit()
    return ep


async def _mk_running_run(client, auth_headers, db_session, n=1) -> int:
    from app.models import EvalQuestion, EvalRun
    kb_id = (await client.post(
        "/api/kbs", json={"name": "ev库"}, headers=auth_headers)
    ).json()["id"]
    db_session.add_all([EvalQuestion(kb_id=kb_id, question=f"q{i}")
                        for i in range(n)])
    run = EvalRun(kb_id=kb_id, mode="retrieval", summary=None,
                  item_count=n, status="running", triggered_by=1)
    db_session.add(run)
    await db_session.commit()
    return run.id


async def test_eval_completed_emits(client, auth_headers, db_session,
                                    monkeypatch):
    await _subscribed_ep(db_session)
    run_id = await _mk_running_run(client, auth_headers, db_session)
    nudged = []
    monkeypatch.setattr("app.services.eval_runner.nudge",
                        lambda: nudged.append(1))
    run_evaluation.run(run_id, "retrieval", False, 8)
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1 and rows[0].event_type == "eval.completed"
    assert rows[0].payload["data"]["run"]["id"] == run_id
    assert rows[0].payload["data"]["run"]["mode"] == "retrieval"
    assert nudged == [1]


async def test_eval_failed_emits(client, auth_headers, db_session,
                                 monkeypatch):
    import app.services.eval_runner as runner_mod
    await _subscribed_ep(db_session)
    run_id = await _mk_running_run(client, auth_headers, db_session)

    async def boom(db, kb_id, q, top_k, reranker):
        raise RuntimeError("炸")

    monkeypatch.setattr(runner_mod, "retrieval_item", boom)
    monkeypatch.setattr(runner_mod, "nudge", lambda: None)
    run_evaluation.run(run_id, "retrieval", False, 8)
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1 and rows[0].event_type == "eval.failed"
    assert "炸" in rows[0].payload["data"]["error"]
```

(monkeypatch 目标:`eval_runner` 模块里 import 的名字 `nudge`——所以 T3 在 eval_runner 用 `from app.services.outbound import emit_event, nudge` 模块级导入,测试 patch `app.services.eval_runner.nudge`。pipeline/ask/agent/mcp 同理按各自模块命名空间 patch。)

`test_pipeline.py` 扩两用例(复用 `_upload_pdf`):

```python
async def test_document_done_emits_delivery(client, auth_headers, db_session,
                                            monkeypatch):
    from app.models import WebhookDelivery, WebhookEndpoint
    ep = WebhookEndpoint(name=f"pd{time.time_ns()}", url="http://x/h",
                         secret="wh_s", events=["document.done"], created_by=1)
    db_session.add(ep)
    await db_session.commit()
    monkeypatch.setattr("app.workers.pipeline.nudge", lambda: None)
    kb_id = (await client.post(
        "/api/kbs", json={"name": "事件库"}, headers=auth_headers)).json()["id"]
    up = await _upload_pdf(client, auth_headers, kb_id)
    assert up.status_code == 201
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1 and rows[0].event_type == "document.done"
    assert rows[0].payload["data"]["document"]["kb_id"] == kb_id


async def test_document_failed_emits_delivery(client, auth_headers,
                                              db_session, monkeypatch):
    from app.models import WebhookDelivery, WebhookEndpoint
    from app.workers.pipeline import _mark_failed
    ep = WebhookEndpoint(name=f"pf{time.time_ns()}", url="http://x/h",
                         secret="wh_s", events=["document.failed"],
                         created_by=1)
    db_session.add(ep)
    await db_session.commit()
    monkeypatch.setattr("app.workers.pipeline.nudge", lambda: None)
    kb_id = (await client.post(
        "/api/kbs", json={"name": "事件库二"}, headers=auth_headers)).json()["id"]
    up = await _upload_pdf(client, auth_headers, kb_id)
    await _mark_failed(up.json()["id"], "boom")
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1 and rows[0].event_type == "document.failed"
    assert "boom" in rows[0].payload["data"]["error"]
```

(test_pipeline.py 顶部补 `import time`;`select` 已有。)

chat.refused 两用例(agent REST + SSE Web)放 `test_webhook_events.py`:REST 走既有 `test_agent_ask.py` 的假图/夹具模式(monkeypatch `app.api.agent.nudge`,断言 source=rest);SSE 走既有 ask 测试模式(文件在 `tests/test_ask*.py`,以现状为准;断言 source=web、question 截断)。MCP 面用 `tests/test_mcp.py` 既有 mcp_client 夹具加一用例(source=mcp)——若 MCP 夹具驱动成本高,REST+SSE 覆盖 + mcp_server 挂点与 agent.py 同构由代码评审兜底,在报告注明该取舍。

- [ ] **Step 2: 跑测试确认失败**(新增用例红:无投递行)
- [ ] **Step 3: 实现七落点**(上文模式;nudge 记 emit 返回值>0 才调)
- [ ] **Step 4: 跑测试通过 + 全量**

Run: 全量 `.venv\Scripts\python -m pytest tests -q` → **~366P/0F**(360 + 约 6 新用例;既有 eval/pipeline/ask/agent 用例因多出的 emit(无端点时零行零副作用)不受影响——若有因新增 import 循环等红,修并记录)。

- [ ] **Step 5: Commit**

```bash
git add backend/app/workers/pipeline.py backend/app/services/eval_runner.py backend/app/api/ask.py backend/app/api/agent.py backend/app/mcp_server.py backend/tests/test_webhook_events.py backend/tests/test_pipeline.py
git commit -m "feat(webhook): emit five events from document/eval/refusal terminals"
```

---

### Task 4: worker 任务 + beat 兜底

**Files:**
- Create: `backend/app/workers/webhook_tasks.py`
- Modify: `backend/app/workers/celery_app.py`(include + beat_schedule)
- Test: `backend/tests/test_webhook_tasks.py`

**Interfaces:**
- Consumes: T2 `deliver_due`
- Produces: Celery 任务 `app.workers.webhook_tasks.deliver_pending`(无参);beat 条目 `webhook-delivery-scan`(60s)。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_webhook_tasks.py
"""M17 T4:投递任务壳(自持 NullPool 引擎,eval_tasks 同款)。"""
import app.services.outbound as outbound_mod
from app.workers.webhook_tasks import deliver_pending


def test_task_registered():
    assert "app.workers.webhook_tasks.deliver_pending" in \
        deliver_pending.app.tasks


def test_task_runs_deliver_due(monkeypatch):
    calls = []
    delivered = []

    async def fake_due(db, client=None):
        calls.append(1)
        return 3

    monkeypatch.setattr(outbound_mod, "deliver_due", fake_due)
    # 任务壳内 import 方式决定 patch 面:壳内 `from app.services.outbound
    # import deliver_due` 则 patch webhook_tasks 命名空间——按实现为准,
    # 断言 deliver_pending.run() 返回 3 且 fake_due 被调恰一次
    assert deliver_pending.run() == 3 and calls == [1]
```

(patch 面按实际 import 写法落定;另补一用例:任务对异常吞掉记日志不抛——投递批失败不炸 beat。)

- [ ] **Step 2: 红**(模块不存在)
- [ ] **Step 3: 实现**

```python
# backend/app/workers/webhook_tasks.py
"""M17:webhook 投递任务。自持 NullPool 引擎(worker 每任务新事件循环,
池化连接不得跨循环复用——eval_tasks 同款);异常吞掉——beat 下一轮再扫,
单批失败不得炸掉 worker/beat。"""
from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.services.outbound import deliver_due
from app.workers.celery_app import celery_app
from app.workers.pipeline import _engine, _run_async


@celery_app.task(name="app.workers.webhook_tasks.deliver_pending",
                 ignore_result=True)
def deliver_pending():
    async def _amain() -> int:
        engine = _engine(settings.DATABASE_URL)
        try:
            async with async_sessionmaker(engine,
                                          expire_on_commit=False)() as db:
                return await deliver_due(db)
        finally:
            await engine.dispose()

    try:
        return _run_async(_amain())
    except Exception:
        logger.exception("webhook deliver_pending failed; beat will retry")
        return 0
```

`celery_app.py`:include 加 `"app.workers.webhook_tasks"`;beat_schedule 加:

```python
        "webhook-delivery-scan": {
            "task": "app.workers.webhook_tasks.deliver_pending",
            "schedule": 60.0,
        },
```

- [ ] **Step 4: 绿 + 全量** → **~369P/0F**
- [ ] **Step 5: Commit**

```bash
git add backend/app/workers/webhook_tasks.py backend/app/workers/celery_app.py backend/tests/test_webhook_tasks.py
git commit -m "feat(webhook): deliver_pending worker task with beat fallback scan"
```

---

### Task 5: admin API(CRUD/rotate/test-send/投递记录)

**Files:**
- Modify: `backend/app/schemas/admin.py`(加 5 个 schema)
- Modify: `backend/app/api/admin.py`(加 6 端点)
- Test: `backend/tests/test_admin_webhooks.py`

**Interfaces:**
- Consumes: T1 模型;T2 `sign_headers/deliver_one/emit_event/nudge/EVENT_TYPES`
- Produces(前端 T6 依赖的响应形状):
  - `POST /api/admin/webhooks` 201 → `WebhookCreatedOut`(全字段 + `secret` 明文一次性;未传 secret 自动 `secrets.token_hex(16)`)
  - `GET /api/admin/webhooks` → `list[WebhookOut]`(secret 为 masked 串 `wh_****xxxx`)
  - `PUT /api/admin/webhooks/{id}` → `WebhookOut`;body 带 `rotate_secret=true` → 响应为 `WebhookCreatedOut`(新 secret 明文一次)
  - `DELETE /api/admin/webhooks/{id}` → 204
  - `POST /api/admin/webhooks/{id}/test` → `{"status": "succeeded"|"dead"|"retrying", "response_status": int|None, "error": str|None}`(同步内联单发,事件类型 `test`,绕过订阅过滤)
  - `GET /api/admin/webhook-deliveries?endpoint_id=&event_type=&status=&page=&page_size=` → `{total, items}`(id desc,page_size≤100;item 含 endpoint_name)

Schemas:

```python
class WebhookCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: HttpUrl
    events: list[str] = []            # 空=订阅全部;值域校验在端点
    description: str | None = Field(None, max_length=200)
    secret: str | None = Field(None, min_length=16, max_length=64)

class WebhookUpdateIn(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    url: HttpUrl | None = None
    events: list[str] | None = None
    enabled: bool | None = None
    description: str | None = None
    rotate_secret: bool = False

class WebhookOut(BaseModel):           # secret=masked
    id: int; name: str; url: str; events: list[str] | None
    enabled: bool; description: str | None
    secret_masked: str; created_at: datetime
    model_config = ConfigDict(from_attributes=True)  # secret_masked 由端点拼

class WebhookCreatedOut(WebhookOut):
    secret: str                        # 明文仅此一次

class WebhookDeliveryOut(BaseModel):
    id: int; endpoint_id: int; endpoint_name: str
    event_type: str; status: str; attempts: int
    response_status: int | None; last_error: str | None
    payload: dict | None; next_attempt_at: datetime | None
    created_at: datetime
```

(实现时按 schemas/admin.py 现有风格精化;masked 规则 `f"wh_****{secret[-4:]}"`,非 `wh_` 前缀的自定义 secret 同样只露尾 4。)

端点实现要点(全部 `Depends(require_admin)` + audit,动作名 `webhook_create/webhook_update/webhook_delete/webhook_test`):
- POST:events 值域 ⊆ EVENT_TYPES 否则 422;name 重复 409(IntegrityError 或先查);audit 后 commit;响应明文。
- PUT:字段 None 跳过;rotate_secret=true → `ep.secret = secrets.token_hex(16)`,响应 WebhookCreatedOut;否则 WebhookOut(**不接受明文写回 secret 字段**)。
- DELETE:204,级联删投递行(FK)。
- test:插一行 event_type="test" 的 pending(payload=信封 data={"message": "airag webhook test"})→ commit → `await deliver_one(db, row)`(注入不了 client 就让它真建 httpx.AsyncClient 打真实 URL——**测试里 monkeypatch `app.api.admin.deliver_one`** 或 patch httpx)→ 返回状态。disabled 端点 400。
- deliveries:照抄 audit-logs 分页模式 + endpoint_name 联查(页内 endpoint_id IN 一次查映射,与 eval kb_name 同法)。

- [ ] **Step 1: 写失败测试(test_admin_webhooks.py,覆盖矩阵)**

用例清单(复用 `client/auth_headers/db_session` + `_register_and_login` + SQL `UPDATE users SET role='admin'` 提权模式,参照 test_eval_questions.py:8-29):

1. admin POST 201:响应含明文 secret;GET 列表 secret 仅 masked、无明文
2. 未传 secret 自动生成;传自定义(≥16)用之
3. name 重复 409;events 非法值 422;url 非 http(s) 422
4. 非 admin(editor/viewer)POST/DELETE 403;未带 token 401
5. PUT 改 name/enabled 生效;rotate_secret=true 响应新明文且 GET 后 masked
6. DELETE 204 后 GET 空、其投递行已级联删(先直插一行)
7. test 端点:monkeypatch `app.api.admin.deliver_one` 为假(改状态 succeeded)→ 响应 {"status": "succeeded", ...};disabled 端点 400
8. deliveries 列表:插 3 行(不同 status)→ total=3、过滤 status=retrying 得 1、分页 page_size=2 → items 2、含 endpoint_name

- [ ] **Step 2: 红** → **Step 3: 实现** → **Step 4: 绿 + 全量** → **~378P/0F**
- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/admin.py backend/app/api/admin.py backend/tests/test_admin_webhooks.py
git commit -m "feat(webhook): admin CRUD, secret rotation, test-send, delivery log endpoints"
```

---

### Task 6: 前端 WebhooksPage(双页签)

**Files:**
- Modify: `frontend/src/api/admin.ts`(类型 + 6 函数)
- Create: `frontend/src/pages/admin/WebhooksPage.vue`?——**放 `frontend/src/pages/WebhooksPage.vue`**(与 UsersPage/AuditLogPage/KeysPage 同级平铺,现状无 pages/admin/ 目录)
- Modify: `frontend/src/router/index.ts`(children 加 `/admin/webhooks`)
- Modify: `frontend/src/layouts/MainLayout.vue`(admin 组菜单项,icon 用 `Promotion`,import 加)
- Test: `frontend/src/pages/__tests__/WebhooksPage.spec.ts`(与 KbPage/KeysPage spec 同级平铺)

**Interfaces:**
- Consumes: T5 六端点的响应形状(api/admin.ts 类型镜像:`WebhookEndpoint {id,name,url,events,enabled,description,secret_masked,created_at}`、`WebhookCreated = WebhookEndpoint & {secret}`、`WebhookDeliveryRow {id,endpoint_id,endpoint_name,event_type,status,attempts,response_status,last_error,payload,next_attempt_at,created_at}`)
- Produces: 页面路由 `/admin/webhooks`(title=出站推送)、菜单项「出站推送」、api 函数 `listWebhooks/createWebhook/updateWebhook/deleteWebhook/testWebhook/listWebhookDeliveries`

**页面结构(照 EvalPage 双页签 + AuditLogPage 表格 + KeysPage 对话框模式):**

- `el-tabs` 两页签「端点管理」「投递记录」
- 端点页签:工具行(新建按钮)+ el-table(名称/URL(mono,show-overflow-tooltip)/订阅事件 el-tag 列(空=「全部」,多个 tag)/状态 el-switch 直切(updateWebhook enabled)/操作:测试(loading 态,结果 ElMessage 成功或失败详情)/编辑/删除(ElMessageBox.confirm))
- 新建/编辑对话框(el-dialog + 表单):name/url/description/事件订阅 el-select multiple(六选项:五事件+「留空订阅全部」提示文案;实际语义:不选=全部)/新建时可选自定义 secret(留空自动);编辑时 rotate_secret el-switch;表单校验 name 必填、url 必填 http(s)
- **secret 一次性展示**:POST/rotate 成功后 `el-dialog`(独立小弹窗)显示明文 + 复制按钮(navigator.clipboard)+ 文案「仅此一次展示,请妥善保存」;关闭后不可再见
- 投递页签:筛选行(端点下拉(本页数据源 listWebhooks)/事件下拉(五+test)/状态下拉(pending/retrying/succeeded/dead))+ el-table(时间/端点名/事件 tag/状态 tag:pending info灰·retrying warning·succeeded success·dead danger/attempts/response_status(—)/last_error(show-overflow-tooltip))+ el-pagination(total/prev/pager/next/sizes)
- 空态 el-empty;双主题 CSS 变量沿用现有页面模式;`fmtTime` 照抄 AuditLogPage

- [ ] **Step 1: 写失败测试(WebhooksPage.spec.ts,mock adminApi)**

用例(≥5):

1. 端点表渲染:name/url/masked secret 断言「文本中不包含明文 secret 值,含 `****`」;「全部」订阅显示
2. 新建对话框提交 → `createWebhook` 以表单值调用;响应带 secret → 一次性弹窗出现且含明文、含「仅此一次」提示
3. enabled switch 切换 → `updateWebhook` 调用 `{enabled:false}`
4. 测试按钮 → `testWebhook` 调用;mock resolved `{status:"succeeded"}` → 成功消息路径(断言函数被调即可,ElMessage 不断言);mock rejected → 不抛
5. 投递页签:切 tab → `listWebhookDeliveries` 调用;行渲染状态 tag(dead 红 class 或 tag type=danger);筛选变更触发重查

(mock 模式照 KeysPage.spec/KbPage.spec:`vi.mock('@/api/admin', ...)` + mount + ElementPlus 插件 + flushPromises。)

- [ ] **Step 2: 红** → **Step 3: 实现(api 函数 + 页面 + 路由 + 菜单)** → **Step 4: 绿 + build**

Run: `npm run test:unit -- --run` → **~66-67/66-67**(61 + 5~6);`npm run build` 零错。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/admin.ts frontend/src/pages/WebhooksPage.vue frontend/src/pages/__tests__/WebhooksPage.spec.ts frontend/src/router/index.ts frontend/src/layouts/MainLayout.vue
git commit -m "feat(webhook-ui): admin webhooks page with one-time secret dialog and delivery log"
```

---

### Task 7: 门禁 + 真栈验收 + 收尾文档

**Files:**
- Create: `backend/scripts/m17_acceptance.py`
- Modify: `docs/superpowers/plans/2026-09-23-airag-m17-outbound-webhooks.md`(执行记录)

- [ ] **Step 1: 全量门禁**

Run(backend): `.venv\Scripts\python -m pytest tests -q` → ~378P/0F
Run(frontend): `npm run test:unit -- --run` + `npm run build` → 全绿零错

- [ ] **Step 2: m17_acceptance.py(真栈;worker+beat 必须已起)**

骨架照 m15(head 注明「worker 与 beat 必须 start_worker.bat/start_beat.bat 已起」;`check/summary_and_exit/make_user/login` helper 复制 m15 同名实现)。新增**本地 receiver**:

```python
import http.server, json, threading, hmac, hashlib

class _Receiver(http.server.BaseHTTPRequestHandler):
    # 类属性 store: list[dict](url path/headers/body);/reject 路径回 404
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        RECEIVED.append({"path": self.path,
                         "headers": dict(self.headers), "body": body})
        code = 404 if self.path == "/reject" else 200
        self.send_response(code); self.end_headers()
    def log_message(self, *a): pass

def start_receiver() -> tuple[str, int]:  # (host, port),threading.Thread daemon
    ...
```

检查项:

```python
# 流程(每步 check()):
# 1. admin 登录;receiver 起随机端口;
#    POST /admin/webhooks {url: http://127.0.0.1:{port}/hook, events: []}
#    → 201 响应取 secret 明文;GET 列表确认 masked
# 2. 验签 helper:最近收包 headers/body,重算
#    HMAC-SHA256(secret, f"{ts}.{body}") 与 X-AIRag-Signature 一致
# 3. eval.completed:建 KB+3 题(1 设 doc_ids)+retrieval run → 轮询 completed
#    (≤120s)→ 收包:验签通过、X-AIRag-Event=eval.completed、
#    body JSON 含 event_id/data.run.id、GET deliveries 见 succeeded 行
# 4. chat.refused:建 API key(M10 /api/keys 或 admin 代发)→
#    POST /api/agent/ask 问「库外完全无关问题xyzzy」→ refused=true 响应
#    → 收包 source=rest、question 截断 500 内;deliveries 行 succeeded
# 5. document.failed:上传损坏 PDF(b"%PDF-1.4 坏字节" 内嵌)→
#    轮询文档 failed(≤120s)→ 收包 document.failed 含 error
# 6. document.done:pymupdf 生成最小 PDF 上传 → 轮询 done →
#    收包 document.done 含 chunk_count≥1
# 7. 4xx 永久:POST 第二端点 url=/reject → 触发一次 eval.completed
#    (复用步骤 3 库再跑一次 run)→ 轮询 deliveries 该端点行 status=dead
#    且 response_status=404
# 8. beat 兜底:直插一行 pending(绕过 emit;SQL insert)→ 不触发任何
#    事件 → 等待 ≤90s(beat 60s 周期)→ 该行 succeeded(receiver 收包)
# 9. 权限负例:第二账号(viewer)POST/DELETE webhooks 403、
#    GET deliveries 403
# 10. 清理:DELETE 两端点(204);验收 KB/用户/temp key 清理(m15 模式)
# summary_and_exit()
```

(步骤 4 的拒答问题必须保证真 LLM 拒答:问验收 KB 语义完全无关的问题;若真栈 ZHIPU_API_KEY 未配则该步 SKIP 并注明。)

Run: `.venv\Scripts\python scripts\m17_acceptance.py` → **全 PASS(0 SKIP 或仅步骤 4 注明 SKIP)**

- [ ] **Step 3: 收尾文档**

计划追加「执行记录」:提交链/测试计数/验收输出/实施期裁决。走查清单(交用户):

1. admin 菜单「出站推送」→ 端点管理页签;新建端点(自动 secret)→ 一次性 secret 弹窗复制
2. 用 https://webhook.site(或任意外部接收器)配真端点 → 上传文档/跑评估 → 外部收到签名请求,验签脚本核对(计划附录给验签示例)
3. 测试发送按钮:正确 URL → succeeded;错误 URL → 失败反馈
4. 投递记录页签:状态 tag/筛选/分页;kill 掉 receiver 再触发事件 → retrying(1 分钟后)→ 若干次后 dead
5. 启停 switch、rotate secret(masked 不泄明文)、删除端点级联清记录
6. 非 admin 账号看不到菜单与 403
7. 双主题抽查

- [ ] **Step 4: Commit**

```bash
git add backend/scripts/m17_acceptance.py docs/superpowers/plans/2026-09-23-airag-m17-outbound-webhooks.md
git commit -m "test(m17): acceptance script with local receiver, docs"
```

---

## 验收门槛(整计划)

1. pytest 全绿(~378P);vitest 全绿(~66);`npm run build` 零错
2. 真栈 `m17_acceptance.py` 全 PASS(worker+beat 在跑;真 LLM 步骤如 key 未配允许注明 SKIP)
3. `alembic upgrade head`/`downgrade -1`/再 upgrade 可逆(dev 库亲验)
4. 用户走查通过(清单见 Task 7)
5. spec 台账记录裁决;M18 候选(企微/钉钉/飞书适配、per-KB 订阅、重投按钮、SSRF 黑名单)回流记忆

## 执行记录(2026-09-23,SDD)

**提交链**(spec c268e10 → 计划 fb1a868):T1 7da4bea → T2 9893de2 → T3 c981107 → T4 9e9b963 → T5 efba2d0 → T6 bbab8da + 487f8d1(修复轮:resetFields 赋值后调用清空编辑预填→clearValidate+显式赋全字段+回归用例)→ T7 0580df7(验收脚本)。六任务一次评审通过(T6 经 1 修复轮),其余零修复轮。

**测试与验收(控制端亲验)**:pytest 348→**379P/0F**(T1 +1、T2 +12、T3 +7、T4 +3、T5 +8);vitest 61→**67/67**(T6 +5、修复轮 +1);`npm run build` 零错;`alembic upgrade head`/`downgrade -1`/再 upgrade 可逆亲验。真栈 `m17_acceptance.py` **37/37 PASS、0 SKIP**(单 worker+单 beat+后端 8001;receiver 127.0.0.1:49628):endpoint 创建+masked、五事件端到端收包+HMAC 全验(eval.completed run30/chat.refused source=rest+截断/document.failed 坏 PDF/document.done 真 MinerU 解析 chunk≥1)、reject 端点 404→dead+permanent、beat 兜底(直插 pending 行 60s 周期补投)、viewer 三负例 403、清理+外部验签示例一并输出。

**实施期裁决**(详见 SDD 台账):
- T2:next_attempt_at 全链 naive UTC(T1 列无时区,asyncpg 拒 aware;`_utcnow_naive` 单源);conftest CLEANUP_ORDER 补两表(deliveries 前)。
- T2 环境事故:宿主 DLP(360/金山)对 python.exe 写入的仓库文件透明加密→git 提交密文/pytest 读明文;恢复=工具重写+amend,blob 亲验干净;此后任务全程规避(编辑器工具写文件)。**建议用户把仓库加入 DLP 白名单**。
- T3:MCP 用例走 `ask_knowledge_base.fn` 直调+contextvar(test_mcp 的 StreamableHTTPSessionManager 单 run 不可跨模块复用,已复现)——仍穿真实生产路径;eval failed 分支 rollback 后实例过期→run_id 参数化+kb_id 预快照;test_outbound time_ns 名字碰撞 flake 修复随任务提交。
- T6:switch 触发用 ElSwitch `$emit('change')`(与 :model-value 配对,等效 brief 首选);投递表增「下次尝试」列(契约字段,评审通过)。
- 验收脚本:chat.refused 走 admin 代发临时 key 给临时 viewer 用户(无自助签发/删除路由);beat 探针用 `beat.check` 事件类型直插 NullPool;执行中控制端曾误判双 beat(两条 findstr 链重复输出)全清重启单实例——环境操作失误,与代码无关,记录在案。

**用户走查清单**:
1. admin 菜单「出站推送」→ 端点管理;新建端点(自动 secret)→ 一次性 secret 弹窗+复制
2. 外部接收器(如 webhook.site)配真端点 → 上传文档/跑评估/问库外问题 → 外部收到签名请求;用脚本输出的验签一行命令核对
3. 测试发送:正确 URL→succeeded;错误 URL→失败反馈
4. 投递记录页签:状态 tag/筛选/分页;停掉外部接收器再触发→retrying(1 分钟退避)→数次后 dead
5. 启停 switch、rotate secret(masked 不泄明文)、编辑预填、删除级联清记录
6. 非 admin 账号不见菜单、直调 403
7. 双主题抽查

## 执行记录补遗(终审 + 修复波,控制端)

- **终审(whole-branch,fb1a868..e6fc231)**:spec 逐节覆盖确认(C 节退避/签名/状态机逐条核对)、secret 泄漏面全路径扫描干净(响应/审计/前端明文生命周期,含 vitest 植入伪 secret 的对抗性反证)、七挂点铁律抽验全对、admin 信任边界与 spec 一致。verdict **With fixes**,1 Important:`deliver_due` 按 id asc limit 50 无偏移扫描,禁用端点的行不离开扫描集——最低 50 行全属禁用端点时 `attempted==0→break` 永久触发,引擎静默全停且无报错(触发:接收器宕机积累 ≥50 行后端点被禁)。
- **修复波 a0a2b08**:扫描查询 join `WebhookEndpoint` 过滤 `enabled=true`(禁用行不进扫描集,pending 不耗次语义不变;deliver_one 单行防御保留)+2 回归用例(55 行禁用队头+更高 id enabled 行仍投递;51 行跨批),RED 先复现饥饿(0==1/0==51)。381P/0F。复审 ADDRESSED,唯一新行为为罕见窗口返回计数多计 1(已披露,无消费方受影响)。
- **验收复验**:worker 重启载入修复码后 `m17_acceptance.py` 复跑 **37/37 PASS**(run 32/33,receiver 51298)。终态门禁:pytest **381P/0F**、vitest **67/67**、build 零错。
- **spec 偏差补备案(终审指出)**:spec D/E 的「统计字段/最近统计」未实现(GET /webhooks 无统计列,页面无该列)——计划期 schema 未包含即已偏离,投递记录页签+过滤在功能上覆盖,补记于此;M18 候选回填。
- **M18 候选(终审 triage,全部可留)**:端点统计列回填;3xx→dead 语义文档化(接收方指导);URL 长度 500 vs HttpUrl 2083 差距防护;投递筛选变更重查 vitest;大积压阻塞 solo worker 的每轮行数上限;description 清空语义;copySecret 剪贴板拒绝反馈;企微/钉钉/飞书 payload 适配、per-KB 订阅、重投按钮、SSRF 黑名单(原非目标)。
- **环境备忘**:进程核对应以精确串匹配(`--pool=solo`/` beat --`)或 CSV dump;本里程碑两次误判(双 beat/双 worker)皆因 findstr 链输出重复或 `%worker%` 误匹配 `app.workers`——控制端操作失误,未伤代码,已复盘。
