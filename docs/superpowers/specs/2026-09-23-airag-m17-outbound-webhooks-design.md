# AIRag M17 设计:出站集成(通用 webhook 推送)

## 背景与动机

出站集成自 M13 起一直列在候选池「仍等输入」。用户拍板(2026-09-23):**出站集成为 M17 主线(纯主线,不捎带小项)**——事件覆盖**文档解析终态 + 评估终态 + 拒答/知识缺口**;通道为**通用 HTTP webhook**(HMAC-SHA256 签名,企微/钉钉/飞书 payload 适配留 M18);管理面为 **admin 全局配置**(per-KB 订阅留 M18);投递管道选**方案 A:DB 为真相源 + 事件点 nudge + beat 兜底双驱动**(对比同步直发(外部抖动即丢事件)与 Celery 内置 retry(状态在 broker、可观测弱),A 与 M15 孤儿清扫同哲学,投递记录页天然有数据)。

现状(探索实证):

- **事件源挂点已明确**:文档终态 `app/workers/pipeline.py:133-135`(done)/`44-56 _mark_failed`(failed);评估终态 `app/services/eval_runner.py:210-213/220-223`;拒答汇聚点 `app/services/agent_facade.py agent_ask()` 尾部(REST+MCP 双面)与 `app/api/ask.py:96-115`(Web SSE)。
- **基础设施现成**:Celery solo worker + 独立 beat(`start_worker.bat`/`start_beat.bat`),beat_schedule 内联于 `celery_app.py:19-24`;新任务模块须进 `include` 列表;httpx≥0.27 已是依赖。
- **M15 教训必须规避**:worker 每任务一个新事件循环,进程级异步客户端单例跨循环即毒化(NoneType.send)——投递用**每次调用新建、随调用关闭**的 `httpx.AsyncClient`(不缓存),DB 走任务自持 NullPool 引擎(eval_tasks 同款)。
- **管理面模式现成**:`require_admin`(`core/deps.py:32-35`)、admin keys 明文一次性返回模式(`api/admin.py:91-150`)、audit-logs 分页信封(`admin.py:131-160`)、前端 admin 页复刻模式(AuditLogPage/KeysPage + MainLayout admin 组 + router children)。
- **迁移链**:当前 head `f6a7b8c9d0e1`,新迁移挂其后;文件名 `{12hex}_m17_webhook_tables.py`。

## 目标

1. 五类事件出站:`document.done` / `document.failed` / `eval.completed` / `eval.failed` / `chat.refused`(含 REST/MCP/Web 三来源面)
2. 通用 webhook 端点管理:admin CRUD + 事件订阅 + 启停 + secret 签发(明文一次性返回、GET masked)+ 测试发送
3. 投递语义:at-least-once、退避重试(1/5/15/60 分钟,上限 5 次)、4xx 永久失败、HMAC-SHA256 签名 + 时间戳、payload 唯一 event_id 供幂等
4. 投递记录:admin 分页查询(状态/端点/事件过滤),前端 WebhooksPage 双页签
5. 验收:pytest 全绿(基线 348P,预计 +18~24)+ vitest(基线 61,预计 +4~6)+ build 零错 + 真栈 `m17_acceptance.py`(内置本地 receiver,零外部依赖、无 LLM 依赖)全 PASS + 用户走查

## 非目标(明确不做)

- 企微/钉钉/飞书群机器人 payload 适配(通道即 URL+格式,后续按需加格式模板;M18 候选)
- per-KB 订阅与 owner 级配置(M18 候选)
- 投递重放/手动重投按钮(M18 候选)
- 顺序保证与精确一次(文档明示 at-least-once + event_id 幂等键)
- 出站 URL SSRF 防护(admin 信任边界内;私网地址黑名单列 M18 候选,如实告知)
- 出站内容的 LLM 摘要/改写
- 审计全量出站、每次问答出站(用户已否)

## A. 数据模型(2 表,零既有表改动)

新模块 `app/models/webhook.py`(登记进 `models/__init__.py`),迁移 `{12hex}_m17_webhook_tables.py`,`down_revision="f6a7b8c9d0e1"`。

**`webhook_endpoints`**(TimestampMixin):

| 列 | 类型 | 说明 |
|---|---|---|
| id | PK | |
| name | String(100) | 唯一非空,展示名 |
| url | String(500) | 非空,http/https |
| secret | String(64) | **原文存储**(HMAC 签名需原文);GET/PUT 只回 masked(`wh_****尾 4 位`);POST 未提供则 `secrets.token_hex(16)` 生成 |
| events | JSON | 订阅事件类型数组,值域校验 ⊆ 五事件;空数组=订阅全部 |
| enabled | Boolean default true | false 不参与投递 |
| created_by | Integer | 触发创建的 admin uid |
| description | String(200) nullable | 备注 |

**`webhook_deliveries`**(TimestampMixin):

| 列 | 类型 | 说明 |
|---|---|---|
| id | PK | |
| endpoint_id | FK webhook_endpoints.id CASCADE | |
| event_type | String(32) | 触发时的类型快照 |
| event_id | String(36) | uuid4.hex,随 payload 出站作幂等键 |
| payload | JSON | 事件快照(信封含 event_id/event_type/occurred_at/data) |
| status | String(16) | `pending`(未投)/ `succeeded` / `retrying`(失败待重试)/ `dead`(终态失败) |
| attempts | Integer default 0 | 已尝试次数 |
| next_attempt_at | DateTime nullable | retrying 状态的到期时间 |
| response_status | Integer nullable | 最近一次 HTTP 状态码 |
| last_error | String(500) nullable | 最近失败原因(截 500) |

状态机:`pending → succeeded | retrying →(到期)succeeded | retrying | dead`;非 2xx 且非 429/5xx 的 4xx → **直接 dead**(对方拒收,重试无意义);attempts 达上限(默认 5)→ dead。

## B. 事件发射与挂点(services/outbound.py)

**`emit_event(db, event_type, data) -> int`**:与业务**同事务**(audit() 同哲学,不自行 commit)查 `enabled 且 (events 为空或含 event_type)` 的端点 → 逐端点插 `pending` delivery(payload=信封快照)→ 返回行数。**无订阅端点=零行零任务,系统默认关闭**。

**`nudge()`**:`deliver_pending.delay()` fire-and-forget——**必须在业务 commit 之后调用**(事务未提交时任务扫不到行,空转无害);五挂点各自在终态 commit 后调用。早到的 nudge 空转由 beat 兜底覆盖,at-least-once 成立。

**payload 信封**:`{"event_id": uuid4hex, "event_type": ..., "occurred_at": iso8601, "data": {...}}`;签名对**完整 body 的 JSON 序列化串**计算。

**五事件 data**:

| 事件 | 挂点 | data |
|---|---|---|
| document.done | pipeline.py:135 commit 后 | `{document: {id, kb_id, filename, chunk_count}}` |
| document.failed | _mark_failed 54 commit 后 | `{document: {id, kb_id, filename}, error: 截500}` |
| eval.completed | eval_runner.py:213 commit 后 | `{run: {id, kb_id, mode, item_count, summary}}` |
| eval.failed | eval_runner.py:223 commit 后 | `{run: {id, kb_id, mode}, error: 截500}` |
| chat.refused | agent_facade.agent_ask() 尾部 + ask.py:102-110 落库后(commit 后) | `{source: "rest"\|"mcp"\|"web", kb_ids: [..], question: 截500}` |

(挂点函数签名/所属会话以实施时现状为准,表内行号为写作时锚点;worker 侧挂点用任务自持会话,API 侧用请求会话,均同事务。)

**签名三请求头**:`X-AIRag-Event`(event_type)、`X-AIRag-Timestamp`(unix 秒)、`X-AIRag-Signature = hex(HMAC-SHA256(secret, f"{ts}.{body}"))`;`Content-Type: application/json; charset=utf-8`。时间戳容差校验由接收方负责(文档明示,建议 5 分钟)。

## C. 投递引擎(services/outbound.py + workers/webhook_tasks.py)

**`deliver_due(db, client=None) -> int`**(async,核心函数,可测;`client` 可注入 AsyncClient 实例供单测,缺省内部 `async with httpx.AsyncClient()` 新建):

1. 扫描 `status='pending' OR (status='retrying' AND next_attempt_at <= now())`,按 id asc 批量(每批 50,循环至尽)
2. 每行:attempts+=1;POST(url, headers=签名头, content=body, timeout=WEBHOOK_TIMEOUT_S)——**客户端随调用新建随调用关闭,绝不跨调用缓存(M15 毒化教训)**
3. 结果落库(逐行 commit,与 eval 逐题进度同哲学):
   - 2xx → succeeded,记录 response_status
   - 429 / 5xx / httpx 异常(超时/连接失败)→ attempts < MAX → retrying + next_attempt_at=now+BACKOFF[attempts-1];attempts ≥ MAX → dead;记 last_error
   - 其余 4xx → dead(永久,记 last_error="permanent 4xx")
4. endpoint 已删/已禁用:投递前复查 enabled,禁用则本轮跳过(保持 pending,不耗 attempts)

**退避表**(内置常量,分钟):`[1, 5, 15, 60, 60]`;`WEBHOOK_MAX_ATTEMPTS=5`、`WEBHOOK_TIMEOUT_S=10`(见配置节)。

**任务**:`app/workers/webhook_tasks.py` → `@celery_app.task(name="app.workers.webhook_tasks.deliver_pending")`,`_run_async` 跑 `deliver_due`(NullPool 引擎自持 + finally dispose,eval_tasks 同款);模块加进 `celery_app.py` include。

**beat**:`celery_app.py` beat_schedule 加 `webhook-delivery-scan`(每 60s)——兜底:nudge 丢失、worker 重启窗口、retrying 到期。任务幂等(状态机保证并发扫描双投可能,窗口极小且 at-least-once 语义下可接受;行级 `SELECT ... FOR UPDATE SKIP LOCKED` 不引入(SQLite 不支持),单 worker solo 池实际无并发)。

## D. 管理 API(api/admin.py,全 require_admin + audit)

| 端点 | 语义 |
|---|---|
| POST /api/admin/webhooks | 建;body: name/url/events/description/secret(可选);secret 未提供自动生成;响应含 **secret 明文(仅此一次)** |
| GET /api/admin/webhooks | 列表(name/url/enabled/events/统计字段),secret masked |
| PUT /api/admin/webhooks/{id} | 改 name/url/events/enabled/description;body 带 `rotate_secret: true` → 新 secret,响应明文一次;不带则 secret 不动(不接受明文写回) |
| DELETE /api/admin/webhooks/{id} | 204;级联删投递记录 |
| POST /api/admin/webhooks/{id}/test | 同步直发一条 `test` 事件(绕过订阅过滤与异步队列,内联调投递单行逻辑),返回 {status, response_status, error?}——配置页即时验证用 |
| GET /api/admin/webhook-deliveries | 分页 `page/page_size(≤100)` + 过滤 `endpoint_id/event_type/status`;audit-logs 同信封 `{total, items}`;id desc |

校验:url http(s) 且 ≤500;name 唯一(409);events 值域 422。schemas: `WebhookCreateIn/WebhookUpdateIn/WebhookOut(secret masked)/WebhookDeliveryOut`。

## E. 前端 WebhooksPage(frontend/src/pages/admin/WebhooksPage.vue)

- 路由 `/admin/webhooks`(lazy children + meta.title=`出站推送`),MainLayout admin 组菜单项(现有图标集内选,如 Promotion)
- 双页签(M15 EvalPage 模式):
  - **端点管理**:表格(名称/URL(mono 截断)/订阅事件 tag/启停 switch/最近统计/操作:测试·编辑·删除);新建/编辑对话框(name/url/description/事件订阅多选/「自动生成或自定义 secret」——自定义仅创建时;编辑对话框 rotate_secret 开关);secret 创建/轮换成功后**一次性弹窗展示+复制**(ElMessageBox 或组件内 dialog+复制按钮),提示不再可见
  - **投递记录**:表格(时间/端点名/事件 tag/状态 tag:succeeded 绿·retrying 橙·dead 红·pending 灰/attempts/response_status/last_error tooltip);筛选(端点/事件/状态)+ 分页(audit-logs 模式)
- api/admin.ts 增 6 函数;vitest(≥4 用例:端点表渲染+masked 无明文/建端点表单与一次性 secret 展示/测试发送反馈/记录页状态 tag 与过滤)
- 双主题沿用现有 CSS 变量体系

## 配置项

`app/core/config.py` + `.env.example`(M17 注释组):

- `WEBHOOK_MAX_ATTEMPTS: int = 5`
- `WEBHOOK_TIMEOUT_S: int = 10`

(退避表为内置常量;无全局开关——无 enabled 端点即零投递,天然关闭。)

## 测试与验收

### pytest(基线 348P,预计 +18~24)

- 模型/迁移:两表可建可回滚(既有 migration 测试模式若有则挂,无则以 CRUD 用例代)
- outbound 服务:emit_event 展开(订阅匹配/空订阅=全部/无端点零行/禁用排除)、payload 信封与幂等 event_id、签名公式(已知 secret/ts/body 断言 HMAC hex)、deliver_due 状态机全分支(2xx/429 重试/5xx 重试/超时异常重试/4xx dead/attempts 耗尽 dead/退避时刻计算/禁用端点跳过不耗次)
- 挂点:pipeline done/failed、eval_runner completed/failed、agent_facade refused、ask.py SSE refused 各发对事件(含 commit 后 nudge 被调,monkeypatch 断言)
- worker:deliver_pending 任务壳跑通(eager)+ NullPool dispose 模式(eval_tasks 同款用例)
- API:CRUD 权限矩阵(admin 200/非 admin 403/未登录 401)、name 409、events 422、secret 明文仅 POST/rotate 出现、masked 不泄、test 端点内联投递(mock httpx)、deliveries 过滤分页
- 复用既有夹具(client/auth_headers/db_session/_promote_admin);httpx 全 mock(respx 不引,直接 monkeypatch httpx.AsyncClient 或注入 client_factory)

### vitest(基线 61,预计 +4~6)

见 E 节;组件测试沿用 mount+ElementPlus+mock api 模式。

### 真栈 m17_acceptance.py(新,worker+beat 在跑)

内置**本地 receiver 线程**(http.server 落盘收包:headers+body,脚本自验签)零外部依赖;流程:

1. admin 登录;receiver 起在随机端口;POST /admin/webhooks 指 receiver(secret 自动生成,读响应明文)
2. 触发 `eval.completed`:建 KB+题集+retrieval run → 轮询 completed → receiver 收包验签+事件体断言
3. 触发 `chat.refused`:agent REST ask(api key,M10 模式)问库外问题 → refused → 收包断言 source=rest
4. 触发 `document.failed`:上传损坏 PDF(内嵌坏字节)→ worker 解析失败 → 收包断言
5. 触发 `document.done`:上传内嵌最小合法 PDF → done → 收包断言
6. 4xx 永久失败:临时端点指向 receiver 的 /reject 路径(返回 404)→ 投递 dead 断言
7. nudge 缺失兜底:制造一条 pending 行(直插)→ 不触发任何事件 → 等 beat 60s 扫描 → 投递成功断言
8. 权限负例:非 admin 建/删端点 403;deliveries 非 admin 403;清理端点+验收 KB/用户(m15 清理模式)

(顺序可并;每步 check() + summary_and_exit,head 注明「worker+beat 必须 start_worker.bat/start_beat.bat 已起」。)

## 风险与权衡

- **at-least-once 可能重复投递**:接收方按 event_id 幂等——文档明示,验收断言 event_id 存在
- **beat+任务双驱动窗口内双投**:solo 池无实际并发;若未来多 worker 需行锁——spec 明示现状前提
- **SSRF**:admin 可配内网 URL——admin 信任边界内,黑名单列 M18 候选(已如实告知用户)
- **secret 原文落库**:泄露面=DB 与 admin;与 ApiKey hash 模式的差异明示(HMAC 需原文),GET/PUT 全 masked + 审计留痕补偿
- **beat 进程未起时重试延迟**:文档注明生产须跑 start_beat.bat(与审计清理同依赖)
