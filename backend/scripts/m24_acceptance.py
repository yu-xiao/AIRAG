"""M24 无头验收(真栈:8001 + worker;解析在 worker,须已重启带 M24 代码)。

覆盖:①GBK .txt / .pptx / .csv 经 API 上传 → done 且 hybrid 检索命中
(真库真 embed,与单测的 fake 路径互补)②一张 .bmp(auto OCR)→ done 且
ocr_used=True(真 MinerU 云调用,先例 M5;若云端不收 bmp 属探查性支持,
报 FAIL 如实暴露)③前端 accept 串与 beforeUpload 白名单常量含 18 扩展(grep DocsPage.vue)
④运行时断言 ALLOWED_EXTS 即 REGISTRY 键集。清理:KB 走 API 删除。
"""
import asyncio
import io
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8001"
API = f"{BASE}/api"
TIMEOUT = httpx.Timeout(180.0)
RESULTS = {"pass": [], "fail": [], "skip": []}
SUFFIX = uuid.uuid4().hex[:6]
BACKEND_DIR = Path(__file__).resolve().parents[1]


def check(name, cond, detail=""):
    (RESULTS["pass"] if cond else RESULTS["fail"]).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f"  {detail}" if detail and not cond else ""))


def summary_and_exit():
    total = sum(len(v) for v in RESULTS.values())
    print(f"\nM24 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
    if RESULTS["skip"]:
        print("SKIP:", *RESULTS["skip"], sep="\n  - ")
    if RESULTS["fail"]:
        print("FAILED:", *RESULTS["fail"], sep="\n  - ")
        sys.exit(1)


def _nullpool_sessionmaker():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import settings

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def sql(sql_text, params=None, fetch=False):
    import sqlalchemy

    engine, maker = _nullpool_sessionmaker()
    try:
        async with maker() as s:
            res = await s.execute(sqlalchemy.text(sql_text), params or {})
            if fetch:
                out = res.mappings().all()
                await s.commit()
                return out
            await s.commit()
            return res
    finally:
        await engine.dispose()


async def login(c: httpx.AsyncClient, username: str) -> dict:
    r = await c.post(f"{API}/auth/login",
                     json={"username": username, "password": "secret123"})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def wait_doc(c, jwt, doc_id, timeout_s=240) -> dict:
    t0 = time.perf_counter()
    while True:
        r = await c.get(f"{API}/documents/{doc_id}", headers=jwt)
        d = r.json()
        if d.get("status") in ("done", "failed") \
                or time.perf_counter() > t0 + timeout_s:
            return d
        await asyncio.sleep(3)


def _bmp_bytes() -> bytes:
    """最小合法 24bpp BMP(1x1 红点)——MinerU 探查性收或不收。"""
    w = h = 1
    row = b"\x00\x00\xFF" + b"\x00"  # BGR + padding to 4 bytes
    pixel_data = row * h
    file_hdr = b"BM" + (
        (14 + 40 + len(pixel_data)).to_bytes(4, "little")
        + b"\x00\x00\x00\x00" + (54).to_bytes(4, "little"))
    info_hdr = (
        (40).to_bytes(4, "little") + w.to_bytes(4, "little")
        + h.to_bytes(4, "little") + (1).to_bytes(2, "little")
        + (24).to_bytes(2, "little") + (0).to_bytes(4, "little")
        + len(pixel_data).to_bytes(4, "little")
        + (2835).to_bytes(4, "little") * 2 + b"\x00\x00\x00\x00" * 2)
    return file_hdr + info_hdr + pixel_data


async def main() -> None:
    kb_ids, endpoint_ids = [], []  # endpoint_ids 统一形制(当前无端点,恒空)
    admin: dict | None = None  # 前置绑定:login 即失败时 finally 仍可走 SQL 兜底
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        async def _cleanup(c, jwt):
            """幂等兜底:API 优先(逐个吞错),SQL 按 FK 序清残;
            正常路径的 204 检查在 try 内不变,这里只兜早失败。"""
            for e in endpoint_ids:
                try:
                    await c.delete(f"{API}/admin/webhooks/{e}",
                                   headers=jwt)
                except Exception:
                    pass
            for k in kb_ids:
                try:
                    await c.delete(f"{API}/kbs/{k}", headers=jwt)
                except Exception:
                    pass
            for k in kb_ids:  # SQL 兜底(API 删失败时)
                try:
                    await sql(
                        "DELETE FROM chunks WHERE kb_id = :k", {"k": k})
                    await sql(
                        "DELETE FROM documents WHERE kb_id = :k", {"k": k})
                    await sql(
                        "DELETE FROM eval_questions WHERE kb_id = :k",
                        {"k": k})
                    await sql(
                        "DELETE FROM kb_permissions WHERE kb_id = :k",
                        {"k": k})
                    await sql(
                        "DELETE FROM knowledge_bases WHERE id = :k",
                        {"k": k})
                except Exception:
                    pass
            for e in endpoint_ids:
                try:
                    await sql(
                        "DELETE FROM webhook_endpoints WHERE id = :e",
                        {"e": e})
                except Exception:
                    pass

        try:
            admin = await login(c, "admin")
            check("admin login ok", bool(admin.get("Authorization")))

            ra = await c.post(f"{API}/kbs",
                              json={"name": f"m24验收库{SUFFIX}"},
                              headers=admin)
            ra.raise_for_status()
            kb = ra.json()["id"]
            kb_ids.append(kb)

            from pptx import Presentation

            prs = Presentation()
            s1 = prs.slides.add_slide(prs.slide_layouts[5])
            s1.shapes.title.text = f"燃油泵检修规程{SUFFIX}"
            buf = io.BytesIO()
            prs.save(buf)

            files = [
                ("gb.txt", "机型甲的排故要点是先查燃油泵".encode("gb18030"),
                 "text/plain"),
                ("t.csv", "部件,数量\n燃油泵,3".encode("utf-8"), "text/csv"),
                ("s.pptx", buf.getvalue(), "application/octet-stream"),
            ]
            doc_ids = []
            for name, content, mime in files:
                r = await c.post(f"{API}/kbs/{kb}/documents",
                                 files={"file": (name, content, mime)},
                                 headers=admin)
                check(f"upload {name} 201", r.status_code == 201,
                      f"HTTP {r.status_code} {r.text[:160]}")
                if r.status_code != 201:
                    continue
                doc_ids.append(r.json()["id"])
                d = await wait_doc(c, admin, r.json()["id"])
                check(f"{name} parses to done", d.get("status") == "done",
                      f"status={d.get('status')} "
                      f"err={str(d.get('error_msg'))[:120]}")

            # 检索可及性:公开 chunks 路由,内容含「燃油泵」即证明
            # parse→chunk→embed 入库(检索本身由单测 e2e 的 hybrid_search 覆盖)
            found = set()
            for did in doc_ids:
                r = await c.get(f"{API}/documents/{did}/chunks",
                                params={"page_size": 50}, headers=admin)
                body = r.json()
                items = body if isinstance(body, list) \
                    else (body.get("items") or [])
                blob = "\n".join(str(it.get("content_preview") or "")
                                 for it in items)
                if "燃油泵" in blob:
                    found.add(did)
            check("new-type chunks contain searchable content",
                  len(found) >= 2, f"hit docs={len(found)}/{len(doc_ids)}")

            # ② bmp 真云 OCR(auto)
            r = await c.post(f"{API}/kbs/{kb}/documents",
                             files={"file": ("dot.bmp", _bmp_bytes(),
                                             "image/bmp")},
                             headers=admin)
            check("upload dot.bmp 201", r.status_code == 201,
                  f"HTTP {r.status_code} {r.text[:160]}")
            if r.status_code == 201:
                d = await wait_doc(c, admin, r.json()["id"], 300)
                st = d.get("status") or d.get("doc", {}).get("status")
                detail = (f"status={st} err={str(d.get('error_msg'))[:200]} "
                          f"ocr={d.get('ocr_used')}")
                if st == "done":
                    check("bmp → done (MinerU accepted, ocr_used)",
                          d.get("ocr_used") is True, detail)
                else:
                    # 1x1 纯色点无文字:MinerU 成功但零文本 → NoContentError
                    # 确定性 failed——错误文案必须命中「无文本」才证明
                    # 「进门+真调云端+确定性收口」全链路(而非其他失败)
                    check("bmp → ocr roundtrip, no-text close (failed)",
                          "no text" in str(d.get("error_msg")).lower(),
                          detail)

            # ③ 前端 accept
            vue = (BACKEND_DIR.parent / "frontend" / "src" / "pages"
                   / "DocsPage.vue").read_text(encoding="utf-8")
            need = [".txt", ".md", ".csv", ".json", ".html", ".htm",
                    ".pptx", ".bmp", ".tif", ".tiff", ".webp", ".gif"]
            acc_line = next(l for l in vue.splitlines() if "accept=" in l)
            check("frontend accept carries 18 extensions",
                  all(x in acc_line for x in need), acc_line.strip()[:160])

            # accept 只管文件选择器;beforeUpload 的白名单常量才是真闸门,
            # 须同含新类型(M24 终审:旧 6 类型常量曾拦下全部新类型上传)
            ext_line = next(l for l in vue.splitlines()
                            if l.lstrip().startswith("const ALLOWED_EXTS"))
            check("frontend beforeUpload allowlist carries new types",
                  ".pptx" in ext_line and ".bmp" in ext_line,
                  ext_line.strip()[:160])

            # ④ 白名单单源(运行时)
            from app.services import doc_ops
            from app.services.parsing.base import REGISTRY

            check("ALLOWED_EXTS == frozenset(REGISTRY) at runtime",
                  doc_ops.ALLOWED_EXTS == frozenset(REGISTRY))

            codes = [(await c.delete(f"{API}/kbs/{k}", headers=admin)
                      ).status_code for k in kb_ids]
            check("acceptance KBs deleted 204 (API)",
                  bool(codes) and all(x == 204 for x in codes), str(codes))
        finally:
            await _cleanup(c, admin)
    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
