from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import get_db
from app.models import User
from app.services.api_keys import KeyRejected, resolve_api_key

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    payload = decode_access_token(creds.credentials)
    if payload is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    user = await db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="user not found or disabled")
    return user


async def require_admin(current: User = Depends(get_current_user)) -> User:
    if current.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return current


# ---- M9:对外 Agent 主体(双路径:JWT 或 airag_ key) ----
@dataclass
class Principal:
    user: User
    kind: str  # jwt | api_key
    key_id: int | None = None
    key_name: str | None = None
    key_role: str | None = None  # M11:read_only|editor;JWT 恒 None


# MCP 侧由 ASGI 中间件写入(mcp_server.py),工具函数读取
current_principal: ContextVar[Principal | None] = ContextVar(
    "current_principal", default=None
)

# 同上:中间件从 ASGI scope 提取的客户端 ip(纯 ASGI 无 Request 对象,
# REST 面走 api/agent._ip,MCP 工具审计从这里取,满足 spec B 节 ip 要求)
current_client_ip: ContextVar[str | None] = ContextVar(
    "current_client_ip", default=None
)


async def resolve_bearer_principal(db: AsyncSession, raw: str) -> Principal:
    """把 Bearer 凭证解析为 Principal;失败抛 401 HTTPException。"""
    if raw.startswith("airag_"):
        try:
            key = await resolve_api_key(db, raw)
        except KeyRejected as e:
            raise HTTPException(status_code=401, detail=e.code)
        user = await db.get(User, key.user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="invalid_key")
        key.last_used_at = datetime.now(timezone.utc)
        return Principal(user=user, kind="api_key", key_id=key.id,
                         key_name=key.name, key_role=key.role)
    payload = decode_access_token(raw)
    if payload is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    user = await db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="user not found or disabled")
    return Principal(user=user, kind="jwt")


async def get_agent_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    if creds is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return await resolve_bearer_principal(db, creds.credentials)


def _api_key_id(principal: Principal) -> int | None:
    """api_key 主体返回 key_id,JWT(人工调试)返回 None——限流/配额/配额查询
    的统一守卫(M10 顺延小项②:替换各处 kind+key_id 双条件)。"""
    if principal.kind == "api_key":
        return principal.key_id
    return None


def require_editor_key(principal: Principal) -> None:
    """写操作 key 能力守卫:JWT 调试通道视为 editor(仍受用户 KB 权限约束,
    与限流豁免 JWT 同哲学);api_key 须为 editor。"""
    if principal.kind == "api_key" and principal.key_role != "editor":
        raise HTTPException(status_code=403,
                            detail={"code": "editor_key_required"})
