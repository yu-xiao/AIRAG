from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models import ApiKey, User
from app.schemas.auth import (
    ApiKeyCreateIn,
    ApiKeyCreatedOut,
    ApiKeyOut,
    LoginIn,
    RegisterIn,
    TokenOut,
    UserOut,
)
from app.services.api_keys import generate_api_key
from app.services.audit import audit

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=201)
async def register(payload: RegisterIn, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == payload.username))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="username already exists")
    user = User(
        username=payload.username, password_hash=hash_password(payload.password)
    )
    db.add(user)
    await db.flush()
    await audit(db, user.username, "register", f"user:{user.id}")
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenOut)
async def login(
    payload: LoginIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ip = request.client.host if request.client else None
    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        await audit(db, payload.username, "login_fail", ip=ip)
        await db.commit()
        raise HTTPException(status_code=401, detail="invalid credentials")
    if not user.is_active:
        await audit(db, payload.username, "login_fail", detail="disabled", ip=ip)
        await db.commit()
        raise HTTPException(status_code=401, detail="user disabled")
    await audit(db, user.username, "login_success", f"user:{user.id}", ip=ip)
    await db.commit()
    return TokenOut(access_token=create_access_token(user.id))


@router.get("/me", response_model=UserOut)
async def me(current: User = Depends(get_current_user)):
    return current


# ---- M9:API 密钥管理(JWT-only;key 不能创建 key) ----
@router.post("/keys", response_model=ApiKeyCreatedOut, status_code=201)
async def create_api_key(
    payload: ApiKeyCreateIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    count = (
        await db.execute(
            select(func.count()).select_from(ApiKey).where(
                ApiKey.user_id == current.id, ApiKey.is_active == True  # noqa: E712
            )
        )
    ).scalar_one()
    if count >= settings.AGENT_MAX_KEYS_PER_USER:
        raise HTTPException(status_code=409, detail="api key limit reached")
    raw, prefix, digest = generate_api_key()
    key = ApiKey(
        user_id=current.id,
        name=payload.name,
        key_prefix=prefix,
        key_hash=digest,
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days)
            if payload.expires_in_days
            else None
        ),
    )
    db.add(key)
    await db.flush()
    await audit(db, current.username, "key_create", f"apikey:{key.id}",
                {"name": payload.name})
    await db.commit()
    await db.refresh(key)
    # pydantic 2.13 的 model_validate 不支持 update=,手工拼字段(明文仅此一次)
    return ApiKeyCreatedOut(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        is_active=key.is_active,
        expires_at=key.expires_at,
        last_used_at=key.last_used_at,
        created_at=key.created_at,
        key=raw,
    )


@router.get("/keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(ApiKey).where(ApiKey.user_id == current.id)
            .order_by(ApiKey.id.desc())
        )
    ).scalars().all()
    return rows


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    key = await db.get(ApiKey, key_id)
    if key is None or key.user_id != current.id:
        raise HTTPException(status_code=404, detail="api key not found")
    key.is_active = False
    await audit(db, current.username, "key_revoke", f"apikey:{key.id}")
    await db.commit()
    return Response(status_code=204)
