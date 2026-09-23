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
