"""M10 无头验收脚本(真栈:http://127.0.0.1:8001 + worker + Redis)。

用法(backend 目录,项目 venv,start_dev.bat 已起服务):
    .venv\\Scripts\\python scripts\\m10_acceptance.py

覆盖:ask 正常(答案+引用)/ ask 拒答(refused)/ 无权库 403 /
配额烧穿 429+Retry-After 头 / 审计 agent.ask 落库。
真实 LLM(智谱),答案断言宽松(内容或拒答语义)。
"""
import asyncio
import datetime as dt
import sys
import time
import uuid

import httpx
import pymupdf as fitz

BASE = "http://127.0.0.1:8001"
API = f"{BASE}/api"
TIMEOUT = httpx.Timeout(180.0)  # ask 全链路 40~90s/次
RESULTS = {"pass": [], "fail": [], "skip": []}

FACT = "青鸾号高空气船的巡航升限为八千五百米"
FACT_Q = "青鸾号高空气船的巡航升限是多少米?"
OFF_Q = "《红楼梦》的作者是谁?"
SUFFIX = uuid.uuid4().hex[:6]
PDF_MIME = "application/pdf"


def check(name, cond, detail=""):
    (RESULTS["pass"] if cond else RESULTS["fail"]).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f"  {detail}" if detail and not cond else ""))


def skip(name, why):
    RESULTS["skip"].append(name)
    print(f"SKIP {name}  ({why})")


def summary_and_exit():
    total = sum(len(v) for v in RESULTS.values())
    print(f"\nM10 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
    if RESULTS["fail"]:
        print("FAILED:", *RESULTS["fail"], sep="\n  - ")
        sys.exit(1)


def _nullpool_sessionmaker():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import settings

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def promote_roles(usernames: dict[str, str]) -> None:
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
                    text("DELETE FROM kb_permissions WHERE kb_id = :k"),
                    {"k": kid})
                await s.execute(
                    text("DELETE FROM knowledge_bases WHERE id = :k"), {"k": kid})
            for name in usernames:
                await s.execute(
                    text("DELETE FROM users WHERE username = :n"), {"n": name})
            await s.commit()
    finally:
        await engine.dispose()


async def make_user(c: httpx.AsyncClient, role="editor") -> tuple[str, int]:
    name = f"m10_{SUFFIX}_{role}"
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


def quota_redis_key(key_id: int) -> str:
    return f"agent_tq:{key_id}:{dt.datetime.now():%Y%m%d}"


async def main():
    user_ids: list[int] = []
    usernames: list[str] = []
    kb_ids: list[int] = []
    touched_redis: list[str] = []

    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            health = await c.get(f"{API}/health")
            health.raise_for_status()
        except Exception as e:
            skip("M10 acceptance(全部场景)", f"服务未启动({e});先跑 start_dev.bat")
            summary_and_exit()
            return

        try:
            # ---- 1. 主用户 + 建库 + 文档 ----
            owner_name, owner_id = await make_user(c, role="editor")
            user_ids.append(owner_id)
            usernames.append(owner_name)
            owner = await login(c, owner_name)

            r = await c.post(f"{API}/kbs",
                             json={"name": f"M10验收库{SUFFIX}"}, headers=owner)
            kb_id = r.json().get("id")
            kb_ids.append(kb_id)
            check("kb created 201", r.status_code == 201 and kb_id > 0,
                  r.text[:200])

            up = await c.post(
                f"{API}/kbs/{kb_id}/documents",
                files={"file": ("m10青鸾号.pdf", make_pdf_bytes(
                    "青鸾号高空气船简介",
                    [FACT, "青鸾号以氦气提供升力。",
                     "青鸾号机身采用轻质碳纤维复合材料制造,可在高空持续巡航数十小时。"]),
                    PDF_MIME)},
                data={"ocr": "auto"}, headers=owner,
            )
            doc = await wait_doc_done(c, owner, kb_id, up.json().get("id"))
            check("pdf upload & processed done",
                  up.status_code == 201 and doc["status"] == "done"
                  and doc["chunk_count"] > 0,
                  f"up={up.status_code} doc={doc}")

            created = await create_key(c, owner, "验收key")
            raw_key, key_id = created["key"], created["id"]
            key_hdr = {"Authorization": f"Bearer {raw_key}"}

            # ---- 2. ask 正常:答案 + 引用 + elapsed ----
            r = await c.post(f"{API}/agent/ask",
                             json={"kb_ids": [kb_id], "query": FACT_Q},
                             headers=key_hdr)
            body = r.json() if r.status_code == 200 else {}
            check("ask 200 answer cites fact",
                  r.status_code == 200
                  and ("八千五百" in body.get("answer", "")
                       or "8500" in body.get("answer", ""))
                  and body.get("refused") is False
                  and len(body.get("citations", [])) >= 1
                  and body.get("tokens_used", 0) > 0
                  and isinstance(body.get("elapsed_ms"), int),
                  r.text[:300])

            # ---- 3. ask 拒答:库内只有飞艇资料 ----
            r = await c.post(f"{API}/agent/ask",
                             json={"kb_ids": [kb_id], "query": OFF_Q},
                             headers=key_hdr)
            body = r.json() if r.status_code == 200 else {}
            check("ask refused is 200",
                  r.status_code == 200 and body.get("refused") is True,
                  r.text[:300])

            # ---- 4. 无权库 403(他人 key) ----
            other_name, other_id = await make_user(c, role="viewer")
            user_ids.append(other_id)
            usernames.append(other_name)
            other = await login(c, other_name)
            other_key = (await create_key(c, other, "他人key"))["key"]
            r = await c.post(f"{API}/agent/ask",
                             json={"kb_ids": [kb_id], "query": FACT_Q},
                             headers={"Authorization": f"Bearer {other_key}"})
            detail = r.json().get("detail", {}) if r.status_code == 403 else {}
            check("403 kb_forbidden + denied_kb_ids",
                  r.status_code == 403 and detail.get("code") == "kb_forbidden"
                  and kb_id in detail.get("denied_kb_ids", []), r.text[:200])

            # ---- 5. 配额烧穿:直写 redis 到量 → 429 + Retry-After 头 ----
            from redis import asyncio as aioredis

            from app.core.config import settings as app_settings

            full = await create_key(c, owner, "配额key")
            rkey = quota_redis_key(full["id"])
            redis = aioredis.from_url(app_settings.REDIS_URL,
                                      decode_responses=True)
            try:
                await redis.set(rkey, 999_999_999)
                touched_redis.append(rkey)
                r = await c.post(
                    f"{API}/agent/ask",
                    json={"kb_ids": [kb_id], "query": FACT_Q},
                    headers={"Authorization": f"Bearer {full['key']}"})
                detail = r.json().get("detail", {}) if r.status_code == 429 else {}
                check("quota 429 + Retry-After header",
                      r.status_code == 429
                      and detail.get("code") == "quota_exhausted"
                      and int(r.headers.get("Retry-After", 0)) >= 1,
                      f"{r.status_code} {r.text[:200]}")
            finally:
                for rk in touched_redis:
                    await redis.delete(rk)
                await redis.aclose()

            # ---- 6. 审计 agent.ask 落库 ----
            from sqlalchemy import text

            engine, maker = _nullpool_sessionmaker()
            try:
                async with maker() as s:
                    n = (await s.execute(text(
                        "SELECT count(*) FROM audit_logs "
                        "WHERE action = 'agent.ask'"))).scalar()
            finally:
                await engine.dispose()
            check("agent.ask audit rows", (n or 0) >= 2, f"rows={n}")
        finally:
            try:
                await cleanup(user_ids, kb_ids, usernames)
                print(f"cleanup done: users={usernames} kbs={kb_ids}")
            except Exception as e:
                print(f"cleanup FAILED(需手工清理): {e}")

    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
