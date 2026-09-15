from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.session import get_db
from app.models import User
from app.schemas.auth import RegisterIn, UserOut

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
    await db.commit()
    await db.refresh(user)
    return user
