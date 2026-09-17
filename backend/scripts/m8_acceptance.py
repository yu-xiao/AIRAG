"""M8 无头验收脚本（真栈：http://127.0.0.1:8001 + worker + 智谱 key）。

用法（backend 目录，项目 venv）：
    .venv\\Scripts\\python scripts\\m8_acceptance.py

覆盖 M8：拒答子串判定（零命中 refused + 话术包含断言）/ 正常题不误伤回归 /
KB 重名 409 / 孤儿 eval_sets dry-run。ZHIPU_API_KEY 未配置时打印 SKIP 整体退出。
收尾直接清 DB（chunks/documents/kb_permissions/knowledge_bases，FK 顺序）。
"""
import asyncio
import json
import subprocess
import sys
import time

import httpx
import pymupdf as fitz

BASE = "http://127.0.0.1:8001/api"
TIMEOUT = httpx.Timeout(300.0)
RESULTS = {"pass": [], "fail": [], "skip": []}

KB_NAME = "M8验收库"
KB_NAME_B = "M8验收库B"
FACT1 = "青鸾号高空气船的巡航升限为八千五百米"
NORMAL_Q1 = "青鸾号高空气船的巡航升限是多少米？"
ZERO_HIT_Q1 = "珠穆朗玛峰的海拔是多少米？"
ZERO_HIT_Q2 = "世界杯足球赛每几年举办一次？"
REFUSAL_PHRASE = "知识库中未找到相关内容"
PDF_MIME = "application/pdf"


def check(name, cond, detail=""):
    if cond:
        RESULTS["pass"].append(name)
        print(f"PASS {name}")
    else:
        RESULTS["fail"].append(name)
        print(f"FAIL {name}  {detail}")


def skip(name, why):
    RESULTS["skip"].append(name)
    print(f"SKIP {name}  ({why})")


def summary_and_exit():
    print()
    total = sum(len(v) for v in RESULTS.values())
    print(f"M8 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
    if RESULTS["skip"]:
        print("SKIP:", *RESULTS["skip"], sep="\n  - ")
    if RESULTS["fail"]:
        print("FAILED:", *RESULTS["fail"], sep="\n  - ")
        sys.exit(1)


def promote_roles(usernames: dict[str, str]) -> None:
    # 每次调用用一次性 NullPool 引擎:SessionLocal 的连接池跨事件循环复用
    # asyncpg 连接会崩("Event loop is closed"),与 workers/pipeline._engine 同法。
    async def _run():
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.core.config import settings

        engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                for username, role in usernames.items():
                    await s.execute(
                        text("UPDATE users SET role = :r WHERE username = :u"),
                        {"r": role, "u": username},
                    )
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(_run())


def cleanup_kbs(kb_ids: list[int]) -> None:
    async def _run():
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.core.config import settings

        engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                for kid in kb_ids:
                    await s.execute(text("DELETE FROM chunks WHERE kb_id = :k"), {"k": kid})
                    await s.execute(text("DELETE FROM documents WHERE kb_id = :k"), {"k": kid})
                    await s.execute(text("DELETE FROM kb_permissions WHERE kb_id = :k"), {"k": kid})
                    await s.execute(text("DELETE FROM knowledge_bases WHERE id = :k"), {"k": kid})
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(_run())


def register_and_login(client: httpx.Client, username: str) -> dict:
    client.post(f"{BASE}/auth/register",
                json={"username": username, "password": "secret123"})
    r = client.post(f"{BASE}/auth/login",
                    json={"username": username, "password": "secret123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def make_pdf_bytes(title: str, paragraphs: list[str]) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    y = 96
    page.insert_text((72, y), title, fontname="china-s", fontsize=16)
    for p in paragraphs:
        y += 28
        page.insert_text((72, y), p, fontname="china-s", fontsize=12)
    return doc.tobytes()


def upload_doc(client: httpx.Client, headers: dict, kb_id: int,
               filename: str, content: bytes) -> dict:
    r = client.post(
        f"{BASE}/kbs/{kb_id}/documents",
        files={"file": (filename, content, PDF_MIME)},
        data={"ocr": "auto"},
        headers=headers,
    )
    return {"status": r.status_code, "body": r.json(), "text": r.text[:200]}


def wait_doc_status(client: httpx.Client, headers: dict, doc_id: int,
                    terminal=("done", "failed"), timeout_s=120) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = client.get(f"{BASE}/documents/{doc_id}", headers=headers)
        doc = r.json()
        if doc["status"] in terminal:
            return doc
        time.sleep(2)
    return {"status": "timeout", "error_msg": "poll timeout"}


def sse_ask(client: httpx.Client, headers: dict, kb_ids: list[int],
            question: str, rerank: bool = True) -> dict:
    out = {"tokens": "", "citations": None, "done": None, "error": None}
    payload = {"kb_ids": kb_ids, "question": question, "rerank": rerank}
    with client.stream("POST", f"{BASE}/chat/ask", json=payload,
                       headers=headers, timeout=TIMEOUT) as resp:
        out["status"] = resp.status_code
        buf = b""
        for chunk in resp.iter_bytes():
            buf += chunk
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                line = frame.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                evt = json.loads(line[5:])
                if evt["type"] == "token":
                    out["tokens"] += str(evt["data"])
                elif evt["type"] == "citations":
                    out["citations"] = evt["data"]
                elif evt["type"] == "done":
                    out["done"] = evt["data"]
                elif evt["type"] == "error":
                    out["error"] = evt["data"]
                    return out
    return out


def final_answer(res: dict) -> str:
    return ((res.get("done") or {}).get("answer") or res["tokens"]) or ""


def ask_with_retry(client, headers, kb_id, question, rerank, judge, attempts=2):
    res, ok = None, False
    for _ in range(attempts):
        res = sse_ask(client, headers, [kb_id], question, rerank=rerank)
        ok = judge(res)
        if ok:
            break
    return res, ok


def main():
    from app.core.config import settings

    if not settings.ZHIPU_API_KEY:
        skip("M8 acceptance（全部场景）", "ZHIPU_API_KEY 未配置")
        summary_and_exit()
        return

    client = httpx.Client(timeout=60.0)
    kb_ids = []

    # ---- 0. 基线与账号 ----
    r = client.get(f"{BASE}/health")
    check("health 200", r.status_code == 200, r.text[:100])
    if r.status_code != 200:
        summary_and_exit()
        return

    client.post(f"{BASE}/auth/register",
                json={"username": "m8_owner", "password": "secret123"})
    promote_roles({"m8_owner": "editor"})
    owner = register_and_login(client, "m8_owner")
    me = client.get(f"{BASE}/auth/me", headers=owner).json()
    check("m8_owner promoted editor", me["role"] == "editor", str(me))

    # ---- S1: KB 重名 409 / 异名 201 ----
    r = client.post(f"{BASE}/kbs", json={"name": KB_NAME}, headers=owner)
    kb_id = r.json().get("id")
    kb_ids.append(kb_id)
    check("kb created 201", r.status_code == 201 and kb_id > 0, r.text[:200])

    dup = client.post(f"{BASE}/kbs", json={"name": KB_NAME}, headers=owner)
    check("S1 duplicate name 409",
          dup.status_code == 409
          and dup.json().get("detail") == "knowledge base name already exists",
          dup.text[:200])

    strip_dup = client.post(f"{BASE}/kbs", json={"name": f"  {KB_NAME}  "}, headers=owner)
    check("S1 duplicate after strip 409", strip_dup.status_code == 409,
          strip_dup.text[:200])

    rb = client.post(f"{BASE}/kbs", json={"name": KB_NAME_B}, headers=owner)
    kb_b = rb.json().get("id")
    kb_ids.append(kb_b)
    check("S1 distinct name 201", rb.status_code == 201 and kb_b > 0, rb.text[:200])

    # ---- S2: 文档就绪（真 pdf，worker 解析）----
    # 第三段仅用于把单页字符数抬到 OCR_THIN_CHARS_PER_PAGE(50) 之上,避免
    # auto 模式误判"文本稀薄"而走 MinerU 云 OCR(验收不覆盖该外部依赖)。
    up = upload_doc(client, owner, kb_id, "m8青鸾号.pdf",
                    make_pdf_bytes("青鸾号高空气船简介",
                                   [FACT1, "青鸾号以氦气提供升力。",
                                    "青鸾号机身采用轻质碳纤维复合材料制造,可在高空持续巡航数十小时。"]))
    doc = wait_doc_status(client, owner, up["body"].get("id"))
    check("pdf upload & processed done",
          up["status"] == 201 and doc["status"] == "done" and doc["chunk_count"] > 0,
          f"{up} {doc}")

    # ---- S3: 零命中 → refused + 话术子串命中（M8 核心断言）----
    s3, s3_ok = ask_with_retry(
        client, owner, kb_id, ZERO_HIT_Q1, rerank=True,
        judge=lambda r: (r["done"] is not None and r["error"] is None
                         and r["done"].get("refused") is True
                         and REFUSAL_PHRASE in final_answer(r).strip()))
    check("S3 zero-hit(rerank on) refused + phrase contained", s3_ok, str(s3)[:300])

    s3b, s3b_ok = ask_with_retry(
        client, owner, kb_id, ZERO_HIT_Q2, rerank=False,
        judge=lambda r: (r["done"] is not None and r["error"] is None
                         and REFUSAL_PHRASE in final_answer(r).strip()))
    check("S3b zero-hit(rerank off) prompt fallback phrase contained", s3b_ok,
          final_answer(s3b)[:200])

    # ---- S4: 正常题不误伤（提示词收紧回归）----
    s4, s4_ok = ask_with_retry(
        client, owner, kb_id, NORMAL_Q1, rerank=True,
        judge=lambda r: (r["done"] is not None and r["error"] is None
                         and r["done"].get("refused") is False
                         and (r["citations"] or []) and len(r["citations"]) >= 1
                         and REFUSAL_PHRASE not in final_answer(r).strip()))
    check("S4 normal not refused, citations >= 1", s4_ok, str(s4)[:300])

    # ---- S5: 孤儿清理 dry-run（不真删）----
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.purge_orphan_evalsets"],
        capture_output=True, text=True, timeout=60,
    )
    ok5 = ("DRY-RUN" in proc.stdout
           and f"{kb_id}.json" not in proc.stdout
           and f"{kb_b}.json" not in proc.stdout)
    check("S5 purge dry-run runs, created kbs not orphaned", ok5,
          proc.stdout[-300:] + proc.stderr[-200:])

    summary_and_exit()
    cleanup_kbs(kb_ids)
    print(f"cleanup done: {kb_ids}")


if __name__ == "__main__":
    main()
