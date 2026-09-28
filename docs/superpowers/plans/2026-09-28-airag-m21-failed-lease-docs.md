# AIRag M21 实施计划:failed 条件收口 + 心跳租约 + 文档精装

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清掉 M20 终审 triage 五候选 + 走查通道引导文案:异常路径 failed 条件收口、eval 心跳租约、webhooks.md 精装、空 commit 早返回、测试 import 上提、前端通道引导。

**Architecture:** ①`_finalize_run` 加 `error` 参数,异常路径删 ORM 无条件终态写,failed/completed 同享「cancelling 兜底用户赢」结构,两跳全零行提前返回不空 commit;②eval_runs 加 `heartbeat_at` 列(naive UTC),任务逐题续租,sweep 只收 stale(NULL 或超宽限),并注册 beat 60s 周期兜底;③webhooks.md 头部刷新+逐事件负载示例;④前端 PROVIDER_HELP 按通道联动引导。

**Tech Stack:** FastAPI + SQLAlchemy async + Alembic + Celery beat(后端)/ Vue3 + Element Plus + vitest(前端)。

**Spec:** `docs/superpowers/specs/2026-09-28-airag-m21-failed-lease-docs-design.md`

## Global Constraints

- 测试一律 `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest …`(conftest 自指测试库 airag_test、fake 嵌入);vitest 一律 `cd E:\Projects\AIRag\frontend && pnpm vitest run …`。
- 迁移链头 = `b0c1d2e3f4a5`(m18);新迁移 `down_revision = "b0c1d2e3f4a5"`。
- conftest 用 `Base.metadata.create_all` 建测试库,模型加列即生效;迁移只服务 dev/prod 库。
- 时间戳列全链 naive UTC(单一来源 `app/core/timeutil.utcnow_naive()`,asyncpg 拒 aware)。
- 每任务一个 commit,消息用 conventional commit(中文冒号后的简述可中文)。
- 既有基线:pytest 489P / vitest 84 / build 零错——只增不减。
- `nudge()` 只在 emit 出事件(n>0)后调用;事件与终态同事务 commit(M17 哲学,不许拆)。

---

### Task 1: 测试局部 import 上提(候选⑤,先铺平)

**Files:**
- Modify: `backend/tests/test_eval_cancel.py`(头部 import 区 + 9 处函数内 import 删除)

**Interfaces:**
- Consumes: 无(纯机械重构)。
- Produces: 模块级 `runner`(eval_runner 模块对象)、`run_eval_task`、`_finalize_run`、`_recover_orphan_runs` 供后续任务新测试直接使用。

- [ ] **Step 1: 上提 import**

在 `backend/tests/test_eval_cancel.py` 头部(`import time` 与 `from sqlalchemy import …` 之后、`from app.models import …` 附近)加:

```python
import app.services.eval_runner as runner
from app.services.eval_runner import _finalize_run, run_eval_task
from app.workers.eval_tasks import _recover_orphan_runs
```

然后删除函数内的 9 处局部 import(全部内容为以下三行的重复):

```python
    import app.services.eval_runner as runner
    from app.services.eval_runner import run_eval_task
    from app.services.eval_runner import _finalize_run
    from app.workers.eval_tasks import _recover_orphan_runs
```

函数体内对这些名字的使用不变(名字相同)。注意 `_flip_guard` 内的 `import app.api.eval as ev` **保留**(它 monkeypatch 的是 API 模块,与本次候选无关)。

- [ ] **Step 2: 跑全文件验证**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_cancel.py -v`
Expected: 全部 PASS(既有 13 个用例,无 import 顺序问题)。

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_eval_cancel.py
git commit -m "test: hoist eval_cancel local imports to module level (m20 triage)"
```

---

### Task 2: _finalize_run error 参数 + 异常路径条件收口 + None 早返回(候选①+④)

**Files:**
- Modify: `backend/app/services/eval_runner.py:162-203`(`_finalize_run` 全函数)
- Modify: `backend/app/services/eval_runner.py:274-288`(`run_eval_task` 异常分支)
- Test: `backend/tests/test_eval_cancel.py`(追加 3 个用例)

**Interfaces:**
- Consumes: Task 1 的模块级 import(`_finalize_run`、`runner`、`run_eval_task`)。
- Produces: `_finalize_run(db, run_id, kb_id, mode, results, cancelled, error=None) -> tuple[str | None, int]`;`error` 非空 → final="failed"(写 `run.error` 列,事件 `eval.failed` 负载含 `run{…, item_count, summary}` + `error`)。异常撞并发取消 → 用户赢收口 cancelled + `eval.cancelled`。两跳全零行 → `(None, 0)` 提前返回,不 commit。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_eval_cancel.py` 的 `test_finalize_cancelled_emits_cancelled_event` 之后追加(沿用 `_mk_bare_run` 与全订阅端点模式):

```python
async def test_finalize_failed_only_from_running(db_session):
    """M21:failed 条件写——只落在仍是 running 的行;run 形状与 completed
    统一(item_count/summary),error 字段保留。"""
    run_id = await _mk_bare_run(db_session, "running")
    db_session.add(WebhookEndpoint(
        name=f"fl{time.time_ns()}", url="http://x/h", secret="wh_s",
        events=[], created_by=1))
    await db_session.commit()
    final, n = await _finalize_run(db_session, run_id, 1, "retrieval",
                                   [{"question": "q"}], False, error="炸了")
    assert final == "failed" and n == 1
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "failed" and run.error == "炸了"
    assert run.item_count == 1 and run.summary["item_count"] == 1
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert [r.event_type for r in rows] == ["eval.failed"]
    d = rows[0].payload["data"]
    assert d["error"] == "炸了"
    assert d["run"]["item_count"] == 1 and "summary" in d["run"]


async def test_finalize_failed_raced_cancel_wins(db_session):
    """异常撞上并发取消:行已被端点置 cancelling → 用户赢,收口 cancelled、
    发 eval.cancelled 而非 eval.failed,run.error 不写(取消非失败)。"""
    run_id = await _mk_bare_run(db_session, "cancelling")
    db_session.add(WebhookEndpoint(
        name=f"fr{time.time_ns()}", url="http://x/h", secret="wh_s",
        events=[], created_by=1))
    await db_session.commit()
    final, n = await _finalize_run(db_session, run_id, 1, "retrieval",
                                   [{"question": "q"}], False, error="炸了")
    assert final == "cancelled" and n == 1
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "cancelled" and run.error is None
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert [r.event_type for r in rows] == ["eval.cancelled"]


async def test_run_task_exception_failed_via_finalize(
        client, auth_headers, db_session, monkeypatch):
    """M21:run_eval_task 异常路径经 _finalize_run 条件收口——不再 ORM
    无条件写;部分结果照写 summary(数据诚实,与取消同语义)。"""
    run_id = await _mk_run(client, auth_headers, db_session, n=2)
    calls = {"n": 0}

    async def flaky(db, kb_id, q, top_k, reranker):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("第二题炸")
        return {"question": q.question, "hit_at_k": None, "mrr": None,
                "keyword_recall": None}

    monkeypatch.setattr(runner, "retrieval_item", flaky)
    monkeypatch.setattr(runner, "nudge", lambda: None)
    await run_eval_task(run_id, "retrieval", False, 8)
    db_session.expire_all()
    run = (await db_session.execute(
        select(EvalRun).where(EvalRun.id == run_id))).scalar_one()
    assert run.status == "failed"
    assert "第二题炸" in run.error
    assert run.item_count == 1 and run.summary["item_count"] == 1
    items = (await db_session.execute(
        select(EvalItem).where(EvalItem.run_id == run_id))).scalars().all()
    assert len(items) == 1  # 第 1 题已逐题落库,不受异常影响
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_cancel.py -k "failed_only or raced_cancel or exception_failed" -v`
Expected: FAIL(`_finalize_run() got an unexpected keyword argument 'error'` / 旧 ORM 路径 summary 缺失)。

- [ ] **Step 3: 实现**

`_finalize_run` 整体替换为:

```python
async def _finalize_run(db: AsyncSession, run_id: int, kb_id: int,
                        mode: str, results: list[dict],
                        cancelled: bool, error: str | None = None,
                        ) -> tuple[str | None, int]:
    """M20 条件收口:终态只经条件 UPDATE 落库,消端点/任务丢更新竞态。
    M21:failed 同入此口——异常路径最后一个 ORM 无条件终态写删除。

    completed/failed 只许写在仍是 running 的行上(端点在最后检查点后
    并发置 cancelling 时零行命中,fallback 按用户已赢收口 cancelled——
    failed 撞上并发取消同样用户赢);cancelled 写在 cancelling 上。
    两跳全零行=行已被 sweep 等他人收口,不写不发(返回 None;空事务
    无 pending 变更,提前返回不走 commit)。事件按 final 分流,同事务
    commit;返回 (final, n) 供调用方 nudge。"""
    from sqlalchemy import update

    from app.models import EvalRun

    summary = summarize(results)
    item_count = len(results)
    final = "failed" if error else ("cancelled" if cancelled else "completed")
    expect = "cancelling" if cancelled else "running"
    values = dict(status=final, summary=summary, item_count=item_count)
    if error:
        values["error"] = error  # 仅 failed 落 error 列;取消非失败
    res = await db.execute(
        update(EvalRun)
        .where(EvalRun.id == run_id, EvalRun.status == expect)
        .values(**values))
    if res.rowcount == 0 and not cancelled:
        # A1 竞态:最后检查点后端点已置 cancelling——用户已赢(failed 同享)
        res = await db.execute(
            update(EvalRun)
            .where(EvalRun.id == run_id, EvalRun.status == "cancelling")
            .values(status="cancelled", summary=summary,
                    item_count=item_count))
        final = "cancelled" if res.rowcount else None
    elif res.rowcount == 0:
        final = None  # 行已不在 cancelling(他人收口),不写不发
    if final is None:
        return None, 0  # 空事务:零命中零事件,commit 无意义
    data = {"run": {"id": run_id, "kb_id": kb_id, "mode": mode,
                    "item_count": item_count, "summary": summary}}
    if final == "completed":
        n = await emit_event(db, "eval.completed", data)  # M17
    elif final == "cancelled":
        n = await emit_event(db, "eval.cancelled", data)  # M20
    else:
        data["error"] = error  # M21:eval.failed 的 run 形状与 completed 统一
        n = await emit_event(db, "eval.failed", data)  # M17
    await db.commit()
    return final, n
```

`run_eval_task` 异常分支整体替换为:

```python
            except Exception as e:
                # 先 rollback 丢弃未提交脏状态:异常可能源自 DB 操作本身
                # (逐题 commit/flush 失败、连接中断),session 处于
                # PendingRollback 时直接 commit 会二次抛异常、逃出函数,
                # run 永远停在 running——状态机必须兜住。已逐题 commit 的
                # items 不受影响(它们已落库)。
                await db.rollback()
                # M21:failed 也走条件收口(并发取消用户赢→cancelled),
                # 最后一个 ORM 无条件终态写删除;kb_id 是 M17 快照
                final, n = await _finalize_run(
                    db, run_id, kb_id, mode, results, False,
                    error=str(e)[:500])
                if n:
                    nudge()
```

(`final` 变量此后不再使用,名字保留供读代码;若 linter 报未使用改为 `_final`。)

- [ ] **Step 4: 跑新测试 + 相关存量**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_cancel.py tests\test_webhook_events.py tests\test_eval_runner.py tests\test_eval_task.py -v`
Expected: 全 PASS(含 `test_eval_failed_emits`——其断言 `"炸" in payload.data.error` 与 run 增字段兼容)。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/eval_runner.py backend/tests/test_eval_cancel.py
git commit -m "fix(eval): failed closes via conditional update; raced cancel wins; no empty commit (m20 triage)"
```

---

### Task 3: timeutil 助手 + heartbeat 列 + 迁移 + 逐题续租(候选②上)

**Files:**
- Create: `backend/app/core/timeutil.py`
- Create: `backend/alembic/versions/c2d3e4f5a6b7_m21_eval_runs_heartbeat.py`
- Modify: `backend/app/models/eval.py`(EvalRun 加列)
- Modify: `backend/app/services/outbound.py:34-37`(`_utcnow_naive` 改薄委托)
- Modify: `backend/app/services/eval_runner.py`(两循环逐题续租)
- Test: `backend/tests/test_eval_cancel.py`(追加 1 用例)

**Interfaces:**
- Consumes: 无前置依赖(与 Task 2 独立,但按序执行)。
- Produces: `app.core.timeutil.utcnow_naive() -> datetime`(naive UTC);`EvalRun.heartbeat_at: Mapped[datetime | None]`;outbound 保留 `_utcnow_naive` 名(薄委托,既有 import/文档引用不破)。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_eval_cancel.py` 追加(文件头部补 `from datetime import timedelta` 与 `from app.core.timeutil import utcnow_naive`):

```python
async def test_heartbeat_renewed_per_item(client, auth_headers,
                                          db_session, monkeypatch):
    """M21 租约:逐题续签 heartbeat_at(naive UTC,随逐题 commit 落库);
    全部题跑完后心跳不应早于宽限。"""
    run_id = await _mk_run(client, auth_headers, db_session, n=2)
    before = utcnow_naive()
    fake, _ = _fake_retrieval_item(run_id, flip_at=99)  # 不触发取消
    monkeypatch.setattr(runner, "retrieval_item", fake)
    await run_eval_task(run_id, "retrieval", False, 8)
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "completed"
    assert run.heartbeat_at is not None
    assert run.heartbeat_at >= before  # 续租发生在观测点之后
    assert (utcnow_naive() - run.heartbeat_at).total_seconds() < 60
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_cancel.py -k heartbeat -v`
Expected: FAIL(`AttributeError: 'EvalRun' object has no attribute 'heartbeat_at'`,create_all 建出的表无此列)。

- [ ] **Step 3: 实现**

新建 `backend/app/core/timeutil.py`:

```python
"""naive UTC 时间助手:无 tz 的 TIMESTAMP 列(webhook next_attempt_at、
eval heartbeat_at)写入与比较全链路同源无歧义(asyncpg 拒 aware 入参;
自 outbound._utcnow_naive 约定提升为公共助手,M21)。"""
from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
```

`backend/app/models/eval.py`:import 区加 `from datetime import datetime` 与 `from sqlalchemy import DateTime`;EvalRun 加列(放在 `error` 之后):

```python
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime)  # M21 租约:任务逐题续签(naive UTC);NULL=stale 兼容存量
```

`backend/app/services/outbound.py`:`_utcnow_naive` 函数体替换为薄委托(import 区加 `from app.core.timeutil import utcnow_naive`):

```python
def _utcnow_naive() -> datetime:
    """M21 起真身在 app.core.timeutil(heartbeat 同源);保留名字兼容
    既有引用与文档。"""
    return utcnow_naive()
```

`backend/app/services/eval_runner.py`:模块顶 import 区加 `from app.core.timeutil import utcnow_naive`;retrieval 与 generation 两循环中,在 `db.add(EvalItem(run_id=run.id, **item_kwargs(r)))` 之前各加一行:

```python
                        run.heartbeat_at = utcnow_naive()  # M21 租约续签(同 commit,零额外往返)
```

(注意两处循环都要加;generation 循环同样位置。)

新建迁移 `backend/alembic/versions/c2d3e4f5a6b7_m21_eval_runs_heartbeat.py`(照 m18 文件风格):

```python
"""m21 eval_runs: heartbeat_at

Revision ID: c2d3e4f5a6b7
Revises: b0c1d2e3f4a5
Create Date: 2026-09-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b0c1d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("eval_runs", sa.Column("heartbeat_at", sa.DateTime(),
                                         nullable=True))


def downgrade() -> None:
    op.drop_column("eval_runs", "heartbeat_at")
```

- [ ] **Step 4: 跑测试 + dev 库迁移**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_cancel.py tests\test_outbound.py tests\test_admin_webhooks.py -v`
Expected: 全 PASS。

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m alembic upgrade head`
Expected: 输出 `Running upgrade b0c1d2e3f4a5 -> c2d3e4f5a6b7`(dev 库加列;可空加列对在跑栈安全)。

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/timeutil.py backend/app/models/eval.py backend/app/services/outbound.py backend/app/services/eval_runner.py backend/alembic/versions/c2d3e4f5a6b7_m21_eval_runs_heartbeat.py backend/tests/test_eval_cancel.py
git commit -m "feat(eval): heartbeat lease — per-item renewal + naive-utc timeutil + migration (m21)"
```

---

### Task 4: sweep stale-only + beat 周期兜底(候选②下)

**Files:**
- Modify: `backend/app/workers/eval_tasks.py:28-70`(sweep 判定 + 新任务 + 返回值)
- Modify: `backend/app/workers/celery_app.py:19-28`(beat_schedule)
- Modify: `backend/app/core/config.py`(新设置)
- Test: `backend/tests/test_eval_cancel.py`(追加 2 用例 + 改 1 存量断言)

**Interfaces:**
- Consumes: Task 3 的 `EvalRun.heartbeat_at` 与 `utcnow_naive`。
- Produces: 设置 `EVAL_HEARTBEAT_GRACE_MINUTES: int = 10`;celery 任务 `app.workers.eval_tasks.sweep_orphan_runs`(beat 60s);`_recover_orphan_runs() -> int`;孤儿 error 文案改为 `"orphaned: heartbeat expired (worker died or restarted)"`。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_eval_cancel.py` 追加:

```python
async def test_sweep_skips_fresh_heartbeat(db_session):
    """M21 租约:心跳新鲜的在途行是活任务,重启/周期 sweep 都不收口;
    NULL 与超宽限行照收(存量兼容)。"""
    db_session.add_all([
        EvalRun(kb_id=1, mode="retrieval", summary=None, item_count=0,
                status="running", heartbeat_at=utcnow_naive()),
        EvalRun(kb_id=2, mode="retrieval", summary=None, item_count=0,
                status="cancelling",
                heartbeat_at=utcnow_naive() - timedelta(minutes=30)),
        EvalRun(kb_id=3, mode="retrieval", summary=None, item_count=0,
                status="running", heartbeat_at=None),
    ])
    await db_session.commit()
    _recover_orphan_runs()
    db_session.expire_all()
    runs = (await db_session.execute(
        select(EvalRun).order_by(EvalRun.id))).scalars().all()
    assert [r.status for r in runs] == ["running", "failed", "failed"]


def test_beat_schedule_registers_orphan_sweep():
    from app.workers.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["eval-orphan-sweep"]
    assert entry["task"] == "app.workers.eval_tasks.sweep_orphan_runs"
    assert entry["schedule"] == 60.0
```

同时改存量 `test_sweep_collects_cancelling` 的断言(文案更新):

```python
    assert runs[0].error == "orphaned: heartbeat expired (worker died or restarted)"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_cancel.py -k "sweep or beat_schedule" -v`
Expected: FAIL(fresh 行被收口为 failed;beat_schedule 无 "eval-orphan-sweep" 键;error 文案不匹配)。

- [ ] **Step 3: 实现**

`backend/app/core/config.py`(webhook 设置附近)加:

```python
    # M21 租约宽限:评估单题(三次 LLM 裁决)远小于此;心跳早于此=孤儿
    EVAL_HEARTBEAT_GRACE_MINUTES: int = 10
```

`backend/app/workers/eval_tasks.py`:模块顶加 `from datetime import timedelta`;`_sweep_orphan_runs` / `_recover_orphan_runs` / 新任务整体替换为:

```python
async def _sweep_orphan_runs() -> int:
    """在途(status='running' 或 'cancelling',M19 T3)且心跳过期(M21
    租约)的 EvalRun 收口为 failed,返回受影响行数。

    M21 前:solo 池 worker_ready 瞬间无在途任务,无条件收口成立;多
    worker 前必须能区分「活任务」与「孤儿」——任务逐题续签 heartbeat_at,
    本函数只收 stale(NULL 或早于宽限;NULL 兼容存量行与「已建未开跑」
    孤儿)。除 worker_ready 外,beat 60s 周期兜底:worker 崩溃后孤儿
    不再「只能等下次重启」,≤ 宽限+间隔内必被收口(强于 M15 现状)。
    DB 访问同 pipeline._mark_failed 模式:自持 NullPool 引擎,用完
    dispose(不与 API/worker 常驻引擎共享连接池)。"""
    from sqlalchemy import or_, update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.config import settings
    from app.core.timeutil import utcnow_naive
    from app.models import EvalRun

    engine = _engine(settings.DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            cutoff = utcnow_naive() - timedelta(
                minutes=settings.EVAL_HEARTBEAT_GRACE_MINUTES)
            result = await session.execute(
                update(EvalRun)
                .where(EvalRun.status.in_(("running", "cancelling")),
                       or_(EvalRun.heartbeat_at.is_(None),
                           EvalRun.heartbeat_at <= cutoff))
                .values(
                    status="failed",
                    error="orphaned: heartbeat expired (worker died or restarted)",
                )
            )
            await session.commit()
            return result.rowcount
    finally:
        await engine.dispose()


def _recover_orphan_runs() -> int:
    """信号处理器本体;独立成可直调函数供测试调用(信号在 pytest 不触发)。"""
    n = _run_async(_sweep_orphan_runs())
    if n:
        logger.info(f"recovered {n} orphaned running eval run(s) on worker start")
    return n


@celery_app.task(name="app.workers.eval_tasks.sweep_orphan_runs",
                 ignore_result=True)
def sweep_orphan_runs() -> int:
    """beat 60s 周期兜底(worker_ready 之外的第二个触发面)。"""
    return _recover_orphan_runs()
```

`backend/app/workers/celery_app.py` beat_schedule 追加(webhook-delivery-scan 之后):

```python
        "eval-orphan-sweep": {
            "task": "app.workers.eval_tasks.sweep_orphan_runs",
            "schedule": 60.0,
        },
```

- [ ] **Step 4: 跑测试**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_cancel.py -v`
Expected: 全 PASS(存量 `test_sweep_collects_cancelling` 的行 heartbeat 为 NULL,照收不破)。

- [ ] **Step 5: Commit**

```bash
git add backend/app/workers/eval_tasks.py backend/app/workers/celery_app.py backend/app/core/config.py backend/tests/test_eval_cancel.py
git commit -m "feat(eval): sweep closes stale heartbeats only; beat-scheduled orphan sweep (m21)"
```

---

### Task 5: docs/webhooks.md 头部刷新 + 逐事件负载示例(候选③)

**Files:**
- Modify: `docs/webhooks.md`(第 3 行头部 + 新增「事件负载示例」节,插在「事件与订阅」之后)

**Interfaces:**
- Consumes: Task 2 定型的 `eval.failed` 负载形状(run 含 item_count/summary)。
- Produces: 文档事实与代码一致(无代码依赖)。

- [ ] **Step 1: 改头部**

第 3 行 `(AIRag M17/M18;admin 在「出站推送」页配置端点)` 替换为:

```markdown
(AIRag M17 引入,M18 平台适配+per-KB 订阅,M19 密钥生命周期+出站加固,
M20 新增 eval.cancelled,M21 统一 eval.failed 负载形状;admin 在「出站推送」页配置端点)
```

- [ ] **Step 2: 加逐事件负载示例节**

在「## 事件与订阅」小节末尾(at-least-once 行之后、「## 通用端点」之前)插入:

````markdown
### 事件负载示例(generic 通道全量信封的 `data` 字段)

信封外层恒为 `{"event_id", "event_type", "occurred_at", "data"}`;
以下示例均为 `data` 内容。平台通道收到的是中文 markdown 摘要,非全量信封。

- **document.done**

  ```json
  {"document": {"id": 42, "kb_id": 3, "filename": "预算说明.pdf",
                "chunk_count": 17}}
  ```

- **document.failed**

  ```json
  {"document": {"id": 43, "kb_id": 3, "filename": "扫描件.pdf"},
   "error": "OCR 空结果"}
  ```

- **eval.completed / eval.cancelled**(cancelled 的 `item_count` 是取消前
  已完成子集,非全量)

  ```json
  {"run": {"id": 7, "kb_id": 3, "mode": "retrieval", "item_count": 12,
           "summary": {"item_count": 12, "hit": 0.9167, "mrr": 0.8611,
                       "keyword_recall": 0.8333}}}
  ```

  generation 模式的 summary 形状:`{"item_count", "faithfulness_avg",
  "relevancy_avg", "refused_count", "reference_avg"(未设参考答案则缺席)}`;
  未测量的字段缺席,不落 0(「未测量≠零分」,M16 语义)。

- **eval.failed**(M21 起 `run` 形状与 completed 统一,含部分汇总)

  ```json
  {"run": {"id": 8, "kb_id": 3, "mode": "generation", "item_count": 5,
           "summary": {"item_count": 5, "faithfulness_avg": 0.82,
                       "relevancy_avg": 0.9, "refused_count": 1}},
   "error": "ZHIPU_API_KEY 未配置,生成评估无法执行"}
  ```

- **chat.refused**(`source`:`web` 网页 / `rest` API Key / `mcp` MCP 工具)

  ```json
  {"source": "rest", "kb_ids": [3, 5], "question": "竞品价格是多少"}
  ```
````

- [ ] **Step 3: 核对文档与代码零漂移**

逐一对照 emit_event 调用点(`pipeline.py:55,142` / `eval_runner.py` `_finalize_run` / `ask.py:113` / `agent.py:162` / `mcp_server.py:166`)的字段名;确认无编造字段。

- [ ] **Step 4: Commit**

```bash
git add docs/webhooks.md
git commit -m "docs(webhooks): refresh header lineage; add per-event payload examples (m20 triage)"
```

---

### Task 6: 前端通道引导文案(走查候选)

**Files:**
- Modify: `frontend/src/pages/WebhooksPage.vue`(PROVIDER_OPTIONS 之后加映射;模板 561 行静态帮助换联动)
- Test: `frontend/src/pages/__tests__/WebhooksPage.spec.ts`(追加 1 用例)

**Interfaces:**
- Consumes: 既有 `PROVIDER_OPTIONS` / `form.provider`。
- Produces: `PROVIDER_HELP: Record<string, string>`;模板消费 `PROVIDER_HELP[form.provider]`。

- [ ] **Step 1: 写失败测试**

在 `WebhooksPage.spec.ts` 的 provider 联动用例之后追加:

```typescript
  it('通道引导文案随 provider 联动(wecom 含群机器人与误报提示)', async () => {
    const w = mountPage()
    await flushPromises()
    await findBtn(w, '新建端点').trigger('click')
    await flushPromises()
    const sel = w.getComponent('.provider-select') as never as {
      vm: { $emit: (e: string, v: unknown) => void }
    }
    // generic:HMAC + 仅看 HTTP 状态码
    expect(w.text()).toContain('HMAC')
    ;(sel.vm as never as { $emit: (e: string, v: unknown) => void })
      .$emit('update:modelValue', 'wecom')
    await flushPromises()
    expect(w.text()).toContain('群机器人')
    expect(w.text()).toContain('误报成功') // 09-28 走查:generic 接企微的坑
    ;(sel.vm as never as { $emit: (e: string, v: unknown) => void })
      .$emit('update:modelValue', 'dingtalk')
    await flushPromises()
    expect(w.text()).toContain('加签密钥')
  })
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\frontend && pnpm vitest run src\pages\__tests__\WebhooksPage.spec.ts`
Expected: FAIL(现静态帮助无「HMAC」等文案)。

- [ ] **Step 3: 实现**

`WebhooksPage.vue` 的 `URL_PLACEHOLDER` 定义之后加:

```typescript
// M21:按通道联动的引导——generic 只看 HTTP 状态码,企微业务错误也回 200,
// 用 generic 接企微会误报成功(09-28 走查口头确认的坑)
const PROVIDER_HELP: Record<string, string> = {
  generic: '通用通道:自签 HMAC 密钥,成功判定只看 HTTP 状态码(2xx 即成功)',
  wecom: '企微群机器人请用本通道(URL 取「群设置→群机器人→新建→复制 Webhook」),无需密钥。注意:通用通道只看 HTTP 状态码,企微业务错误也回 200,用通用通道接企微会误报成功',
  dingtalk: '钉钉自定义机器人;机器人安全设置中的加签密钥可选(填入后按平台方式加签)',
  feishu: '飞书自定义机器人;机器人安全设置中的签名密钥可选(填入后按平台方式加签)',
}
```

模板中(`<el-form-item label="平台类型">` 内)静态帮助行:

```html
          <div class="form-help">企业微信无需密钥;钉钉 / 飞书支持平台加签</div>
```

替换为:

```html
          <div class="form-help">{{ PROVIDER_HELP[form.provider] }}</div>
```

- [ ] **Step 4: 跑测试**

Run: `cd E:\Projects\AIRag\frontend && pnpm vitest run src\pages\__tests__\WebhooksPage.spec.ts`
Expected: 全 PASS(85 个)。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/WebhooksPage.vue frontend/src/pages/__tests__/WebhooksPage.spec.ts
git commit -m "feat(ui): provider-aware webhook channel guidance (wecom false-success pitfall)"
```

---

## 收尾(控制端执行,不派子代理)

1. **全量门禁**:`cd backend && .venv\Scripts\python -m pytest -q`(≥495P 预期,0F);`cd frontend && pnpm vitest run`(≥85)+ `pnpm build`(零错)。
2. **真栈验收** `backend/scripts/m21_acceptance.py`(照 m20 模式,重启栈后跑):①generation 无 key 触发→failed 条件落库+eval.failed 真送达 receiver、负载含部分 summary;②直插 stale/NULL 心跳 running 行→(调 sweep_orphan_runs 或等 beat)收口 failed;③fresh 心跳行不被收口;④真实短评估运行中 heartbeat_at 推进;⑤webhooks.md 头部无「M17/M18」旧标签且含负载示例节(脚本 grep)。
3. **终审 whole-branch**(requesting-code-review 模式)+ M22 候选回流。
4. **执行记录** 追加至本计划;更新记忆(推送仍等用户走查,既定节奏)。

---

## 执行记录(2026-09-28,SDD 六任务零修复轮 + 终审一波修复)

**交付(4388fcc..63bf51a,11 提交,main 本地未推)**:
- T1 c56f87c 测试局部 import 上提(实为 8 块 11 句,brief 计数笔误按语义全清)
- T2 3bd6556 `_finalize_run(error=)`:failed 条件收口(撞并发取消→用户赢 cancelled
  + eval.cancelled)、双 miss 提前返回不空 commit、eval.failed 负载 run 形状统一
  (item_count/summary + error);最后一个 ORM 无条件终态写删除
- T3 565cce0 timeutil.utcnow_naive 公共化(outbound 薄委托)+eval_runs.heartbeat_at
  (迁移 c2d3e4f5a6b7,dev 库已升)+两循环逐题续租(同 commit 零额外往返)
- T4 e10a72a sweep stale-only(NULL 或超 EVAL_HEARTBEAT_GRACE_MINUTES=10)+beat
  60s 任务 sweep_orphan_runs(worker_ready 保留);孤儿文案改「orphaned: heartbeat
  expired」;test_eval_trigger.py:150 同款断言计划漏列、实现者按成对规则补修
- T5 5ef0d85 webhooks.md 头部 M17~M21 事实 + 逐事件负载示例节;交叉核对三处
  code-wins 修正(generation 均值 present-as-null 而非缺席)
- T6 c1ac4ea 前端 PROVIDER_HELP 四通道联动引导(企微误报成功坑)
- 8930756 m21_acceptance(15 检查)
- 63bf51a 终审修复波:trigger_run 创建即初始化 heartbeat_at(排队即在宽限内)——
  终审抓到 NULL 心跳 × beat sweep 会把「排队中的活任务」误判孤儿(solo FIFO 下
  确定性复现);+日志「on worker start」措辞修正

**门禁**:pytest 489→**496P/0F**(T2+3、T3+1、T4+2、修复波+1);vitest 84→**85/85**;
build 零错。唯一 warning 为 fastmcp/authlib 第三方弃用提示,先存且与本次无关。

**真栈验收 m21_acceptance 15/15 PASS 0 SKIP**(栈重启后):①scrubbed-key 子进程
eager 触发→failed 条件落库→eval.failed 经真 worker 投递→receiver 实收信封(run
形状统一+error)②beat sweep 收口 NULL/stale 行(failed+新孤儿文案)③fresh 心跳行
存活 ≥2 beat tick(stale-only 判别:旧代码必死)④真实 retrieval 运行心跳续租
(hb≥触发前)⑤docs 头部/示例节检查⑥API 清理 204。

**终审(whole-branch,4388fcc..8930756)**:Important×1(排队假孤儿,63bf51a 修复,
scoped 复审 PASS 双 ADDRESSED 零新破)——终审另确认:eval_runs 全部终态写路径中仅
剩 api/eval.py:368 派发失败微窗口无条件写(先存,单写者毫秒窗,park M22)。

**过程中裁定(全记 SDD ledger)**:①直接 main 实施(20 里程碑既定;推送等走查)
②dev 库幻影 alembic_version b9c0d1e2f3a4(09-25 回退事故残留)核验后纠正 stamp
(独立复核:im_secret 是 API 字段非列,链外列 im_secret/channel/kb_scope 为惰性
残留,git 迁移链无缺失)③Task4 计划漏列断言追认 ④Task5 三处 code-wins 文档修正。

**M22 候选(终审+ledger triage)**:①多 worker 租约残余——排队等待>宽限仍会假孤儿
(owner 列/任务启动续租);②sweep 收口不发 eval.failed 事件(接收方对孤儿死亡无
感知);③api/eval.py:366-372 派发失败 ORM 无条件写→条件收口(与 trigger_run 同
pass);④_finalize_run cancelled+error 组合防护;⑤PROVIDER_HELP ?? 兜底/docs 措辞
nit(顺手);大件 A2A/MinerU 本地化/LDAP 仍等输入。

**栈态**:8001/worker/beat/5173 运行最终代码;dev 库 head=c2d3e4f5a6b7。

