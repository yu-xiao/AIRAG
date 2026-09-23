# backend/tests/test_outbound.py
"""M17 T2:emit 展开/信封/签名公式/投递状态机全分支。"""
import hashlib
import hmac as hmac_mod
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.models import WebhookDelivery, WebhookEndpoint
from app.services.outbound import (
    BACKOFF_MINUTES,
    emit_event,
    sign_headers,
)


async def _mk_ep(db_session, events=None, enabled=True, url="http://x/h"):
    # Windows 时钟粒度可达毫秒级,同测试内连续 time_ns() 会撞名(唯一约束)
    ep = WebhookEndpoint(name=f"ep{uuid.uuid4().hex[:12]}", url=url,
                         secret="wh_s3cret", events=events, enabled=enabled,
                         created_by=1)
    db_session.add(ep)
    await db_session.commit()
    return ep


async def test_emit_expands_only_matching_enabled(db_session):
    """订阅匹配展开;空订阅=全部;禁用排除。"""
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
    assert await emit_event(db_session, "eval.completed", {}) == 0
    await db_session.commit()
    assert (await db_session.execute(
        select(WebhookDelivery))).scalars().all() == []


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


class _Resp:
    def __init__(self, code): self.status_code = code


class _FakeClient:
    """记录 POST;按 URL 末段返回预设状态码或抛异常。"""
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
    d = await _pending(db_session, "ok")
    c = _FakeClient({"ok": 200})
    await deliver_one(db_session, d, client=c)
    await db_session.refresh(d)
    assert d.status == "succeeded" and d.response_status == 200
    url, kw = c.calls[0]
    assert kw["content"] == json.dumps(d.payload, ensure_ascii=False)
    assert "X-AIRag-Signature" in kw["headers"]


async def test_deliver_5xx_and_429_retry_with_backoff(db_session):
    from app.services.outbound import deliver_one
    d = await _pending(db_session, "boom")
    await deliver_one(db_session, d, client=_FakeClient({"boom": 500}))
    await db_session.refresh(d)
    assert d.status == "retrying" and d.attempts == 1
    assert d.next_attempt_at is not None
    assert d.last_error

    d2 = await _pending(db_session, "rate")
    await deliver_one(db_session, d2, client=_FakeClient({"rate": 429}))
    await db_session.refresh(d2)
    assert d2.status == "retrying"


async def test_deliver_4xx_permanent_dead(db_session):
    from app.services.outbound import deliver_one
    d = await _pending(db_session, "reject")
    await deliver_one(db_session, d, client=_FakeClient({"reject": 404}))
    await db_session.refresh(d)
    assert d.status == "dead" and "permanent" in (d.last_error or "")


async def test_deliver_attempts_exhausted_dead(db_session):
    from app.services.outbound import deliver_one
    d = await _pending(db_session, "boom",
                       status="retrying", attempts=4)  # 第 5 次仍失败
    await deliver_one(db_session, d, client=_FakeClient({"boom": 503}))
    await db_session.refresh(d)
    assert d.status == "dead"


async def test_deliver_timeout_exception_retries(db_session):
    from app.services.outbound import deliver_one
    d = await _pending(db_session, "slow")
    await deliver_one(
        db_session, d,
        client=_FakeClient({"slow": httpx.ConnectTimeout("t")}))
    await db_session.refresh(d)
    assert d.status == "retrying"


async def test_deliver_disabled_endpoint_skipped(db_session):
    """端点禁用→本轮跳过:保持 pending、不耗 attempts。"""
    from app.services.outbound import deliver_due
    d = await _pending(db_session, "off")
    # 模型无 relationship(异步懒加载陷阱),按 id 显式取端点
    ep = await db_session.get(WebhookEndpoint, d.endpoint_id)
    ep.enabled = False
    await db_session.commit()
    n = await deliver_due(db_session, client=_FakeClient({"off": 200}))
    await db_session.refresh(d)
    assert n == 0 and d.status == "pending" and d.attempts == 0


async def test_deliver_due_scans_pending_and_due_retrying(db_session):
    from app.services.outbound import deliver_due
    # next_attempt_at 列为 naive TIMESTAMP(asyncpg 拒 aware),统一 naive UTC
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    due = await _pending(db_session, "ok", status="retrying", attempts=1,
                         nxt=now - timedelta(minutes=1))
    future = await _pending(db_session, "ok2", status="retrying", attempts=1,
                            nxt=now + timedelta(hours=1))
    fresh = await _pending(db_session, "ok3")
    c = _FakeClient({"ok": 200, "ok2": 200, "ok3": 200})
    n = await deliver_due(db_session, client=c)
    assert n == 2  # due + fresh;future 不动
    await db_session.refresh(future)
    assert future.attempts == 1


def test_backoff_constants():
    assert BACKOFF_MINUTES == (1, 5, 15, 60, 60)
