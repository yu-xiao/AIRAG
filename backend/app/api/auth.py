from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models import User
from app.schemas.auth import LoginIn, RegisterIn, TokenOut, UserOut
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
