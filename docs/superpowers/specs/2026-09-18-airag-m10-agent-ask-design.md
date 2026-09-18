# AIRag M10 设计:Agent ask 工具(非流式 RAG)与 M9 收尾小项

## 背景与动机

M9 完成了 Agent 对接一期:API Key 体系 + REST/MCP 双面开放知识库发现与混合检索(只读)。当时明确把 `ask` RAG 问答工具分流到二期,条件为"非流式通道 + per-key token 限额"(2026-09-17 M9 spec「M10 候选」节)。

当前缺口:

- 外部 Agent 只能拿 `search` 的检索片段自己拼上下文、自己调 LLM,拿不到与 AIRag Web 端同质量的答案(改写/CRAG/多跳/拒答提示词全部缺席),也拿不到结构化引用与 `refused` 语义
- LLM 生成是计费操作,现有防线只有每分钟频率限流(60/min),无 token 量级控制;一把泄漏的 key 可以无限烧钱
- M9 终审 triage 留下六笔小欠账(Retry-After 头、`AGENT_API_ENABLED=false` 冒烟、redis key 命名对齐 spec、MCP docstring 默认值同源、`last_used_at` 落库断言、`denied_kb_ids` 去重)

用户已拍板的关键决策(2026-09-18):

1. 主线为 ask 工具,REST + MCP 双面,M9 小项全打包
2. **无状态单轮**:query 进、答案出,Agent 自己管理上下文;不落 Conversation/Message 表
3. **每 key 每日 token 配额**(Redis 按日计数自动重置),不做月度/不做仅频率收紧
4. **复用完整问答图**(`build_graph(checkpointer=None)` 非流式 `ainvoke`),不在 facade 里另造精简管线——答案质量与 Web 端一致是 ask 的卖点,也是"同核两面"原则的延续

## 目标

1. `agent_facade.agent_ask`:权限过滤 → 完整问答图(rewrite→retrieve→rerank→grade→[transform/decompose]→generate)→ `answer + citations + refused + tokens_used`
2. 每 key 每日 token 配额:超限 429 + `Retry-After` 头(到次日零点),Redis 降级放行
3. REST `POST /api/agent/ask` 与 MCP `ask_knowledge_base` 工具,共用同一 facade 函数
4. M9 六笔小欠账全部清偿
5. 验收:pytest 全绿 + 真栈 `m10_acceptance.py` + MCP 真客户端走查(复用 `m91_mcp_walkthrough.py` 模式)

## 非目标(明确不做)

- 多轮会话 / conversation_id(无状态单轮;Agent 客户端自管上下文)
- SSE 流式对外(ask 非流式;Web 端 SSE 不动)
- ask 落库 Conversation/Message(不产生会话记录;审计 `agent.ask` 已可追溯)
- 文档写操作、key scope 细化、出站集成、A2A(M9 spec 遗留,继续顺延)
- 图级服务端超时(Web SSE 同语义不设;客户端超时由接入方配置,README 提示)
- 前端改动(零前端提交)

## A. 能力核心:`agent_facade.agent_ask`

```
REST POST /api/agent/ask ─┐
                          ├─► agent_facade.agent_ask(db, user, kb_ids, query, rerank)
MCP ask_knowledge_base ───┘        │
                                   ├─ 权限过滤(与 agent_search 同语义)
                                   ├─ ask_graph = build_graph(checkpointer=None)  # 模块级惰性单例
                                   ├─ ainvoke({question, kb_ids, rerank, history: []},
                                   │          config={"callbacks": [TokenMeter()]})
                                   └─► AskOutcome(answer, citations, refused,
                                                  tokens_used, elapsed_ms)
```

- **权限过滤**:逐 kb_id 查 `get_kb_perm`,无权限或库不存在统一进 `denied_kb_ids` → 抛 `AgentKbDenied`,不泄露存在性;入参 `kb_ids` 先 `list(dict.fromkeys(...))` 去重归一(小项⑥),denied 列表随之无重复
- **图单例**:模块级惰性缓存一个 `checkpointer=None` 的编译图(`make_chat_llm` 已是单例,LLM 客户端复用);与 `api/ask.py` 每次 `build_graph(checkpointer=...)` 互不影响,两份编译实例各自独立
- **checkpointer=None 的红利**:无状态单轮天然规避 checkpointer 跨轮残留类缺陷(M6 教训),且不受 Windows ProactorEventLoop 打断 psycopg checkpointer 的已知坑影响(M6 记录)
- **失败语义**:rewrite/grade/decompose 各节点已有 try/except 降级;generate 失败异常上抛 → REST 500 通用文案 / MCP `ToolError`,日志记全量
- 返回的 `citations` 结构与 Web 端一致:`{number, chunk_id, document_id, filename, page_no, excerpt}`

## B. Token 计量:`TokenMeter` callback

- 自定义 LangChain `BaseCallbackHandler`,`on_llm_end` 累加本次调用的 `total_tokens`
- 取数优先 `generations[0].message.usage_metadata.total_tokens`(langchain 标准字段);实现时兼容 `llm_output["token_usage"]` 老形态;两者皆缺按 0(测试 fake LLM 即 0,不阻断)
- 覆盖图内**全部** LLM 调用(rewrite/grade/decompose/generate),不只 generate——配额语义是"这把 key 今天烧了多少 token"
- langchain-core 1.6 下 `ainvoke` 内部走流式(M9 已知行为),`on_llm_end` 仍每调用恰一次、携带聚合 usage;实现时以集成测试验证,若 usage 缺失则退化计量=0 并在日志 warn 一次
- meter 实例每次 ask 新建(有状态累加器),经 `config={"callbacks": [meter]}` 传入

## C. 每 key 每日 token 配额(扩展 `agent_ratelimit.py`)

- 新增两函数,Redis 结构与风格对齐限流:
  - `quota_check(key_id) -> (ok, retry_after)`:读 `agent_tq:{key_id}:{yyyymmdd}` 当前值,≥ 上限则 `retry_after` = 到次日零点的秒数
  - `quota_consume(key_id, tokens)`:`INCRBY` + `EXPIRE`(当日剩余秒数 + 余量);tokens=0 也调用(幂等无害)
- **check 在请求前、consume 在完成后** → 软上限:单次大消耗可小幅越限(ask 单次量级数千 token,可接受,本文档明示)
- 日界按**服务器本地时区**零点(key 后缀 `yyyymmdd` 与 TTL/Retry-After 同基准,避免跨字段错位)
- 上限 `AGENT_ASK_DAILY_TOKENS` 默认 200000,0=禁用;仅对 api_key 主体生效(JWT 人工调试不限,与限流同原则)
- Redis key 命名(小项③):限流 ident 由 `key:{key_id}` 改为直接传 `key_id`,实际 key 对齐 M9 spec 约定 `agent_rl:{key_id}`;配额 key 同风格 `agent_tq:{key_id}:{yyyymmdd}`
- Redis 异常降级放行(可用性优先,与限流一致;审计兜底可追溯)
- **Retry-After 头(小项①)**:REST 429(新 quota_exhausted 与既有 rate_limited)统一补 `Retry-After` 响应头,body detail 保留 `retry_after` 字段;MCP 无 HTTP 头,以 `ToolError("quota_exhausted, retry_after=Ns")` 文本语义化

## D. REST 面:`POST /api/agent/ask`

- 请求 `AgentAskIn`:`kb_ids: list[int]`(1~5)、`query: str`(1~500)、`rerank: bool = false`——校验边界与 search 完全一致;无 `top_k`(图内定死 `RETRIEVAL_TOP_K`)
- 响应 `AgentAskOut`:`{answer, citations, refused, tokens_used, elapsed_ms}`;**`refused=true` 也是 200**(拒答是合法答案,与 Web 端语义一致)
- 流程顺序:鉴权 → 限流 → 配额 check → facade → 配额 consume → 审计 → commit
- 错误码沿用 M9 约定:401 细分 / 403 `kb_forbidden` + `denied_kb_ids`(已去重)/ 422 pydantic / 429 `rate_limited|quota_exhausted` + `Retry-After` 头
- 审计 `agent.ask`:detail = `{client: rest, key_name, kb_ids, query[:200], refused, tokens, elapsed_ms}`,自动纳入审计留存

## E. MCP 面:`ask_knowledge_base`

- 签名 `ask_knowledge_base(kb_ids: list[int], query: str, rerank: bool = False)`,校验与 ToolError 风格同 `search_knowledge_base`
- 配额 check 在工具内(限流仍在 AgentAuthMiddleware),超限 `ToolError("quota_exhausted, retry_after=Ns")`;consume 在拿到结果后;审计 `agent.ask` client=mcp,ip 走 `current_client_ip`
- docstring 中文写明:直接返回基于知识库生成的答案与引用编号、可能拒答(refused=true 表示知识库无相关内容)、单轮无上下文、耗时可到分钟级
- **docstring 同源(小项④)**:`search_knowledge_base` 的 docstring 改 f-string 引 `settings.RETRIEVAL_TOP_K`(默认值本就取自 settings,消除硬编码"默认 8"漂移;BaseSettings 进程内不变,import 期取值即运行期值)
- README《外部 Agent 接入指南》增补:ask 工具用途、延迟预期(40~90s)与客户端超时建议(如 Claude Code `MCP_TIMEOUT`)

## F. M9 收尾小项清单(全部清偿)

| # | 项 | 落点 |
|---|---|---|
| ① | 429 补 `Retry-After` 响应头 | REST `_check_rate`、配额 429、MCP `_send_json` 支持头参数 |
| ② | `AGENT_API_ENABLED=false` 冒烟 | 测试:agent 路由 404 + `/mcp` 404 + `/api/health` 等 200 |
| ③ | redis key 命名对齐 spec | `agent_rl:{key_id}`(去 `key:` 前缀) |
| ④ | MCP docstring 默认值同源 | search 工具 docstring f-string 化 |
| ⑤ | `last_used_at` 落库断言 | 既有 REST/MCP 测试补 assert(调用后 `last_used_at` 非空/更新) |
| ⑥ | `denied_kb_ids` 去重 | `kb_ids` 归一去重(search 与 ask 同享) |

## 配置项(`.env`,入 `.env.example`)

| 键 | 默认 | 说明 |
|---|---|---|
| AGENT_ASK_DAILY_TOKENS | 200000 | 每 key 每日 ask token 配额,0=禁用 |

## 测试与验收

### 后端 pytest(`.venv`,沿用 conftest 既有 monkeypatch 套路)

- facade:`agent_ask` 正常(fake LLM monkeypatch build_graph 或注入)、denied 去重、`AskOutcome` 字段、TokenMeter 计量(带 usage_metadata 的 stub LLM)+ 缺失按 0
- 配额:`quota_check`/`quota_consume`(fake Redis)、到次日零点 retry_after 计算、Redis 异常降级、`AGENT_ASK_DAILY_TOKENS=0` 禁用
- REST:200(refused 两种)、403、422、429 quota_exhausted + Retry-After 头、审计落库断言
- MCP:`ask_knowledge_base` 真调(session 级 fixture,M9 模式)、quota 超限 ToolError
- 小项:AGENT_API_ENABLED=false 冒烟、last_used_at 断言、429 rate_limited 也有 Retry-After 头

### 无头验收 `backend/scripts/m10_acceptance.py`(真栈,8001)

建用户+授权库+key → ask 正常(答案+引用)→ ask 拒答(refused=true)→ 无权库 403 → 小配额 key 烧穿 → 429 + Retry-After 头 → 审计 `agent.ask` 落库。

### MCP 真客户端走查

复用 `m91_mcp_walkthrough.py` 扩展:ask 真调(答案/拒答两例),Inspector 抽查可选。

## 风险与权衡

- **延迟**:一次 ask 内部 2~5 次 LLM 调用,非流式需等全量,实测预期 40~90s(SSE 首 token ~32s 为参照);客户端超时由接入方配置,README 明示。不设服务端硬超时(与 Web SSE 同语义)
- **计量完备性**:token 数依赖 LLM 响应携带 usage;缺失按 0(低估不阻断,日志可查)。换供应商时需回归验证 usage 字段
- **软上限**:check/consume 两步非原子,单次可越限 ≤ 单次消耗量(数千级),不做 Lua 原子化(复杂度不值)
- **Redis 单点**:降级放行,可用性优先(沿用 M9 裁决)
- **图单例**:`checkpointer=None` 编译图与 SSE 路径的图并存,互不影响;graph 编译开销一次性
- **fastmcp 2.14.7 已知坑**(M9 记录):http_app 挂载/lifespan/session fixture 模式全部沿用,不新增风险面

## M11 候选(本里程碑分流)

- M9 spec 遗留:文档写操作(editor 级 key)、key scope 细化(per-kb/per-capability)、出站集成、A2A 评估
- 历史遗留:KB 删除端点(级联+eval_sets 钩子)、KB 重名 DB 唯一约束+存量去重、评估结果入库/reference_answer、包裹型拒答 LLM 二审、多跳子问题并行检索、MinerU 本地化、LDAP/SSO(仍等输入)
- M10 可能新增:配额余量查询端点/工具、ask 延迟优化(grade 前置剪枝)
