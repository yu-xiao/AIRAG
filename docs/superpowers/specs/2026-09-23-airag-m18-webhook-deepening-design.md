# AIRag M18 设计:Webhook 深化(平台适配/per-KB 订阅/重投/SSRF)与 M17 收尾

日期:2026-09-23 · 前置:M17(main=ac44db0) · 范围拍板:A(Webhook 深化)+ B(M17 七小项);C(M16 遗留三项)留 M19;M17 用户走查随 M18 合并(清单入 M18 计划文档)

## 背景与动机

M17 交付通用 webhook(五事件/HMAC/退避重试/admin 面),遗留四项深化候选与七项收尾小项(终审 triage 全部保留):

1. **平台适配**:国内企业真实场景是企微/钉钉/飞书群机器人,它们不认 JSON 信封——要求平台格式消息,且成功判定必须看 body 错误码(三平台都以 HTTP 200 包裹业务错误,现引擎只看状态码会把失败当成功)
2. **per-KB 订阅**:多团队共用部署时,端点只关心自己的知识库;现状全量广播噪声大
3. **重投**:瞬态误判 dead 或对方短暂故障后,唯一出路是重触发事件;需要 admin 手动重投
4. **SSRF**:端点 URL 由 admin 配置,指向内网是真实风险(admin 误配/账号失守纵深防御)

## 目标

- 端点可声明 provider:generic(默认,行为与 M17 完全一致)/wecom/dingtalk/feishu;平台端点按各平台格式构造消息、加签与响应分类
- 端点可订阅 KB 子集(空=全部,向后兼容);五事件按 kb 命中过滤
- admin 可对 dead/retrying 投递手动重投(审计留痕)
- SSRF 双检查点(建改端点 422 拦截 + 投递前复核转 dead),可配置白名单
- M17 七小项全部落地(统计列/3xx 文档/URL 长度/筛选重查/轮上限/description 清空/copySecret 降级)

## 非目标(明确不做)

- 平台消息卡片(interactive card)、@人、富文本 post——v1 用 markdown/text 平文,格式留扩展
- 重定向跟随(3xx 仍按 M17 语义=永久拒收;不跟随即无重定向 SSRF 扩散面)
- DNS rebinding 全防护(校验与连接两次解析的 TOCTOU 竞态:文档明示为已知限制;admin 信任边界内的纵深防御定位)
- 事件重放窗口/按 event_id 查询 API;订阅粒度到文档级;MCP 出站
- C 集群(M16 遗留:取消运行中评估/题集导入导出/趋势空态)——M19
- 平台官方应用 API(仅群机器人 incoming webhook,无需 appkey/token 管理)

## A. 数据模型与迁移(alembic `b0c1d2e3f4a5_m18_webhook_provider_and_kb_scope`,down=a8b9c0d1e2f3)

`webhook_endpoints` 加两列,零新表、零既有表改动:

- `provider: String(16) NOT NULL server_default='generic'`——存量端点自动 generic,M17 行为零变化
- `kb_ids: JSON NULL`——语义同 events 列:None/[] = 订阅全部 KB;非空 = kb id 列表(创建/更新时校验存在性;KB 删除时同事务剔除,见 C)

`WebhookDelivery` 不动(payload 快照原则:投递行的消息构造在投递时按端点当下配置进行,端点改 provider 不影响已落库行——事实上构造发生在 deliver_one 内,本就如此)。

## B. 平台适配器(新 `app/services/webhook_providers.py`)

零 DB 依赖(deliver_one 注入调用),单测友好:

```python
build_request(provider, secret, event_type, payload, url) -> (url, body_str, headers)
classify_response(provider, status_code, body_text) -> ("succeeded"|"retry"|"dead", err_detail|None)
describe_event(event_type, data) -> str      # 事件 → 中文 markdown 平文(全平台共用文案源)
async def check_url_allowed(url, allowlist) -> None | SsrfError   # 模块内唯一含 IO 项(async getaddrinfo)
```

### B1. build_request

- **generic**(默认):与 M17 逐字节一致——body=信封 JSON,headers=X-AIRag-Event/Timestamp/Signature 三头(HMAC-SHA256(f"{ts}.{body}));test 端点路径不变
- **wecom**:body=`{"msgtype":"markdown","markdown":{"content": <md>}}`,URL 原样;secret 不参与(建端点时自动生成占位随机串维持非空列,rotate_secret 对 wecom 422)
- **dingtalk**:body=`{"msgtype":"markdown","markdown":{"title":"AIRag 事件通知","text": <md>}}`;secret 非空时加签:ts=毫秒,sign=base64(HMAC-SHA256(key=f"{ts}\n{secret}", msg=b"")),URL 追加 `&timestamp={ts}&sign={urlencode(sign)}`(机器人 URL 必带 query,追加用 `&`;secret 空=不加签,适配"自定义关键词"安全模式)
- **feishu**:body=`{"msg_type":"text","content":{"text": <md 平文>}}`;secret 非空时 body 增 `"timestamp": "{秒}", "sign": base64(HMAC-SHA256(key=f"{ts}\n{secret}", msg=b""))`(与钉钉同构,仅秒/毫秒与承载位置不同);secret 空=不加签(适配 IP 白名单模式)
- 平台端点 headers 只发 `Content-Type` + `X-AIRag-Event`(调试便利);**不发签名三头**——secret 语义已变为平台加签,发 X-AIRag-Signature 会误导接收方

### B2. classify_response(核心:三平台 HTTP 200 包业务错误)

通用骨架:HTTP 2xx 且平台码=0 → succeeded;平台码在瞬态集 → retry(走既有退避);在永久集 → dead;**未知非零 → retry(保守:at-least-once 哲学,误判可由重投救;限频是最常见瞬态)**。非 2xx 走 M17 原状态码规则不变。

| provider | 成功字段 | 瞬态集(retry) | 永久集(dead) |
|---|---|---|---|
| wecom | `errcode==0` | 45009(频率限制) | 93000(URL 不合法/机器人被移除) |
| dingtalk | `errcode==0` | -1(系统繁忙)、90001(发送过快) | 310000(安全设置校验未通过:keywords/sign/ip) |
| feishu | `code==0`(兼容旧 `StatusCode==0`) | 9499(频控,飞书限 100 次/分钟·5 次/秒) | —(空:签名错码语义混杂,统一走重试耗尽) |

body 非 JSON(如网关 HTML 错误页)→ 按 HTTP 状态码原规则。实施时以官方文档复核上表,有出入回写本 spec:企微 [消息推送配置](https://developer.work.weixin.qq.com/document/path/91770)、钉钉 [自定义机器人](https://open.dingtalk.com/document/robots/custom-robot-access)、飞书 [自定义机器人](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)。

### B3. describe_event(中文文案源,test 事件含 data.message 分支,未知事件 JSON 摘要兜底)

- `document.done`:**文档解析完成** · 知识库 #{kb_id} · {filename}
- `document.failed`:**文档解析失败** · 知识库 #{kb_id} · {filename} · {error 前 200 字}
- `eval.completed`:**评估完成** · 知识库 #{kb_id} · {mode} · {item_count} 题 · 摘要 {summary 关键指标}
- `eval.failed`:**评估失败** · 知识库 #{kb_id} · {mode} · {error 前 200 字}
- `chat.refused`:**问答拒答** · {source} · 知识库 {kb_ids} · {question 前 100 字}
- `test`:**AIRag webhook 测试消息**(data.message)

超长统一按 UTF-8 字节安全截断至 3800(企微 markdown 上限 4096 字节,留余量),尾部附 `…(截断)`。

### B4. deliver_one 改造

构造/分类委托适配器;SSRF 检查(见 E)插在 POST 前;其余状态机(退避表/attempts/dead 判定/禁用跳过)不动。

## C. per-KB 订阅

**emit_event 过滤**(outbound.py,签名不变):

- `_event_kb_ids(event_type, data) -> set[int] | None`:document.* 取 `data["document"]["kb_id"]`;eval.* 取 `data["run"]["kb_id"]`;chat.refused 取 `data["kb_ids"]`(集合);未知/缺失 → None
- 匹配规则:`ep.kb_ids` 为 None/[] → 命中(订阅全部);非空 → 事件 kb 集合与之交集非空才命中;事件 kb 为 None(理论上五事件都有,防御)→ 命中(宁可多投,at-least-once)

**KB 删除清理**(kb_ops 删除流程同事务):全端点扫描 `kb_ids` 含该 id 的剔除之(量级:端点数 × 小 JSON 列,循环即可)。

**API 校验**:create/update 传 kb_ids 时逐 id 查 KnowledgeBase 存在,含未知 id → 422 `unknown kb id`。

## D. 重投(admin API + UI)

`POST /api/admin/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver`(require_admin):

- 前置:投递行存在且属于该端点(404);状态 ∈ {dead, retrying} 才可重投,否则 422(防 succeeded/pending 重复触发)
- 动作:`attempts=0, status="pending", next_attempt_at=None, last_error=None`,commit 后 nudge(近即时投递);audit `webhook_redeliver` target=`webhook:{eid}` detail={delivery_id, event_type, response_status(旧)}
- UI:投递页签行内「重投」按钮(仅 dead/retrying 行显示)+ ElMessageBox 确认;成功 toast + 刷新列表

## E. SSRF 防护(双检查点 + 白名单)

**判定**(async `webhook_providers.check_url_allowed(url, allowlist)`,异常即违规):

- 解析 host:IP 字面量直判;域名用 `asyncio.get_running_loop().getaddrinfo()`(异步安全),**所有** A/AAAA 记录逐一过闸(任一命中即拒)
- 阻断集(双向:IPv4+IPv6):`is_private / is_loopback / is_link_local / is_multicast / is_reserved / is_unspecified`(覆盖 10/8、172.16/12、192.168/16、127/8、169.254/16 含云 metadata 169.254.169.254、fe80::/10、fc00::/7 等)
- 白名单:`WEBHOOK_SSRF_ALLOWLIST` 逗号分隔 IP/CIDR,命中即放行(dev .env 置 `127.0.0.1` 供本地接收器/验收脚本,生产默认空)
- 非 http/https scheme → 拒(HttpUrl 已挡,防御性复查)

**检查点 1(建/改端点)**:admin API create/update 解析校验,违规 422 `URL 指向内网/保留地址,不在白名单`(解析失败按违规处理——DNS 挂了也不该建)。
**检查点 2(投递前)**:deliver_one 每次 POST 前复核(DNS 可能变化/老端点绕过检查点 1);违规 → 直接 dead,`last_error="SSRF blocked: {ip}"`(永久,admin 改 URL 救)。
`WEBHOOK_SSRF_ENFORCE=false` 时两检查点全跳过(应急开关)。

## F. M17 七小项

1. **端点统计列回填**:GET /webhooks 每端点附 `stats: {total, succeeded, pending, retrying, dead, last_activity_at}`(deliveries 单条 GROUP BY endpoint_id 聚合 + MAX(updated_at),Python 合并;无投递端点 stats.total=0);WebhooksPage 端点表「统计」列紧凑展示(如 `42 · ✅40 ♻1 ☠1`,title tooltip 含最近时间)
2. **3xx 语义文档**:新建 `docs/webhooks.md` 接收方指南——通用信封/验签公式/event_id 幂等/at-least-once/3xx=永久拒收不跟随/**三平台 body 码语义表**(B2 全文照录)/各平台加签算法
3. **URL 长度防护**:create/update 显式校验 `len(url) ≤ 500`(HttpUrl 放行 2083 会撑爆 VARCHAR(500)),违规 422 `URL 超过 500 字符上限`
4. **投递筛选重查 vitest**:WebhooksPage 投递页签筛选(端点/事件/状态)变更 → 回第 1 页并重新请求(现状疑缺,实施时查;测试锁定行为)
5. **大积压每轮上限**:`WEBHOOK_DELIVER_ROUND_LIMIT=500`,deliver_due 单次调用真实尝试行数达上限即返回(剩余下轮 beat/nudge 补),防 solo worker 被单一积压独占
6. **description 清空语义**:PUT 传 `description=""` → 存 NULL(清空);不传(None)→ 不变;前端编辑框空提交即清空(与 M17 resetFields 预填守护共存:预填非空、显式清空生效)
7. **copySecret 降级**:一次性 secret 弹窗复制按钮,`navigator.clipboard.writeText` 失败(非安全上下文/权限拒)→ 降级全选文本框 + 警告 toast「剪贴板不可用,请手动复制」,不再静默

## G. 管理 API 变更汇总(api/admin.py + schemas)

| 路由 | 变更 |
|---|---|
| POST /webhooks | +provider(枚举校验 422)/+kb_ids(存在性校验);secret 规则按 provider(generic 必填可自定义/wecom 自动占位/dingtalk+feishu 可空);+URL 长度/SSRF 检查;审计 detail 增 provider/kb_ids |
| PUT /webhooks/{id} | 同上;description="" 清空;rotate_secret 对 wecom 422;改 url 时重跑 SSRF/长度检查 |
| GET /webhooks | WebhookOut +provider/+kb_ids/+stats |
| POST /webhooks/{id}/test | 走适配器(平台端点发平台格式测试消息)——现状已调 deliver_one,适配后自动生效 |
| POST /webhooks/{eid}/deliveries/{did}/redeliver | 新(见 D) |
| GET /webhook-deliveries | 不变 |

## H. 前端 WebhooksPage(沿用双页签/双主题体系)

- **端点表**:provider tag(generic 灰/企微绿/钉钉蓝/飞书青 或现有色板)、订阅范围列(事件 tag 现状 + KB 范围「全部」/「N 库」tooltip 列名)、统计列(小项 1)
- **新建/编辑对话框**:provider 下拉(联动:secret 字段说明与必填性——generic「自动生成或自定义」/wecom「不需要」/钉钉飞书「平台加签密钥,未开启加签可空」;URL 占位文案按 provider 换机器人 URL 样例);KB 多选(el-select multiple,选项来自现有 /api/kbs,空=全部知识库)
- **投递页签**:重投按钮(D);筛选重查修复(小项 4)
- **secret 弹窗**:copy 降级(小项 7)
- api/admin.ts 增 redeliver 函数;vitest:provider 联动表单/KB 多选呈现/统计列渲染/重投按钮流转(dead 行可见+确认+调 API)/筛选变更重查/copy 降级

## 配置项(app/core/config.py + .env.example M18 注释组)

- `WEBHOOK_SSRF_ENFORCE: bool = True`
- `WEBHOOK_SSRF_ALLOWLIST: str = ""`(逗号分隔 IP/CIDR;dev .env 置 127.0.0.1)
- `WEBHOOK_DELIVER_ROUND_LIMIT: int = 500`

## 测试与验收

### pytest(基线 381P,预计 +38~46 → ~420-427)

- **providers 单测(纯函数)**:build_request 四 provider 全分支(generic 与 M17 逐字节断言/wecom 消息体/钉钉加签 URL 拼接与未加签/飞书加签 body 字段/截断不劈多字节);classify_response 三平台 × {0 成功/瞬态集永久集/未知码/非 JSON body/非 2xx};describe_event 六事件文案
- **SSRF**:IP 字面量各阻断段/域名多 A 记录任一命中/白名单 CIDR 命中放行/解析失败拒/ENFORCE=false 跳过;create/update API 422;deliver_one 复核转 dead
- **emit kb 过滤**:全订阅命中/子集命中/子集未命中跳过/chat.refused 多 kb 交集/None kb 防御命中
- **重投 API**:dead→pending+attempts 清零+nudge/非 dead-retrying 422/跨端点 404/审计断言
- **统计**:多状态聚合数值/MAX(updated_at)/零投递端点 total=0
- **杂项**:URL 501 字符 422;description="" 清空(None 不变);deliver_due 轮上限(插 >LIMIT 行断言单轮尝试数 ≤LIMIT 且状态推进);KB 删除剔除订阅;wecom rotate 422
- httpx 全 mock(既有 monkeypatch 注入模式);getaddrinfo monkeypatch 控参

### vitest(基线 67,预计 +7~9 → ~74-76)

见 H 节;沿用 mount+ElementPlus+mock api 模式。

### 真栈 m18_acceptance.py(worker+beat 在跑;dev .env 须含 WEBHOOK_SSRF_ALLOWLIST=127.0.0.1)

本地 mock 接收器线程按平台语义应答(零外部依赖,三平台不发真包):

1. **wecom 全链路**:建 wecom 端点指 mock(receiver 回 `{"errcode":0}`)→ 触发 document.done → 收包断言 msgtype=markdown + content 含文案 → 投递 succeeded
2. **平台错误码分类**:receiver 回 `{"errcode":93000}` → 投递立即 dead;回 `{"errcode":45009}` → retrying(attempts=1, next_attempt_at 退避)
3. **钉钉加签**:secret 端点 → 收包方按同算法验 URL timestamp+sign + markdown 结构断言
4. **飞书加签**:收包验 body timestamp+sign 字段 + msg_type=text
5. **per-KB**:端点订阅 KB甲 → KB乙 document.done 无投递行,KB甲 有且 succeeded
6. **重投**:制造 dead(4xx receiver)→ redeliver → 指好 receiver → succeeded
7. **SSRF**:建端点 URL=http://10.255.255.1 → 422;127.0.0.1 mock 放行(白名单生效)
8. **统计与列表**:GET /webhooks stats 与脚本累计对账;provider/kb_ids 回显
9. **杂项**:501 字符 URL 422;PUT description="" → GET null;非 admin 全路由 403;清理端点+验收 KB/用户(m15 清理模式)

## 风险与权衡

- **平台错误码表时效**:非官方逐码全表,瞬态/永久集是精选;未知码保守 retry → 最多 5 次退避(~80 分钟)后 dead,重投可救;文档明示
- **TOCTOU DNS rebinding**:校验与连接两次解析;admin 可信场景定位纵深防御,文档明示(全防护需自建 transport 钉 IP,不成比例)
- **kb_ids JSON 无外键**:KB 删除靠同事务代码剔除(非 DB CASCADE);漏剔最坏后果=死 id 永不命中,无投递泄漏——低危;与 events JSON 同模式
- **重投被滥用刷对方**:admin-only+审计留痕,与信任边界一致;对方按 event_id 幂等(文档已明示)
- **平台消息不含完整信封**:平台端点收摘要非全量数据;需要全量的接收方用 generic 端点(文档明示)
- **secret 占位串(wecom)**:列非空约束妥协;永不参与任何计算/回显,文档注明
