# backend/app/mcp_server.py
"""M9:MCP 面(Streamable HTTP)。鉴权/限流在 AgentAuthMiddleware(纯 ASGI),
主体经 core.deps.current_principal 传入;工具与 REST 共用 agent_facade。"""
import base64
import json
from dataclasses import asdict
from pathlib import Path

from fastapi import HTTPException
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from loguru import logger
from sqlalchemy import select

from app.core.config import settings
from app.core.deps import (
    Principal,
    _api_key_id,
    current_client_ip,
    current_principal,
    require_editor_key,
    resolve_bearer_principal,
)
from app.core.perms import get_kb_perm, has_perm
from app.db.session import SessionLocal
from app.models import Document, KnowledgeBase
from app.schemas.agent import CitationOut
from app.services import agent_facade, doc_ops
from app.services.agent_ratelimit import (
    allow as rate_allow,
    quota_check,
    quota_consume,
    quota_remaining,
)
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
        items = await agent_facade.list_kbs_for(db, p.user, p.key_scope)
        await audit(db, p.user.username, "agent.list_kbs", "agent",
                    {"client": "mcp", "key_name": p.key_name,
                     "kb_count": len(items)},
                    ip=current_client_ip.get())
        await db.commit()
        return {"items": [asdict(i) for i in items]}


_TOP_K_DEFAULT = settings.RETRIEVAL_TOP_K

# 小项④:docstring 默认值与配置同源。注:CPython 只把纯字符串字面量首语句
# 设为 __doc__,f-string 表达式语句会被静默丢弃(工具描述变 None),
# 故在模块级求值 f-string 后经 description= 传入,文本与 docstring 逐字一致。
_SEARCH_DOC = f"""在指定知识库中混合检索(向量 + 关键词,RRF 融合)。

Args:
    kb_ids: 知识库 id 列表(1~5 个),须为当前密钥有权访问的库
    (若密钥设了范围,还须在范围内)。
    query: 检索问题,1~500 字。
    top_k: 命中条数上限,1~20,默认 {_TOP_K_DEFAULT}。
    rerank: 是否启用 rerank 重排(服务端未配置 rerank 时忽略)。

Returns:
    {{"hits": [{{chunk_id, document_id, kb_id, filename, page_no,
    content, score, source}}], "total", "elapsed_ms"}}
"""


@mcp.tool(description=_SEARCH_DOC)
async def search_knowledge_base(
    kb_ids: list[int], query: str,
    top_k: int = settings.RETRIEVAL_TOP_K, rerank: bool = False,
) -> dict:
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
                db, p.user, kb_ids, query, top_k, rerank, p.key_scope,
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


@mcp.tool
async def ask_knowledge_base(
    kb_ids: list[int], query: str, rerank: bool = False,
) -> dict:
    """基于知识库内容直接生成回答(单轮、非流式、带引用)。

    与 search_knowledge_base 的区别:本工具返回服务端生成的完整答案与
    引用编号(质量与 AIRag Web 端一致,含查询改写/检索自评/多跳兜底),
    而非检索片段。

    Args:
        kb_ids: 知识库 id 列表(1~5 个),须为当前密钥有权访问的库
        (若密钥设了范围,还须在范围内)。
        query: 问题,1~500 字。单轮无上下文,追问请携带完整问题。
        rerank: 是否启用 rerank 重排(服务端未配置 rerank 时忽略)。

    Returns:
        {"answer", "citations": [{number, chunk_id, document_id,
        filename, page_no, excerpt}], "refused", "tokens_used",
        "elapsed_ms"}。refused=true 表示知识库中未找到相关内容。
        内部多步 LLM 调用,耗时可达 40~90 秒,客户端超时请设充足
        (如 Claude Code 的 MCP_TIMEOUT)。
    """
    p = _principal()
    if not 1 <= len(kb_ids) <= 5:
        raise ToolError("kb_ids must contain 1~5 ids")
    if not 1 <= len(query) <= 500:
        raise ToolError("query must be 1~500 chars")
    if (key_id := _api_key_id(p)) is not None:
        ok, retry_after = await quota_check(key_id)
        if not ok:
            raise ToolError(f"quota_exhausted, retry_after={retry_after}s")
    async with SessionLocal() as db:
        try:
            outcome = await agent_facade.agent_ask(
                db, p.user, kb_ids, query, rerank, p.key_scope,
            )
        except agent_facade.AgentKbDenied as e:
            raise ToolError(f"kb_forbidden, denied_kb_ids={e.denied_kb_ids}")
        except Exception:
            logger.exception("agent ask failed")
            raise ToolError("internal error")
        if (key_id := _api_key_id(p)) is not None:
            await quota_consume(key_id, outcome.tokens_used)
        await audit(db, p.user.username, "agent.ask", "agent",
                    {"client": "mcp", "key_name": p.key_name,
                     "kb_ids": kb_ids, "query": query[:200],
                     "refused": outcome.refused,
                     "tokens": outcome.tokens_used,
                     "elapsed_ms": outcome.elapsed_ms},
                    ip=current_client_ip.get())
        await db.commit()
        return {"answer": outcome.answer,
                "citations": [CitationOut(**c).model_dump()
                              for c in outcome.citations],
                "refused": outcome.refused,
                "tokens_used": outcome.tokens_used,
                "elapsed_ms": outcome.elapsed_ms}


# ---- M11:文档工具(读:可见即可;写:key=editor ∧ perm≥editor,守卫序与 REST 同) ----

_UPLOAD_DOC = """上传文档到指定知识库并触发解析流水线(异步)。

需 editor 密钥(read_only 密钥将被拒绝)。上传后用 get_document 轮询
status 直到 done/failed。

Args:
    kb_id: 目标知识库 id,须为当前密钥归属用户有 editor 权限的库
    (若密钥设了范围,还须在范围内)。
    filename: 文件名(含扩展名;.pdf/.docx/.xlsx/.jpg/.jpeg/.png)。
    content_b64: 文件内容的 base64 编码(解码后不超过服务端 MAX_UPLOAD_MB)。
    ocr: OCR 模式 auto|force|off,默认 auto。

Returns:
    {"id", "filename", "status", "size", "chunk_count", ...}(DocumentOut 字段)。
"""
_LIST_DOC = """列出指定知识库的文档(id/文件名/状态/分块数等)。

Args:
    kb_id: 知识库 id,须为当前密钥有权访问的库(若密钥设了范围,还须在范围内)。
    limit: 返回条数上限 1~200,默认 50。

Returns:
    {"items": [DocumentOut 字段], "total"}
"""
_GET_DOC = """查询单个文档详情与处理状态(上传后轮询用)。

Args:
    doc_id: 文档 id。

Returns:
    DocumentOut 字段(id/filename/status/error_msg/chunk_count/...)。
"""
_DELETE_DOC = """删除文档(不可逆:连同全部分块、向量与源文件)。

需 editor 密钥;文档处理中(status 为 parsing/chunking/embedding)返回 busy。

Args:
    doc_id: 文档 id。

Returns:
    {"deleted": true}
"""
_REPROCESS_DOC = """重新解析文档(清空旧分块后重跑解析流水线)。

需 editor 密钥;文档处理中返回 busy。

Args:
    doc_id: 文档 id。

Returns:
    DocumentOut 字段(status 回到 pending)。
"""


def _e(e: HTTPException) -> ToolError:
    """doc_ops 的 HTTPException → ToolError(文本携带错误 code,spec E)。

    404 detail 是 "document not found" 类自然语句,不含 not_found code,
    故按 status_code 映射 code 前缀、detail 原样保留(409 按 detail 区分
    busy/duplicate)。
    """
    detail = str(e.detail)
    code = {404: "not_found", 413: "too_large", 415: "unsupported_type"}.get(
        e.status_code)
    if code is None and e.status_code == 409:
        code = "busy" if "being processed" in detail else "duplicate"
    if code is None:
        return ToolError(detail)
    return ToolError(f"{code}: {detail}")


@mcp.tool(description=_LIST_DOC)
async def list_documents(kb_id: int, limit: int = 50) -> dict:
    p = _principal()
    if not 1 <= limit <= 200:
        raise ToolError("limit must be 1~200")
    async with SessionLocal() as db:
        try:
            await doc_ops.visible_kb_or_404(db, p.user, kb_id, p.key_scope)
        except HTTPException as e:
            raise _e(e)
        rows = (await db.execute(
            select(Document)
            .where(Document.kb_id == kb_id)
            .order_by(Document.id.desc()).limit(limit)
        )).scalars().all()
        items = [{"id": d.id, "filename": d.filename, "status": d.status,
                  "size": d.size, "error_msg": d.error_msg,
                  "page_count": d.page_count, "chunk_count": d.chunk_count,
                  "ocr_mode": d.ocr_mode, "ocr_used": d.ocr_used,
                  "created_at": d.created_at.isoformat()} for d in rows]
        await audit(db, p.user.username, "agent.list_documents", "agent",
                    {"client": "mcp", "key_name": p.key_name, "kb_id": kb_id,
                     "doc_count": len(items)},
                    ip=current_client_ip.get())
        await db.commit()
        return {"items": items, "total": len(items)}


@mcp.tool(description=_GET_DOC)
async def get_document(doc_id: int) -> dict:
    p = _principal()
    async with SessionLocal() as db:
        try:
            d = await doc_ops.visible_doc_or_404(db, p.user, doc_id,
                                                 p.key_scope)
        except HTTPException as e:
            raise _e(e)
        await audit(db, p.user.username, "agent.get_document", "agent",
                    {"client": "mcp", "key_name": p.key_name,
                     "kb_id": d.kb_id, "doc_id": d.id, "status": d.status},
                    ip=current_client_ip.get())
        await db.commit()
        return {"id": d.id, "kb_id": d.kb_id, "filename": d.filename,
                "status": d.status, "error_msg": d.error_msg,
                "size": d.size, "sha256": d.sha256,
                "page_count": d.page_count, "chunk_count": d.chunk_count,
                "ocr_mode": d.ocr_mode, "ocr_used": d.ocr_used,
                "created_at": d.created_at.isoformat()}


@mcp.tool(description=_UPLOAD_DOC)
async def upload_document(kb_id: int, filename: str, content_b64: str,
                          ocr: str = "auto") -> dict:
    p = _principal()
    try:
        payload = base64.b64decode(content_b64, validate=True)
    except Exception:
        raise ToolError("bad_base64: content_b64 is not valid base64")
    if not payload:
        raise ToolError("bad_base64: decoded content is empty")
    ext = Path(filename or "").suffix.lower()
    if ext not in doc_ops.ALLOWED_EXTS:
        raise ToolError(f"unsupported_type: {ext}")
    if len(payload) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise ToolError(f"too_large: exceeds {settings.MAX_UPLOAD_MB}MB")
    if ocr not in ("auto", "force", "off"):
        raise ToolError("ocr must be auto|force|off")
    async with SessionLocal() as db:
        # 校验顺序与 REST 一致(spec B):可见性(404)→ perm → key 能力
        try:
            kb = await doc_ops.visible_kb_or_404(db, p.user, kb_id,
                                                 p.key_scope)
        except HTTPException as e:
            raise _e(e)
        if not has_perm(await get_kb_perm(db, p.user, kb), "editor"):
            raise ToolError("editor permission required")
        try:
            require_editor_key(p)
        except HTTPException:
            raise ToolError("editor_key_required: this key is read-only")
        try:
            d = await doc_ops.save_upload(
                db, kb, filename=filename, payload=payload, mime=None,
                ocr_mode=ocr, username=p.user.username,
                action="agent.upload_document",
                audit_extra={"client": "mcp", "key_name": p.key_name},
            )
        except HTTPException as e:
            raise _e(e)
        return {"id": d.id, "filename": d.filename, "status": d.status,
                "size": d.size, "chunk_count": d.chunk_count,
                "created_at": d.created_at.isoformat()}


@mcp.tool(description=_DELETE_DOC)
async def delete_document(doc_id: int) -> dict:
    p = _principal()
    async with SessionLocal() as db:
        # 校验顺序与 REST 一致(spec B):可见性(404)→ perm → key 能力
        try:
            d = await doc_ops.visible_doc_or_404(db, p.user, doc_id,
                                                 p.key_scope)
        except HTTPException as e:
            raise _e(e)
        kb = await db.get(KnowledgeBase, d.kb_id)
        if not has_perm(await get_kb_perm(db, p.user, kb), "editor"):
            raise ToolError("editor permission required")
        try:
            require_editor_key(p)
        except HTTPException:
            raise ToolError("editor_key_required: this key is read-only")
        try:
            await doc_ops.delete_document(
                db, d, username=p.user.username,
                action="agent.delete_document",
                audit_extra={"client": "mcp", "key_name": p.key_name},
            )
        except HTTPException as e:
            raise _e(e)
        return {"deleted": True}


@mcp.tool(description=_REPROCESS_DOC)
async def reprocess_document(doc_id: int) -> dict:
    p = _principal()
    async with SessionLocal() as db:
        # 校验顺序与 REST 一致(spec B):可见性(404)→ perm → key 能力
        try:
            d = await doc_ops.visible_doc_or_404(db, p.user, doc_id,
                                                 p.key_scope)
        except HTTPException as e:
            raise _e(e)
        kb = await db.get(KnowledgeBase, d.kb_id)
        if not has_perm(await get_kb_perm(db, p.user, kb), "editor"):
            raise ToolError("editor permission required")
        try:
            require_editor_key(p)
        except HTTPException:
            raise ToolError("editor_key_required: this key is read-only")
        try:
            out = await doc_ops.reprocess_document(
                db, d, username=p.user.username,
                action="agent.reprocess_document",
                audit_extra={"client": "mcp", "key_name": p.key_name},
            )
        except HTTPException as e:
            raise _e(e)
        return {"id": out.id, "filename": out.filename, "status": out.status,
                "chunk_count": out.chunk_count}


_QUOTA_DOC = """查询当前密钥今日 ask token 配额余量。

Returns:
    {"used", "limit", "reset_at"};used=null 表示禁用或 Redis 降级。
"""


@mcp.tool(description=_QUOTA_DOC)
async def get_quota() -> dict:
    key_id = _api_key_id(_principal())
    if key_id is None:
        raise ToolError("api key principal required")
    return await quota_remaining(key_id)


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
            ok, retry_after = await rate_allow(principal.key_id)
            if not ok:
                await _send_json(
                    send, 429,
                    {"detail": {"code": "rate_limited", "retry_after": retry_after}},
                    extra_headers=((b"retry-after",
                                    str(retry_after).encode()),),
                )
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


async def _send_json(send, status: int, body: dict,
                     extra_headers: tuple = ()) -> None:
    payload = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            *extra_headers]})
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
