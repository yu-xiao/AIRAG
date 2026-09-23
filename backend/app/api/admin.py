import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.kbs import visible_kbs_for
from app.core.config import settings
from app.core.deps import require_admin
from app.db.session import get_db
from app.models import (
    AuditLog,
    KnowledgeBase,
    User,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.schemas.admin import (
    AdminKeyCreateIn,
    AdminUserIn,
    AdminUserKbOut,
    AdminUserOut,
    AuditLogOut,
    WebhookCreateIn,
    WebhookCreatedOut,
    WebhookDeliveryOut,
    WebhookOut,
    WebhookStats,
    WebhookUpdateIn,
)
from app.schemas.auth import ApiKeyCreatedOut
from app.services import outbound
from app.services.api_keys import (
    KbScopeInvalid,
    KeyQuotaExceeded,
    issue_api_key,
)
from app.services.audit import audit, purge_expired
from app.services.outbound import EVENT_TYPES, deliver_one
from app.services.webhook_providers import SsrfBlockedError, check_url_allowed

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=list[AdminUserOut])
async def list_users(
    q: str | None = None,
    limit: int | None = Query(None, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User).order_by(User.id)
    if q and q.strip():
        stmt = stmt.where(User.username.ilike(f"%{q.strip()}%"))
    stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = await db.execute(stmt)
    return list(rows.scalars().all())


@router.get("/users/{user_id}/kbs", response_model=list[AdminUserKbOut])
async def admin_user_kbs(
    user_id: int,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """M12:目标用户可见库列表(admin 代发 scoped key 的范围数据源)。"""
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    rows = await visible_kbs_for(db, target)
    return [AdminUserKbOut(id=kb.id, name=kb.name) for kb in rows]


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: int,
    payload: AdminUserIn,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if user_id == current.id:
        raise HTTPException(status_code=400, detail="cannot modify self")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    changes = {
        k: v
        for k, v in {"role": payload.role, "is_active": payload.is_active}.items()
        if v is not None
    }
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    await audit(
        db, current.username, "user_admin_update", f"user:{user_id}",
        {"target": user.username, **changes},
    )
    await db.commit()
    await db.refresh(user)
    return user


# ---- M9.1:admin 为指定账号发 API 密钥(配额按目标用户;审计记 by/to) ----
@router.post("/keys", response_model=ApiKeyCreatedOut, status_code=201)
async def admin_create_api_key(
    payload: AdminKeyCreateIn,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    target = await db.get(User, payload.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if not target.is_active:
        raise HTTPException(status_code=400, detail="target user is disabled")
    try:
        key, raw = await issue_api_key(db, target, payload.name,
                                       payload.expires_in_days, payload.role,
                                       payload.kb_scope)
    except KeyQuotaExceeded:
        raise HTTPException(status_code=409, detail="api key limit reached")
    except KbScopeInvalid as e:
        raise HTTPException(status_code=422, detail=e.message)
    await audit(
        db, current.username, "key_create", f"apikey:{key.id}",
        {"by": current.username, "to": target.username, "name": payload.name}
        | ({"kb_scope": key.kb_scope} if key.kb_scope else {}),
    )
    await db.commit()
    await db.refresh(key)
    return ApiKeyCreatedOut(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        role=key.role,
        is_active=key.is_active,
        expires_at=key.expires_at,
        last_used_at=key.last_used_at,
        created_at=key.created_at,
        kb_scope=key.kb_scope,
        key=raw,
    )


@router.get("/audit-logs")
async def list_audit_logs(
    username: str | None = None,
    action: str | None = None,
    page: int = 1,
    page_size: int = 20,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    where = []
    if username:
        where.append(AuditLog.username == username)
    if action:
        where.append(AuditLog.action == action)
    total = (
        await db.execute(select(func.count(AuditLog.id)).where(*where))
    ).scalar_one()
    rows = (
        await db.execute(
            select(AuditLog)
            .where(*where)
            .order_by(AuditLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return {"total": total, "items": [AuditLogOut.model_validate(r) for r in rows]}


@router.post("/audit-logs/purge")
async def purge_audit_logs(
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    deleted = await purge_expired(db)
    await db.commit()
    return {"deleted": deleted, "retention_days": settings.AUDIT_RETENTION_DAYS}


# ---- M17:webhook 端点管理(secret 铁律:明文仅 POST/rotate 响应一次) ----
def _masked(secret: str) -> str:
    """任意 secret 形态统一只露尾 4。"""
    return f"wh_****{secret[-4:]}"


def _to_out(ep: WebhookEndpoint) -> WebhookOut:
    return WebhookOut(
        id=ep.id, name=ep.name, url=ep.url, events=ep.events,
        enabled=ep.enabled, description=ep.description,
        secret_masked=_masked(ep.secret), created_at=ep.created_at,
        provider=ep.provider, kb_ids=ep.kb_ids,
    )


def _validate_events(events: list[str]) -> None:
    if any(e not in EVENT_TYPES for e in events):
        raise HTTPException(status_code=422, detail="invalid event type")


async def _validate_webhook_url(url_str: str) -> str:
    """M18:长度闸(HttpUrl 放行 2083 会撑爆 VARCHAR(500))+ SSRF 检查点1。"""
    if len(url_str) > 500:
        raise HTTPException(status_code=422,
                            detail="url exceeds 500 character limit")
    if settings.WEBHOOK_SSRF_ENFORCE:
        try:
            await check_url_allowed(url_str)
        except SsrfBlockedError as e:
            raise HTTPException(
                status_code=422,
                detail=f"url blocked by SSRF policy: {e}") from e
    return url_str


async def _validate_kb_ids(db: AsyncSession, kb_ids: list[int]) -> None:
    if not kb_ids:
        return
    found = (await db.execute(
        select(KnowledgeBase.id).where(KnowledgeBase.id.in_(kb_ids))
    )).scalars().all()
    missing = sorted(set(kb_ids) - set(found))
    if missing:
        raise HTTPException(status_code=422,
                            detail=f"unknown kb id: {missing}")


@router.post("/webhooks", response_model=WebhookCreatedOut, status_code=201)
async def create_webhook(
    payload: WebhookCreateIn,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    _validate_events(payload.events)
    if payload.kb_ids:
        await _validate_kb_ids(db, payload.kb_ids)
    url_str = await _validate_webhook_url(str(payload.url))
    dup = await db.execute(
        select(WebhookEndpoint).where(WebhookEndpoint.name == payload.name))
    if dup.scalars().first() is not None:
        raise HTTPException(status_code=409, detail="name already exists")
    # secret 按 provider:generic 必填(自动或自定义);wecom 占位随机
    # (列非空,永不参与计算/回显);钉钉/飞书=可选加签密钥,空=不加签
    if payload.provider == "generic":
        secret = payload.secret or secrets.token_hex(16)
    elif payload.provider == "wecom":
        secret = secrets.token_hex(16)
    else:
        secret = payload.secret or ""
    ep = WebhookEndpoint(
        name=payload.name, url=url_str, secret=secret,
        events=payload.events, description=payload.description,
        created_by=current.id, provider=payload.provider,
        kb_ids=payload.kb_ids or None,
    )
    db.add(ep)
    await db.flush()  # 取 ep.id 进审计 target
    await audit(db, current.username, "webhook_create",
                f"webhook:{ep.id}",
                {"name": ep.name, "url": ep.url,
                 "provider": payload.provider, "kb_ids": payload.kb_ids})
    await db.commit()
    await db.refresh(ep)
    out = _to_out(ep)
    return WebhookCreatedOut(**out.model_dump(), secret=secret)


@router.get("/webhooks", response_model=list[WebhookOut])
async def list_webhooks(
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """M18:列表附投递统计——按 endpoint×status 一次聚合(计数同
    deliveries 联查风格);无投递端点给全 0/None 默认。"""
    rows = (await db.execute(
        select(WebhookEndpoint).order_by(WebhookEndpoint.id))).scalars().all()
    agg: dict[int, dict] = {}
    for eid, status, cnt, last in (await db.execute(
        select(WebhookDelivery.endpoint_id, WebhookDelivery.status,
               func.count(), func.max(WebhookDelivery.created_at))
        .group_by(WebhookDelivery.endpoint_id, WebhookDelivery.status))
    ).all():
        a = agg.setdefault(eid, {"total": 0, "succeeded": 0, "pending": 0,
                                 "retrying": 0, "dead": 0,
                                 "last_activity_at": None})
        a["total"] += cnt
        if status in a:
            a[status] += cnt
        if last is not None and (a["last_activity_at"] is None
                                 or last > a["last_activity_at"]):
            a["last_activity_at"] = last
    out = []
    for ep in rows:
        o = _to_out(ep)
        o.stats = WebhookStats(**agg.get(
            ep.id, {"total": 0, "succeeded": 0, "pending": 0,
                    "retrying": 0, "dead": 0, "last_activity_at": None}))
        out.append(o)
    return out


@router.put("/webhooks/{webhook_id}")
async def update_webhook(
    webhook_id: int,
    payload: WebhookUpdateIn,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    ep = await db.get(WebhookEndpoint, webhook_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="webhook not found")
    changes = {}
    if payload.name is not None and payload.name != ep.name:
        dup = await db.execute(select(WebhookEndpoint).where(
            WebhookEndpoint.name == payload.name))
        if dup.scalars().first() is not None:
            raise HTTPException(status_code=409, detail="name already exists")
        ep.name = payload.name
        changes["name"] = payload.name
    if payload.url is not None:
        ep.url = await _validate_webhook_url(str(payload.url))
        changes["url"] = ep.url
    if payload.events is not None:
        _validate_events(payload.events)
        ep.events = payload.events
        changes["events"] = payload.events
    if payload.enabled is not None:
        ep.enabled = payload.enabled
        changes["enabled"] = payload.enabled
    if payload.provider is not None and payload.provider != ep.provider:
        ep.provider = payload.provider
        changes["provider"] = payload.provider
    if payload.kb_ids is not None:
        await _validate_kb_ids(db, payload.kb_ids)
        ep.kb_ids = payload.kb_ids or None   # [] → NULL(订阅全部)
        changes["kb_ids"] = ep.kb_ids
    if payload.description is not None:
        ep.description = payload.description or None  # "" → NULL 清空
        changes["description"] = ep.description
    new_secret = None
    if payload.rotate_secret:
        if ep.provider == "wecom":
            raise HTTPException(
                status_code=422,
                detail="rotate_secret not supported for wecom")
        new_secret = secrets.token_hex(16)
        ep.secret = new_secret
        changes["rotate_secret"] = True
    await audit(db, current.username, "webhook_update",
                f"webhook:{ep.id}",
                {"target": ep.name, "changed": sorted(changes)})
    await db.commit()
    await db.refresh(ep)
    if new_secret is not None:
        out = _to_out(ep)
        return WebhookCreatedOut(**out.model_dump(), secret=new_secret)
    return _to_out(ep)


@router.delete("/webhooks/{webhook_id}", status_code=204)
async def delete_webhook(
    webhook_id: int,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    ep = await db.get(WebhookEndpoint, webhook_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="webhook not found")
    name = ep.name
    await db.delete(ep)
    await audit(db, current.username, "webhook_delete",
                f"webhook:{webhook_id}", {"name": name})
    await db.commit()


@router.post("/webhooks/{webhook_id}/test")
async def test_webhook(
    webhook_id: int,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """同步内联单发(event_type="test",绕过订阅过滤);真实投递走
    deliver_one(测试 patch app.api.admin.deliver_one)。"""
    ep = await db.get(WebhookEndpoint, webhook_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="webhook not found")
    if not ep.enabled:
        raise HTTPException(status_code=400, detail="endpoint disabled")
    event_id = uuid.uuid4().hex
    row = WebhookDelivery(
        endpoint_id=ep.id, event_type="test", event_id=event_id,
        payload={"event_id": event_id, "event_type": "test",
                 "occurred_at": datetime.now(timezone.utc).isoformat(),
                 "data": {"message": "airag webhook test"}},
        status="pending", attempts=0,
    )
    db.add(row)
    await db.commit()
    await deliver_one(db, row)
    await db.refresh(row)
    await audit(db, current.username, "webhook_test", f"webhook:{ep.id}",
                {"name": ep.name, "status": row.status,
                 "response_status": row.response_status})
    await db.commit()
    return {"status": row.status, "response_status": row.response_status,
            "error": row.last_error}


@router.post("/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver")
async def redeliver_delivery(
    webhook_id: int,
    delivery_id: int,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """M18:手动重投——dead/retrying 归零重排近即时投递(nudge);审计留痕。"""
    ep = await db.get(WebhookEndpoint, webhook_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="webhook not found")
    d = await db.get(WebhookDelivery, delivery_id)
    if d is None or d.endpoint_id != ep.id:
        raise HTTPException(status_code=404, detail="delivery not found")
    if d.status not in ("dead", "retrying"):
        raise HTTPException(
            status_code=422,
            detail="only dead/retrying deliveries can be redelivered")
    old_rs = d.response_status
    d.attempts = 0
    d.status = "pending"
    d.next_attempt_at = None
    d.last_error = None
    await audit(db, current.username, "webhook_redeliver", f"webhook:{ep.id}",
                {"delivery_id": d.id, "event_type": d.event_type,
                 "old_response_status": old_rs})
    await db.commit()
    outbound.nudge()
    return {"status": "pending", "delivery_id": d.id}


@router.get("/webhook-deliveries")
async def list_webhook_deliveries(
    endpoint_id: int | None = None,
    event_type: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """投递记录分页(id desc);endpoint_name 页内 IN 一次联查(eval 同法)。"""
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    where = []
    if endpoint_id is not None:
        where.append(WebhookDelivery.endpoint_id == endpoint_id)
    if event_type:
        where.append(WebhookDelivery.event_type == event_type)
    if status:
        where.append(WebhookDelivery.status == status)
    total = (await db.execute(
        select(func.count(WebhookDelivery.id)).where(*where))).scalar_one()
    rows = (await db.execute(
        select(WebhookDelivery).where(*where)
        .order_by(WebhookDelivery.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    ep_ids = {r.endpoint_id for r in rows}
    ep_names: dict[int, str] = {}
    if ep_ids:
        eps = (await db.execute(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.id.in_(ep_ids)))).scalars().all()
        ep_names = {e.id: e.name for e in eps}
    items = [
        WebhookDeliveryOut(
            id=r.id, endpoint_id=r.endpoint_id,
            endpoint_name=ep_names.get(r.endpoint_id, ""),
            event_type=r.event_type, status=r.status, attempts=r.attempts,
            response_status=r.response_status, last_error=r.last_error,
            payload=r.payload, next_attempt_at=r.next_attempt_at,
            created_at=r.created_at,
        )
        for r in rows
    ]
    return {"total": total, "items": items}
