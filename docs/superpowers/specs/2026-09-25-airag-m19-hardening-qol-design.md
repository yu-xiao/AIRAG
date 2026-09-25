# AIRag M19 设计:出站加固(ALLOWLIST 毒环优先)+ 评估 QoL(取消评估/题集导入导出/趋势空态)

## 背景与动机

M18 webhook 深化完成于本地 main=d272a05(未推送,等 M17+M18 合并走查,走查延后在案)。用户指示「继续M19」;范围卡片未答,按 M13 先例执行推荐默认「**全清治理包**」= M18 终审 triage 全部 rides + M16 遗留评估三项。大件(A2A/MinerU 本地化/LDAP)仍等输入。

当前缺口(2026-09-25 探索实证,行号锚定 d272a05):

1. **ALLOWLIST 拼错毒环(M19 最高优先)**:`webhook_providers.py:185-191` 对白名单条目裸调 `ip_network`——一条拼错(如 `127.0.0.1:8000`)即抛 AddressValueError,逃出 `check_url_allowed`(在 207 行 DNS try 块之外)、逃出 `deliver_one` 的 `except SsrfBlockedError`(outbound.py:121-128,此时 attempts 未自增,行保持 pending)→ `deliver_due` 按 id asc 每轮先撞同一行整轮中断 → **拼错存续期间全部投递永久停滞**(beat 越扫越死);API 建端点直接 500。
2. **wecom 占位 secret 回显**:create 时 wecom 存 `token_hex(16)` 占位(admin.py:252-257)且 **274-275 行把占位值当真密钥一次性明文回显**;列表 masked 伪装「有密钥」;前端弹「密钥已生成」明文弹窗——企微根本无需 secret(key 在 URL)。
3. **切 provider 旧 secret 结转**:PUT 换 provider 只改 provider(admin.py:340-342),旧 secret 原样留用——wecom 占位 hex 带进钉钉加签=永远 310000 dead;feishu 空 secret 带进 generic=空密钥 HMAC。
4. **PLATFORM_TRANSIENT 死表**:常量零代码读取,`classify_response` 实际「非永久即 retry」,表纯注释性死代码。
5. **范围列无 KB 名**:WebhooksPage.vue:379-383 只显数量;`disambiguateKbNames` 现成可用。
6. **轮换开关全通道显示**:编辑态对 wecom 显示,点了 422;钉钉/飞书轮换会用随机 hex 覆盖平台加签密钥(语义错误——平台 secret 来自 IM 后台,不可随机生成)。
7. **.env.example ALLOWLIST 默认 127.0.0.1**:按模板部署的生产默认放行回环,与代码默认(空)不一致。
8. **SSRF 测试缺口**:IPv6 带端口/公网 IPv6 字面量、settings 默认路径、10.0.0.9 宽 CIDR 参数化、坏条目回归,四类零覆盖。
9. **评估不可取消**:run 只能等跑完/失败;取消需协作式(逐题 commit 后检查;worker solo 池 revoke 不可用,M16 已侦察)。孤儿清扫(eval_tasks.py:47 只扫 running)与 409 防重(api/eval.py:273-278)须与新状态对齐。
10. **题集无导入导出**:eval questions 只有单条 CRUD(api/eval.py:165-234);换库/备份/批量建题只能手工逐条。
11. **趋势全未测量不显空态**:有 completed run 但 summary 全未测量时 times 非空、series 全 null(evalTrend.ts:9-23),TrendCard 只看 times(44-45)→ 渲染无数据点空图,与「暂无已完成的运行」无法区分。

## 目标

1. **A1 毒环修复(fail-open)**:白名单解析对坏条目 **skip + `logger.warning`(点名条目)**,不抛——白名单是「放行豁免」机制,跳过坏条目只会更严不会更松;投递/API 路径永不再因解析异常中断。补四类 SSRF 测试(A8)。
2. **A2 wecom secret 净化**:wecom 创建不回显明文(`WebhookCreatedOut.secret: str | None`,wecom 为 None);`secret_masked` 对 wecom 返回空串,前端显示「无需密钥」;docs/webhooks.md 补注。
3. **A3 provider 切换重置 secret**:PUT 变更 provider 时 secret 一律重置——切到 wecom=新占位;切到 generic=自动生成且**响应一次性明文**;切到钉钉/飞书=清空旧值(payload 可随带新 `im_secret`)。审计 detail 记 `provider_reset`。
4. **A6 轮换语义收紧**:`rotate_secret=true` 仅 generic 合法;wecom/钉钉/飞书 422(「平台密钥来自 IM 后台,请直接修改」);前端轮换开关仅 generic 显示。
5. **A4/A5/A7**:删 PLATFORM_TRANSIENT 死表(注释并入 PERMANENT);范围列 el-tooltip 显示 KB 名(disambiguateKbNames);.env.example ALLOWLIST 默认空+注释示例。
6. **B9 取消评估(协作式)**:`EvalRun.status` 增 `cancelling`(过渡)/`cancelled`(终态)——`POST /api/eval/runs/{id}/cancel`(权限同触发;running→cancelling 200,幂等再调 200,终态 409);`run_eval_task` 两处逐题 commit 后 `db.refresh(run)`,`status=="cancelling"` → 停止循环、写 `status="cancelled"` + 已完成子集的 summary/item_count;孤儿清扫 where 扩为 `status.in_(("running","cancelling"))`;409 防重仅查 running 不变(cancelling 视同已结束可新发);前端 running 行加「取消」按钮、cancelling 显示「取消中」tag,hasRunning 扩两值。
7. **B10 题集导入导出**:`GET /api/eval/questions/export?kb_id=`(JSON 附件:`{kb_id, kb_name, exported_at, count, questions:[{question, expect_doc_ids, expect_keywords, reference_answer}]}`——不含 id,可移植);`POST /api/eval/questions/bulk {kb_id, questions:[≤500 条 EvalQuestionIn]}`(逐条校验、**部分成功**:`{created, errors:[{index, detail}]}`);权限同题集 CRUD(owner/admin)。前端 QuestionsTab toolbar「导出」(blob 下载)+「导入」(文件选择→预览条数确认→bulk→报 created/errors)。
8. **B11 趋势空态细分**:TrendCard 判空改「times 为空 **或** 所选指标序列全 null」;全 null 显示「有运行,但所选指标均未测量」。
9. 验收:pytest 全绿(基线 443P,预计 +24~30)+ vitest(基线 74,预计 +6~8)+ build 零错 + 真栈 `m19_acceptance.py` 全 PASS;走查延后在案,推送沿用「合并走查通过后再推」惯例。

## 非目标(明确不做)

- ALLOWLIST fail-closed 方案(坏条目拒绝一切)——豁免机制取 fail-open,更严不更松
- 取消评估的硬杀(revoke/terminate)与多 worker 并发语义(solo 池前提)
- 题集 CSV 格式(JSON 一种;CSV 中文逗号转义不值)
- 导入自动迁移 kb_id(导出可移植无 id;导入显式指定目标库)
- 死表 A4 的「瞬态分级告警」(仅删死代码)
- M18 其余化妆项(PLATFORM_TRANSIENT 之外的)如有留 M20;大件等输入

## A. 出站加固(后端 webhook_providers/admin + 前端 WebhooksPage + docs)

**A1(毒环)**:`webhook_providers.py _allowlist_networks` 逐条:

```python
def _allowlist_networks(raw: str | None) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            # 白名单是放行豁免:坏条目跳过只会更严(拒多放少),
            # 绝不让解析异常逃出去打断投递循环(M19 毒环修复)
            logger.warning(f"WEBHOOK_SSRF_ALLOWLIST 坏条目已跳过: {part!r}")
    return nets
```

**A8 补测**(test_webhook_providers.py 参数化):`http://[2001:db8::1]:8000/x`(带端口 IPv6 直判放行)/`[::1]` 拒;`check_url_allowed(url)` 不传 allowlist 走 settings 默认(monkeypatch settings 两态);`10.0.0.9`/`10.0.0.1`/`192.168.0.9` 无白名单必拒;坏条目(`"127.0.0.1:8000"`、`"not-an-ip"`)→ 函数不抛、坏条目不放行(`http://127.0.0.1/x` 仍拒)+ `deliver_one` 在坏 allowlist 下正常投递公网端点(毒环回归)。

**A2/A3/A6(admin.py)**:
- create:wecom 分支 secret 仍存占位(列非空),但响应 `secret=None`;`_to_out` 对 wecom `secret_masked=""`。
- PUT provider 变更(与现值不同)分支:`provider==wecom→secrets.token_hex(16) 占位`;`generic→token_hex(16) 新密钥,响应 WebhookCreatedOut(明文一次)`;`dingtalk/feishu→im_secret=payload.im_secret or None(secret 列同 wecom 占位)`,响应 WebhookOut;audit changes 加 `{"provider_reset": "old→new"}`。
- rotate_secret:`ep.provider != "generic"` → 422 `"rotate_secret not supported; platform secret comes from the IM console"`。
- docs/webhooks.md:wecom 段补「占位密钥不参与任何签名,不会下发」;平台段补「更换密钥请直接编辑,勿用轮换」。

**A4**:删 `PLATFORM_TRANSIENT` 常量,其 docstring 并入 `PLATFORM_PERMANENT` 注释(「瞬态与未知非零码一律 retry,保守」——行为不变,仅去死代码);若有测试 import 该常量则同步删。

**A5(前端)**:范围列外包 `el-tooltip`(照统计列 397-408 写法),内容为 kbOptions 经 `disambiguateKbNames` 映射的 KB 名列表(空=「全部知识库」);`v-if="row.kb_ids?.length"`。

**A6(前端)**:轮换开关 `v-if="editing && form.provider === 'generic'"`;wecom 行 secret 列显示「无需密钥」(`row.provider==='wecom'` 分支)。

**A7**:`.env.example` 的 `WEBHOOK_SSRF_ALLOWLIST=` 置空,注释改「本地开发需放行回环接收器时显式设 127.0.0.1;生产留空」。

## B. 评估 QoL

**B9 取消**:
- `api/eval.py` 增 `POST /eval/runs/{run_id}/cancel`:权限复用触发端点守卫(kb owner/admin;不可见 404);`run.status=="running"→"cancelling"` commit 后 200(`{id, status:"cancelling"}`);`"cancelling"` 再调 200 幂等;completed/failed/cancelled → 409;audit 动作 `eval_cancel`。
- `eval_runner.run_eval_task`:retrieval(190-195)/generation(206-211)两循环在逐题 commit 后 `await db.refresh(run)`,`run.status == "cancelling"` → break;循环后统一:`if cancelled: run.status="cancelled"`(summary/item_count 仍写已完成子集——数据诚实);终态写法与现 completed/failed 同 commit。自然完成/异常路径不变。
- `eval_tasks._sweep_orphan_runs`:where 扩 `EvalRun.status.in_(("running", "cancelling"))`,error 文案不变(worker 重启收口两类在途)。
- 409 防重不动(仅 running 视为占用;cancelling 可立即新发)。
- 前端 `EvalRunsTab.vue`:状态列 `cancelling` → warning tag「取消中」;running 行操作列加「取消」按钮(confirm→`cancelRun(id)`→刷新);`hasRunning()` 扩 `["running","cancelling"].includes(r.status)`;api/eval.ts 增 `cancelRun`。
- schemas:run 相关 Out 的 status 文案/类型不变(字符串);前端类型扩 `'cancelling' | 'cancelled'`。

**B10 导入导出**:
- `GET /api/eval/questions/export?kb_id=`:权限同列表(owner/admin,同 questions GET 守卫);`Response` 带 `media_type="application/json"` + `headers={"Content-Disposition": f'attachment; filename="eval-questions-kb{kb_id}.json"'}`;体如目标(字段仅四项,题序 id asc)。
- `POST /api/eval/questions/bulk`:body `{"kb_id": int, "questions": [EvalQuestionIn...]}`(去 kb_id 的四字段;≤500 条 else 422);逐条走与单条 POST 相同校验(空题/超长/doc_ids 非正整数等),**部分成功**:合法条全部创建,非法条进 errors(index=数组下标,detail=首条校验错);响应 201 `{created, errors}`;audit 动作 `eval_questions_bulk`。
- 前端 QuestionsTab:toolbar「导出」(`evalApi.exportQuestions(kbId)` blob→`URL.createObjectURL` a[download];无题禁用)与「导入」(hidden input[type=file]→`JSON.parse`→ElMessageBox 确认「将导入 N 题」→bulk→结果 ElMessage(created/errors 数,失败行弹 details);文件解析失败 error 提示)。

**B11 趋势空态**:`TrendCard.vue` render 内:

```ts
  const allUnmeasured = series.length > 0
    && series.every((s) => s.data.every((v) => v == null))
  emptyKind.value = times.length === 0 ? "no-runs" : allUnmeasured ? "unmeasured" : ""
```

(`empty` 布尔改 `emptyKind` 三态;模板:unmeasured→el-empty「有运行,但所选指标均未测量」,no-runs→现有文案;画布 v-if 对应非空。)vitest 补:有 completed run 但 summary 无所选指标 → unmeasured 文案、无画布。

## 配置项

无新增(A7 仅改 .env.example 默认值)。

## 测试与验收

### pytest(基线 443P,预计 +22~28)

- A1/A8:坏条目 skip+warning、四类 SSRF 补测、deliver_one 毒环回归(约 8)
- A2/A3/A6:wecom create 不回显/`secret_masked` 空、provider 切换三向重置(generic 响应明文)、rotate 非 generic 422、审计 changes(约 8)
- B9:cancel 权限矩阵/幂等/409、循环中断(首题后置 cancelling→终态 cancelled+子集 summary)、sweep 扩 cancelling、409 防重仍只查 running(约 8)
- B10:export 形状/权限/空库、bulk 全量/部分成功/上限 500/校验错进 errors(约 6)

### vitest(基线 74,预计 +6~8)

- 取消按钮/取消中 tag/hasRunning 扩(2);导入导出交互(2);趋势 unmeasured 空态(1);范围 tooltip+wecom 无需密钥(1-2)

### 真栈 m19_acceptance.py(新;worker+beat 起;.env 沿用 127.0.0.1 白名单)

①毒环负例:临时端点配公网 URL+坏 allowlist(monkeypatch 不可行,改走单测——**真栈只验正向**:allowlist 含 127.0.0.1 时 create 成功)②cancel:触发 generation run(真 LLM 慢)→ 立即 cancel → 轮询至 cancelled、items<总数、再触发同库同 mode 不 409 ③export→bulk 回环:建 3 题→export 读 JSON→删 2 题→bulk 导回→total 恢复 ④wecom create 响应 secret 为 null、列表 masked 空 ⑤权限负例(viewer cancel/export/bulk 403)⑥清理。若 ZHIPU key 未配,②改 retrieval run+首题后 cancel(轮询窗口内)。

## 风险与权衡

- A1 fail-open:坏条目静默(有 warning 日志)可能让运维以为豁免生效——docs/webhooks.md 补「启动后检查 warning」一句;比毒环与 fail-closed 全拒都优
- A3 切换即重置:generic 切换响应明文一次性——与 create 语义对齐;钉钉/飞书清空旧值要求用户重填,故意为之(旧值必错)
- B9 cancelling→cancelled 窗口:cancel 后任务可能在「最后一题」路上,items 数≤全量是预期;worker 崩溃时 cancelling 被 sweep 收口 failed(与 running 同命,可接受)
- B10 部分成功:调用方须读 errors——前端弹详情兜底
- 走查延后+不推送沿用惯例;M19 提交叠在未推的 M18 之上(合并走查后一并推)
