# AIRag M20 设计:取消竞态收口 + eval.cancelled 事件 + 治理小包

> 基线:本地 main=7d1e044(M19 终态;origin=ac44db0,按惯例 M18+M19+M20 合并走查后统一推)。
> 范围:M19 终审 triage 的五项候选,按 M13/M19 先例默认全清。大件(A2A/MinerU 本地化/LDAP)仍等输入,不在本里程碑。

## A. 取消竞态收口(端点/任务双侧条件 UPDATE)

M19 的取消是协作式:端点置 `cancelling`,任务循环逐题 commit 后 refresh 检查。终审 triage 指出两个丢更新窄竞态:

| 竞态 | 时序 | 后果(M19 现状) |
|---|---|---|
| A1 任务盖端点 | 端点在任务**最后一次 refresh 之后**、收口 commit 之前置 cancelling | 任务无条件写 `completed`,用户的取消被覆盖(且收到假「评估完成」) |
| A2 端点盖任务 | 任务收口 commit `completed` 落在端点**读 status 之后**、端点 commit 之前 | 端点无条件写 `cancelling`,终态被改写,c run 卡 cancelling,只能等 worker 重启 sweep 收口 failed |

**修复原则:终态只能由条件 UPDATE 落库,谁的条件不满足谁让路。**

### A-1 任务侧:抽取 `_finalize_run`(`app/services/eval_runner.py`)

```python
async def _finalize_run(db, run_id, kb_id, mode, results, cancelled):
    """M20 条件收口:completed 只许写在仍是 running 的行上;
    cancelled 写在 cancelling 上。读后竞态(A1)由 fallback 分支兜住。
    返回 (final_status, n_deliveries);行已不属于本任务(双miss)返回 (None, 0)。"""
```

- 第一跳:`UPDATE ... SET status=完成/取消, summary, item_count WHERE id=? AND status=('cancelling' if cancelled else 'running')`。
- `cancelled=False` 且零行命中(A1 竞态):fallback `UPDATE ... WHERE status='cancelling'` → 按取消收口;再零行(行被 sweep 等他人收口)→ 不写不发,返回 (None, 0)。
- 事件按 final 分流:completed→`eval.completed`,cancelled→`eval.cancelled`(B 节);同事务 commit;返回 n 供调用方 `nudge()`。
- `run_eval_task` 两循环后的收口段替换为 `final, n = await _finalize_run(...)`;异常路径(failed + eval.failed)不变——失败收口与 sweep 同语义,无条件写合理。

### A-2 端点侧:`cancel_run`(`app/api/eval.py`)

保留 404/owner/终态 409/幂等前置;置位改为条件 UPDATE:

```python
res = await db.execute(update(EvalRun)
    .where(EvalRun.id == run_id, EvalRun.status == "running")
    .values(status="cancelling"))
if res.rowcount == 0:            # A2 竞态:读后任务已收口
    await db.refresh(run)
    if run.status == "cancelling":
        return {"id": run_id, "status": "cancelling"}   # 幂等
    raise HTTPException(status_code=409, detail="run already finished")
# 命中才 audit + commit
```

**测试策略**:任务侧竞态(A1)与双 miss 由 `_finalize_run` 单测直测三分支;端点侧竞态(A2)用 guard-flip seam——monkeypatch `_require_kb_owner` 在守卫内先经测试会话把 run 翻成 completed/cancelling 再放行,确定性复现「读后更新前」窗口。既有 M19 取消测试全部保持语义。

## B. eval.cancelled 独立事件

- `outbound.EVENT_TYPES` 扩 `"eval.cancelled"`;`_event_kb_ids` 的 eval 分支元组同步扩(`run.kb_id` 过滤)。
- `webhook_providers.describe_event` 增分支:「**评估已取消**」+ 知识库/模式 + 题数(已完成子集,数据诚实)。
- 发射点:`_finalize_run` 的 cancelled 终态(含 A1 fallback)。`eval.completed` 在取消路径仍不发自(M19 T3 修复波语义不回退)。
- 前端:`WebhooksPage.vue` `EVENT_OPTIONS` 增 `{ value: 'eval.cancelled', label: '评估已取消' }`(注释「五事件」改「六事件」);投递记录标签自动跟随 EVENT_LABEL。
- 文档:`docs/webhooks.md` 事件列表与负载示例补 eval.cancelled。
- **既有测试改写**:`test_eval_cancel.test_cancelled_run_emits_no_completed_event` —— 取消后全订阅端点恰一行 `event_type=='eval.cancelled'`、无 eval.completed 行、nudge 被调(n>0)。

## C. check_url_allowed 括号异常包裹

`urlparse('http://[::1')` 族在括号不配对时直接抛 ValueError,穿透 `check_url_allowed` 逃逸(两调用方 create/deliver 均有前置校验,纯理论,但要堵)。修复:urlparse 与 hostname 取值包 try/except ValueError → `raise SsrfBlockedError(f"url malformed: ...")`。新增用例:不配对括号两形态抛 SsrfBlockedError 而非 ValueError。

## D. 平台通道密钥列显「已设置/未设置」

后端 `WebhookOut.im_secret_set`(M19 已有)前端未用:钉钉/飞书端点的 secret 列现在显示占位 secret_masked,误导(真签名密钥在 im_secret 位)。修复:

- `frontend/src/api/admin.ts` `WebhookOut` 补 `im_secret_set: boolean`。
- `WebhooksPage.vue` secret 列三分支:wecom→「无需密钥」(不变);dingtalk/feishu→`im_secret_set ? '已设置' : '未设置'`(未设置用灰字 no-secret 样式);generic→secret_masked(不变)。
- vitest:钉钉行未设置/已设置两断言。

## E. 测试卫生四件(终审台账)

1. **ENFORCE 钉死**:conftest 的 SSRF autouse 夹具(现只 fake `_resolve_host`)补 `WEBHOOK_SSRF_ENFORCE=True` 钉死——dev `.env` 关断时 `test_deliver_one_survives_bad_allowlist` 的毒环前提 silently 失效。局部显式设 False 的用例(test_outbound:366)不受影响(monkeypatch 后写优先)。
2. **deliver_due 整轮回归**:一条用例走完状态机——pending→500→retrying(attempts=1,退避+1min)→手动到期→二轮 200→succeeded(attempts=2);同轮 404→立即 dead。
3. **T4 spy restore**:`EvalRunsTab.spec.ts` 对 `ElMessageBox.confirm` 的 spyOn 无 restore,泄漏到后续用例;显式变量持有 + afterEach `mockRestore()`。
4. **T5 audit 断言**:`test_eval_questions.test_bulk_creates_all` 补 `eval_questions_bulk` 审计行断言(action/target/detail=={created,errors}),直查 AuditLog 表(不依赖 admin 读权限)。

## 验收门槛

1. pytest 全绿(基线 477P,预计 ~490P);vitest 全绿(基线 82,预计 ~86);build 零错。
2. 真栈 `m20_acceptance.py` 全 PASS:坏括号 URL 建端点被拒;events=['eval.cancelled'] 可建;取消回路(生成模式真 LLM)后投递记录恰一行 eval.cancelled 且 receiver 实收;cancel 幂等/终态 409 回归;平台端点列表 im_secret_set 字段在。
3. 终审 whole-branch;M18+M19+M20 合并走查与推送仍延后(用户指示)。
