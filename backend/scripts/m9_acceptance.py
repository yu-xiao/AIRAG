"""M9 无头验收脚本（真栈：http://127.0.0.1:8001 + worker + Redis）。

用法（backend 目录，项目 venv）：
    .venv\\Scripts\\python scripts\\m9_acceptance.py

覆盖：key 生命周期（创建/列表/吊销）、agent REST（kbs 可见性/search 真检索/
403/401）、限流 429、MCP initialize（带/不带 key）。前置：start_dev.bat
已起服务（含 worker），Redis 可用。收尾清 DB（api_keys/chunks/documents/
kb_permissions/knowledge_bases/users，FK 顺序）。
"""
import asyncio
import sys
import time
import uuid

import httpx
import pymupdf as fitz

BASE = "http://127.0.0.1:8001"
API = f"{BASE}/api"
TIMEOUT = httpx.Timeout(120.0)
RESULTS = {"pass": [], "fail": [], "skip": []}

ACCEPT = "application/json, text/event-stream"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "m9acc", "version": "0"}}}

FACT = "青鸾号高空气船的巡航升限为八千五百米"
FACT_Q = "青鸾号高空气船的巡航升限是多少米?"
SUFFIX = uuid.uuid4().hex[:6]
PDF_MIME = "application/pdf"


def check(name, cond, detail=""):
    (RESULTS["pass"] if cond else RESULTS["fail"]).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))


def skip(name, why):
    RESULTS["skip"].append(name)
    print(f"SKIP {name}  ({why})")


def summary_and_exit():
    total = sum(len(v) for v in RESULTS.values())
    print(f"\nM9 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
    if RESULTS["fail"]:
        print("FAILED:", *RESULTS["fail"], sep="\n  - ")
        sys.exit(1)


def _nullpool_sessionmaker():
    # m8 同款：一次性 NullPool 引擎（SessionLocal 连接池跨事件循环复用
    # asyncpg 连接会崩，与 workers/pipeline._engine 同法）
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import settings

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def promote_roles(usernames: dict[str, str]) -> None:
    """直连 DB 提权（register 默认 viewer，建库/上传需要 editor）。"""
    from sqlalchemy import text

    engine, maker = _nullpool_sessionmaker()
    try:
        async with maker() as s:
            for username, role in usernames.items():
                await s.execute(
                    text("UPDATE users SET role = :r WHERE username = :u"),
                    {"r": role, "u": username},
                )
            await s.commit()
    finally:
        await engine.dispose()


async def cleanup(user_ids: list[int], kb_ids: list[int],
                  usernames: list[str]) -> None:
    """FK 顺序清库：api_keys → chunks → documents → kb_permissions →
    knowledge_bases → users（m8 cleanup_kbs 同款惯例，追加 key/用户两层）。"""
    from sqlalchemy import text

    engine, maker = _nullpool_sessionmaker()
    try:
        async with maker() as s:
            for uid in user_ids:
                await s.execute(
                    text("DELETE FROM api_keys WHERE user_id = :u"), {"u": uid})
            for kid in kb_ids:
                await s.execute(
                    text("DELETE FROM chunks WHERE kb_id = :k"), {"k": kid})
                await s.execute(
                    text("DELETE FROM documents WHERE kb_id = :k"), {"k": kid})
                await s.execute(
                    text("DELETE FROM kb_permissions WHERE kb_id = :k"), {"k": kid})
                await s.execute(
                    text("DELETE FROM knowledge_bases WHERE id = :k"), {"k": kid})
            for name in usernames:
                await s.execute(
                    text("DELETE FROM users WHERE username = :n"), {"n": name})
            await s.commit()
    finally:
        await engine.dispose()


async def make_user(c: httpx.AsyncClient, role="editor") -> tuple[str, int]:
    name = f"m9_{SUFFIX}_{role}"
    r = await c.post(f"{API}/auth/register",
                     json={"username": name, "password": "secret123"})
    r.raise_for_status()
    await promote_roles({name: role})
    return name, r.json()["id"]


async def login(c: httpx.AsyncClient, username: str) -> dict:
    r = await c.post(f"{API}/auth/login",
                     json={"username": username, "password": "secret123"})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def create_key(c: httpx.AsyncClient, jwt: dict, name: str) -> dict:
    r = await c.post(f"{API}/auth/keys",
                     json={"name": name, "expires_in_days": 1}, headers=jwt)
    r.raise_for_status()
    return r.json()


def make_pdf_bytes(title: str, paragraphs: list[str]) -> bytes:
    # m8 同款真 PDF；第三段抬单页字符数，避免 auto OCR 误判文本稀薄走 MinerU
    doc = fitz.open()
    page = doc.new_page()
    y = 96
    page.insert_text((72, y), title, fontname="china-s", fontsize=16)
    for p in paragraphs:
        y += 28
        page.insert_text((72, y), p, fontname="china-s", fontsize=12)
    return doc.tobytes()


async def wait_doc_done(c: httpx.AsyncClient, jwt: dict, kb_id: int,
                        doc_id: int | None, timeout_s=120) -> dict:
    if doc_id is None:
        return {"status": "upload_failed"}
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = await c.get(f"{API}/kbs/{kb_id}/documents", headers=jwt)
        r.raise_for_status()
        doc = next((d for d in r.json() if d["id"] == doc_id), None)
        if doc and doc["status"] in ("done", "failed"):
            return doc
        await asyncio.sleep(2)
    return {"status": "timeout", "error_msg": "poll timeout"}


async def main():
    user_ids: list[int] = []
    usernames: list[str] = []
    kb_ids: list[int] = []

    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            health = await c.get(f"{API}/health")
            health.raise_for_status()
        except Exception as e:
            skip("M9 acceptance（全部场景）", f"服务未启动（{e}）；先跑 start_dev.bat")
            summary_and_exit()
            return

        try:
            # ---- 1. 主用户 + 建库 + 上传文档 + 等待解析完成 ----
            owner_name, owner_id = await make_user(c, role="editor")
            user_ids.append(owner_id)
            usernames.append(owner_name)
            owner = await login(c, owner_name)
            me = (await c.get(f"{API}/auth/me", headers=owner)).json()
            check("owner promoted editor", me["role"] == "editor", str(me))

            kb_name = f"M9验收库{SUFFIX}"
            r = await c.post(f"{API}/kbs", json={"name": kb_name}, headers=owner)
            kb_id = r.json().get("id")
            kb_ids.append(kb_id)
            check("kb created 201", r.status_code == 201 and kb_id > 0, r.text[:200])

            up = await c.post(
                f"{API}/kbs/{kb_id}/documents",
                files={"file": ("m9青鸾号.pdf", make_pdf_bytes(
                    "青鸾号高空气船简介",
                    [FACT, "青鸾号以氦气提供升力。",
                     "青鸾号机身采用轻质碳纤维复合材料制造,可在高空持续巡航数十小时。"]),
                    PDF_MIME)},
                data={"ocr": "auto"},
                headers=owner,
            )
            doc = await wait_doc_done(c, owner, kb_id, up.json().get("id"))
            check("pdf upload & processed done",
                  up.status_code == 201 and doc["status"] == "done"
                  and doc["chunk_count"] > 0,
                  f"up={up.status_code} doc={doc}")

            # ---- 2. 创建 key（+ 列表可见；生命周期：创建/列表/吊销）----
            created = await create_key(c, owner, "验收key")
            raw_key, key_id = created["key"], created["id"]
            check("key created & plaintext once",
                  raw_key.startswith("airag_") and key_id > 0
                  and created["expires_at"] is not None,
                  str(created)[:200])
            keys_list = (await c.get(f"{API}/auth/keys", headers=owner)).json()
            check("key listed", any(k["id"] == key_id and k["is_active"]
                                    for k in keys_list), str(keys_list)[:200])

            key_hdr = {"Authorization": f"Bearer {raw_key}"}

            # ---- 3. GET /api/agent/kbs（可见性：含验收库且 owner）----
            r = await c.get(f"{API}/agent/kbs", headers=key_hdr)
            items = r.json().get("items", []) if r.status_code == 200 else []
            mine = next((i for i in items if i["id"] == kb_id), None)
            check("agent kbs 200 & contains kb (owner)",
                  r.status_code == 200 and mine is not None
                  and mine["my_perm"] == "owner", r.text[:200])

            # ---- 4. POST /api/agent/search（真检索：命中事实片段）----
            r = await c.post(f"{API}/agent/search",
                             json={"kb_ids": [kb_id], "query": FACT_Q, "top_k": 8},
                             headers=key_hdr)
            body = r.json() if r.status_code == 200 else {}
            hits = body.get("hits", [])
            check("agent search hits fact & elapsed_ms",
                  r.status_code == 200 and len(hits) >= 1
                  and any("八千五百米" in h["content"] for h in hits)
                  and isinstance(body.get("elapsed_ms"), int),
                  r.text[:300])

            # ---- 5. 401：无凭证 / 伪造 key ----
            r = await c.get(f"{API}/agent/kbs")
            check("401 no credentials", r.status_code == 401
                  and r.json()["detail"] == "not authenticated", r.text[:200])
            r = await c.get(f"{API}/agent/kbs",
                            headers={"Authorization": "Bearer airag_bogus"})
            check("401 invalid_key", r.status_code == 401
                  and r.json()["detail"] == "invalid_key", r.text[:200])

            # ---- 6. 他人 key：kbs 不可见 + search 403 kb_forbidden ----
            other_name, other_id = await make_user(c, role="viewer")
            user_ids.append(other_id)
            usernames.append(other_name)
            other = await login(c, other_name)
            other_key = (await create_key(c, other, "他人key"))["key"]
            other_hdr = {"Authorization": f"Bearer {other_key}"}
            r = await c.get(f"{API}/agent/kbs", headers=other_hdr)
            other_items = r.json().get("items", []) if r.status_code == 200 else []
            check("other key kbs excludes kb",
                  r.status_code == 200
                  and all(i["id"] != kb_id for i in other_items), r.text[:200])
            r = await c.post(f"{API}/agent/search",
                             json={"kb_ids": [kb_id], "query": FACT_Q},
                             headers=other_hdr)
            detail = r.json().get("detail", {}) if r.status_code == 403 else {}
            check("403 kb_forbidden + denied_kb_ids",
                  r.status_code == 403 and detail.get("code") == "kb_forbidden"
                  and kb_id in detail.get("denied_kb_ids", []), r.text[:200])

            # ---- 7. 限流：新 key 连发 61 次 /api/agent/kbs（默认 60/min）----
            rl_key = (await create_key(c, owner, "限流key"))["key"]
            rl_hdr = {"Authorization": f"Bearer {rl_key}"}
            seen_429 = None
            ok_count = 0
            for _ in range(61):
                r = await c.get(f"{API}/agent/kbs", headers=rl_hdr)
                if r.status_code == 429:
                    seen_429 = r.json()["detail"]
                    break
                ok_count += 1
            check("rate limit 429 rate_limited",
                  seen_429 is not None and seen_429.get("code") == "rate_limited"
                  and isinstance(seen_429.get("retry_after"), int)
                  and seen_429["retry_after"] >= 1 and ok_count >= 1,
                  f"ok={ok_count} last={str(seen_429)[:120]}")

            # ---- 8. MCP：initialize 无凭证 401 / 带 key 200 + serverInfo ----
            r = await c.post(f"{BASE}/mcp", json=INIT, headers={"Accept": ACCEPT})
            check("mcp initialize 401 without credentials",
                  r.status_code == 401, f"{r.status_code} {r.text[:150]}")
            r = await c.post(f"{BASE}/mcp", json=INIT,
                             headers={"Accept": ACCEPT, **key_hdr})
            result = r.json().get("result", {}) if r.status_code == 200 else {}
            check("mcp initialize 200 + serverInfo",
                  r.status_code == 200
                  and result.get("serverInfo", {}).get("name") == "AIRag",
                  f"{r.status_code} {r.text[:200]}")

            # ---- 9. 吊销 key → 401 key_revoked ----
            r = await c.delete(f"{API}/auth/keys/{key_id}", headers=owner)
            check("key revoked 204", r.status_code == 204, f"{r.status_code}")
            r = await c.get(f"{API}/agent/kbs", headers=key_hdr)
            check("401 key_revoked after revoke", r.status_code == 401
                  and r.json()["detail"] == "key_revoked", r.text[:200])
        finally:
            # ---- 10. 清理（无论成败都执行；FK 顺序见 cleanup docstring）----
            try:
                await cleanup(user_ids, kb_ids, usernames)
                print(f"cleanup done: users={usernames} kbs={kb_ids}")
            except Exception as e:
                print(f"cleanup FAILED（需手工清理）: {e}")

    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
