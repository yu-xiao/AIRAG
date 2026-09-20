"""M12 无头验收脚本(真栈:http://127.0.0.1:8001 + worker + Redis)。

用法(backend 目录,项目 venv,start_dev.bat 已起服务):
    .venv\\Scripts\\python scripts\\m12_acceptance.py

覆盖:scoped key 越界 403/404 与界内全通 / admin 代发 scoped(422 按目标用户)/
owner 删 KB 级联(检索零命中、库消失、审计 kb_delete 按本次 target 绑定)/
KB 重名 DB 约束冒烟 / 铸造 422(空列表/幽灵库)。
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
FACT = f"朱雀灯塔编号ZQ-{SUFFIX}的塔灯每夜亮十一个小时"
FACT_Q = f"朱雀灯塔编号ZQ-{SUFFIX}的塔灯每夜亮几个小时?"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def check(name, cond, detail=""):
    (RESULTS["pass"] if cond else RESULTS["fail"]).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f"  {detail}" if detail and not cond else ""))


def summary_and_exit():
    total = sum(len(v) for v in RESULTS.values())
    print(f"\nM12 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
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


async def make_user(c: httpx.AsyncClient, name: str, role: str) -> tuple[str, int]:
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


async def mint_key(c: httpx.AsyncClient, jwt: dict, name: str,
                   role: str = "read_only", kb_scope: list[int] | None = None,
                   ) -> httpx.Response:
    payload = {"name": name, "role": role}
    if kb_scope is not None:
        payload["kb_scope"] = kb_scope
    return await c.post(f"{API}/auth/keys", json=payload, headers=jwt)


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
            u1_name, u1_id = await make_user(c, f"m12_{SUFFIX}_u1", "editor")
            adm_name, adm_id = await make_user(c, f"m12_{SUFFIX}_admin",
                                               "admin")
            usernames += [u1_name, adm_name]
            user_ids += [u1_id, adm_id]
            jwt1 = await login(c, u1_name)
            jwt_adm = await login(c, adm_name)

            r = await c.post(f"{API}/kbs", json={"name": f"m12界内库{SUFFIX}"},
                             headers=jwt1)
            r.raise_for_status()
            kb_in, kb_in_name = r.json()["id"], r.json()["name"]
            kb_ids.append(kb_in)
            r = await c.post(f"{API}/kbs", json={"name": f"m12界外库{SUFFIX}"},
                             headers=jwt1)
            r.raise_for_status()
            kb_out, kb_out_name = r.json()["id"], r.json()["name"]
            kb_ids.append(kb_out)

            # ① 铸造 422:空列表 / 幽灵库
            m1 = await mint_key(c, jwt1, "空范围", kb_scope=[])
            check("mint empty scope 422", m1.status_code == 422,
                  f"status={m1.status_code} body={m1.text[:200]}")
            m2 = await mint_key(c, jwt1, "幽灵", kb_scope=[999999])
            check("mint ghost kb 422", m2.status_code == 422,
                  f"status={m2.status_code} body={m2.text[:200]}")

            # ② scoped read_only key(scope=[kb_in])的越界四面
            ro = await mint_key(c, jwt1, "m12只读scoped", kb_scope=[kb_in])
            ro.raise_for_status()
            rk = {"Authorization": f"Bearer {ro.json()['key']}"}

            s = await c.post(f"{API}/agent/search",
                             json={"kb_ids": [kb_out], "query": "x"},
                             headers=rk)
            d = (s.json().get("detail") or {})
            check("scoped search out 403", s.status_code == 403
                  and d.get("code") == "kb_forbidden"
                  and d.get("denied_kb_ids") == [kb_out],
                  f"status={s.status_code} body={s.text[:200]}")
            a = await c.post(f"{API}/agent/ask",
                             json={"kb_ids": [kb_out], "query": "x"},
                             headers=rk)
            da = (a.json().get("detail") or {})
            check("scoped ask out 403", a.status_code == 403
                  and da.get("code") == "kb_forbidden",
                  f"status={a.status_code} body={a.text[:200]}")
            g = await c.get(f"{API}/agent/kbs", headers=rk)
            items = [i["id"] for i in g.json().get("items", [])]
            check("scoped agent kbs filtered", g.status_code == 200
                  and items == [kb_in], f"items={items}")
            gd = await c.get(f"{API}/agent/kbs/{kb_out}/documents", headers=rk)
            check("scoped docs out 404", gd.status_code == 404,
                  f"status={gd.status_code} body={gd.text[:200]}")

            # ③ scoped editor key(scope=[kb_in])界内全通:上传→done→命中
            ek_m = await mint_key(c, jwt1, "m12编辑scoped", role="editor",
                                  kb_scope=[kb_in])
            ek_m.raise_for_status()
            ek = {"Authorization": f"Bearer {ek_m.json()['key']}"}

            up = await c.post(
                f"{API}/agent/kbs/{kb_in}/documents",
                files={"file": (f"m12-{SUFFIX}.docx", make_docx_bytes(),
                                DOCX_MIME)},
                data={"ocr": "off"}, headers=ek,
            )
            check("scoped upload in 201", up.status_code == 201,
                  f"status={up.status_code} body={up.text[:200]}")
            doc_id = up.json()["id"]
            status = None
            for _ in range(60):
                g2 = await c.get(f"{API}/agent/documents/{doc_id}", headers=ek)
                status = g2.json()["status"]
                if status in ("done", "failed"):
                    break
                await asyncio.sleep(3)
            check("scoped poll done", status == "done", f"status={status}")
            s1 = await c.post(f"{API}/agent/search",
                              json={"kb_ids": [kb_in], "query": FACT_Q},
                              headers=ek)
            hit_ids = {h["document_id"] for h in s1.json()["hits"]}
            check("scoped search hits new doc", doc_id in hit_ids,
                  f"total={s1.json()['total']}")

            # ④ admin 代发:合法范围 201 回显;按目标用户判界的越界 422
            ak = await c.post(
                f"{API}/admin/keys",
                json={"user_id": u1_id, "name": "admin代发scoped",
                      "kb_scope": [kb_in]},
                headers=jwt_adm,
            )
            check("admin mint scoped 201", ak.status_code == 201
                  and ak.json().get("kb_scope") == [kb_in],
                  f"status={ak.status_code} body={ak.text[:200]}")
            r = await c.post(f"{API}/kbs",
                             json={"name": f"admin自己的库{SUFFIX}"},
                             headers=jwt_adm)
            r.raise_for_status()
            kb_adm = r.json()["id"]
            kb_ids.append(kb_adm)
            bad = await c.post(
                f"{API}/admin/keys",
                json={"user_id": u1_id, "name": "越界代发",
                      "kb_scope": [kb_adm]},
                headers=jwt_adm,
            )
            check("admin mint out-of-target 422", bad.status_code == 422,
                  f"status={bad.status_code} body={bad.text[:200]}")

            # ⑤ owner 删 KB:204 → 库消失 404 → 检索零命中(403 界外语义)
            d5 = await c.delete(f"{API}/kbs/{kb_in}", headers=jwt1)
            check("owner delete kb 204", d5.status_code == 204,
                  f"status={d5.status_code} body={d5.text[:200]}")
            g404 = await c.get(f"{API}/kbs/{kb_in}", headers=jwt1)
            check("deleted kb gone 404", g404.status_code == 404,
                  f"status={g404.status_code} body={g404.text[:200]}")
            s2 = await c.post(f"{API}/agent/search",
                              json={"kb_ids": [kb_in], "query": FACT_Q},
                              headers=rk)
            d2 = (s2.json().get("detail") or {})
            check("search deleted kb denied", s2.status_code == 403
                  and d2.get("code") == "kb_forbidden",
                  f"status={s2.status_code} body={s2.text[:200]}")

            # ⑥ KB 重名 DB 约束冒烟:直插 kb_out 同名 → IntegrityError
            from sqlalchemy import text
            from sqlalchemy.exc import IntegrityError

            engine, maker = _nullpool_sessionmaker()
            dup_blocked = False
            try:
                async with maker() as s6:
                    await s6.execute(
                        text("INSERT INTO knowledge_bases "
                             "(name, owner_id, created_at) "
                             "VALUES (:n, :o, now())"),
                        {"n": kb_out_name, "o": u1_id},
                    )
                    # 走到这 = 约束缺失(冒烟失败);rollback 收尸不落库
                    await s6.rollback()
            except IntegrityError:
                dup_blocked = True
            finally:
                await engine.dispose()
            check("kb name unique db constraint", dup_blocked,
                  "direct duplicate insert committed: unique constraint missing")

            # ⑦ 审计绑定:kb_delete→kb:{kb_in} / agent.upload→doc:{doc_id}
            from sqlalchemy import select

            from app.models import AuditLog

            engine, maker = _nullpool_sessionmaker()
            try:
                async with maker() as s7:
                    # M13 快修⑥:按 (action, target) 对断言(独立超集会误配)
                    pairs = set(
                        (await s7.execute(
                            select(AuditLog.action, AuditLog.target).where(
                                AuditLog.action.in_([
                                    "kb_delete", "agent.upload_document"]),
                                AuditLog.target.in_([f"kb:{kb_in}",
                                                     f"doc:{doc_id}"]),
                            )
                        )).all()
                    )
            finally:
                await engine.dispose()
            check("audit pairs bound to run",
                  {("kb_delete", f"kb:{kb_in}"),
                   ("agent.upload_document", f"doc:{doc_id}")} <= pairs,
                  f"pairs={sorted(pairs)}")
        finally:
            await cleanup(user_ids, kb_ids, usernames)
    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
