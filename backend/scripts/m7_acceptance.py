"""M7 无头验收脚本(真栈:http://127.0.0.1:8001 + worker + 智谱 key)。

用法(backend 目录,项目 venv):
    .venv\\Scripts\\python scripts\\m7_acceptance.py

覆盖 M7:零命中双门控(rerank 相关度阈值 + 提示词兜底)/ refused 全链路
(done 帧 + 落库消息)/ eval_generation --rerank 拒答率与忠实度。
ZHIPU_API_KEY 未配置时打印 SKIP 整体退出(不误报,门控逻辑单测已覆盖)。
若 S2/S5 出现正常题误拒:把 .env 的 RETRIEVAL_MIN_SCORE 按 0.05 下调,
重启后端(start_dev.bat)后重跑本脚本,终值回填 spec 附录。
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

KB_NAME = "M7验收库"
FACT1 = "青鸾号高空气船的巡航升限为八千五百米"
FACT2 = "赤霄超导电缆的传输容量为三百二十兆瓦"
NORMAL_Q1 = "青鸾号高空气船的巡航升限是多少米?"
NORMAL_Q2 = "赤霄超导电缆的传输容量是多少兆瓦?"
ZERO_HIT_Q1 = "珠穆朗玛峰的海拔是多少米?"
ZERO_HIT_Q2 = "世界杯足球赛每几年举办一次?"
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
    print(f"M7 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
    if RESULTS["skip"]:
        print("SKIP:", *RESULTS["skip"], sep="\n  - ")
    if RESULTS["fail"]:
        print("FAILED:", *RESULTS["fail"], sep="\n  - ")
        print("提示:若为正常题误拒,把 .env 的 RETRIEVAL_MIN_SCORE 下调 0.05 并"
              "重启后端(start_dev.bat)后重跑。")
        sys.exit(1)


def promote_roles(usernames: dict[str, str]) -> None:
    async def _run():
        from sqlalchemy import text

        from app.db.session import SessionLocal

        async with SessionLocal() as s:
            for username, role in usernames.items():
                await s.execute(
                    text("UPDATE users SET role = :r WHERE username = :u"),
                    {"r": role, "u": username},
                )
            await s.commit()

    asyncio.run(_run())


def register_and_login(client: httpx.Client, username: str) -> dict:
    client.post(f"{BASE}/auth/register",
                json={"username": username, "password": "secret123"})
    r = client.post(f"{BASE}/auth/login",
                    json={"username": username, "password": "secret123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def make_pdf_bytes(title: str, paragraphs: list[str]) -> bytes:
    """pymupdf 造真文本 pdf(内置 china-s 中文字体,内存字节,不落盘)。"""
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
    """POST /chat/ask(可指定 rerank),消费 SSE,返回 {tokens, citations, done, error}。"""
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
    """done 帧携带权威终稿;流式 token 作对账兜底。"""
    return ((res.get("done") or {}).get("answer") or res["tokens"]) or ""


def ask_with_retry(client, headers, kb_id, question, rerank, judge,
                   attempts=2):
    """真 LLM 有抖动:同一断言最多试 2 次(连续两次失败才 FAIL),返回末次结果。"""
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
        skip("M7 acceptance(全部场景)", "ZHIPU_API_KEY 未配置")
        summary_and_exit()
        return

    client = httpx.Client(timeout=60.0)

    # ---- 0. 基线 ----
    r = client.get(f"{BASE}/health")
    check("health 200", r.status_code == 200, r.text[:100])
    if r.status_code != 200:
        summary_and_exit()
        return

    client.post(f"{BASE}/auth/register",
                json={"username": "m7_owner", "password": "secret123"})
    promote_roles({"m7_owner": "editor"})
    owner = register_and_login(client, "m7_owner")
    me = client.get(f"{BASE}/auth/me", headers=owner).json()
    check("m7_owner promoted editor", me["role"] == "editor", str(me))

    # ---- 验收库:两篇事实明确的真 pdf ----
    r = client.post(f"{BASE}/kbs", json={"name": KB_NAME}, headers=owner)
    kb_id = r.json()["id"]
    check("kb created", r.status_code == 201 and kb_id > 0, r.text[:200])

    up1 = upload_doc(client, owner, kb_id, "m7青鸾号.pdf",
                     make_pdf_bytes("青鸾号高空气船简介", [FACT1, "青鸾号以氦气提供升力。"]))
    doc1_id = up1["body"].get("id")
    doc1 = wait_doc_status(client, owner, doc1_id)
    check("pdf#1 upload & processed done",
          up1["status"] == 201 and doc1["status"] == "done"
          and doc1["chunk_count"] > 0, f"{up1} {doc1}")

    up2 = upload_doc(client, owner, kb_id, "m7赤霄.pdf",
                     make_pdf_bytes("赤霄超导电缆简介", [FACT2, "赤霄电缆采用低温冷却。"]))
    doc2_id = up2["body"].get("id")
    doc2 = wait_doc_status(client, owner, doc2_id)
    check("pdf#2 upload & processed done",
          up2["status"] == 201 and doc2["status"] == "done"
          and doc2["chunk_count"] > 0, f"{up2} {doc2}")

    # ---- S1: rerank on + 零命中 → 阈值门控 → refused ----
    s1, s1_ok = ask_with_retry(
        client, owner, kb_id, ZERO_HIT_Q1, rerank=True,
        judge=lambda r: (r["done"] is not None and r["error"] is None
                         and r["done"].get("refused") is True
                         and r["citations"] == []))
    check("S1 rerank-on zero-hit refused, citations empty", s1_ok,
          str(s1)[:300])

    # ---- S2: rerank on + 正常题 → 不误伤 ----
    s2, s2_ok = ask_with_retry(
        client, owner, kb_id, NORMAL_Q1, rerank=True,
        judge=lambda r: (r["done"] is not None and r["error"] is None
                         and r["done"].get("refused") is False
                         and (r["citations"] or []) and len(r["citations"]) >= 1))
    check("S2 rerank-on normal not refused, citations >= 1", s2_ok,
          str(s2)[:300])

    # ---- S3: rerank off + 零命中 → 提示词兜底 ----
    s3, s3_ok = ask_with_retry(
        client, owner, kb_id, ZERO_HIT_Q2, rerank=False,
        judge=lambda r: (r["done"] is not None and r["error"] is None
                         and final_answer(r).strip().startswith(REFUSAL_PHRASE)))
    check("S3 rerank-off zero-hit prompt fallback phrase", s3_ok,
          final_answer(s3)[:200])

    # ---- S4: 消息落库 refused 与 S1/S2 一致 ----
    if s1["done"]:
        conv1 = s1["done"]["conversation_id"]
        msgs = client.get(f"{BASE}/chat/conversations/{conv1}/messages",
                          headers=owner).json()
        asst = [m for m in msgs if m["role"] == "assistant"]
        check("S4 s1 conversation assistant refused persisted True",
              bool(asst) and asst[-1]["refused"] is True
              and (asst[-1]["citations"] or []) == [], str(asst[-1:])[:300])
    else:
        check("S4 s1 conversation assistant refused persisted True", False,
              "S1 无 done 帧,无法对账")
    if s2["done"]:
        conv2 = s2["done"]["conversation_id"]
        msgs = client.get(f"{BASE}/chat/conversations/{conv2}/messages",
                          headers=owner).json()
        asst = [m for m in msgs if m["role"] == "assistant"]
        check("S4 s2 conversation assistant refused persisted False",
              bool(asst) and asst[-1]["refused"] is False
              and len(asst[-1]["citations"] or []) >= 1, str(asst[-1:])[:300])
    else:
        check("S4 s2 conversation assistant refused persisted False", False,
              "S2 无 done 帧,无法对账")

    # ---- S5: eval_generation --rerank(前 2 正常题 / 后 2 零命中)----
    # eval set 写入 eval_sets/{kb_id}.json 并留存(git 版本化,供复评)
    from pathlib import Path

    eval_path = Path("eval_sets") / f"{kb_id}.json"
    try:
        eval_path.parent.mkdir(exist_ok=True)
        eval_path.write_text(json.dumps({
            "kb_id": kb_id,
            "items": [
                {"question": NORMAL_Q1,
                 "expect_doc_ids": [doc1_id, doc2_id],
                 "expect_keywords": ["八千五百"]},
                {"question": NORMAL_Q2,
                 "expect_doc_ids": [doc1_id, doc2_id],
                 "expect_keywords": ["三百二十"]},
                {"question": ZERO_HIT_Q1, "expect_doc_ids": [],
                 "expect_keywords": []},
                {"question": ZERO_HIT_Q2, "expect_doc_ids": [],
                 "expect_keywords": []},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.eval_generation",
             "--kb", str(kb_id), "--rerank", "--json"],
            capture_output=True, text=True, timeout=900,
        )
        try:
            eval_out = json.loads(proc.stdout)
            normal, zeros = eval_out[:2], eval_out[2:]
            faith = [i["faithfulness"]["score"] for i in normal]
            zero_refused = [i["refused"] for i in zeros]
            faith_avg = (
                sum(faith) / len(faith)
                if faith and all(s is not None for s in faith) else 0.0
            )
            print(f"S5 eval detail: faithfulness={faith} avg={faith_avg:.2f} "
                  f"zero_hit_refused={zero_refused}")
            check("S5 eval zero-hit refused 100% (2/2)",
                  len(eval_out) == 4 and all(zero_refused), proc.stdout[:300])
            check("S5 eval normal faithfulness avg >= 0.8",
                  len(eval_out) == 4
                  and all(s is not None for s in faith)
                  and faith_avg >= 0.8, proc.stdout[:300])
        except Exception as exc:
            detail = (f"{exc}; stdout={proc.stdout[:200]} "
                      f"stderr={proc.stderr[:200]}")
            check("S5 eval zero-hit refused 100% (2/2)", False, detail)
            check("S5 eval normal faithfulness avg >= 0.8", False, detail)
    except Exception as exc:
        check("S5 eval zero-hit refused 100% (2/2)", False, f"运行失败:{exc}")
        check("S5 eval normal faithfulness avg >= 0.8", False, f"运行失败:{exc}")

    summary_and_exit()


if __name__ == "__main__":
    main()
