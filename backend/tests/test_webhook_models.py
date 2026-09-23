# backend/tests/test_webhook_models.py
"""M17 T1:webhook 两表模型与级联。"""
import uuid

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


# ---- M18:provider / kb_ids 两列 ----
async def test_endpoint_provider_and_kb_ids_roundtrip(db_session):
    ep = WebhookEndpoint(
        name=f"m18ep{uuid.uuid4().hex[:8]}", url="http://x/h",
        secret="s" * 16, events=[], provider="dingtalk",
        kb_ids=[1, 3], created_by=1)
    db_session.add(ep)
    await db_session.commit()
    await db_session.refresh(ep)
    assert ep.provider == "dingtalk" and ep.kb_ids == [1, 3]


async def test_endpoint_defaults_generic_and_null_kbs(db_session):
    ep = WebhookEndpoint(
        name=f"m18def{uuid.uuid4().hex[:8]}", url="http://x/h",
        secret="s" * 16, events=[], created_by=1)
    db_session.add(ep)
    await db_session.commit()
    await db_session.refresh(ep)
    assert ep.provider == "generic" and ep.kb_ids is None
