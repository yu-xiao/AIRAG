# backend/app/services/outbound.py
"""M17:出站事件发射与 webhook 投递引擎。

emit_event 与业务同事务(audit() 同哲学,不自行 commit);nudge 须在
业务 commit 之后调用——早到的投递任务扫不到未提交行,空转无害,beat
60s 兜底补。投递用 httpx.AsyncClient 每次调用新建随调用关闭,绝不
模块级缓存——worker 每任务一个新事件循环,跨循环复用即 M15 毒化
(NoneType.send,见 eval_runner._fresh_chat_llm 注释)。
"""
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from loguru import logger
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import WebhookDelivery, WebhookEndpoint

EVENT_TYPES = ("document.done", "document.failed", "eval.completed",
               "eval.failed", "chat.refused")
# 第 n 次失败后的退避分钟数(末位封顶;彻底放弃由 WEBHOOK_MAX_ATTEMPTS 裁决)
BACKOFF_MINUTES = (1, 5, 15, 60, 60)


def _utcnow_naive() -> datetime:
    """next_attempt_at 列为 naive TIMESTAMP(asyncpg 拒 aware 入参),
    全链路统一 naive UTC:写入与扫描比较同源,无歧义。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _envelope(event_type: str, data: dict) -> dict:
    """事件信封:event_id 为 uuid4().hex(32 位,幂等键)。"""
    return {
        "event_id": uuid.uuid4().hex,
        "event_type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }


async def emit_event(db: AsyncSession, event_type: str, data: dict) -> int:
    """按订阅展开为每个匹配端点一条 pending 投递行;同事务不 commit。

    订阅语义:events 为 None/[] = 订阅全部;非空且不含本事件 → 跳过。
    """
    env = _envelope(event_type, data)
    eps = (await db.execute(
        select(WebhookEndpoint).where(WebhookEndpoint.enabled.is_(True)),
    )).scalars().all()
    n = 0
    for ep in eps:
        if ep.events and event_type not in ep.events:
            continue
        db.add(WebhookDelivery(
            endpoint_id=ep.id, event_type=event_type,
            event_id=env["event_id"], payload=env,
            status="pending", attempts=0,
        ))
        n += 1
    return n


def sign_headers(secret: str, event_type: str, body: str) -> dict[str, str]:
    """签名头:hex(HMAC-SHA256(secret, f"{ts}.{body}"));ts 为 Unix 秒。"""
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.{body}".encode(),
                   hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json; charset=utf-8",
        "X-AIRag-Event": event_type,
        "X-AIRag-Timestamp": ts,
        "X-AIRag-Signature": sig,
    }


def _mark_retry_or_dead(delivery: WebhookDelivery, err: str) -> None:
    """可重试失败公共落点:耗尽转 dead,否则按退避表排下次(naive UTC)。"""
    delivery.last_error = err[:500]
    delivery.status = (
        "dead" if delivery.attempts >= settings.WEBHOOK_MAX_ATTEMPTS
        else "retrying"
    )
    if delivery.status == "retrying":
        delay = BACKOFF_MINUTES[
            min(delivery.attempts - 1, len(BACKOFF_MINUTES) - 1)]
        delivery.next_attempt_at = _utcnow_naive() + timedelta(minutes=delay)


async def deliver_one(db: AsyncSession, delivery: WebhookDelivery,
                      client=None) -> None:
    """投递单行并推进状态机;自建 client 随本次调用关闭(注入的不关)。

    端点已删除/禁用直接返回:不耗 attempts、状态不动,留待端点恢复后
    由下一轮扫描补投。状态分派:2xx→succeeded;429/5xx 及网络异常→
    退避重试,耗尽转 dead;其余 4xx→对方明确拒收,立即 dead。
    """
    ep = await db.get(WebhookEndpoint, delivery.endpoint_id)
    if ep is None or not ep.enabled:
        return
    delivery.attempts += 1
    body = json.dumps(delivery.payload, ensure_ascii=False)
    headers = sign_headers(ep.secret, delivery.event_type, body)
    owned = client is None
    ac = client if client is not None else httpx.AsyncClient()
    try:
        try:
            resp = await ac.post(ep.url, content=body, headers=headers,
                                 timeout=settings.WEBHOOK_TIMEOUT_S)
        except httpx.HTTPError as e:  # 超时/连接/传输类统一按可重试处理
            _mark_retry_or_dead(delivery, str(e))
        else:
            code = resp.status_code
            delivery.response_status = code
            if 200 <= code < 300:
                delivery.status = "succeeded"
                delivery.last_error = None
            elif code == 429 or code >= 500:
                _mark_retry_or_dead(delivery, f"retryable {code}")
            else:
                delivery.status = "dead"
                delivery.last_error = f"permanent {code}"
    finally:
        if owned:  # 注入的 client 归调用方管,绝不在此关闭
            await ac.aclose()
    await db.commit()


async def deliver_due(db: AsyncSession, client=None) -> int:
    """扫描到期行(pending 或 retrying 且 next_attempt_at 到期)分批投递。

    每批 50 按 id 升序;注入 client 时全程复用同一实例(测试语义),
    自建则随本次调用创建/关闭。返回真实尝试(POST 过)的行数。批内零
    尝试即停:状态无人推进,续扫必空转(死循环防线)。

    扫描必须 join 端点排除禁用:否则禁用行不离开扫描集(deliver_one
    跳过不改状态),最低 50 行全禁用时零尝试 break——队头饥饿,更高
    id 的其他端点投递被永久静默堵死。禁用行保持 pending 不耗次,等
    端点重新启用;行内无需再复查端点(deliver_one 内部仍保留,防单行
    调用路径)。
    """
    owned = client is None
    ac = client if client is not None else httpx.AsyncClient()
    total = 0
    try:
        while True:
            rows = (await db.execute(
                select(WebhookDelivery)
                .join(WebhookEndpoint,
                      WebhookDelivery.endpoint_id == WebhookEndpoint.id)
                .where(WebhookEndpoint.enabled.is_(True))
                .where(or_(
                    WebhookDelivery.status == "pending",
                    and_(WebhookDelivery.status == "retrying",
                         WebhookDelivery.next_attempt_at <= _utcnow_naive()),
                ))
                .order_by(WebhookDelivery.id.asc())
                .limit(50),
            )).scalars().all()
            attempted = 0
            for d in rows:
                await deliver_one(db, d, client=ac)
                attempted += 1
            total += attempted
            if attempted == 0:
                break  # 空批:禁用行已被 join 挡在扫描集外,无队头饥饿
    finally:
        if owned:
            await ac.aclose()
    return total


def nudge() -> None:
    """业务 commit 后踢投递任务免等 beat 60s;任何失败只告警不抛。

    webhook_tasks 的 import 必须在函数内——模块级会循环 import
    (webhook_tasks 引本模块的 deliver_due,反向顶层 import 成环)。
    """
    try:
        from app.workers.webhook_tasks import deliver_pending
        deliver_pending.delay()
    except Exception as e:  # noqa: BLE001 — nudge 绝不阻塞业务主流程
        logger.warning("webhook nudge 失败:{}", e)
