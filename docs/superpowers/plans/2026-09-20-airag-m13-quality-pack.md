# AIRag M13 质量包 + 治理快修 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 拒答 LLM 二审、评估入库(reference_answer)、ask 结构性提速(零命中短路+强一致跳过)、多跳并行检索、KB 重命名端点+前端、M12 终审七小项全清。

**Architecture:** 全部落在既有分层内:二审/短路/并行是 `chat_graph/nodes.py` 的节点内改造(不动图拓扑、不动提示词语义);评估入库为两张新表 + `scripts/eval_store.py` 单一落库实现供两个 CLI 复用;重命名与删除同置于 `services/kb_ops.py`;快修为点状修正。出口(refused 流向/HTTP 报文)零变化。

**Tech Stack:** FastAPI + SQLAlchemy 2 async + Alembic + LangGraph + Vue3/Element Plus + pytest/vitest。

**Spec:** `docs/superpowers/specs/2026-09-20-airag-m13-quality-pack-design.md`(执行者须同时读 spec)。

## Global Constraints

- Windows CMD;后端命令在 `E:\Projects\AIRag\backend` 下用 `.venv\Scripts\python`;pytest 从 backend 目录跑;前端在 `frontend/` 下 `npx vitest run` / `npm run build`。
- 测试基线:后端 **276P/0F**、前端 vitest 28/28、build 零错——每任务结束时不得低于基线。
- conftest 钉死(既有):AGENTIC_*/CHECKPOINTER/MULTI_HOP/RERANK 全 false;本计划新增钉死:`REFUSAL_RECHECK_ENABLED=false`、`GRADE_CONFIDENT_SKIP_N=0`(T1/T4 落)。
- chat_graph 节点签名惯例:`node(state, llm=...)`——llm 由 `build_graph(llm=)` 注入,测试直调 `node(state, llm=_LLMScript([...]))`(test_chat_graph.py 既有 `_LLMScript`/`_hit` 辅助)。
- alembic 当前 head `d4e5f6a7b8c9`(M12);T2 新迁移 `e5f6a7b8c9d0` 接它,dev 栈真跑在 T10。
- refused 语义与出口零变化:state 键不增、SSE/落库/导出/REST/MCP 出口不动。
- HTTP 报文零变化约束(快修①):DocOpError 经 main.py 全局 handler 出同 status 同 detail。
- 走查期间勿在 backend\ 写临时文件(--reload);每任务 conventional commit。

---

### Task 1: 包裹型拒答 LLM 二审

**Files:**
- Modify: `backend/app/core/config.py`(REFUSAL_RECHECK_ENABLED)
- Modify: `backend/app/services/chat_graph/nodes.py`(generate_node + 新增 _looks_like_refusal/_recheck_refusal)
- Modify: `backend/tests/conftest.py`(钉死开关)
- Test: Modify `backend/tests/test_chat_graph.py`(追加)

**Interfaces:**
- Consumes: `REFUSAL_PHRASE`、`_extract_json`(nodes.py 既有)。
- Produces: `REFUSAL_RECHECK_ENABLED: bool = True`(config);`_looks_like_refusal(answer: str, has_hits: bool) -> bool`、`_recheck_refusal(llm, question: str, answer: str) -> bool | None`(None=判定失败 fail-open);generate_node 的 refused 判定扩展(子串优先,二审兜底)。

- [ ] **Step 1: 写失败测试**

`tests/test_chat_graph.py` 末尾追加(`_LLMScript`/`_hit` 为文件既有辅助;二审用例需 monkeypatch 打开开关):

```python
# ---- M13:包裹型拒答 LLM 二审 ----
async def test_recheck_catches_paraphrased_refusal(monkeypatch):
    """包裹型/近似措辞拒答:子串未命中 → 启发式触发 → LLM 二审改判 True。"""
    from app.core.config import settings
    from app.services.chat_graph.nodes import generate_node

    monkeypatch.setattr(settings, "REFUSAL_RECHECK_ENABLED", True)
    out = await generate_node(
        {"question": "预算多少", "hits": [_hit(1, "无关资料")]},
        # 第 1 个响应=答案(近似措辞),第 2 个=二审 judge JSON
        llm=_LLMScript(["很抱歉,知识库中暂无该资料,无法提供预算信息。",
                        '{"refused": true, "reason": "表示无法回答"}']),
    )
    assert out["refused"] is True


async def test_recheck_normal_answer_stays_false(monkeypatch):
    from app.core.config import settings
    from app.services.chat_graph.nodes import generate_node

    monkeypatch.setattr(settings, "REFUSAL_RECHECK_ENABLED", True)
    out = await generate_node(
        {"question": "预算多少", "hits": [_hit(1, "预算为三千万元")]},
        llm=_LLMScript(["预算为三千万元。",   # 短答案(≤40字)会触发启发式
                        '{"refused": false, "reason": "给出了事实"}']),
    )
    assert out["refused"] is False


async def test_recheck_disabled_keeps_substring_semantics():
    """开关关(conftest 默认):近似措辞仍 False(M8 语义锁定,零额外调用)。"""
    from app.services.chat_graph.nodes import generate_node

    out = await generate_node(
        {"question": "q", "hits": []},
        llm=_LLMScript(["知识库中未找到相关文档"]),
    )
    assert out["refused"] is False


async def test_recheck_failopen_on_bad_judge(monkeypatch):
    """二审 LLM 坏输出:保持子串结果(False),不误杀。"""
    from app.core.config import settings
    from app.services.chat_graph.nodes import generate_node

    monkeypatch.setattr(settings, "REFUSAL_RECHECK_ENABLED", True)
    out = await generate_node(
        {"question": "q", "hits": [_hit(1, "资料")]},
        llm=_LLMScript(["这段资料说明了一切。", "not-json-at-all"]),
    )
    assert out["refused"] is False


async def test_recheck_not_triggered_for_long_normal_answer(monkeypatch):
    """正常长答案(>40 字、无信号词):启发式不触发,judge 零调用。"""
    from app.core.config import settings
    from app.services.chat_graph import nodes as nodes_mod

    monkeypatch.setattr(settings, "REFUSAL_RECHECK_ENABLED", True)
    called = []

    async def _spy(*a, **k):
        called.append(1)
        return False

    monkeypatch.setattr(nodes_mod, "_recheck_refusal", _spy)
    long_answer = "该项目共分三期实施。" + "详细进度与里程碑安排如下。" * 6
    out = await nodes_mod.generate_node(
        {"question": "项目进展", "hits": [_hit(1, "项目资料")]},
        llm=_LLMScript([long_answer]),
    )
    assert out["refused"] is False
    assert called == []  # 启发式未触发 → 二审未调
```

(若 `_LLMScript` 的实现一次消费一个响应,上述双响应写法直接可用;若它对 `ainvoke` 的 messages 形态有假设导致二审调用取不到第二个响应,按其实现微调注入方式并在报告说明。)

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_chat_graph.py -q`
Expected: FAIL——`REFUSAL_RECHECK_ENABLED` 不存在(AttributeError)/`_recheck_refusal` 不存在。

- [ ] **Step 3: 实现**

`app/core/config.py`(MULTI_HOP 配置块附近)加:

```python
    # M13:拒答 LLM 二审(启发式触发;conftest 钉 False)
    REFUSAL_RECHECK_ENABLED: bool = True
```

`tests/conftest.py` 环境变量钉死区(MULTI_HOP_ENABLED 之后)加:

```python
# M13:拒答二审默认关闭(存量用例锁 M8 子串语义;二审用例内 monkeypatch 打开)
os.environ["REFUSAL_RECHECK_ENABLED"] = "false"
```

`app/services/chat_graph/nodes.py`——`REFUSAL_PHRASE` 常量块之后加:

```python
# M13:包裹型拒答二审——先廉价启发式,命中才发一次 LLM(fail-open)
_REFUSAL_SIGNALS = ("未找到", "没有找到", "没找到", "无法回答", "无法提供",
                    "暂无", "无相关", "知识库中没", "抱歉", "超出")

RECHECK_SYSTEM = (
    "你是拒答判定器。判断下面这个\"答案\"是否实质上在表示知识库无法回答该问题"
    "(明确表示没有相关资料/无法回答/建议查阅其他渠道等),"
    "而非给出了实质内容。答案确实给出与问题相关的事实内容时判 false。"
    '只输出 JSON:{"refused": true|false, "reason": "<一句话>"}'
)


def _looks_like_refusal(answer: str, has_hits: bool) -> bool:
    a = (answer or "").strip()
    return (not has_hits) or len(a) <= 40 or any(s in a for s in _REFUSAL_SIGNALS)


async def _recheck_refusal(llm, question: str, answer: str) -> bool | None:
    """LLM 二审;任何失败返回 None(fail-open,保持子串结果)。"""
    try:
        resp = await llm.ainvoke([
            ("system", RECHECK_SYSTEM),
            ("user", f"问题:{question}\n答案:{answer.strip()[:500]}"),
        ])
        parsed = json.loads(_extract_json(resp.content))
        return bool(parsed["refused"])
    except Exception:
        logger.warning("refusal recheck failed, keep substring verdict")
        return None
```

`generate_node` 的 refused 计算(现 `REFUSAL_PHRASE in (answer or "").strip()` 一行)改为:

```python
    refused = REFUSAL_PHRASE in (answer or "").strip()
    if (not refused and settings.REFUSAL_RECHECK_ENABLED
            and _looks_like_refusal(answer or "", bool(hits))):
        verdict = await _recheck_refusal(llm, state["question"], answer or "")
        if verdict is True:
            refused = True
```

(nodes.py 顶部若缺 `json`/`logger` import 则补:`import json`、loguru 既有则复用;`settings` 已有。)

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_chat_graph.py -q`
Expected: PASS 全绿(既有 M8 拒答用例因 conftest 钉死开关不受影响)。

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/app/core/config.py backend/app/services/chat_graph/nodes.py backend/tests/conftest.py backend/tests/test_chat_graph.py
git commit -m "feat(chat): paraphrased-refusal LLM recheck with fail-open"
```

---

### Task 2: 评估入库模型与迁移(EvalRun/EvalItem)

**Files:**
- Create: `backend/app/models/eval.py`
- Modify: `backend/app/models/__init__.py`(导出)
- Create: `backend/alembic/versions/e5f6a7b8c9d0_m13_eval_tables.py`
- Modify: `backend/tests/conftest.py`(CLEANUP_ORDER)
- Test: Create `backend/tests/test_eval_models.py`

**Interfaces:**
- Consumes: 无。
- Produces: `EvalRun(id, kb_id:int 无FK, mode:str, summary:dict JSON, item_count:int, created_at)`;`EvalItem(id, run_id FK eval_runs.id ondelete CASCADE, question:Text, expect_doc_ids:JSON|None, expect_keywords:JSON|None, answer:Text|None, refused:bool|None, hit_at_k/mrr/keyword_recall/faithfulness/relevancy/reference_score:float|None)`;`EvalRun.items` relationship。T3 的 `scripts/eval_store.py` 消费。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_eval_models.py`:

```python
# backend/tests/test_eval_models.py
"""M13 Task2:评估入库两张表的模型行为(create_all 真源)。"""
from sqlalchemy import select

from app.models import EvalItem, EvalRun


async def test_eval_run_roundtrip(db_session):
    run = EvalRun(kb_id=3, mode="retrieval",
                  summary={"hit": 0.8, "mrr": 0.6}, item_count=2)
    db_session.add(run)
    await db_session.flush()
    db_session.add_all([
        EvalItem(run_id=run.id, question="q1", expect_doc_ids=[11],
                 expect_keywords=["三千"], hit_at_k=1.0, mrr=1.0,
                 keyword_recall=1.0),
        EvalItem(run_id=run.id, question="q2", answer="答",
                 refused=False, faithfulness=0.9, relevancy=0.8,
                 reference_score=None),
    ])
    await db_session.commit()
    db_session.expire_all()
    got = (await db_session.execute(
        select(EvalRun).where(EvalRun.mode == "retrieval"))).scalars().one()
    assert got.summary["hit"] == 0.8 and got.item_count == 2
    assert [i.question for i in got.items] == ["q1", "q2"]
    assert got.items[1].reference_score is None


async def test_eval_item_cascade_on_run_delete(db_session):
    run = EvalRun(kb_id=1, mode="generation", summary={}, item_count=1)
    db_session.add(run)
    await db_session.flush()
    item = EvalItem(run_id=run.id, question="q")
    db_session.add(item)
    await db_session.commit()
    await db_session.delete(run)
    await db_session.commit()
    assert (await db_session.execute(
        select(EvalItem))).scalars().first() is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_eval_models.py -q`
Expected: FAIL——`EvalRun` 不存在于 `app.models`(ImportError)。

- [ ] **Step 3: 实现**

Create `backend/app/models/eval.py`:

```python
# backend/app/models/eval.py
from sqlalchemy import Boolean, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class EvalRun(Base, TimestampMixin):
    """M13:一次评估运行(retrieval|generation);kb_id 无 FK——KB 删除后
    历史评估保留(spec B 设计意图)。"""

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(16))  # retrieval | generation
    summary: Mapped[dict] = mapped_column(JSON)    # 各指标均值 + 计数
    item_count: Mapped[int]

    items: Mapped[list["EvalItem"]] = relationship(
        back_populates="run", cascade="all, delete-orphan")


class EvalItem(Base):
    __tablename__ = "eval_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    expect_doc_ids: Mapped[list | None] = mapped_column(JSON)
    expect_keywords: Mapped[list | None] = mapped_column(JSON)
    answer: Mapped[str | None] = mapped_column(Text)
    refused: Mapped[bool | None] = mapped_column(Boolean)
    hit_at_k: Mapped[float | None] = mapped_column(Float)
    mrr: Mapped[float | None] = mapped_column(Float)
    keyword_recall: Mapped[float | None] = mapped_column(Float)
    faithfulness: Mapped[float | None] = mapped_column(Float)
    relevancy: Mapped[float | None] = mapped_column(Float)
    reference_score: Mapped[float | None] = mapped_column(Float)

    run: Mapped[EvalRun] = relationship(back_populates="items")
```

`app/models/__init__.py` 按既有导出风格加 `EvalRun`/`EvalItem`(`from app.models.eval import EvalItem, EvalRun` + `__all__` 并入)。

`tests/conftest.py` 的 `CLEANUP_ORDER` 列表 `"audit_logs"` 之后插入 `"eval_items", "eval_runs"`(FK 顺序:子先父后)。

迁移 `alembic/versions/e5f6a7b8c9d0_m13_eval_tables.py`(头部格式照抄 `d4e5f6a7b8c9` 迁移,down_revision="d4e5f6a7b8c9"):

```python
def upgrade() -> None:
    op.create_table(
        "eval_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kb_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "eval_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("expect_doc_ids", sa.JSON(), nullable=True),
        sa.Column("expect_keywords", sa.JSON(), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("refused", sa.Boolean(), nullable=True),
        sa.Column("hit_at_k", sa.Float(), nullable=True),
        sa.Column("mrr", sa.Float(), nullable=True),
        sa.Column("keyword_recall", sa.Float(), nullable=True),
        sa.Column("faithfulness", sa.Float(), nullable=True),
        sa.Column("relevancy", sa.Float(), nullable=True),
        sa.Column("reference_score", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["eval_runs.id"],
                                ondelete="CASCADE"),
    )
    op.create_index("ix_eval_items_run_id", "eval_items", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_eval_items_run_id", table_name="eval_items")
    op.drop_table("eval_items")
    op.drop_table("eval_runs")
```

(created_at 的 server_default 以 `models/base.py` TimestampMixin 实际定义为准——先读它,若其列定义不同(如 server_default=func.now())照抄同款,勿引入模型与迁移的形态漂移。)

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_eval_models.py -q` → PASS。

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿(276P + 2)。

```bash
git add backend/app/models/eval.py backend/app/models/__init__.py backend/alembic/versions/e5f6a7b8c9d0_m13_eval_tables.py backend/tests/conftest.py backend/tests/test_eval_models.py
git commit -m "feat(eval): eval_runs/eval_items models and migration"
```

---

### Task 3: 评估 CLI --save、eval_runs 查询、reference_answer 评审

**Files:**
- Create: `backend/scripts/eval_store.py`
- Create: `backend/scripts/eval_runs.py`
- Modify: `backend/scripts/eval_retrieval.py`(--save + item 期望字段透传)
- Modify: `backend/scripts/eval_generation.py`(--save + reference_answer)
- Modify: `backend/app/services/eval_judge.py`(reference_score)
- Modify: `backend/eval_sets/README.md`(字段说明)
- Test: Create `backend/tests/test_eval_store.py`;Modify `backend/tests/test_eval_judge.py`(若存在,否则新建)

**Interfaces:**
- Consumes: Task 2 的 `EvalRun`/`EvalItem`。
- Produces: `eval_store.save_run(kb_id: int, mode: str, results: list[dict]) -> int`(async,自开 session,落 run+items,返回 run_id);`eval_judge.reference_score(llm, question, answer, reference) -> dict`(同 faithfulness 形态);CLI 参数 `--save`(两脚本);`scripts/eval_runs.py`(`--kb N [--mode M] [--last 10] [--json]`)。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_eval_store.py`:

```python
# backend/tests/test_eval_store.py
"""M13 Task3:eval_store.save_run 落库与 summary 口径。"""
from sqlalchemy import select

from app.models import EvalItem, EvalRun
from scripts.eval_store import save_run, summarize


def test_summarize_retrieval():
    results = [
        {"question": "q1", "expect_doc_ids": [1], "expect_keywords": ["a"],
         "hit_at_k": True, "mrr": 1.0, "keyword_recall": 1.0},
        {"question": "q2", "expect_doc_ids": [2], "expect_keywords": [],
         "hit_at_k": False, "mrr": 0.0, "keyword_recall": 0.0},
    ]
    s = summarize(results)
    assert s["hit"] == 0.5 and s["mrr"] == 0.5 and s["keyword_recall"] == 0.5
    assert s["item_count"] == 2


def test_summarize_generation_skips_none_scores():
    results = [
        {"question": "q", "faithfulness": {"score": 0.9},
         "relevancy": {"score": None}, "refused": True},
    ]
    s = summarize(results)
    assert s["faithfulness_avg"] == 0.9 and s["relevancy_avg"] is None
    assert s["refused_count"] == 1


async def test_save_run_roundtrip(db_session):
    results = [
        {"question": "q1", "expect_doc_ids": [11], "expect_keywords": ["三千"],
         "hit_at_k": True, "mrr": 1.0, "keyword_recall": 1.0},
    ]
    run_id = await save_run(3, "retrieval", results)
    assert run_id > 0
    rows = (await db_session.execute(select(EvalRun))).scalars().all()
    assert len(rows) == 1 and rows[0].kb_id == 3
    items = (await db_session.execute(select(EvalItem))).scalars().all()
    assert len(items) == 1 and items[0].hit_at_k == 1.0
```

(注:`save_run` 自开 `SessionLocal`,与测试的 `db_session`(TestSession)不同库连接但同库;断言前 `db_session.expire_all()` 若需要。)

`backend/tests/test_eval_judge.py`(若已存在则追加,不存在则新建)加 reference_score 用例:

```python
async def test_reference_score_parses_and_clamps():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services.eval_judge import reference_score

    llm = FakeListChatModel(responses=['{"score": 1.7, "reasons": "一致"}'])
    out = await reference_score(llm, "预算?", "三千万元", "约三千万元")
    assert out["score"] == 1.0 and out["reasons"] == "一致"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_eval_store.py tests/test_eval_judge.py -q`
Expected: FAIL——`scripts.eval_store` / `reference_score` 不存在。

- [ ] **Step 3: 实现**

Create `backend/scripts/eval_store.py`:

```python
"""M13:评估结果落库(两 CLI 共用)与汇总口径。"""
from app.db.session import SessionLocal
from app.models import EvalItem, EvalRun


def _avg(vals: list[float]):
    return round(sum(vals) / len(vals), 4) if vals else None


def summarize(results: list[dict]) -> dict:
    """retrieval/generation 通用汇总:各自字段缺席则跳过。"""
    s: dict = {"item_count": len(results)}
    hits = [r["hit_at_k"] for r in results if "hit_at_k" in r]
    if hits:
        s["hit"] = _avg([float(h) for h in hits])
        s["mrr"] = _avg([r["mrr"] for r in results if "mrr" in r])
        s["keyword_recall"] = _avg(
            [r["keyword_recall"] for r in results if "keyword_recall" in r])
    faith = [r["faithfulness"]["score"] for r in results
             if r.get("faithfulness", {}).get("score") is not None]
    if faith or any("faithfulness" in r for r in results):
        s["faithfulness_avg"] = _avg(faith)
        s["relevancy_avg"] = _avg(
            [r["relevancy"]["score"] for r in results
             if r.get("relevancy", {}).get("score") is not None])
        s["refused_count"] = sum(1 for r in results if r.get("refused"))
    refs = [r["reference"]["score"] for r in results
            if r.get("reference", {}).get("score") is not None]
    if refs or any("reference" in r for r in results):
        s["reference_avg"] = _avg(refs)
    return s


async def save_run(kb_id: int, mode: str, results: list[dict]) -> int:
    """落 EvalRun + 逐题 EvalItem;返回 run_id(自开 session 自 commit)。"""
    async with SessionLocal() as db:
        run = EvalRun(kb_id=kb_id, mode=mode, summary=summarize(results),
                      item_count=len(results))
        db.add(run)
        await db.flush()
        for r in results:
            db.add(EvalItem(
                run_id=run.id, question=r["question"],
                expect_doc_ids=r.get("expect_doc_ids"),
                expect_keywords=r.get("expect_keywords"),
                answer=r.get("answer"), refused=r.get("refused"),
                hit_at_k=(float(r["hit_at_k"])
                          if isinstance(r.get("hit_at_k"), bool) else None)
                or (r["hit_at_k"] if isinstance(r.get("hit_at_k"), float)
                    else None),
                mrr=r.get("mrr"), keyword_recall=r.get("keyword_recall"),
                faithfulness=(r.get("faithfulness") or {}).get("score"),
                relevancy=(r.get("relevancy") or {}).get("score"),
                reference_score=(r.get("reference") or {}).get("score"),
            ))
        await db.commit()
        return run.id
```

(注意 `hit_at_k` 在 retrieval 结果里是 bool——落库转 float 1.0/0.0;generation 结果无该键则 None。上面表达式若不够直白,可改为先取值再 if/else 三行,语义:bool→1.0/0.0、float 原样、缺键 None。)

`app/services/eval_judge.py` 末尾加:

```python
REFERENCE_SYSTEM = (
    "你是答案一致性评审。对照参考答案判断回答是否覆盖了参考答案的关键事实:"
    "关键事实完整一致接近1.0,部分覆盖取中间值,存在明显冲突或遗漏接近0.0;"
    '回答明确表示无法从资料回答时按0.0。'
    '只输出 JSON:{"score": <0.0~1.0 的数值>, "reasons": "<一句话依据>"}'
)


async def reference_score(llm, question: str, answer: str,
                          reference: str) -> dict:
    user = f"问题:{question}\n参考答案:{reference}\n回答:{answer}"
    return await _judge(llm, REFERENCE_SYSTEM, user)
```

`scripts/eval_retrieval.py`——`run()` 的 results.append 增加期望字段透传(question 旁):

```python
                "expect_doc_ids": item.get("expect_doc_ids"),
                "expect_keywords": item.get("expect_keywords"),
```

`main()` 加 `ap.add_argument("--save", action="store_true")`;结果打印后:

```python
    if args.save:
        from scripts.eval_store import save_run

        run_id = asyncio.run(save_run(args.kb, "retrieval", results))
        print(f"saved: run_id={run_id}")
```

(注意 `run()` 已由一个 asyncio.run 驱动,`--save` 的 save_run 再起一个事件循环在 CPython 下顺序执行无嵌套问题;若实现时重构为先 run 再 save 同循环更佳,任选,报告注明。)

`scripts/eval_generation.py`——`run()` 每题 results.append 增:

```python
                "answer": answer,
                "reference": (
                    await reference_score(llm, item["question"], answer,
                                          item["reference_answer"])
                    if item.get("reference_answer") else None
                ),
```

(import 行加 `reference_score`);`main()` 同款 `--save`(`save_run(args.kb, "generation", results)`)。

Create `backend/scripts/eval_runs.py`:

```python
"""M13:评估运行历史查询。

用法(backend 目录):py -m scripts.eval_runs --kb 3 [--mode retrieval]
                                     [--last 10] [--json]
"""
import argparse
import asyncio
import json


async def query(kb_id: int, mode: str | None, last: int) -> list[dict]:
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models import EvalRun

    async with SessionLocal() as db:
        stmt = select(EvalRun).where(EvalRun.kb_id == kb_id)
        if mode:
            stmt = stmt.where(EvalRun.mode == mode)
        stmt = stmt.order_by(EvalRun.id.desc()).limit(last)
        rows = (await db.execute(stmt)).scalars().all()
        return [
            {"id": r.id, "kb_id": r.kb_id, "mode": r.mode,
             "item_count": r.item_count, "summary": r.summary,
             "created_at": r.created_at.isoformat()}
            for r in rows
        ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", type=int, required=True)
    ap.add_argument("--mode", choices=["retrieval", "generation"])
    ap.add_argument("--last", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = asyncio.run(query(args.kb, args.mode, args.last))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("无评估记录")
        return
    print(f"{'id':<6} {'模式':<12} {'题数':<5} 汇总(截选)                 时间")
    for r in rows:
        s = json.dumps(r["summary"], ensure_ascii=False)[:44]
        print(f"{r['id']:<6} {r['mode']:<12} {r['item_count']:<5} "
              f"{s:<44} {r['created_at'][:19]}")


if __name__ == "__main__":
    main()
```

`eval_sets/README.md` 增补字段行:`reference_answer(可选,字符串):标准答案;eval_generation 会用它做一致性评审`。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_eval_store.py tests/test_eval_judge.py tests/test_eval_models.py -q` → PASS;`.venv\Scripts\python -m py_compile scripts/eval_runs.py scripts/eval_retrieval.py scripts/eval_generation.py` 无输出。

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/scripts/eval_store.py backend/scripts/eval_runs.py backend/scripts/eval_retrieval.py backend/scripts/eval_generation.py backend/app/services/eval_judge.py backend/eval_sets/README.md backend/tests/test_eval_store.py backend/tests/test_eval_judge.py
git commit -m "feat(eval): cli --save persistence, run history, reference_score"
```

---

### Task 4: ask 提速(零命中 grade 短路 + 强一致跳过)

**Files:**
- Modify: `backend/app/core/config.py`(GRADE_CONFIDENT_SKIP_N)
- Modify: `backend/app/services/chat_graph/nodes.py`(grade_node 入口)
- Modify: `backend/tests/conftest.py`(钉 0)
- Test: Modify `backend/tests/test_chat_graph.py`(追加;必要时适配既有 grade 用例)

**Interfaces:**
- Consumes: 无(既有 grade_node)。
- Produces: `GRADE_CONFIDENT_SKIP_N: int = 3`(0=关闭);grade_node 两级短路——空 hits → `{"grade": "insufficient"}`(无 LLM);≥N 条 `source=="both"` → `{"grade": "sufficient"}`(无 LLM)。路由与 verdict 值域不变。

- [ ] **Step 1: 写失败测试**

`tests/test_chat_graph.py` 追加(先读文件内既有 grade_node 用例的调用形态并保持一致;`_LLMScript` 会记录消费——若其不记录调用次数,用 monkeypatch 包装断言):

```python
# ---- M13:ask 提速——grade 两级短路 ----
async def test_grade_empty_hits_short_circuits(monkeypatch):
    """零命中:不再对空候选烧 LLM,直接 insufficient。"""
    from app.services.chat_graph import nodes as nodes_mod

    calls = []

    class _Boom:
        async def ainvoke(self, *a, **k):
            calls.append(1)
            raise AssertionError("must not call llm on empty hits")

    out = await nodes_mod.grade_node({"question": "q", "hits": []},
                                     llm=_Boom())
    assert out["grade"] == "insufficient"
    assert calls == []


async def test_grade_confident_both_skips_llm(monkeypatch):
    """≥GRADE_CONFIDENT_SKIP_N 条 source=both → sufficient 且 LLM 未调。"""
    from app.core.config import settings
    from app.services.chat_graph import nodes as nodes_mod

    monkeypatch.setattr(settings, "GRADE_CONFIDENT_SKIP_N", 3)
    both = [_hit(i, f"内容{i}") for i in range(1, 4)]
    for h in both:
        h["source"] = "both"

    class _Boom:
        async def ainvoke(self, *a, **k):
            raise AssertionError("must not call llm when confident")

    out = await nodes_mod.grade_node(
        {"question": "q", "hits": both}, llm=_Boom())
    assert out["grade"] == "sufficient"


async def test_grade_confident_disabled_falls_back_to_llm():
    """GRADE_CONFIDENT_SKIP_N=0(conftest 默认):3 条 both 仍走 LLM。"""
    from app.services.chat_graph.nodes import grade_node

    both = [_hit(i, f"内容{i}") for i in range(1, 4)]
    for h in both:
        h["source"] = "both"
    out = await grade_node(
        {"question": "q", "hits": both},
        llm=_LLMScript(['{"verdict": "insufficient"}']),
    )
    assert out["grade"] == "insufficient"
```

(既有 grade 用例若显式依赖"空 hits 也调 LLM"或"both 命中仍调 LLM"(在 conftest 默认 0 下后者不变),仅前者需按短路语义适配——运行后按失败信息最小修改并在报告说明。)

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_chat_graph.py -q`
Expected: FAIL——`GRADE_CONFIDENT_SKIP_N` 不存在;短路用例因 LLM 被调(或键缺失)失败。

- [ ] **Step 3: 实现**

`app/core/config.py`(REFUSAL_RECHECK_ENABLED 旁)加:

```python
    # M13:≥N 条 source=both(向量+关键词双中)时跳过 grade LLM;0=关闭
    GRADE_CONFIDENT_SKIP_N: int = 3
```

`tests/conftest.py` 钉死区加:

```python
# M13:grade 强一致跳过默认关闭(既有 grade 行为用例不受影响)
os.environ["GRADE_CONFIDENT_SKIP_N"] = "0"
```

`nodes.py` `grade_node` 函数体入口(prompt 拼接之前)加:

```python
    hits = state.get("hits") or []
    if not hits:  # M13:零命中短路,不对空候选烧 LLM
        return {"grade": "insufficient"}
    n = settings.GRADE_CONFIDENT_SKIP_N
    if n > 0 and sum(1 for h in hits if h.get("source") == "both") >= n:
        # M13:双半场强一致 → 检索充分,跳过 LLM(路由值域不变)
        return {"grade": "sufficient"}
```

(其后原有逻辑照旧;原函数内 hits 取值方式与上面对齐,避免重复取。)

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_chat_graph.py -q` → 全绿(含既有 grade/多跳用例——零命中路径 verdict 仍 insufficient,路由行为不变)。

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/app/core/config.py backend/app/services/chat_graph/nodes.py backend/tests/conftest.py backend/tests/test_chat_graph.py
git commit -m "feat(chat): grade short-circuits for empty hits and confident both"
```

---

### Task 5: 多跳子问题并行检索

**Files:**
- Modify: `backend/app/services/chat_graph/nodes.py`(retrieve_node + 模块级 _search_one)
- Test: Modify `backend/tests/test_chat_graph.py`(追加)

**Interfaces:**
- Consumes: `hybrid_search`、`SessionLocal`(nodes.py 既有)。
- Produces: `_search_one(query: str, kb_ids: list[int]) -> list[dict]`(模块级,session-per-query);retrieve_node 的 per_query 改 `asyncio.gather`;合并逻辑(轮转+去重+cap)零改动。

- [ ] **Step 1: 写失败测试**

`tests/test_chat_graph.py` 追加:

```python
# ---- M13:多跳子问题并行检索 ----
async def test_retrieve_parallel_queries_merge(monkeypatch):
    """多查询并行检索结果与串行语义等价(合并按 chunk_id 去重)。"""
    import asyncio

    from app.services.chat_graph import nodes as nodes_mod

    enter_ts = {}

    async def fake_search_one(query, kb_ids):
        enter_ts[query] = asyncio.get_event_loop().time()
        await asyncio.sleep(0.05)          # 并行时三个查询的进入时间应重叠
        return [{"chunk_id": hash(query) % 1000, "document_id": 1,
                 "kb_id": kb_ids[0], "filename": "f", "page_no": 1,
                 "content": f"内容-{query}", "score": 0.5,
                 "source": "vector"}]

    monkeypatch.setattr(nodes_mod, "_search_one", fake_search_one)
    out = await nodes_mod.retrieve_node(
        {"question": "q", "kb_ids": [3],
         "sub_queries": ["子1", "子2", "子3"]})
    starts = sorted(enter_ts.values())
    assert starts[2] - starts[0] < 0.04    # 三个几乎同时进入(并行)
    assert len(out["hits"]) == 3           # 去重后各保留一条
```

(若既有 retrieve_node 多查询用例存在且 monkeypatch 目标是 `hybrid_search`,按其惯例对齐——新用例允许只断言"并行进入重叠 + 合并正确"两点。)

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_chat_graph.py -q`
Expected: FAIL——`_search_one` 不存在(AttributeError)。

- [ ] **Step 3: 实现**

`nodes.py` `retrieve_node` 上方加模块级函数,`retrieve_node` 内的 `async with SessionLocal() ... for q in queries` 串行段替换为 gather:

```python
async def _search_one(query: str, kb_ids: list[int]) -> list:
    """单查询检索;session-per-query(M13 并行,并发不可共享会话)。"""
    async with SessionLocal() as db:
        return await hybrid_search(db, kb_ids, query)
```

retrieve_node 内:

```python
    per_query = list(await asyncio.gather(
        *(_search_one(q, state["kb_ids"]) for q in queries)))
```

(顶部 `import asyncio` 若缺则补;单查询路径 gather 单元素行为同串行;其后的轮转合并段零改动。)

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_chat_graph.py -q` → 全绿。

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/app/services/chat_graph/nodes.py backend/tests/test_chat_graph.py
git commit -m "feat(chat): parallel sub-query retrieval via gather"
```

---

### Task 6: 治理快修(后端五件 + M12 验收配对断言)

**Files:**
- Modify: `backend/app/services/kb_ops.py`(①busy → DocOpError)
- Modify: `backend/app/api/documents.py`、`backend/app/api/agent.py`(②413 预检后移)
- Modify: `backend/app/schemas/auth.py`(③kb_scope 默认 None)
- Modify: `backend/app/services/agent_facade.py`(④denied 条件重排)
- Modify: `backend/app/mcp_server.py`(⑦c 描述括注排版统一单行)
- Modify: `backend/tests/test_auth_keys_api.py`(⑦a 删未用 kb_id)
- Modify: `backend/tests/test_mcp.py`(⑦b JWT ask 补 quota 未调用断言)
- Modify: `backend/scripts/m12_acceptance.py`(⑥审计配对断言)
- Test: 追加断言散布于 `tests/test_kbs.py`、`tests/test_agent_docs_api.py`、`tests/test_kb_scope.py`、`tests/test_auth_keys_api.py`

**Interfaces:**
- Consumes: Task 2 无关;`DocOpError`(M12 已有)。
- Produces: 全部为行为等价或收紧,无新接口;唯一可见变化——越界/不可见库上传超限 body 得 404/403 而非 413。

- [ ] **Step 1: 写失败测试**

`tests/test_agent_docs_api.py` 追加:

```python
async def test_precheck_after_visibility_invisible_kb(client, auth_headers,
                                                      monkeypatch):
    """M13 快修②:不可见库+超限 body → 404(预检不得先于可见性)。"""
    from app.core.config import settings
    from tests.test_agent_api import _create_key_role

    key = await _create_key_role(client, auth_headers, "预检序编辑", "editor")
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 0)
    resp = await client.post(
        "/api/agent/kbs/999999/documents",
        files={"file": ("big.docx", b"x" * (2 * 1024 * 1024),
                        "application/octet-stream")},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404  # 现状是 413(预检在前)→ 本用例先红
```

`tests/test_kb_scope.py` 追加:

```python
async def test_out_of_scope_skips_perm_query(client, auth_headers, db_session,
                                             monkeypatch):
    """M13 快修④:界外库不再烧 perm DB 查询(条件重排,行为不变)。"""
    from unittest.mock import AsyncMock

    import app.services.agent_facade as facade_mod

    kb_in, kb_out = await _two_kbs(client, auth_headers)
    key = await _scoped_key(client, auth_headers, db_session, [kb_in])
    real_perm = facade_mod.get_kb_perm
    perm = AsyncMock(side_effect=real_perm)
    monkeypatch.setattr(facade_mod, "get_kb_perm", perm)
    r = await client.post("/api/agent/search",
                          json={"kb_ids": [kb_out], "query": "q"},
                          headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403
    assert perm.await_count == 0  # 界外判定不触达 perm(现状 ≥1 → 先红)
```

`tests/test_auth_keys_api.py` 追加:

```python
async def test_api_key_out_kb_scope_default():
    """M13 快修③:kb_scope 缺省 None(第三方构造不必显式传)。"""
    from app.schemas.auth import ApiKeyOut

    base = dict(id=1, name="n", key_prefix="airag_x", role="editor",
                is_active=True, expires_at=None, last_used_at=None,
                created_at="2026-09-20T00:00:00")
    assert ApiKeyOut.model_validate(base).kb_scope is None
```

`tests/test_mcp.py` 的 `test_mcp_ask_with_jwt_principal` 内(`rj = await _tool_call(...)` 之前)补 quota 断言(⑦b):

```python
    quota_calls = []
    orig_quota = __import__("app.mcp_server", fromlist=["quota_check"]).quota_check

    async def spy_quota(key_id):
        quota_calls.append(key_id)
        return await orig_quota(key_id)

    monkeypatch.setattr("app.mcp_server.quota_check", spy_quota)
```

断言段末尾加:

```python
    assert quota_calls == []  # JWT 主体不烧配额检查(结构性断言强化)
```

(实现时以该用例现有结构融合,spy 放在 ainvoke 之前。)

`tests/test_kbs.py` 追加(①的出口等价):

```python
async def test_delete_kb_busy_docoperror出口(client, auth_headers, db_session):
    """M13 快修①:kb_ops busy 409 走 DocOpError,HTTP 报文不变。"""
    from app.models import Document, KnowledgeBase

    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = KnowledgeBase(name="快修忙库", owner_id=me.json()["id"])
    db_session.add(kb)
    await db_session.flush()
    db_session.add(Document(kb_id=kb.id, filename="a.docx", file_path="x",
                            mime="m", size=1, sha256="fixbusy",
                            status="parsing"))
    await db_session.commit()
    r = await client.delete(f"/api/kbs/{kb.id}", headers=auth_headers)
    assert r.status_code == 409
    assert r.json()["detail"] == "knowledge base has documents being processed"
```

(与既有 `test_delete_kb_busy_409` 断言等价属预期——本条是①改造的行为锁定;既有用例回归即证明报文未变。)

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_agent_docs_api.py tests/test_kb_scope.py tests/test_auth_keys_api.py tests/test_mcp.py tests/test_kbs.py -q`
Expected: 新 4 用例 FAIL(413≠404 / await_count≥1 / kb_scope KeyError / —;⑦b spy 断言在未改动前应已通过,若已通过则作为护栏)。

- [ ] **Step 3: 实现**

①`kb_ops.py`:import 改 `from app.services.doc_ops import BUSY_STATUSES, DocOpError`;busy raise 改:

```python
    if busy is not None:
        raise DocOpError("busy", 409,
                         "knowledge base has documents being processed")
```

(若 HTTPException 不再被本文件使用则删 import;`fastapi` import 行相应清理。)

②`documents.py` upload_document:把 Content-Length 预检三行从 `_require_kb_editor` 之前移到其后(`kb = await db.get(...)` 之前);`agent.py` agent_upload_document:预检三行移到 `_kb_editor_or_403(db, principal, kb)` 之后、`payload = await file.read()` 之前(两处注释同步更新顺序说明:可见性 → 权限 → 预检 → 权威校验)。

③`schemas/auth.py` ApiKeyOut:`kb_scope: list[int] | None = None`。

④`agent_facade.py` `_permitted_kb_ids` denied 列表条件重排为:

```python
    denied = [
        kb_id for kb_id in kb_ids
        if kb_id not in by_id
        or (key_scope is not None and kb_id not in key_scope)  # M13:界外先判
        or await get_kb_perm(db, user, by_id[kb_id]) is None
    ]
```

⑦a `test_auth_keys_api.py` validation 用例:删除未使用的 `kb_id = await _create_kb(..., "范围库")` 行(若 `_create_kb` import 因此闲置则一并清)。
⑦c `mcp_server.py` 四处工具描述里跨行的"(若密钥设了范围,还须在范围内)"括注合并为单行(与 `_LIST_DOC` 的行式一致)。
⑥ `m12_acceptance.py` 审计 check(⑦ 段):查询改为拉 (action, target) 二元组并按对断言:

```python
                    pairs = set(
                        (await s.execute(
                            select(AuditLog.action, AuditLog.target).where(
                                AuditLog.action.in_([
                                    "kb_delete", "agent.upload_document"]),
                                AuditLog.target.in_([f"kb:{kb_in}",
                                                     f"doc:{doc_id}"]),
                            )
                        )).all()
                    )
            # ...(engine dispose 后)
            check("audit pairs bound to run",
                  {("kb_delete", f"kb:{kb_in}"),
                   ("agent.upload_document", f"doc:{doc_id}")} <= pairs,
                  f"pairs={sorted(pairs)}")
```

(以脚本现有结构融合:select 两列返回 Row 元组,set() 直接可交;check 名可保留原名或更新,打印带 pairs。)

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_agent_docs_api.py tests/test_kb_scope.py tests/test_auth_keys_api.py tests/test_mcp.py tests/test_kbs.py tests/test_doc_ops.py -q` → 全绿;`.venv\Scripts\python -m py_compile scripts/m12_acceptance.py` 无输出。

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/app/services/kb_ops.py backend/app/api/documents.py backend/app/api/agent.py backend/app/schemas/auth.py backend/app/services/agent_facade.py backend/app/mcp_server.py backend/tests/test_auth_keys_api.py backend/tests/test_mcp.py backend/tests/test_kbs.py backend/tests/test_kb_scope.py backend/tests/test_agent_docs_api.py backend/scripts/m12_acceptance.py
git commit -m "refactor(m13): governance quick fixes (precheck order, scope perm skip, docop unify)"
```

---

### Task 7: KB 重命名端点

**Files:**
- Modify: `backend/app/services/kb_ops.py`(rename_knowledge_base)
- Modify: `backend/app/api/kbs.py`(PUT /{kb_id})
- Modify: `backend/app/schemas/kb.py`(RenameIn)
- Test: Modify `backend/tests/test_kbs.py`(追加)

**Interfaces:**
- Consumes: Task 6 后的 kb_ops(DocOpError 风格)。
- Produces: `rename_knowledge_base(db, kb, *, name: str | None, description: str | None, username: str) -> KnowledgeBase`(唯一性检查+审计 kb_update+commit);`PUT /api/kbs/{kb_id}` body `RenameIn{name: str|None=None, description: str|None=None}` → KBOut(不可见 404 / 可见非 owner 403 / 成功 200;重名 409 文案同 create;两项皆空或 name strip 空 → 422)。前端 Task 8 消费。

- [ ] **Step 1: 写失败测试**

`tests/test_kbs.py` 追加:

```python
# ---- M13:KB 重命名 ----
async def test_rename_kb_matrix(client, auth_headers, db_session):
    from sqlalchemy import text

    created = await client.post("/api/kbs", json={"name": "原名库"},
                                headers=auth_headers)
    kb_id = created.json()["id"]
    other = await client.post("/api/kbs", json={"name": "占位库"},
                              headers=auth_headers)
    # 皆空 422 / 空名 422
    assert (await client.put(
        f"/api/kbs/{kb_id}", json={}, headers=auth_headers)).status_code == 422
    assert (await client.put(
        f"/api/kbs/{kb_id}", json={"name": "   "},
        headers=auth_headers)).status_code == 422
    # 重名 409(文案与 create 一致)
    r = await client.put(f"/api/kbs/{kb_id}",
                         json={"name": "占位库"}, headers=auth_headers)
    assert r.status_code == 409
    assert r.json()["detail"] == "knowledge base name already exists"
    # 正常改名 + 改描述 → 200,回显新名
    r2 = await client.put(f"/api/kbs/{kb_id}",
                          json={"name": "新名库", "description": "新描述"},
                          headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["name"] == "新名库"
    assert r2.json()["description"] == "新描述"
    assert r2.json()["my_perm"] == "owner"
    # 陌生人 404 / 授权 editor 403
    stranger = await _register_and_login(client, "ren_str1")
    assert (await client.put(f"/api/kbs/{kb_id}", json={"name": "x"},
                             headers=stranger)).status_code == 404
    member = await _register_and_login(client, "ren_mem1")
    mid = (await client.get("/api/auth/me", headers=member)).json()["id"]
    from app.models import KbPermission

    db_session.add(KbPermission(kb_id=kb_id, user_id=mid, perm="editor"))
    await db_session.commit()
    assert (await client.put(f"/api/kbs/{kb_id}", json={"name": "x"},
                             headers=member)).status_code == 403
    # 审计 kb_update(detail 含新旧名)
    import json as _json

    from sqlalchemy import select as _select

    from app.models import AuditLog

    db_session.expire_all()
    audit_row = (await db_session.execute(
        _select(AuditLog).where(AuditLog.action == "kb_update",
                                AuditLog.target == f"kb:{kb_id}")
    )).scalars().one()
    detail = _json.loads(audit_row.detail)
    assert detail["name"] == {"old": "原名库", "new": "新名库"}


async def test_rename_kb_integrity_fallback_409(client, auth_headers,
                                                db_session, monkeypatch):
    """并发兜底:唯一性 SELECT 恒空时 flush 撞 DB 约束仍 409。"""
    import app.services.kb_ops as kb_ops_mod
    from app.models import KnowledgeBase

    await client.post("/api/kbs", json={"name": "竞态改名库"},
                      headers=auth_headers)
    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = KnowledgeBase(name="竞态改名目标", owner_id=me.json()["id"])
    db_session.add(kb)
    await db_session.commit()
    real_select = kb_ops_mod.select

    def blind_select(*a, **k):
        return real_select(KnowledgeBase).where(KnowledgeBase.id < 0)

    monkeypatch.setattr(kb_ops_mod, "select", blind_select)
    r = await client.put(f"/api/kbs/{kb.id}",
                         json={"name": "竞态改名库"}, headers=auth_headers)
    assert r.status_code == 409
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_kbs.py -q`
Expected: FAIL——405(无 PUT 路由)。

- [ ] **Step 3: 实现**

`app/schemas/kb.py` 加:

```python
class RenameIn(BaseModel):
    """M13:KB 重命名(至少一项;name 入库前 strip)。"""

    name: str | None = None
    description: str | None = None
```

`app/services/kb_ops.py` 加(imports 需含 `select` 与 `KnowledgeBase`——既有):

```python
async def rename_knowledge_base(
    db: AsyncSession, kb: KnowledgeBase, *, name: str | None,
    description: str | None, username: str,
) -> KnowledgeBase:
    """重命名/改描述(owner/admin 由端点校验);重名 409,审计 kb_update。"""
    old_name = kb.name
    if name is not None:
        name = name.strip()
        if name and name != kb.name:
            dup = (await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.name == name)
            )).scalars().first()
            if dup is not None:
                raise DocOpError("duplicate", 409,
                                 "knowledge base name already exists")
            kb.name = name
    if description is not None:
        kb.description = description
    detail = {"name": {"old": old_name, "new": kb.name}} \
        if kb.name != old_name else {"description": "updated"}
    await audit(db, username, "kb_update", f"kb:{kb.id}", detail)
    await db.commit()
    await db.refresh(kb)
    return kb
```

(注:端点已拦 strip 后空名,service 内 `if name` 为双保险;空名直接不改。)

`app/api/kbs.py`——import 加 `RenameIn`(schemas 导入行并入)与 `from sqlalchemy.exc import IntegrityError`(若 Task 1 M12 已有则复用);`delete_kb` 端点后加:

```python
@router.put("/{kb_id}", response_model=KBOut)
async def rename_kb(
    kb_id: int,
    payload: RenameIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """M13:重命名/改描述(admin/owner;重名 409)。"""
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    perm = await get_kb_perm(db, current, kb)
    if perm is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if not has_perm(perm, "owner"):
        raise HTTPException(status_code=403,
                            detail="owner or admin required")
    name = payload.name.strip() if payload.name is not None else None
    if name == "":
        raise HTTPException(status_code=422,
                            detail="knowledge base name cannot be blank")
    if name is None and payload.description is None:
        raise HTTPException(status_code=422, detail="nothing to update")
    try:
        kb = await kb_ops.rename_knowledge_base(
            db, kb, name=name, description=payload.description,
            username=current.username)
    except IntegrityError:  # 并发兜底(同 create)
        await db.rollback()
        raise HTTPException(status_code=409,
                            detail="knowledge base name already exists")
    except kb_ops.DocOpError:
        await db.rollback()
        raise HTTPException(status_code=409,
                            detail="knowledge base name already exists")
    out = KBOut.model_validate(kb)
    out.my_perm = perm
    out.doc_count = (await db.execute(
        select(func.count(Document.id)).where(Document.kb_id == kb_id)
    )).scalar_one()
    return out
```

(DocOpError 分支直接转 409 文案——rename 只产生 duplicate 一种;rollback 必须。`select`/`func`/`Document` 已在该文件 import。)

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_kbs.py -q` → 全绿。

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv\Scripts\python -m pytest -q` → 全绿。

```bash
git add backend/app/services/kb_ops.py backend/app/api/kbs.py backend/app/schemas/kb.py backend/tests/test_kbs.py
git commit -m "feat(kb): rename endpoint with audit and integrity fallback"
```

---

### Task 8: 前端 KbPage 重命名对话框

**Files:**
- Modify: `frontend/src/api/kb.ts`(rename)
- Modify: `frontend/src/pages/KbPage.vue`(编辑对话框 + 按钮)
- Test: Modify `frontend/src/pages/__tests__/KbPage.spec.ts`(追加)

**Interfaces:**
- Consumes: Task 7 的 `PUT /api/kbs/{id}`(RenameIn 形态;409 文案同 create)。
- Produces: `kbApi.rename(kbId, payload: {name?: string; description?: string | null}): Promise<KbItem>`;KbPage owner 卡片「重命名」按钮 + 编辑对话框(name 必填/description 可选/409 内联错误)。

- [ ] **Step 1: 写失败测试**

`KbPage.spec.ts` mock 区 `kbApi` 加 `rename: vi.fn()`;新 describe:

```ts
describe('KbPage rename dialog', () => {
  const owned3: KbItem = {
    id: 12, name: '旧名库', description: '旧描述', owner_id: 1,
    embed_provider: 'fake', embed_model: 'x', my_perm: 'owner',
    doc_count: 0, created_at: '2026-09-20T10:00:00',
  }

  it('owner card opens edit dialog prefilled; submit calls rename', async () => {
    vi.mocked(kbApi.list).mockResolvedValue([owned3])
    vi.mocked(kbApi.rename).mockResolvedValue({ ...owned3, name: '新名库' })
    const w = mount(KbPage, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await w.findAll('button').find((b) =>
      b.text().trim() === '重命名')!.trigger('click')
    await flushPromises()
    const nameInput = w.find('input[placeholder="请输入知识库名称"]')
    expect((nameInput.element as HTMLInputElement).value).toBe('旧名库')
    await nameInput.setValue('新名库')
    await w.findAll('button').find((b) =>
      b.text() === '保存')!.trigger('click')
    await flushPromises()
    expect(kbApi.rename).toHaveBeenCalledWith(12, {
      name: '新名库', description: '旧描述',
    })
  })

  it('409 shows inline name error and keeps dialog open', async () => {
    vi.mocked(kbApi.list).mockResolvedValue([owned3])
    vi.mocked(kbApi.rename).mockRejectedValue({
      response: { status: 409, data: { detail: 'knowledge base name already exists' } },
    })
    const w = mount(KbPage, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await w.findAll('button').find((b) =>
      b.text().trim() === '重命名')!.trigger('click')
    await flushPromises()
    await w.find('input[placeholder="请输入知识库名称"]').setValue('重名库')
    await w.findAll('button').find((b) =>
      b.text() === '保存')!.trigger('click')
    await flushPromises()
    await vi.waitFor(() => {
      expect(w.find('.el-form-item__error').exists()).toBe(true)
    })
    expect(w.find('.el-form-item__error').text()).toContain('已存在')
  })
})
```

(编辑对话框复用创建对话框的 name placeholder 会与既有 create 用例撞——**用独立 placeholder「请输入知识库名称」若与创建对话框相同则改用对话框 title 定位**:实现里编辑对话框 title 为「重命名知识库」,测试以 `w.text()).toContain('重命名知识库')` + 最后一个该 placeholder input 定位;以实际 DOM 为准微调,断言底线:预填旧名、提交体 {name, description}、409 内联。)

- [ ] **Step 2: 跑测试确认失败**

Run:`npx vitest run src/pages/__tests__/KbPage.spec.ts`
Expected: FAIL——无重命名按钮/`kbApi.rename` 未实现。

- [ ] **Step 3: 实现**

`src/api/kb.ts`:

```ts
  /** M13:重命名/改描述(owner/admin;重名 409) */
  async rename(kbId: number, payload: KbIn): Promise<KbItem> {
    const { data } = await http.put<KbItem>(`/kbs/${kbId}`, payload)
    return data
  },
```

`KbPage.vue`——script 增(复用创建对话框的 rules):

```ts
const renameVisible = ref(false)
const renameRef = ref<FormInstance>()
const renameForm = reactive({ id: 0, name: '', description: '' })
const renameSubmitting = ref(false)
const renameServerError = ref('')

function openRename(row: KbItem) {
  renameForm.id = row.id
  renameForm.name = row.name
  renameForm.description = row.description ?? ''
  renameServerError.value = ''
  renameRef.value?.resetFields()
  renameVisible.value = true
}

async function submitRename() {
  const valid = await renameRef.value?.validate().catch(() => false)
  if (!valid) return
  renameSubmitting.value = true
  try {
    await kbApi.rename(renameForm.id, {
      name: renameForm.name.trim(),
      description: renameForm.description.trim() || null,
    })
    ElMessage.success('已更新')
    renameVisible.value = false
    await load()
  } catch (e) {
    const resp = (e as { response?: { status?: number;
      data?: { detail?: string } } })?.response
    if (resp?.status === 409) {
      renameServerError.value = '该名称已存在,请换一个名称'
    } else {
      ElMessage.error(resp?.data?.detail ?? '更新失败')
    }
  } finally {
    renameSubmitting.value = false
  }
}
```

template——卡片操作区「成员」按钮后加:

```html
          <el-button v-if="row.my_perm === 'owner'" size="small" plain
                     @click="openRename(row)">
            重命名
          </el-button>
```

页尾新增编辑对话框(title「重命名知识库」,结构与创建对话框一致:name 必填/description 可选/`:error="renameServerError"`/footer 取消+保存):

```html
    <el-dialog v-model="renameVisible" title="重命名知识库" width="480px">
      <el-form ref="renameRef" :model="renameForm" :rules="rules"
               label-position="top">
        <el-form-item label="名称" prop="name"
                      :error="renameServerError || undefined">
          <el-input v-model="renameForm.name" maxlength="128"
                    placeholder="请输入知识库名称"
                    @input="renameServerError = ''" />
        </el-form-item>
        <el-form-item label="描述" prop="description">
          <el-input v-model="renameForm.description" type="textarea" :rows="3"
                    placeholder="可选,不超过 512 字符" maxlength="512" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="renameVisible = false">取消</el-button>
        <el-button type="primary" :loading="renameSubmitting"
                   @click="submitRename">保存</el-button>
      </template>
    </el-dialog>
```

- [ ] **Step 4: 跑测试确认通过**

Run:`npx vitest run src/pages/__tests__/KbPage.spec.ts` → 全绿(含既有 7 用例)。

- [ ] **Step 5: build + Commit**

Run:`npm run build` → 零错。

```bash
git add frontend/src/api/kb.ts frontend/src/pages/KbPage.vue frontend/src/pages/__tests__/KbPage.spec.ts
git commit -m "feat(web): kb rename dialog"
```

---

### Task 9: 前端 KeysPage 加载收敛

**Files:**
- Modify: `frontend/src/pages/KeysPage.vue`(watch 收敛)
- Test: Modify `frontend/src/pages/__tests__/KeysPage.spec.ts`(追加)

**Interfaces:**
- Consumes: M12 的 `watch(() => form.userId, onBindUserChange)`/`loadKbOptions`。
- Produces: watch 仅在新值非空且 ≠ 旧值时重载+清空 kbScope;置空路径只清选项不重载。

- [ ] **Step 1: 写失败测试**

`KeysPage.spec.ts` 的 scope describe 追加:

```ts
  it('clearing bound account does not reload kb options', async () => {
    vi.mocked(useAuthStore).mockReturnValueOnce({
      user: { id: 1, username: 'boss', role: 'admin' },
    } as never)
    vi.mocked(keysApi.list).mockResolvedValue([])
    vi.mocked(usersApi.search).mockResolvedValue([{ id: 7, username: 'alice' }])
    vi.mocked(keysApi.adminUserKbs).mockResolvedValue([])
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '创建密钥').trigger('click')
    await flushPromises()
    const sels = w.findAllComponents(ElSelect)
    const bindSel = sels.find((s) =>
      s.props('placeholder') === '默认绑定当前账号')!
    await (bindSel.props('remoteMethod') as (q: string) => void)('ali')
    await flushPromises()
    await bindSel.vm.$emit('update:modelValue', 7)
    await flushPromises()
    expect(keysApi.adminUserKbs).toHaveBeenCalledTimes(1)
    // 清空绑定 → 只清选项,不再发起请求(现状会再调一次 → 先红)
    await bindSel.vm.$emit('update:modelValue', null)
    await flushPromises()
    expect(keysApi.adminUserKbs).toHaveBeenCalledTimes(1)
    expect((bindSel.props('modelValue') as unknown)).toBeNull()
  })
```

- [ ] **Step 2: 跑测试确认失败**

Run:`npx vitest run src/pages/__tests__/KeysPage.spec.ts`
Expected: FAIL——清空时 adminUserKbs 被调第二次(现状双触发)。

- [ ] **Step 3: 实现**

`KeysPage.vue` 的 watch 改(回调带新旧值判断):

```ts
watch(() => form.userId, (nv, ov) => {
  if (nv && nv !== ov) {   // M13 收敛:仅在绑定到不同账号时重载
    form.kbScope = []
    loadKbOptions()
  } else if (!nv) {        // 置空:只清选项不请求
    kbOptions.value = []
  }
})
```

(原 `onBindUserChange` 函数与 `@change` 残留若有则移除;`watch` import 已有。)

- [ ] **Step 4: 跑测试确认通过**

Run:`npx vitest run src/pages/__tests__/KeysPage.spec.ts` → 全绿(含 M12 的 admin 换绑用例——7→null 不触发,再选 7 时 nv=7≠ov=null 触发重载)。

- [ ] **Step 5: build + Commit**

Run:`npm run build` 零错;`npx vitest run` 全量全绿。

```bash
git add frontend/src/pages/KeysPage.vue frontend/src/pages/__tests__/KeysPage.spec.ts
git commit -m "fix(web): kb options reload only on account switch"
```

---

### Task 10: m13 无头验收 + .env.example + dev 栈迁移

**Files:**
- Create: `backend/scripts/m13_acceptance.py`
- Modify: `backend/.env.example`(两个新配置)
- (dev 栈操作,无新代码)

**Interfaces:**
- Consumes: Task 1-9 全部;dev 栈(8001)+ worker + Redis + 智谱真 key。
- Produces: 真栈验收判定;延迟数字记录(入执行记录)。

- [ ] **Step 1: dev 栈迁移与配置**

```bash
cd E:\Projects\AIRag\backend
.venv\Scripts\python -m alembic current    # 应显示 d4e5f6a7b8c9
.venv\Scripts\python -m alembic upgrade head  # 应用 e5f6a7b8c9d0
```

`.env.example` 检索/多跳配置块加:

```
REFUSAL_RECHECK_ENABLED=true
GRADE_CONFIDENT_SKIP_N=3
```

重启 dev 栈(杀 8001 监听 PID + 按命令行定位 reload 孤儿子进程补杀——M12 备忘;start_dev.bat + start_worker.bat);`curl http://127.0.0.1:8001/api/health` 200。**跑长验收前确认无待触发 reload**。

- [ ] **Step 2: 写 m13_acceptance.py**

骨架照抄 `m12_acceptance.py`(SUFFIX/check/summary_and_exit/_nullpool_sessionmaker/make_user/promote_roles/cleanup 复用;kb 名带 SUFFIX;users/kbs 记账)。判定(以实际打印为准,目标 ≥12 check):

1. 建 u1(editor)+ kb_in;python-docx 上传含 `FACT=白鲸灯塔编号BJ-{SUFFIX}的塔高八十八米` 的文档(Web 面,轮询 done)——m12 同款
2. **拒答二审(真 LLM)**:`POST /api/ask` SSE 流式问两条库内无答案的问题——`f"彗星捕手计划{SUFFIX}的发射窗口是哪天?"` 与 `f"红楼梦{SUFFIX}的作者是谁?"`;解析 SSE 首帧 `{"event":"done"}`(ask.py 的 done 帧结构,读 `data:` JSON)断言 `refused is True`(canonical 或包裹措辞均可,二审兜底)
3. **正常事实题**:`FACT_Q=白鲸灯塔编号BJ-{SUFFIX}的塔高多少米?` → refused False 且 answer 含"八十八"或 citations 非空
4. **延迟记录**:FACT_Q 一次 SSE 计时——记录 `首 token 时刻 - 请求发出` 与 `done - 发出`(打印,不判 PASS/FAIL,标注 `[info]`)
5. **评估入库**:`subprocess` 或直接 import 跑 `eval_retrieval --kb {kb_in} --save` 与 `eval_generation --kb {kb_in} --save`(eval_sets/{kb_in}.json 由脚本现场写入:2 题——FACT_Q(expect_doc_ids=[doc_id], expect_keywords=["八十八"], reference_answer="八十八米")+ 一条零命中题);随后 `eval_runs.query(kb_in)` 断言 ≥2 条且 generation run 的 summary 含 `faithfulness_avg` 与 `reference_avg` 键
6. **KB 重命名**:PUT 改名 200(回显新名)→ 重名 409 → 审计 kb_update 落库(target 绑定)
7. **快修②出口**:agent 面 editor key 对不可见库 POST 超限 body(2MB,MAX_UPLOAD_MB 为真值 20——预期 CL 21MB 门槛不触发,**改用 monkeypatch 不可行(真栈),构造 21MB body 代价大** → 改为只验 404 语义:不可见库 + 普通 body → 404(既有回归),413 顺序留单测护栏,验收不重复)
8. cleanup:users/kbs;eval_sets/{kb_in}.json 现场文件删除;eval_runs 行保留(历史记录设计意图,打印 `[info] eval run rows kept: N`)

- [ ] **Step 3: 跑验收**

Run:`.venv\Scripts\python scripts/m13_acceptance.py` → 全 PASS 退出码 0;回归 `m12_acceptance.py`(含快修⑥新配对断言)全 PASS、`m11_acceptance.py` 全 PASS。

- [ ] **Step 4: 全量回归 + Commit**

Run:后端 `.venv\Scripts\python -m pytest -q` 全绿;前端 vitest 全量 + build 零错(T8/T9 已验,未再动则免)。

```bash
git add backend/scripts/m13_acceptance.py backend/.env.example
git commit -m "test(m13): acceptance script and env example"
```

---

## 执行后(收官清单,不属于单任务)

1. 本文件追加执行记录(裁决/勘误/延迟数字)。
2. 用户走查(M12+M13 合并):KeysPage 范围/收敛、KbPage 删除+重命名、拒答二审样例问、eval_runs 查询演示。
3. 走查通过后 `git push`;更新项目记忆。

## Self-Review 记录

- **Spec 覆盖**:A→T1;B→T2/T3(模型+CLI+reference_score+README);C→T4(+T10 延迟记录);D→T5;E①→T6①、②→T6、③→T6、④→T6、⑤→T9、⑥→T6、⑦→T6(⑦c 排版)/T9 无关——⑦c 在 T6;F→T7/T8;配置→T1/T4/T10;验收→T10。无缺口。
- **占位符扫描**:T6 ⑦b 与 T8 Step 1 的"以实际 DOM/现有结构融合"是对既有用例结构的显式适配指令,非 TBD(断言底线已写明);其余无。
- **类型一致性**:`_recheck_refusal(llm, question, answer) -> bool | None`、`_search_one(query, kb_ids) -> list`、`save_run(kb_id, mode, results) -> int`、`reference_score(llm, question, answer, reference) -> dict`、`rename_knowledge_base(db, kb, *, name, description, username)`、`kbApi.rename(kbId, payload)` 前后端一致;EvalItem 字段名与 T3 落库键一致。

---

## 执行记录(2026-09-20,SDD)

**提交链**(spec 525fa0e → 计划 7d04488):T1 a3c1215 → T2 f60bba3 → T3 dcde338 → T4 d4e3592 → T5 ceb5942 → T6 4ce92b7 → T7 9c4a475 → T8 129624a → T9 f49defc → T10 3c81cd6+1da9580(+修复轮 ff623f7)→ 终审修复波 982f3a9。

**质量**:10 任务中 9 个零修复轮一次 Approved;T10 经 1 轮修复(补回归测试);终审 FINDINGS 2 Important——①RenameIn 长度校验缺失(超长名 500)已由修复波 982f3a9 修复(rename 面与 create 面一致 422);②见下"记载补正"。

**测试与验收**:后端 pytest 276P→**298P/0F**;前端 vitest 28→**31/31** + build 零错;真栈 `m13_acceptance.py` **13/13**(真 LLM 拒答二审两题 refused、事实题不拒答、评估入库 4 项、重命名矩阵、m12 子验);m12 子验收 **16/16**(含 T6⑥ 配对断言真栈补验);m11 回归 10/10;dev 栈迁移 `d4e5f6a7b8c9→e5f6a7b8c9d0` 一次通过。**延迟 [info]:SSE first-token=4.1s / total=7.0s**(单轮事实题,快乐路径 LLM 3→2 次生效)。

**验收首跑暴露并修复的两个 T3 真产品 bug**(3c81cd6):①eval CLI `--save` 双 `asyncio.run` 复用池化连接绑死已关闭事件循环(Windows 必崩)→ 单次 asyncio.run(`_amain`);②`summarize` 对 `"reference": None` 崩溃 → `or {}` 防护(修复轮 ff623f7 补回归用例锁定)。

**⚠️ 记载补正(终审 Important#2,推翻本计划 T4 与台账的两处失实表述)**:零命中 grade 短路对**生产路径**(CRAG/MULTI_HOP 默认开)同样改变了图流——原 grade_node 空命中返回 `{}` 直达 decompose,**并非"零命中仍烧一次 grade LLM"**(计划该前提失实);M13 后空命中 → insufficient → 先 transform(以原始 question 重检索一轮,retries 0→1)→ 再 decompose。行为无害且可能捞回改写漏检(retries 护栏、终止性、LLM 次数、验收全过),但 spec C1"路由逐字不变"与 T4 任务审查的"仅影响测试配置"裁决均与事实不符,以本节为准。M14 可选微调:空命中且 retries==0 时保留旧 `{}` 语义。

**实施期裁决要点**(全部经任务审查者独立核验;详见 SDD 台账,已随收官清理则以本节为准):
- T2:`EvalRun.items` 加 `lazy="selectin"`(默认 lazy 在 async expire_all 后必 MissingGreenlet)。
- T4:短路置于 CRAG 开关之前(测试需在 conftest 配置下生效);既有空命中用例断言适配 `{}`→insufficient。
- T5:fake 改返 SearchHit(合并段属性访问,计划笔误);既有 patch hybrid_search 零适配(运行时模块全局解析)。
- T8:`.at(-1)`→`.slice(-1)[0]`(tsconfig lib:[] 覆盖);`resetFields()` 提前修掉二次打开预填回滚真 bug。
- T9:红态断言对象 adminUserKbs→kbApi.list(清空时实际走自服务分支)。
- T10:SSE 实端点 `POST /api/chat/ask`(body `question`,data-JSON 帧——计划写 `/api/ask`/`query` 系笔误);cleanup 扩展 conversations/messages;`.env.example` 在仓库根非 backend/。

**M14 候选**(终审 triage 全部 ride):二审 judge prompt 定界符(注入面);gather 孤儿任务日志噪音;前端 description 置空语义(产品决策);Web 面预检越界组合用例;并行测试 hash 碰撞加固;`.env.example` 注释措辞;CLI `_amain` 单测(装配路径零覆盖);saved: 行改 stderr;T7 同名无变更审计 detail;其余台账 minors。旧候选:评估 Web 管理界面、出站集成、A2A、MinerU 本地化、LDAP/SSO(仍等输入)。

