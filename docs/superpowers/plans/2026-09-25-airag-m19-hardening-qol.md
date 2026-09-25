# AIRag M19 出站加固 + 评估 QoL 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修掉 ALLOWLIST 拼错毒环与 webhook secret 卫生问题,交付评估取消/题集导入导出/趋势空态细分。

**Architecture:** 全部为既有子系统内的小步扩展:webhook_providers 白名单解析 fail-open;admin 密钥生命周期收紧(wecom 占位不下发、切换即重置、轮换限 generic);EvalRun 增 cancelling/cancelled 两值协作式取消;eval questions 增 export/bulk 两端点;前端三处 UI 补齐。

**Tech Stack:** FastAPI + SQLAlchemy async + Celery(solo);Vue3 + Element Plus + vitest。

**Spec:** `docs/superpowers/specs/2026-09-25-airag-m19-hardening-qol-design.md`(先读;冲突以 spec 为准;行号锚定 d272a05,执行时以现状为准)。

## Global Constraints

- 后端测试:工作目录 `E:\Projects\AIRag\backend`,`.venv\Scripts\python -m pytest tests -q`。基线 **443P/0F**,不得回退。前端:`npm run test:unit -- --run` 基线 **74/74**;`npm run build` 零错。
- Windows CMD:没有 `ls`,用 `dir`;conventional commits;每任务一提交,不 push。
- **DLP 警示**:改仓库文件一律编辑工具,勿用 python/cmd echo 写文件(cmd echo UTF-8 必乱码,GBK 代码页)。
- 行为红线:A4 删死表**不改 classify_response 行为**;B9 自然完成/异常路径不变;A1 fail-open(坏条目 skip+warning,绝不抛)。
- 注释中文简洁讲约束;不引新依赖;naive UTC 惯例沿用。

---

### Task 1: 白名单加固(fail-open)+ 删死表 + SSRF 补测 + .env 默认空

**Files:**
- Modify: `backend/app/services/webhook_providers.py:185-191`(_allowlist_networks)、`:129-134`(删 PLATFORM_TRANSIENT)
- Modify: `E:\Projects\AIRag\.env.example:46-50`(ALLOWLIST 置空)
- Test: `backend/tests/test_webhook_providers.py`(追加约 9)

**Interfaces:**
- Produces: `_allowlist_networks(raw) -> list` 契约——坏条目跳过不抛(毒环修复);`check_url_allowed` 行为对合法输入不变。若任何文件 import `PLATFORM_TRANSIENT` 须同步(全仓 grep 确认仅定义处)。

- [ ] **Step 1: 失败测试(test_webhook_providers.py 追加;先读现有 SSRF 区块 203-266 的夹具/参数化风格照抄)**

```python
def test_allowlist_bad_entries_skipped_not_raised():
    """M19 毒环修复:坏白名单条目 skip+warning,不抛、不放行。"""
    from app.services import webhook_providers as wp

    nets = wp._allowlist_networks("127.0.0.1:8000,not-an-ip,10.0.0.0/8")
    assert [str(n) for n in nets] == ["10.0.0.0/8"]


def test_check_url_survives_bad_allowlist():
    """坏条目存在时 check_url_allowed 不抛;回环仍拒(坏条目不生效)。"""
    import pytest as _pytest

    from app.services import webhook_providers as wp

    with _pytest.raises(wp.SsrfBlockedError):
        wp.check_url_allowed("http://127.0.0.1/x",
                             allowlist="127.0.0.1:8000")
    assert wp.check_url_allowed("http://8.8.8.8/x",
                                allowlist="not-an-ip") is None


async def test_deliver_one_survives_bad_allowlist(db_session, monkeypatch):
    """毒环回归:坏 allowlist 下 deliver_one 照常投公网端点不被打断。"""
    from app.core.config import settings as cfg

    import app.services.webhook_providers as wp

    monkeypatch.setattr(cfg, "WEBHOOK_SSRF_ALLOWLIST", "127.0.0.1:8000")
    # 其余照 test_outbound 的 deliver_one 公网端点用例组装
    # (enforce 开关按现有测试默认;断言行落 succeeded 而非异常逃出)


def test_ssrf_ipv6_with_port_allowed_public():
    from app.services.webhook_providers import check_url_allowed

    check_url_allowed("http://[2001:db8::1]:8000/x")  # 公网 IPv6 直判放行


def test_ssrf_ipv6_loopback_blocked():
    import pytest as _pytest

    from app.services import webhook_providers as wp

    with _pytest.raises(wp.SsrfBlockedError):
        wp.check_url_allowed("http://[::1]/x")


def test_ssrf_settings_default_path(monkeypatch):
    """不传 allowlist 走 settings 默认(空=全拒私网;127.0.0.1=放行回环)。"""
    from app.core.config import settings as cfg

    from app.services import webhook_providers as wp

    monkeypatch.setattr(cfg, "WEBHOOK_SSRF_ALLOWLIST", "")
    with pytest.raises(wp.SsrfBlockedError):
        wp.check_url_allowed("http://10.0.0.9/x")
    monkeypatch.setattr(cfg, "WEBHOOK_SSRF_ALLOWLIST", "127.0.0.0/8")
    wp.check_url_allowed("http://127.0.0.1/x")


@pytest.mark.parametrize("host", ["10.0.0.9", "10.0.0.1", "192.168.0.9",
                                  "172.20.1.5"])
def test_ssrf_wide_private_ranges_blocked_without_allowlist(host):
    from app.services import webhook_providers as wp

    with pytest.raises(wp.SsrfBlockedError):
        wp.check_url_allowed(f"http://{host}/x")
```

(`test_deliver_one_survives_bad_allowlist` 组装细节以现有 test_outbound.py 公网投递用例为准;顶部缺的 import 补齐。)

- [ ] **Step 2: 红** → **Step 3: 实现**

`_allowlist_networks` 改 spec A 节代码块(逐条 try/except ValueError → `logger.warning(f"WEBHOOK_SSRF_ALLOWLIST 坏条目已跳过: {part!r}")`;函数需 import logger)。删 `PLATFORM_TRANSIENT` 常量块,其 docstring 语义(「瞬态与未知非零码一律 retry,保守」)并入 `PLATFORM_PERMANENT` 注释;`classify_response` 不动。`.env.example` 的 `WEBHOOK_SSRF_ALLOWLIST=127.0.0.1` 改 `WEBHOOK_SSRF_ALLOWLIST=`,注释改「本地开发需放行回环接收器时显式设 127.0.0.1;生产留空」。

- [ ] **Step 4: 绿 + 全量** → **~452P/0F**(443 + 约 9)
- [ ] **Step 5: Commit**

```bash
git add backend/app/services/webhook_providers.py E:/Projects/AIRag/.env.example backend/tests/test_webhook_providers.py
git commit -m "fix(webhook): fail-open allowlist parsing kills delivery poison loop; drop dead transient table"
```

---

### Task 2: webhook secret 卫生(wecom 不回显 / 切换重置 / 轮换限 generic)

**Files:**
- Modify: `backend/app/schemas/admin.py`(WebhookCreatedOut.secret 可空)
- Modify: `backend/app/api/admin.py`(create wecom 分支 252-257/274-275、_to_out 192-201、PUT provider 分支 340-342、rotate 351-357)
- Modify: `docs/webhooks.md`(两段补注)
- Test: `backend/tests/test_admin_webhooks.py`(追加约 8)

**Interfaces:**
- Produces:
  - `WebhookCreatedOut.secret: str | None`(wecom 创建/任何不回显场景为 None)
  - `WebhookOut.secret_masked: str`(wecom 为空串;前端 T6 据此显示「无需密钥」)
  - PUT provider 变更 → secret 重置(wecom=新占位;generic=新随机且响应 WebhookCreatedOut 明文一次;dingtalk/feishu=im_secret 取 payload 否则清空,secret 列置占位)
  - `rotate_secret=true` 且 `ep.provider != "generic"` → 422 `"rotate_secret not supported; platform secret comes from the IM console"`

- [ ] **Step 1: 失败测试(追加;沿用该文件夹具)**

1. `test_wecom_create_no_secret_echo`:provider=wecom POST → 201 且 `r.json()["secret"] is None`;GET 列表该项 `secret_masked == ""`;响应文本不含任何 32 位 hex
2. `test_wecom_masked_empty_in_list`:直插/创建 wecom 端点 → 列表 masked 空串;dingtalk 端点 masked 非空
3. `test_provider_switch_resets_secret`:建 generic(secret=S1)→ PUT {provider:"wecom"} → 200 响应为 WebhookOut(masked 空);再 PUT {provider:"generic"} → 响应含新明文(≠S1),后续 GET masked 非空
4. `test_provider_switch_to_platform_clears_secret`:generic→PUT {provider:"dingtalk", im_secret:"NEWHOOK"} → GET im_secret_set=true;不随带 im_secret 的切换(feishu)→ im_secret_set=false
5. `test_rotate_rejected_for_platforms`:wecom/钉钉端点 PUT {rotate_secret:true} → 422;generic 端点 → 200 明文一次(既有 rotate 语义不回退)
6. `test_provider_reset_audited`:切换 PUT 后 audit-logs 出现 `webhook_update` 且 detail 含 `provider_reset`
7. `test_wecom_switch_carries_no_placeholder_to_dingtalk_sign`:切换 wecom→dingtalk 未带 im_secret → im_secret 为 None(占位 hex 不得进入平台密钥位)——断言 GET `im_secret_set is False`
8. `test_create_generic_still_returns_secret_once`(既有语义回归锁)

- [ ] **Step 2: 红** → **Step 3: 实现**(admin.py 分支改造;`_to_out` wecom masked 置空串;PUT 分支结构:先判 provider 是否变更,变更时按目标通道重置 secret/im_secret 并组装相应响应模型;audit detail 加 `provider_reset: "old→new"`。docs/webhooks.md:wecom 段补「占位密钥不参与签名、不会下发;secret_masked 为空即无需密钥」;平台段补「更换密钥请编辑 im_secret,勿用轮换(已禁)」。)
- [ ] **Step 4: 绿 + 全量** → **~460P/0F**
- [ ] **Step 5: Commit** `fix(webhook): wecom placeholder never echoed, provider switch resets secret, rotate generic-only`

---

### Task 3: 取消评估(cancelling/cancelled 协作式)

**Files:**
- Modify: `backend/app/api/eval.py`(增 cancel 端点)
- Modify: `backend/app/services/eval_runner.py`(两循环 190-211 增检查)
- Modify: `backend/app/workers/eval_tasks.py`(sweep 47 行 where 扩)
- Test: `backend/tests/test_eval_trigger.py` 或新 `test_eval_cancel.py`(约 8 用例,夹具照 test_eval_task.py `_mk_running_run`)

**Interfaces:**
- Produces:
  - `POST /api/eval/runs/{run_id}/cancel` → `{id, status}`;running→cancelling 200;cancelling 再调 200 幂等;completed/failed/cancelled→409;不可见库 404;viewer 403;audit `eval_cancel`
  - `EvalRun.status` 值域扩 `cancelling`/`cancelled`;cancelled 终态写 `summary=summarize(已完成子集)、item_count=子集数`
  - sweep 收口 `status.in_(("running","cancelling"))`;409 防重仍仅 `=="running"`

- [ ] **Step 1: 失败测试(核心用例)**

```python
async def test_cancel_running_run(client, auth_headers, db_session):
    from app.models import EvalRun
    kb_id = ...(建库+一题)
    run = EvalRun(kb_id=kb_id, mode="retrieval", summary=None,
                  item_count=1, status="running", triggered_by=1)
    ...commit
    r = await client.post(f"/api/eval/runs/{run.id}/cancel",
                          headers=auth_headers)
    assert r.status_code == 200 and r.json()["status"] == "cancelling"
    r2 = await client.post(...)  # 幂等
    assert r2.status_code == 200
    # 终态 run → 409;viewer → 403;不存在/不可见 → 404


async def test_run_eval_task_stops_on_cancelling(client, auth_headers,
                                                 db_session, monkeypatch):
    """第 1 题后置 cancelling → 任务收口 cancelled,子集数据落库。"""
    from app.services.eval_runner import run_eval_task
    kb + 3 题(1 题带期望);fake retrieval_item 首题提交后把 run 置
    cancelling(在 fake 内 db 直改+commit,模拟用户并发取消);
    await run_eval_task(run_id, "retrieval", False, 8)
    断言 status=="cancelled"、len(items)==1、summary["item_count"]==1


async def test_sweep_collects_cancelling(...):
    # 造 cancelling run → 调 _sweep_orphan_runs → 收口 failed
async def test_dup_guard_ignores_cancelling(...):
    # cancelling 状态下再 POST /eval/runs 同库同 mode → 201(不 409)
async def test_natural_completion_unchanged(...):
    # 既有 test_run_eval_task_completes 语义回归(不改断言)
```

- [ ] **Step 2: 红** → **Step 3: 实现**

eval_runner 两循环(retrieval/generation)逐题 commit 后:

```python
                        await db.refresh(run)
                        if run.status == "cancelling":
                            cancelled = True
                            break
```

(循环前 `cancelled = False`;循环后与 completed 写法同段:`if cancelled: run.status = "cancelled"` 替代 completed;summary/item_count 统一写 `summarize(results)`/`len(results)`——两路径共用,仅 status 不同。)api/eval.py cancel 端点权限照触发端点的 owner/admin 守卫复用(先读该守卫函数名照抄);eval_tasks sweep where 扩。

- [ ] **Step 4: 绿 + 全量** → **~468P/0F**
- [ ] **Step 5: Commit** `feat(eval): cooperative run cancellation with cancelling/cancelled states`

---

### Task 4: 前端——取消按钮/取消中 tag/轮询扩展

**Files:**
- Modify: `frontend/src/api/eval.ts`(`cancelRun(id)`;EvalRun.status 类型扩 `'cancelling' | 'cancelled'`)
- Modify: `frontend/src/pages/eval/EvalRunsTab.vue`(状态列 cancelling tag「取消中」/cancelled tag;running 行取消按钮;hasRunning 扩)
- Test: `frontend/src/pages/eval/__tests__/EvalRunsTab.spec.ts`(追加 2)

**改动要点**:状态列加 `v-else-if="row.status === 'cancelling'"` warning tag「取消中」与 cancelled tag(info「已取消」);操作列 `v-if="row.status==='running'"` 加「取消」按钮(ElMessageBox.confirm → `evalApi.cancelRun(row.id)` → 刷新);`hasRunning()` 改 `['running','cancelling'].includes(r.status)`(cancelling 也轮询到终态)。vitest:①cancelling 行渲染 tag 且轮询持续(hasRunning 扩)②取消按钮调用 cancelRun(mock)。

- [ ] 红 → 实现 → **绿 + build** → vitest **~76** → Commit `feat(eval-ui): cancel running evaluation with cancelling state`

---

### Task 5: 题集导出/批量导入端点

**Files:**
- Modify: `backend/app/schemas/eval.py`(BulkIn/BulkResultOut/ExportOut)
- Modify: `backend/app/api/eval.py`(export/bulk 两端点)
- Test: `backend/tests/test_eval_questions.py`(追加约 6)

**Interfaces:**
- Produces:
  - `GET /api/eval/questions/export?kb_id=` → JSON 附件 `{kb_id, kb_name, exported_at, count, questions:[{question, expect_doc_ids, expect_keywords, reference_answer}]}`(id asc;无 id 字段)
  - `POST /api/eval/questions/bulk` body `{kb_id, questions:[≤500×{question, expect_doc_ids?, expect_keywords?, reference_answer?}]}` → 201 `{created, errors:[{index, detail}]}`(部分成功;>500 → 422);audit `eval_questions_bulk`
  - 权限同 questions CRUD(owner/admin;不可见 404;viewer 403)

- [ ] **Step 1: 失败测试**

1. `test_export_shape_and_order`:建库+3 题(顺序打乱 id)→ GET export → Content-Disposition 含 `eval-questions-kb{id}.json`;JSON count==3、questions[0] 无 id 键、题序 id asc、kb_name 正确
2. `test_export_empty_kb`:空库 → count==0 空数组(仍 200)
3. `test_export_permissions`:viewer 403;不可见库 404
4. `test_bulk_creates_all`:3 条合法 → created==3、errors==[]、GET questions total==3
5. `test_bulk_partial_success`:2 合法+1 空白题 → created==2、errors==[{index:1, detail 含「题干」}];库里恰 2 条
6. `test_bulk_cap_500`:501 条 → 422

- [ ] **Step 2: 红** → **Step 3: 实现**(bulk 复用单条 POST 的校验逻辑——抽公共函数或逐条 `EvalQuestionIn(**q)` model_validate 报 ValidationError 首错进 errors;export 端点用 `Response(content=json.dumps(..., ensure_ascii=False), media_type="application/json", headers=...)`)
- [ ] **Step 4: 绿 + 全量** → **~474P/0F**
- [ ] **Step 5: Commit** `feat(eval): question set export and bulk import endpoints`

---

### Task 6: 前端——导入导出/趋势空态/范围 tooltip/轮换与 wecom 显示

**Files:**
- Modify: `frontend/src/api/eval.ts`(exportQuestions 返回 blob;bulkImport)
- Modify: `frontend/src/pages/eval/QuestionsTab.vue`(toolbar 导出/导入)
- Modify: `frontend/src/pages/eval/TrendCard.vue`(emptyKind 三态)
- Modify: `frontend/src/pages/WebhooksPage.vue`(范围 tooltip、轮换开关限 generic、wecom secret 列「无需密钥」)
- Test: `frontend/src/pages/eval/__tests__/QuestionsTab.spec.ts`(追加 2)、`TrendCard.spec.ts`(追加 1)、`frontend/src/pages/__tests__/WebhooksPage.spec.ts`(追加 2)

**改动要点**:
- QuestionsTab:toolbar「导出」(有题可用;`exportQuestions(kbId)` → blob → a[download] 点击);「导入」hidden file input → JSON.parse → ElMessageBox 确认「将导入 N 题」→ `bulkImport` → ElMessage `{created} 题导入成功`+errors>0 时附 `{errors.length} 条失败`;解析失败 error 提示不炸
- TrendCard:`empty` 改 `emptyKind: '' | 'no-runs' | 'unmeasured'`(判据 spec B11 代码块);模板三分支文案
- WebhooksPage:范围列外包 el-tooltip(kbOptions + `disambiguateKbNames` 映射,空=「全部知识库」);轮换开关 `v-if="editing && form.provider === 'generic'"`;secret 列 `row.provider==='wecom'` → 「无需密钥」
- vitest:导入确认与结果消息(mock bulk);导出按钮无题禁用;TrendCard unmeasured 文案+无画布;范围 tooltip 内容;wecom 行「无需密钥」

- [ ] 红 → 实现 → **绿 + build** → vitest **~81** → Commit `feat(ui): question import/export, unmeasured trend empty state, webhook scope tooltip and rotate gating`

---

### Task 7: 门禁 + 真栈验收 + 收尾(控制端)

- [ ] Step 1: 全量门禁(pytest ~474P / vitest ~81 / build)
- [ ] Step 2: 栈重启三件套+前端(单实例核对);`.env` 保持 `WEBHOOK_SSRF_ALLOWLIST=127.0.0.1`(dev)
- [ ] Step 3: `m19_acceptance.py`(骨架照 m18;receiver 复用):①wecom create 响应 secret null+列表 masked 空 ②generation 真跑+立即 cancel→轮询 cancelled(items<total)→同库同 mode 再触发不 409 ③export→删 2 题→bulk 回读→total 恢复 ④viewer cancel/export/bulk 403 ⑤坏 allowlist 真栈负例不做(单测覆盖,头注注明)⑥清理
- [ ] Step 4: 执行记录+走查清单增量+终审 whole-branch(修复波按需)
- [ ] Step 5: 不推送(M17+M18+M19 合并走查后统一推,惯例)

---

## 验收门槛(整计划)

1. pytest 全绿(~474P);vitest 全绿(~81);build 零错
2. 真栈 m19_acceptance 全 PASS(允许注明坏-allowlist 分支仅单测覆盖)
3. 终审 clean 或修复波闭环;M20 候选回流记忆;不推送(合并走查后统一推)
4. 范围按推荐默认执行(卡片未答,M13 先例)

## 执行记录(2026-09-25,SDD)

**提交链**(spec f4a9108 → 计划 8def9bf):T1 4a9b45e → T2 f4ab9ea → T3 659f3e8 + 60747c4(修复轮:取消路径抑制 eval.completed emit)→ T4 d2d377d → T5 9afda12 → T6 f319f7c → T7 c38e303(验收脚本)。六任务评审:T3 经 1 修复轮,其余一次通过。

**测试与验收(控制端亲验)**:pytest 443→**476P/0F**(+10/+8/+9/+6:白名单毒环与 SSRF 补测/secret 卫生/取消状态机/导入导出);vitest 74→**81/81**(+2/+5);build 零错。真栈 `m19_acceptance.py` **29/29 PASS 0 SKIP**(generation 真 LLM 取消回路 run 34/35:62.6s/24.2s cancelled、子集 items/summary 一致、防重不 409、终态再取消 409;export→删 2→bulk 回环 total 3→1→4;部分成功 errors[0].index;wecom secret None+masked 空;viewer 四负例 404;清理含端点与 KB)。

**实施期裁决**(SDD 台账全量;关键):
- T1:2001:db8::1 在 py3.12 判 is_private(RFC3849)→ 换真公网 IPv6;断拒用例显式 allowlist="" 隔离 dev .env。
- T2:rotate 422 detail 加 `(provider: …)` 后缀(既有断言保绿);`im_secret or None` 落 `or ""`(列 NOT NULL,空串=不加签既定编码)。
- T3 修复轮(评审 Important):取消收口段原样 emit eval.completed → 外部 IM 订阅者收「评估完成」假消息;改 `n = 0 if cancelled else emit_event(...)`+回归用例;**eval.cancelled 独立事件类型留 M20**。
- T4:`@click.stop` 防取消按钮冒泡到行点击开抽屉(评审命名风险确认必要)。
- T5:brief 导出顺序断言自相矛盾 → 取契约(id asc);非 dict 数组项整体 422(结构错边界)。
- T7 脚本:计划清单「删 2 导 3 total==3」算术错误 → 修正为 3→1→4;cancel 模式按 ZHIPU_API_KEY 自动选 generation(取消窗口足够)。

**用户走查清单增量**(M19 部分;叠加在 M17+M18 合并清单之上):
1. 运行中的评估(生成模式最直观)→ 操作列「取消」→ 确认 → 状态「取消中」→「已取消」,题数为已完成子集;再触发同库同模式不报 409
2. 题集管理:导出 JSON(浏览器下载)→ 删几题 → 导入该 JSON → 条数恢复;导入含坏行的文件 → 部分成功提示
3. 趋势卡:选一个「跑过但全未设期望」的库 → 「有运行,但所选指标均未测量」空态
4. 出站推送:企微端点创建无密钥弹窗、列表「无需密钥」;编辑企微端点无轮换开关;范围列悬停见 KB 名;切换通道保存后密钥按通道重置(generic 切换弹一次性新密钥)
5. 双主题抽查

**不推送**:按惯例 M17(已推)之后的 M18+M19 合并走查通过后统一推 origin;栈已起(backend/worker/beat/前端)随时可查。

## 执行记录补遗(终审 + 修复波,控制端)

- **终审(whole-branch,d272a05..71cfaf6)**:评审者抽查复跑 M19 测试全绿;四个正确性重点(毒环完备性/取消竞态四方/secret 全读路径/bulk 注入与事务)源码级核查无 Critical。verdict **With fixes**,2 Important:①前端 submit() 仅 rotate 时捕获 PUT 响应的一次性密钥——「切到 generic」新密钥静默丢失(走查清单第 4 条直接矛盾,API 测试测不到的前端半边);②WebhookUpdateIn.im_secret max_length=200 超列宽 64(65~200 字符 asyncpg 截断 500)。
- **修复波 16a7a36**:①捕获条件去 `form.rotate &&` 前置(凡响应带 secret 即弹一次性弹窗)+ vitest;②max_length=64 + 超长 422/边界 200 两路径断言。pytest **477P/0F**、vitest **82/82**、build 零错。复审两 finding ADDRESSED、零新破坏。
- **M20 候选(终审 triage)**:cancel 条件 UPDATE(端点/任务丢更新窄竞态,worker 重启才收口)> `check_url_allowed` urlparse 括号异常包裹(理论逃逸,两调用方已先验)> 平台通道 im_secret_set=false 显「未设置」(前端未用现成字段)> eval.cancelled 独立事件 > 台账测试卫生项(T1 ENFORCE 钉死/deliver_due 整轮回归/T4 spy restore/T5 audit 断言等)。
- 终态:本地 main=16a7a36(M18 d272a05 + M19 十三提交),origin=ac44db0;**M18+M19 合并走查通过后统一推**。
