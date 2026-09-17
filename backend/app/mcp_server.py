# backend/app/mcp_server.py
"""M9:MCP 面(Streamable HTTP)。鉴权/限流在 AgentAuthMiddleware(纯 ASGI),
主体经 core.deps.current_principal 传入;工具与 REST 共用 agent_facade。"""
import json
from dataclasses import asdict

from fastapi import HTTPException
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from app.core.config import settings
from app.core.deps import (
    Principal,
    current_client_ip,
    current_principal,
    resolve_bearer_principal,
)
from app.db.session import SessionLocal
from app.services import agent_facade
from app.services.agent_ratelimit import allow as rate_allow
from app.services.audit import audit

mcp = FastMCP(name="AIRag")


def _principal() -> Principal:
    p = current_principal.get()
    if p is None:  # 中间件已拦截,此为防御
        raise ToolError("unauthorized: missing or invalid API key")
    return p


@mcp.tool
async def list_knowledge_bases() -> dict:
    """列出当前 API Key 归属用户有权访问的知识库。

    返回 {"items": [{"id", "name", "description", "my_perm"}]},
    my_perm ∈ viewer|editor|owner。检索前先调用本工具确认可用知识库 id。
    """
    p = _principal()
    async with SessionLocal() as db:
        items = await agent_facade.list_kbs_for(db, p.user)
        await audit(db, p.user.username, "agent.list_kbs", "agent",
                    {"client": "mcp", "key_name": p.key_name,
                     "kb_count": len(items)},
                    ip=current_client_ip.get())
        await db.commit()
        return {"items": [asdict(i) for i in items]}


@mcp.tool
async def search_knowledge_base(
    kb_ids: list[int], query: str,
    top_k: int = settings.RETRIEVAL_TOP_K, rerank: bool = False,
) -> dict:
    """在指定知识库中混合检索(向量 + 关键词,RRF 融合)。

    Args:
        kb_ids: 知识库 id 列表(1~5 个),须为当前密钥有权访问的库。
        query: 检索问题,1~500 字。
        top_k: 命中条数上限,1~20,默认 8。
        rerank: 是否启用 rerank 重排(服务端未配置 rerank 时忽略)。

    Returns:
        {"hits": [{chunk_id, document_id, kb_id, filename, page_no,
        content, score, source}], "total", "elapsed_ms"}
    """
    p = _principal()
    if not 1 <= len(kb_ids) <= 5:
        raise ToolError("kb_ids must contain 1~5 ids")
    if not 1 <= len(query) <= 500:
        raise ToolError("query must be 1~500 chars")
    if not 1 <= top_k <= 20:
        raise ToolError("top_k must be 1~20")
    async with SessionLocal() as db:
        try:
            outcome = await agent_facade.agent_search(
                db, p.user, kb_ids, query, top_k, rerank,
            )
        except agent_facade.AgentKbDenied as e:
            raise ToolError(f"kb_forbidden, denied_kb_ids={e.denied_kb_ids}")
        await audit(db, p.user.username, "agent.search", "agent",
                    {"client": "mcp", "key_name": p.key_name, "kb_ids": kb_ids,
                     "query": query[:200], "hit_count": len(outcome.hits)},
                    ip=current_client_ip.get())
        await db.commit()
        return {"hits": [asdict(h) for h in outcome.hits],
                "total": len(outcome.hits),
                "elapsed_ms": outcome.elapsed_ms}


class AgentAuthMiddleware:
    """纯 ASGI:解析 Bearer(airag_ key 或 JWT)→ current_principal;401/429 短路。"""

    def __init__(self, app):
        self.app = app

    @property
    def lifespan(self):
        # fastmcp http_app 的 lifespan 需在宿主启动期运行(session manager)
        return self.app.lifespan

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # mount("/") 是兜底路由:宿主中一切未匹配路径(/api 拼写错、错误方法、
        # 尾斜杠变体等)都会落进本中间件,若一律鉴权会把正常 404/405/307 变成
        # 401。仅 /mcp(含尾斜杠变体)归我们管,其余直接 404 保持路由语义。
        if scope["path"] not in ("/mcp", "/mcp/"):
            await _send_json(send, 404, {"detail": "not found"})
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        authz = headers.get("authorization", "")
        if not authz.lower().startswith("bearer "):
            await _send_json(send, 401, {"detail": "not authenticated"})
            return
        raw = authz[7:].strip()
        async with SessionLocal() as db:
            try:
                principal = await resolve_bearer_principal(db, raw)
                await db.commit()  # last_used_at(MCP 工具另开会话)
            except HTTPException as e:
                # 透传细分 code(invalid_key|key_revoked|key_expired 等)
                await _send_json(send, e.status_code, {"detail": e.detail})
                return
        if principal.kind == "api_key":
            ok, retry_after = await rate_allow(f"key:{principal.key_id}")
            if not ok:
                await _send_json(send, 429, {"detail": {
                    "code": "rate_limited", "retry_after": retry_after}})
                return
        client = scope.get("client")  # (host, port);与 api/agent._ip 同语义
        ip = client[0] if client else None
        principal_token = current_principal.set(principal)
        ip_token = current_client_ip.set(ip)
        try:
            await self.app(scope, receive, send)
        finally:
            current_client_ip.reset(ip_token)
            current_principal.reset(principal_token)


async def _send_json(send, status: int, body: dict) -> None:
    payload = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": payload})


def build_mcp_asgi_app():
    """path="/mcp" 且挂载于宿主根("/")使完整端点恰为 /mcp(fastmcp 官方
    FastAPI 集成法);注:Starlette 1.6 的 Mount("/mcp") 不匹配裸路径 /mcp
    (仅 /mcp/...),故不能 path="/" + mount("/mcp")。
    json_response=True:POST 响应 application/json(SSE 通道仍按需保留),
    客户端 Accept 两类均可(MCP 规范允许)。"""
    return AgentAuthMiddleware(
        mcp.http_app(path="/mcp", transport="streamable-http",
                     json_response=True)
    )
