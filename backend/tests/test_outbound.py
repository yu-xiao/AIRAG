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
from app.services import webhook_providers as wp
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
    def __init__(self, code, text=None):
        self.status_code = code
        self.text = text  # M18:平台 body 码分类需要响应体(generic 不读)


class _FakeClient:
    """记录 POST;按 URL 末段返回预设状态码或抛异常。"""
    def __init__(self, routes): self.routes, self.calls = routes, []
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, **kw):
        self.calls.append((url, kw))
        # 先去 query 再取路径末段:钉钉加签后 URL 带 ?timestamp=&sign=,
        # 不剥离则路由键形如 "wx?timestamp=..." 永远 miss
        r = self.routes.get(url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1])
        if isinstance(r, Exception): raise r
        if isinstance(r, tuple): return _Resp(r[0], r[1])  # (code, text)
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


async def _bulk_pending(db_session, ep, n):
    """向同一端点直插 n 行 pending(不经 emit;队头饥饿场景需跨批量)。"""
    rows = [WebhookDelivery(
        endpoint_id=ep.id, event_type="test",
        event_id=uuid.uuid4().hex, payload={"event_id": uuid.uuid4().hex},
        status="pending", attempts=0,
    ) for _ in range(n)]
    db_session.add_all(rows)
    await db_session.commit()
    return rows


async def test_deliver_due_head_of_line_disabled_stall(db_session):
    """最低 55 行全属禁用端点时,更高 id 的 enabled 行必须仍被投递。

    修复前:第一批 50 行全禁用→零尝试 break,enabled 行被队头饿死,
    之后所有扫描取同一批再 break,引擎静默全停。
    """
    from app.services.outbound import deliver_due
    dead_ep = await _mk_ep(db_session, events=[], enabled=False)
    dead_rows = await _bulk_pending(db_session, dead_ep, 55)   # 55>50 跨批
    live_ep = await _mk_ep(db_session, events=[], url="http://h/alive")
    live = (await _bulk_pending(db_session, live_ep, 1))[0]    # 更高 id
    n = await deliver_due(db_session, client=_FakeClient({"alive": 200}))
    assert n == 1
    await db_session.refresh(live)
    assert live.status == "succeeded" and live.attempts == 1
    for d in dead_rows:
        await db_session.refresh(d)
        assert d.status == "pending" and d.attempts == 0


async def test_deliver_due_enabled_rows_beyond_first_batch(db_session):
    """55 行禁用垫底 + 51 行 enabled:批 2 内的 enabled 行也要投到。

    修复前第一批全禁用即 break,51 行 enabled(含跨到第二批的最后 1 行)
    一行都投不出去;修复后 join 过滤禁用行,50+1 两批全部成功。
    """
    from app.services.outbound import deliver_due
    dead_ep = await _mk_ep(db_session, events=[], enabled=False)
    await _bulk_pending(db_session, dead_ep, 55)
    live_ep = await _mk_ep(db_session, events=[], url="http://h/alive")
    live_rows = await _bulk_pending(db_session, live_ep, 51)   # 50+1 跨批
    n = await deliver_due(db_session, client=_FakeClient({"alive": 200}))
    assert n == 51
    for d in live_rows:
        await db_session.refresh(d)
        assert d.status == "succeeded" and d.attempts == 1


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


# ---- M18:平台端点经 deliver_one 的构造与分类 ----
async def _mk_platform_ep(db_session, provider, secret="SECk" + "1" * 15):
    ep = WebhookEndpoint(
        name=f"m18p{uuid.uuid4().hex[:10]}", url="http://h/wx",
        secret=secret, events=[], enabled=True, created_by=1,
        provider=provider)
    db_session.add(ep)
    await db_session.commit()
    return ep


async def _platform_delivery(db_session, ep, payload=None):
    d = WebhookDelivery(
        endpoint_id=ep.id, event_type="document.done",
        event_id=uuid.uuid4().hex,
        payload=payload or {"event_id": "e" * 32, "event_type": "document.done",
                            "occurred_at": "t", "data": {
                                "document": {"kb_id": 1, "filename": "f.pdf"}}},
        status="pending", attempts=0)
    db_session.add(d)
    await db_session.commit()
    return d


async def test_deliver_wecom_body_success_and_platform_shape(db_session):
    from app.services.outbound import deliver_one
    ep = await _mk_platform_ep(db_session, "wecom")
    d = await _platform_delivery(db_session, ep)
    ac = _FakeClient({"wx": (200, '{"errcode": 0}')})
    await deliver_one(db_session, d, client=ac)
    assert d.status == "succeeded"
    url, kw = ac.calls[0]
    body = json.loads(kw["content"])
    assert body["msgtype"] == "markdown"
    assert "X-AIRag-Signature" not in kw["headers"]


async def test_deliver_wecom_permanent_code_dead(db_session):
    from app.services.outbound import deliver_one
    ep = await _mk_platform_ep(db_session, "wecom")
    d = await _platform_delivery(db_session, ep)
    await deliver_one(db_session, d,
                      client=_FakeClient({"wx": (200, '{"errcode": 93000}')}))
    assert d.status == "dead" and "93000" in d.last_error


async def test_deliver_dingtalk_transient_code_retries(db_session):
    from app.services.outbound import deliver_one
    ep = await _mk_platform_ep(db_session, "dingtalk")
    d = await _platform_delivery(db_session, ep)
    await deliver_one(db_session, d,
                      client=_FakeClient({"wx": (200, '{"errcode": -1}')}))
    assert d.status == "retrying" and d.attempts == 1
    assert d.next_attempt_at is not None


async def test_deliver_dingtalk_signed_url_and_body(db_session):
    from app.services.outbound import deliver_one
    ep = await _mk_platform_ep(db_session, "dingtalk")
    d = await _platform_delivery(db_session, ep)
    ac = _FakeClient({"wx": (200, '{"errcode": 0}')})
    await deliver_one(db_session, d, client=ac)
    url, kw = ac.calls[0]
    assert url.startswith("http://h/wx?") and "timestamp=" in url and "sign=" in url
    assert json.loads(kw["content"])["msgtype"] == "markdown"


async def test_deliver_feishu_signed_body(db_session):
    from app.services.outbound import deliver_one
    ep = await _mk_platform_ep(db_session, "feishu", secret="fs" + "k" * 14)
    d = await _platform_delivery(db_session, ep)
    ac = _FakeClient({"wx": (200, '{"code": 0}')})
    await deliver_one(db_session, d, client=ac)
    _, kw = ac.calls[0]
    obj = json.loads(kw["content"])
    assert obj["msg_type"] == "text" and "sign" in obj and "timestamp" in obj


# ---- SSRF 检查点2:投递前复核,配置性阻断直接 dead 不耗次 ----
async def test_deliver_ssrf_blocked_goes_dead_without_attempt(db_session,
                                                              monkeypatch):
    from app.services.outbound import deliver_one

    async def _priv(host):
        return ["10.0.0.1"]
    monkeypatch.setattr(wp, "_resolve_host", _priv)
    ep = await _mk_ep(db_session, events=[], url="http://private.example/x")
    d = WebhookDelivery(endpoint_id=ep.id, event_type="test",
                        event_id="e" * 32, payload={"event_id": "e" * 32},
                        status="pending", attempts=0)
    db_session.add(d)
    await db_session.commit()
    await deliver_one(db_session, d,
                      client=_FakeClient({}))  # 无路由:若发起 POST 会 KeyError
    assert d.status == "dead" and "SSRF blocked" in d.last_error
    assert d.attempts == 0  # 配置错误不算尝试,改 URL 后可重投


async def test_deliver_ssrf_enforce_off_skips_check(db_session, monkeypatch):
    from app.core.config import settings as cfg
    from app.services.outbound import deliver_one
    monkeypatch.setattr(cfg, "WEBHOOK_SSRF_ENFORCE", False)
    ep = await _mk_ep(db_session, events=[], url="http://private.example/x")
    d = WebhookDelivery(endpoint_id=ep.id, event_type="test",
                        event_id="e" * 32, payload={"event_id": "e" * 32},
                        status="pending", attempts=0)
    db_session.add(d)
    await db_session.commit()
    await deliver_one(db_session, d, client=_FakeClient({"x": 200}))
    assert d.status == "succeeded"  # ENFORCE=false 直投(conftest 替身也无所谓)


# ---- deliver_due 轮上限 ----
async def test_deliver_due_round_limit(db_session, monkeypatch):
    from app.core.config import settings as cfg
    from app.services.outbound import deliver_due
    monkeypatch.setattr(cfg, "WEBHOOK_DELIVER_ROUND_LIMIT", 3)
    ep = await _mk_ep(db_session, events=[], url="http://h/lim")
    for _ in range(5):
        db_session.add(WebhookDelivery(
            endpoint_id=ep.id, event_type="test", event_id=uuid.uuid4().hex,
            payload={"event_id": "x"}, status="pending", attempts=0))
    await db_session.commit()
    n = await deliver_due(db_session, client=_FakeClient({"lim": 200}))
    assert n == 3  # 单轮上限即返,余 2 行留下轮(状态仍 pending)
    from sqlalchemy import select as sa_select
    left = (await db_session.execute(sa_select(WebhookDelivery))).scalars().all()
    assert sum(1 for r in left if r.status == "succeeded") == 3
    assert sum(1 for r in left if r.status == "pending") == 2
