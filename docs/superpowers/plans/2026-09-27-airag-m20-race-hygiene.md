# AIRag M20 取消竞态收口 + eval.cancelled 事件 + 治理小包 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消掉评估取消的端点/任务丢更新竞态,交付 `eval.cancelled` 独立事件,堵 SSRF 括号逃逸,平台通道密钥列显「已设置/未设置」,清偿 M19 终审台账的测试卫生债。

**Architecture:** 全部为既有子系统内小步修改:eval_runner 收口段抽 `_finalize_run` 条件 UPDATE;cancel 端点置位改条件 UPDATE;outbound 事件注册表扩一值;webhook_providers 包 urlparse 异常;前端 WebhooksPage 密钥列三分支。

**Tech Stack:** FastAPI + SQLAlchemy async(asyncpg);Vue3 + Element Plus + vitest。

**Spec:** `docs/superpowers/specs/2026-09-27-airag-m20-race-hygiene-design.md`(先读;冲突以 spec 为准;行号锚定 7d1e044,执行时以现状为准)。

## Global Constraints

- 后端测试:工作目录 `E:\Projects\AIRag\backend`,`.venv\Scripts\python -m pytest tests -q`。基线 **477P/0F**,不得回退。前端:`npm run test:unit -- --run` 基线 **82/82**;`npm run build` 零错。
- Windows CMD:没有 `ls`,用 `dir`;conventional commits;每任务一提交,不 push。
- **DLP 警示**:改仓库文件一律编辑工具,勿用 python/cmd echo 写文件(cmd echo UTF-8 必乱码,GBK 代码页)。
- 行为红线:自然完成路径的 emit/commit 时序语义不变;取消仍不发自 `eval.completed`;`_blocked_ip`/白名单判定逻辑不动。
- 注释中文简洁讲约束;不引新依赖;naive UTC 惯例沿用。

---

### Task 1: eval.cancelled 事件基座(注册表 / kb 过滤 / IM 文案 / 文档 / 前端选项)

**Files:**
- Modify: `backend/app/services/outbound.py:28-29`(EVENT_TYPES)、`:57-58`(_event_kb_ids eval 分支)
- Modify: `backend/app/services/webhook_providers.py:70`(describe_event eval 分支)
- Modify: `docs/webhooks.md:5-8`(事件列表与负载示例)
- Modify: `frontend/src/pages/WebhooksPage.vue:17-24`(EVENT_OPTIONS)
- Test: `backend/tests/test_outbound.py`(追加 2)、`backend/tests/test_webhook_providers.py`(追加 1)、`frontend/src/pages/__tests__/WebhooksPage.spec.ts`(追加 1)

**Interfaces:**
- Produces: `EVENT_TYPES` 含 `"eval.cancelled"`;`_event_kb_ids("eval.cancelled", data) -> {run.kb_id} | None`(与 eval.completed 同型);`describe_event("eval.cancelled", data) -> str` 含「评估已取消」;前端 `EVENT_OPTIONS` 六项。后续 Task 2 的发射点依赖此注册。

- [ ] **Step 1: 失败测试**

`test_outbound.py` 追加(沿用 `_mk_ep`/`emit_event`/`select(WebhookDelivery)` 既有夹具):

```python
async def test_emit_eval_cancelled_expands_with_kb_filter(db_session):
    """M20:eval.cancelled 注册后订阅展开与 per-KB 过滤同 eval.completed 语义。"""
    from app.services.outbound import _event_kb_ids, emit_event
    assert _event_kb_ids("eval.cancelled", {"run": {"kb_id": 3}}) == {3}
    await _mk_ep(db_session, events=["eval.cancelled"])   # 显式订阅命中
    await _mk_ep(db_session, events=["eval.completed"])   # 不命中
    ep_all = await _mk_ep(db_session, events=[])          # 空订阅命中
    await db_session.execute(update(WebhookEndpoint)      # kb 限定 7:不命中 kb 1
        .where(WebhookEndpoint.id == ep_all.id)
        .values(kb_ids=[7]))
    await db_session.commit()
    n = await emit_event(db_session, "eval.cancelled",
                         {"run": {"id": 1, "kb_id": 1, "mode": "retrieval"}})
    await db_session.commit()
    assert n == 1
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert [r.event_type for r in rows] == ["eval.cancelled"]
```

(文件顶部 import 区核对 `update` 是否已引入,缺则补 `from sqlalchemy import select, update`;WebhookEndpoint 已在 import 块。)

`test_webhook_providers.py` 追加:

```python
def test_describe_event_eval_cancelled():
    """M20:取消事件 IM 卡片文案——子集题数诚实展示。"""
    from app.services.webhook_providers import describe_event

    text = describe_event("eval.cancelled", {
        "run": {"kb_id": 2, "mode": "generation", "item_count": 2,
                "summary": {"item_count": 2}}})
    assert "评估已取消" in text and "#2" in text
    assert "generation" in text and "2" in text
```

`WebhooksPage.spec.ts` 追加(照既有「打开新建端点弹窗」用例的打开方式;若 spec 无该 helper,按 216 行用例的 dialog 打开模式适配):

```ts
it('事件选项含「评估已取消」(eval.cancelled)', async () => {
  // 打开新建弹窗后断言事件复选组出现 6 个选项且含评估已取消
  // (照既有 events 复选组定位方式;EVENT_OPTIONS 为 SFC 内常量,经渲染断言)
  ...断言 checkbox group 文本包含 '评估已取消'
})
```

- [ ] **Step 2: 红** → **Step 3: 实现**

`outbound.py`:`EVENT_TYPES = ("document.done", "document.failed", "eval.completed", "eval.failed", "eval.cancelled", "chat.refused")`;`_event_kb_ids` 的 `elif event_type in ("eval.completed", "eval.failed"):` 扩为含 `"eval.cancelled"`。

`webhook_providers.py` describe_event——把 `if event_type in ("eval.completed", "eval.failed"):` 分支内加 cancelled 文案:

```python
    if event_type in ("eval.completed", "eval.failed", "eval.cancelled"):
        run = d.get("run") or {}
        if event_type == "eval.completed":
            ...既有...
        if event_type == "eval.cancelled":
            return "\n".join(["**评估已取消**",
                              f"知识库: #{run.get('kb_id')}  模式: {run.get('mode', '')}",
                              f"已完成题数: {run.get('item_count', '')}(取消前的子集)"])
        ...既有 eval.failed...
```

`WebhooksPage.vue`:`EVENT_OPTIONS` 在 `eval.failed` 后插 `{ value: 'eval.cancelled', label: '评估已取消' }`;行 17 注释「五事件」改「六事件」。

`docs/webhooks.md`:事件列表行补 `eval.cancelled`;负载示例节(若有逐事件示例)加同 `eval.completed` 形状的块(`run{id,kb_id,mode,item_count,summary}`,注明 item_count 为取消前已完成子集)。

- [ ] **Step 4: 绿 + 全量** → ~480P/0F、vitest ~83 → **Step 5: Commit** `feat(webhook): register eval.cancelled event type across stack`

---

### Task 2: 任务侧条件收口——`_finalize_run`(A1 竞态 + 事件分流)

**Files:**
- Modify: `backend/app/services/eval_runner.py:224-237`(收口段替换;新增 `_finalize_run`)
- Test: `backend/tests/test_eval_cancel.py`(追加 4;**改写** `test_cancelled_run_emits_no_completed_event`)

**Interfaces:**
- Consumes: Task 1 的 `eval.cancelled` 注册。
- Produces: `async def _finalize_run(db, run_id, kb_id, mode, results, cancelled) -> tuple[str | None, int]`——条件 UPDATE 收口 + 按 final 分流 emit(`eval.completed`/`eval.cancelled`)+ commit,返回 `(final, n)`;行不属本任务(双 miss)返回 `(None, 0)`。Task 3 的端点条件 UPDATE 与本任务构成双侧防线。

- [ ] **Step 1: 失败测试**(`test_eval_cancel.py` 追加;复用文件顶 `update/select/time/WebhookDelivery/WebhookEndpoint` import)

```python
async def _mk_bare_run(db_session, status="running") -> int:
    run = EvalRun(kb_id=1, mode="retrieval", summary=None,
                  item_count=0, status=status, triggered_by=1)
    db_session.add(run)
    await db_session.commit()
    return run.id


async def test_finalize_completes_only_from_running(db_session):
    from app.services.eval_runner import _finalize_run

    run_id = await _mk_bare_run(db_session, "running")
    final, n = await _finalize_run(db_session, run_id, 1, "retrieval",
                                   [{"question": "q"}], False)
    assert final == "completed"
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "completed"
    assert run.item_count == 1 and run.summary["item_count"] == 1


async def test_finalize_raced_cancel_wins(db_session):
    """A1 竞态:任务未察觉(cancelled=False)但行已被端点置 cancelling →
    fallback 收口 cancelled,绝不覆盖成 completed。"""
    from app.services.eval_runner import _finalize_run

    run_id = await _mk_bare_run(db_session, "cancelling")
    final, n = await _finalize_run(db_session, run_id, 1, "retrieval",
                                   [{"question": "q"}], False)
    assert final == "cancelled"
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "cancelled"


async def test_finalize_double_miss_no_write_no_event(db_session):
    """两跳全零行(行已被 sweep 等收口)→ 不改写、零事件。"""
    from app.services.eval_runner import _finalize_run

    run_id = await _mk_bare_run(db_session, "failed")
    db_session.add(WebhookEndpoint(
        name=f"dm{time.time_ns()}", url="http://x/h", secret="wh_s",
        events=[], created_by=1))
    await db_session.commit()
    final, n = await _finalize_run(db_session, run_id, 1, "retrieval",
                                   [], False)
    assert final is None and n == 0
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "failed"  # 他人终态未被覆盖
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert rows == []


async def test_finalize_cancelled_emits_cancelled_event(db_session):
    from app.services.eval_runner import _finalize_run

    run_id = await _mk_bare_run(db_session, "cancelling")
    db_session.add(WebhookEndpoint(
        name=f"fc{time.time_ns()}", url="http://x/h", secret="wh_s",
        events=[], created_by=1))
    await db_session.commit()
    final, n = await _finalize_run(db_session, run_id, 1, "retrieval",
                                   [{"question": "q"}], True)
    assert final == "cancelled" and n == 1
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert [r.event_type for r in rows] == ["eval.cancelled"]
```

**改写** `test_cancelled_run_emits_no_completed_event` → 更名 `test_cancelled_run_emits_cancelled_event`,末三段断言改为:

```python
    assert run.status == "cancelled"  # 前置:确是取消路径(非 completed)
    deliveries = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    # M20:取消改发独立事件——恰一行 eval.cancelled,绝无 eval.completed
    assert [d.event_type for d in deliveries] == ["eval.cancelled"]
    assert deliveries[0].payload["data"]["run"]["item_count"] == 1
    assert nudged == [1]  # n>0,nudge 被踢(与 completed 同待遇)
```

(docstring 同步:M19「取消不发事件」语义升级为「取消发 eval.cancelled、仍不发 eval.completed」。)

- [ ] **Step 2: 红** → **Step 3: 实现**

`eval_runner.py` 在 `run_eval_task` 前新增(spec A-1 代码块逐字):

```python
async def _finalize_run(db: AsyncSession, run_id: int, kb_id: int,
                        mode: str, results: list[dict],
                        cancelled: bool) -> tuple[str | None, int]:
    """M20 条件收口:终态只经条件 UPDATE 落库,消端点/任务丢更新竞态。

    completed 只许写在仍是 running 的行上(端点在最后检查点后并发置
    cancelling 时零行命中,fallback 按用户已赢收口 cancelled);cancelled
    写在 cancelling 上。两跳全零行=行已被 sweep 等他人收口,不写不发
    (返回 None)。事件按 final 分流,同事务 commit;返回 (final, n)
    供调用方 nudge。"""
    from sqlalchemy import update

    from app.models import EvalRun

    summary = summarize(results)
    item_count = len(results)
    final = "cancelled" if cancelled else "completed"
    expect = "cancelling" if cancelled else "running"
    res = await db.execute(
        update(EvalRun)
        .where(EvalRun.id == run_id, EvalRun.status == expect)
        .values(status=final, summary=summary, item_count=item_count))
    if res.rowcount == 0 and not cancelled:
        # A1 竞态:最后检查点后端点已置 cancelling——用户已赢
        res = await db.execute(
            update(EvalRun)
            .where(EvalRun.id == run_id, EvalRun.status == "cancelling")
            .values(status="cancelled", summary=summary,
                    item_count=item_count))
        final = "cancelled" if res.rowcount else None
    elif res.rowcount == 0:
        final = None  # 行已不在 cancelling(他人收口),不写不发
    data = {"run": {"id": run_id, "kb_id": kb_id, "mode": mode,
                    "item_count": item_count, "summary": summary}}
    if final == "completed":
        n = await emit_event(db, "eval.completed", data)  # M17
    elif final == "cancelled":
        n = await emit_event(db, "eval.cancelled", data)  # M20
    else:
        n = 0
    await db.commit()
    return final, n
```

`run_eval_task` 两循环后的收口段(现 224-237 行)整体替换为:

```python
                # M20:条件收口(竞态防线+事件分流);kb_id 是 M17 快照
                final, n = await _finalize_run(db, run_id, run.kb_id,
                                               mode, results, cancelled)
                if n:
                    nudge()
```

函数 docstring 状态机行补「M20:收口经 `_finalize_run` 条件 UPDATE」。(异常路径 failed/eval.failed 逐字不动。)

- [ ] **Step 4: 绿 + 全量**(既有 `test_run_task_stops_on_cancelling`/`cancel_at_later_question`/`test_dup_guard_ignores_cancelling` 必须不改断言仍绿)→ ~484P → **Step 5: Commit** `fix(eval): conditional close-out kills cancel lost-update race; emit eval.cancelled`

---

### Task 3: 端点侧条件 UPDATE(A2 竞态)

**Files:**
- Modify: `backend/app/api/eval.py:376-396`(cancel_run)、`:8`(import update)
- Test: `backend/tests/test_eval_cancel.py`(追加 2)

**Interfaces:**
- Consumes: Task 2 已让任务侧终态只经条件 UPDATE;本任务端点侧对称。

- [ ] **Step 1: 失败测试**(追加;`_require_kb_owner` 在 `app.api.eval` 命名空间的引用即 monkeypatch 面)

```python
def _flip_guard(monkeypatch, db_session, run_id, to_status):
    """守卫 seam:owner 校验内先经测试会话把 run 翻成 to_status 并 commit,
    确定性复现「端点读 running 之后、条件 UPDATE 之前任务收口」窗口。"""
    import app.api.eval as ev

    orig = ev._require_kb_owner

    async def guard(db, current, kb_id):
        await db_session.execute(
            update(EvalRun).where(EvalRun.id == run_id)
            .values(status=to_status))
        await db_session.commit()
        db_session.expire_all()
        return await orig(db, current, kb_id)

    monkeypatch.setattr(ev, "_require_kb_owner", guard)


async def test_cancel_race_run_completed_before_update(
        client, auth_headers, db_session, monkeypatch):
    """A2 竞态:条件 UPDATE 零行→重读 409;终态不被改写、无 audit。"""
    run_id = await _mk_run(client, auth_headers, db_session)
    _flip_guard(monkeypatch, db_session, run_id, "completed")
    r = await client.post(f"/api/eval/runs/{run_id}/cancel",
                          headers=auth_headers)
    assert r.status_code == 409
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "completed"  # 终态未被覆盖回 cancelling
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "eval_cancel")
    )).scalars().all()
    assert rows == []


async def test_cancel_race_run_cancelling_idempotent(
        client, auth_headers, db_session, monkeypatch):
    """A2 竞态另一形态:翻成 cancelling → 条件 UPDATE 零行→重读幂等 200,
    不重复 audit。"""
    run_id = await _mk_run(client, auth_headers, db_session)
    _flip_guard(monkeypatch, db_session, run_id, "cancelling")
    r = await client.post(f"/api/eval/runs/{run_id}/cancel",
                          headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"id": run_id, "status": "cancelling"}
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "eval_cancel")
    )).scalars().all()
    assert rows == []
```

- [ ] **Step 2: 红** → **Step 3: 实现**

import 行改 `from sqlalchemy import func, or_, select, update`。cancel_run 中 `run.status = "cancelling"` 直写段替换为(spec A-2 代码块):

```python
    # M20:条件 UPDATE 防丢更新——读后任务恰收口终态时零行命中,
    # 重读分流(幂等/409),绝不覆盖任务已落的终态
    res = await db.execute(
        update(EvalRun)
        .where(EvalRun.id == run_id, EvalRun.status == "running")
        .values(status="cancelling"))
    if res.rowcount == 0:
        await db.refresh(run)
        if run.status == "cancelling":
            return {"id": run_id, "status": "cancelling"}  # 幂等,不重复 audit
        raise HTTPException(status_code=409, detail="run already finished")
    await audit(db, current.username, "eval_cancel", f"eval_run:{run_id}",
                {"kb_id": run.kb_id, "mode": run.mode})
    await db.commit()
    return {"id": run_id, "status": "cancelling"}
```

- [ ] **Step 4: 绿 + 全量**(既有取消端点测试不改断言)→ ~486P → **Step 5: Commit** `fix(eval): cancel endpoint uses conditional update; never overwrites terminal state`

---

### Task 4: check_url_allowed 括号异常包裹

**Files:**
- Modify: `backend/app/services/webhook_providers.py:206-212`(check_url_allowed 头部)
- Test: `backend/tests/test_webhook_providers.py`(追加 2)

- [ ] **Step 1: 失败测试**

```python
def test_ssrf_malformed_bracket_url_blocked():
    """M20:括号不配对的 IPv6 字面量——urlparse 抛 ValueError 必须包成
    SsrfBlockedError,不得逃逸打断建端点/投递调用方。"""
    import pytest as _pytest

    from app.services import webhook_providers as wp

    with _pytest.raises(wp.SsrfBlockedError):
        wp.check_url_allowed("http://[::1/x")
    with _pytest.raises(wp.SsrfBlockedError):
        wp.check_url_allowed("http://[/x")


def test_ssrf_valid_bracket_v6_still_parsed():
    from app.services.webhook_providers import check_url_allowed

    check_url_allowed("http://[2001:db8::5]:8000/x")  # 合法公网 v6 照常放行
```

- [ ] **Step 2: 红**(现状 `pytest.raises(ValueError)` 会捕到裸 ValueError?——不,SsrfBlockedError 是 ValueError 子类但此处抛的是裸 ValueError,`raises(SsrfBlockedError)` 红)→ **Step 3: 实现**

`check_url_allowed` 头两行改为:

```python
    from app.core.config import settings
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname  # 括号不配对的 IPv6 在 urlparse/此处抛 ValueError
    except ValueError as e:
        raise SsrfBlockedError(f"url malformed: {url[:80]}") from e
    if parsed.scheme not in ("http", "https") or not hostname:
        raise SsrfBlockedError(f"scheme/host invalid: {url[:80]}")
```

后续 `host = parsed.hostname` 改 `host = hostname`(判空沿用)。

- [ ] **Step 4: 绿 + 全量** → ~488P → **Step 5: Commit** `fix(webhook): wrap urlparse ValueError in SsrfBlockedError for malformed bracket URLs`

---

### Task 5: 平台通道密钥列显「已设置/未设置」(前端)

**Files:**
- Modify: `frontend/src/api/admin.ts:40-48`(WebhookOut 补 `im_secret_set: boolean`)
- Modify: `frontend/src/pages/WebhooksPage.vue:411-415`(secret 列三分支)
- Test: `frontend/src/pages/__tests__/WebhooksPage.spec.ts`(追加 1~2)

- [ ] **Step 1: 失败测试**(mock 行加钉钉端点:`provider: 'dingtalk', im_secret_set: false` 与一条 `im_secret_set: true`,断言 secret 列单元格文本分别「未设置」(灰字类 no-secret)与「已设置」;照既有行断言的定位方式)

```ts
it('平台通道密钥列:im_secret_set 显「未设置/已设置」', async () => {
  // listRuns→mock 含 dingtalk 未设置/已设置两行(照既有 eps mock 形状扩字段)
  // 断言对应行 secret 列文本 '未设置' / '已设置'
})
```

- [ ] **Step 2: 红** → **Step 3: 实现**

`admin.ts` `WebhookOut` 在 `secret_masked: string` 后加 `im_secret_set: boolean`(注释:平台通道加签密钥是否已设,M19 后端已有前端未用)。`WebhooksPage.vue` secret 列模板改三分支:

```html
<span v-if="row.provider === 'wecom'" class="no-secret">无需密钥</span>
<!-- M20:平台通道真实签名密钥在 im_secret 位,secret 列是占位——
     用 im_secret_set 显状态,避免 masked 占位误导 -->
<span v-else-if="row.provider !== 'generic'"
      :class="{ 'no-secret': !row.im_secret_set }">
  {{ row.im_secret_set ? '已设置' : '未设置' }}
</span>
<span v-else class="mono">{{ row.secret_masked }}</span>
```

- [ ] **Step 4: 绿 + build** → vitest ~85 → **Step 5: Commit** `feat(ui): platform webhook secret column shows im_secret_set state`

---

### Task 6: 测试卫生四件(M19 终审台账)

**Files:**
- Modify: `backend/tests/conftest.py:147-156`(SSRF autouse 夹具钉 ENFORCE)
- Test: `backend/tests/test_outbound.py`(追加整轮回归 1)、`backend/tests/test_eval_questions.py`(bulk audit 断言)、`frontend/src/pages/eval/__tests__/EvalRunsTab.spec.ts`(spy restore)

- [ ] **Step 1a: ENFORCE 钉死**——conftest SSRF 夹具(monkeypatch `_resolve_host` 处)追加:

```python
    # M20:ENFORCE 钉死——dev .env 关断(WEBHOOK_SSRF_ENFORCE=false)时,
    # 依赖检查点生效的用例(毒环回归等)前提会 silently 失效
    from app.core.config import settings as _cfg

    monkeypatch.setattr(_cfg, "WEBHOOK_SSRF_ENFORCE", True)
```

(局部显式设 False 的用例不受影响——monkeypatch 后写优先;跑全量验证零破坏。)

- [ ] **Step 1b: deliver_due 整轮回归**(test_outbound.py 追加;`_pending`/`_FakeClient` 既有,核对 `_pending` 默认参形状后照用):

```python
async def test_deliver_due_full_round_lifecycle(db_session):
    """M20 整轮回归:一轮内 pending→500→retrying(退避已排)与 404→立即
    dead 并存;retrying 到期后二轮 200→succeeded(attempts=2,at-least-once)。"""
    from app.services.outbound import deliver_due

    await _mk_ep(db_session, events=[], url="http://h/flaky")
    await _mk_ep(db_session, events=[], url="http://h/gone")
    d1 = await _pending(db_session, "flaky")
    d2 = await _pending(db_session, "gone")
    n = await deliver_due(db_session, client=_FakeClient(
        {"flaky": 500, "gone": 404}))
    assert n == 2
    await db_session.refresh(d1)
    assert d1.status == "retrying" and d1.attempts == 1
    assert d1.next_attempt_at is not None
    await db_session.refresh(d2)
    assert d2.status == "dead" and d2.attempts == 1
    d1.next_attempt_at = (datetime.now(timezone.utc).replace(tzinfo=None)
                          - timedelta(minutes=1))
    await db_session.commit()
    n2 = await deliver_due(db_session, client=_FakeClient({"flaky": 200}))
    assert n2 == 1
    await db_session.refresh(d1)
    assert d1.status == "succeeded" and d1.attempts == 2
```

- [ ] **Step 1c: bulk audit 断言**——`test_eval_questions.test_bulk_creates_all` 末尾追加(直查表,不依赖 admin 读权限;detail 字段名以 `app/models.py` AuditLog 实际为准):

```python
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "eval_questions_bulk")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].target == f"kb:{kb_id}"
    # detail 列为 Text:audit() 落库前 json.dumps(ensure_ascii=False)
    assert json.loads(rows[0].detail) == {"created": 3, "errors": 0}
```

(import 区补 `AuditLog`/`select`/`json`;kb_id 变量名以该用例现状为准。)

- [ ] **Step 1d: spy restore**——`EvalRunsTab.spec.ts`:模块级 `let confirmSpy: ReturnType<typeof vi.spyOn> | null = null`;202 行改 `confirmSpy = vi.spyOn(...)`;358 行 afterEach 改:

```ts
afterEach(() => {
  vi.useRealTimers()
  confirmSpy?.mockRestore()
  confirmSpy = null
})
```

- [ ] **Step 2: 全量门禁**(pytest ~490P/0F、vitest 85、build)→ **Step 3: Commit** `test: pin SSRF enforce in suite, full-round deliver_due regression, audit assertion, spy restore`

---

### Task 7: 门禁 + 真栈验收 + 终审 + 收尾(控制端)

- [ ] Step 1: 全量门禁(pytest ~490P / vitest ~85 / build)
- [ ] Step 2: 栈三件套+前端核对(8001/worker/beat/5173;`.env` 保持 `WEBHOOK_SSRF_ALLOWLIST=127.0.0.1`)
- [ ] Step 3: `m20_acceptance.py`(骨架照 m19;receiver 复用 m18 模式):①坏括号 URL(`http://[::1/x`)建端点被拒(SSRF 预检)②events=["eval.cancelled"] 建端点 201 ③取消回路(generation 真 LLM,照 m19 的 ZHIPU_API_KEY 模式选型):触发→立即 cancel→轮询 cancelled→deliveries 恰一行 eval.cancelled succeeded+receiver 实收 payload(run.item_count=子集)且无该 run 的 eval.completed 行 ④cancel 幂等+终态 409 快速回归 ⑤建 dingtalk 端点(不带 im_secret)→GET 列表 `im_secret_set==False` ⑥清理
- [ ] Step 4: 执行记录+终审 whole-branch(修复波按需)+M21 候选回流
- [ ] Step 5: 不推送(M18+M19+M20 合并走查后统一推,用户指示走查延后)

---

## 验收门槛(整计划)

1. pytest 全绿(~490P);vitest 全绿(~85);build 零错
2. 真栈 m20_acceptance 全 PASS
3. 终审 clean 或修复波闭环;不推送
4. M20 走查增量清单(并到 M18+M19 合并走查一起做):webhook 端点表钉钉行密钥列「未设置/已设置」、新建端点事件选项含「评估已取消」、取消一次运行后投递记录出现 eval.cancelled

## 执行记录(待补)
