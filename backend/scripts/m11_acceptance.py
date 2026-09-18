"""M11 无头验收脚本(真栈:http://127.0.0.1:8001 + worker + Redis)。

用法(backend 目录,项目 venv,start_dev.bat 已起服务):
    .venv\\Scripts\\python scripts\\m11_acceptance.py

覆盖:editor key 上传→轮询 done→search 命中→删除→search 不再命中 /
read_only key 写 403 / 重复上传 409 / 非白名单扩展名 415 /
busy 409(可选) / 配额端点 / 审计落库。
"""
import asyncio
import base64
import datetime as dt
import io
import sys
import time
import uuid

import httpx

BASE = "http://127.0.0.1:8001"
API = f"{BASE}/api"
TIMEOUT = httpx.Timeout(120.0)
RESULTS = {"pass": [], "fail": [], "skip": []}

SUFFIX = uuid.uuid4().hex[:6]
FACT = f"镇海灯塔编号ZT-{SUFFIX}的塔身高四十二米"
FACT_Q = f"镇海灯塔编号ZT-{SUFFIX}的塔身高度是多少米?"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def check(name, cond, detail=""):
    (RESULTS["pass"] if cond else RESULTS["fail"]).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f"  {detail}" if detail and not cond else ""))


def summary_and_exit():
    total = sum(len(v) for v in RESULTS.values())
    print(f"\nM11 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
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


async def cleanup(user_ids, kb_ids, usernames) -> None:
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
                    text("DELETE FROM knowledge_bases WHERE id = :k"),
                    {"k": kid})
            for name in usernames:
                await s.execute(
                    text("DELETE FROM users WHERE username = :n"), {"n": name})
            await s.commit()
    finally:
        await engine.dispose()


async def make_user(c: httpx.AsyncClient, role="editor"):
    name = f"m11_{SUFFIX}_{role}"
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


async def create_key(c, jwt, name, role="read_only") -> str:
    r = await c.post(f"{API}/auth/keys",
                     json={"name": name, "role": role,
                           "expires_in_days": 1},
                     headers=jwt)
    r.raise_for_status()
    return r.json()["key"]


def make_docx_bytes() -> bytes:
    """纯文本 .docx 最小构造:用 python-docx(backend venv 已装,M2 解析依赖)。"""
    from docx import Document as Dx

    dx = Dx()
    dx.add_paragraph(FACT)
    buf = io.BytesIO()
    dx.save(buf)
    return buf.getvalue()


async def main() -> None:
    user_ids, kb_ids, usernames = [], [], []
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            name, uid = await make_user(c)
            usernames.append(name)
            user_ids.append(uid)
            jwt = await login(c, name)
            r = await c.post(f"{API}/kbs", json={"name": f"m11验收库{SUFFIX}"},
                             headers=jwt)
            r.raise_for_status()
            kb_id = r.json()["id"]
            kb_ids.append(kb_id)

            editor_key = await create_key(c, jwt, "m11编辑", "editor")
            ro_key = await create_key(c, jwt, "m11只读")
            ek = {"Authorization": f"Bearer {editor_key}"}
            rk = {"Authorization": f"Bearer {ro_key}"}

            # ① editor key 上传真实 .docx → 201
            content = make_docx_bytes()
            up = await c.post(
                f"{API}/agent/kbs/{kb_id}/documents",
                files={"file": (f"m11-{SUFFIX}.docx", content,
                                DOCX_MIME)},
                data={"ocr": "off"}, headers=ek,
            )
            check("upload 201", up.status_code == 201, str(up.text[:200]))
            doc_id = up.json()["id"]

            # ② 轮询状态至 done(真栈 celery worker)
            status = None
            for _ in range(60):
                g = await c.get(f"{API}/agent/documents/{doc_id}", headers=ek)
                status = g.json()["status"]
                if status in ("done", "failed"):
                    break
                await asyncio.sleep(2)
            check("poll done", status == "done", f"status={status}")

            # ③ search 命中新内容
            s1 = await c.post(f"{API}/agent/search",
                              json={"kb_ids": [kb_id], "query": FACT_Q},
                              headers=ek)
            hit_ids = {h["document_id"] for h in s1.json()["hits"]}
            check("search hits new doc", doc_id in hit_ids,
                  f"hits={s1.json()['total']}")

            # ④ read_only key 写操作 403
            ro_up = await c.post(
                f"{API}/agent/kbs/{kb_id}/documents",
                files={"file": ("ro.docx", content, DOCX_MIME)},
                data={"ocr": "off"}, headers=rk,
            )
            check("read_only 403", ro_up.status_code == 403
                  and ro_up.json()["detail"]["code"] == "editor_key_required",
                  str(ro_up.text[:200]))

            # ⑤ 同内容重传 409(SHA256 去重)
            dup = await c.post(
                f"{API}/agent/kbs/{kb_id}/documents",
                files={"file": (f"dup-{SUFFIX}.docx", content, DOCX_MIME)},
                data={"ocr": "off"}, headers=ek,
            )
            check("duplicate 409", dup.status_code == 409)

            # ⑥ 非白名单扩展名 415
            bad = await c.post(
                f"{API}/agent/kbs/{kb_id}/documents",
                files={"file": ("bad.exe", b"x", "application/octet-stream")},
                headers=ek,
            )
            check("bad ext 415", bad.status_code == 415)

            # ⑦ 配额端点
            q = await c.get(f"{API}/agent/quota", headers=ek)
            check("quota endpoint", q.status_code == 200
                  and "limit" in q.json(), str(q.text[:200]))

            # ⑧ 删除 → search 不再命中
            d = await c.delete(f"{API}/agent/documents/{doc_id}", headers=ek)
            check("delete 204", d.status_code == 204, str(d.text[:200]))
            s2 = await c.post(f"{API}/agent/search",
                              json={"kb_ids": [kb_id], "query": FACT_Q},
                              headers=ek)
            hit_ids2 = {h["document_id"] for h in s2.json()["hits"]}
            check("search after delete", doc_id not in hit_ids2)

            # ⑨ 审计落库
            from sqlalchemy import select

            from app.models import AuditLog

            engine, maker = _nullpool_sessionmaker()
            try:
                async with maker() as s:
                    rows = (await s.execute(
                        select(AuditLog.action).where(
                            AuditLog.action.in_([
                                "agent.upload_document",
                                "agent.delete_document",
                            ]))
                    )).scalars().all()
            finally:
                await engine.dispose()
            check("audit rows", set(rows) >= {"agent.upload_document",
                                              "agent.delete_document"},
                  f"actions={rows}")
        finally:
            await cleanup(user_ids, kb_ids, usernames)
    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
