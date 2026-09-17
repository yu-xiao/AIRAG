# AIRag M9 设计:Agent 对接(知识库开放给外部 Agent)

## 背景与动机

AIRag M1-M8 已具备完整的知识库、混合检索、RAG 问答、RBAC 与审计能力,但只能通过自研 Web 前端使用。需求:让外部 Agent 客户端把 AIRag 知识库当作工具调用,方向为**向内开放**(外部 Agent 消费知识库),非向外调用。

当前缺口:

- 认证仅有 账号密码 → 60 分钟 JWT,无 API Key、无撤销机制,不适合机器客户端
- 无任何 MCP/A2A 代码;QA 仅有 SSE 流式端点(一期不开放 ask,不构成问题)
- 审计与权限体系以 `User` 行为核心(`get_kb_perm(db, user, kb)`),外部主体需要映射到用户身份才能复用

需求画像(已与用户确认):

1. 方向:向内,别的 Agent 用我的知识库
2. 目标客户端:未定,要求通用 → 双协议面(MCP + REST/OpenAPI)
3. 认证:API Key 绑定用户、实时继承其 RBAC 权限(类 GitHub PAT)
4. 一期能力:知识库发现 + 混合检索(只读);不做 ask、不做文档写操作

## 目标

1. API Key 体系:创建/列表/吊销,哈希存储、一次性展示、支持过期
2. 一期开放两个只读能力:知识库发现、混合检索(可选 rerank)
3. 双协议面共用一份能力核心:REST `/api/agent/*`(自动进 OpenAPI) + MCP `/mcp`(Streamable HTTP)
4. 审计(agent.* action)与按 key 限流
5. 前端个人设置页密钥管理
6. 验收:无头脚本 + 真 MCP 客户端(Claude Code/ZCode)走查

## 非目标(明确不做)

- `ask` RAG 问答工具(二期:需非流式通道 + per-key token 限额)
- 文档管理写操作(上传/重解析/删除)
- 出站集成(AIRag 调用外部 Agent)
- MCP resources/prompts、A2A 协议
- refresh token / OAuth / key scope 细化(per-kb、per-capability)
- 独立网关进程部署

## 架构总览:同进程一核两面

```
外部 Agent 客户端
 ├─ MCP 原生(Claude Code/Cursor/ZCode) ──► POST /mcp(Streamable HTTP)
 └─ HTTP 通用(Dify/Coze/内部系统) ─────► GET  /api/agent/kbs
                                          POST /api/agent/search
        两条路都带 Authorization: Bearer airag_xxxx
                      │
                      ▼
     统一鉴权 get_agent_principal(识别 JWT 或 API Key,均解析出 User)
                      │
                      ▼
     能力核心 services/agent_facade.py(权限过滤 → 检索 → 审计)
                      │
                      ▼
   零改动复用:hybrid_search + get_kb_perm + audit_logs
```

- 新增代码集中三处:`app/api/agent.py`(REST)、`app/services/agent_facade.py`(核心)、`main.py` 挂载 MCP 子应用(`fastmcp` 的 `http_app()`,ASGI 内嵌,不起新进程)
- 现有 `/api` 路由、SSE 问答、前端功能全部不动
- 备选方案已否决:B(独立 MCP 网关,Windows 运维负担)、C(仅 REST,MCP 原生客户端覆盖不了)

## A. API Key 模型与鉴权

### 表结构 `api_keys`(新增 alembic 迁移)

| 列 | 类型 | 说明 |
|---|---|---|
| id | PK | |
| user_id | FK→users.id | 归属用户 |
| name | varchar(64) | 展示名,如 "Cursor 工作机" |
| key_prefix | varchar(16) | key 前 14 位(`airag_`+8),列表展示与索引查找 |
| key_hash | char(64) | SHA-256 hex,不明文存全量 key |
| is_active | bool, 默认 true | 吊销=false(软删) |
| expires_at | timestamptz, 可空 | 空=永不过期 |
| last_used_at | timestamptz, 可空 | 每次调用更新 |
| created_at | timestamptz | |

- 索引:`key_prefix` 唯一索引(查找入口)
- Key 格式:`airag_` + `secrets.token_urlsafe(24)`;查找 = prefix 定位 → `hmac.compare_digest(sha256(input), key_hash)`
- 每用户上限 `AGENT_MAX_KEYS_PER_USER`(默认 10),防滥发

### 鉴权依赖 `get_agent_principal`(`app/core/deps.py`)

- 解析 `Authorization: Bearer`:值以 `airag_` 开头 → API Key 路径(查库、校验 is_active/expires_at/所属用户 is_active、更新 last_used_at);否则 → 现有 JWT 路径
- 两种路径都返回 `User` 行 + 附加信息(`kind: jwt|api_key`、`key_id`、`key_name`),供审计与限流区分
- **权限实时继承**:`get_current_user` 与 `get_kb_perm` 零改动,key 身份即 User 行;吊销 key 或改用户授权立即生效,不做权限快照
- Agent 端点同时接受 JWT 与 API Key(方便人工 curl 调试);限流仅对 API Key 生效

### 管理端点(REST,JWT-only,不允许用 key 创建 key)

| 端点 | 行为 |
|---|---|
| POST `/api/auth/keys` | `{name, expires_in_days?}` → 201,响应含**唯一一次**明文 key |
| GET `/api/auth/keys` | 列表(无明文):id/name/prefix/is_active/expires_at/last_used_at/created_at |
| DELETE `/api/auth/keys/{id}` | 吊销(仅本人,软删 is_active=false) |

### 前端

- 个人设置页新增"API 密钥"卡片:创建弹窗(命名 + 可选有效期,预设 7/30/90 天/永久)、创建成功一次性展示 key(复制按钮 + "关闭后不再可见"提示)、列表(状态徽章:活跃/已吊销/已过期)、吊销确认
- 沿用现有 indigo 双主题组件体系;admin 用户管理页不掺和(二期可选)

## B. 能力核心 `services/agent_facade.py`

- `list_kbs_for(db, user)` → 用户可访问 KB(owner/editor/viewer)→ `[{id, name, description, my_perm}]`,查询逻辑与 `GET /api/kbs` 一致
- `agent_search(db, user, kb_ids, query, top_k, rerank)`:
  1. 逐 kb_id 查 `get_kb_perm`,无权限**或库不存在**统一收集进 `denied_kb_ids`(不泄露库存在性)→ 抛 `AgentKbDenied`
  2. `hybrid_search(db, kb_ids, query, top_k)`
  3. `rerank=true` 且 `RERANK_ENABLED` 时复用 `get_reranker()` 重排,并应用与 `rerank_node` 相同的 `RETRIEVAL_MIN_SCORE` 门槛(默认 0=禁用,行为与问答链路一致)
  4. 返回 hits(`SearchHit` 字段直出)+ total + elapsed_ms
- 审计:每次调用写 `audit_logs`(action=`agent.list_kbs`/`agent.search`,username=归属用户名,detail={key_name, kb_ids, query 截断 200 字, hit_count, client: rest|mcp, ip}),自动纳入现有审计留存与清理

## C. REST 面 `/api/agent/*`

- `GET /api/agent/kbs` → `{items: [{id, name, description, my_perm}]}`
- `POST /api/agent/search`,请求 `AgentSearchIn`:
  - `kb_ids: list[int]` 1~5 个;`query: str` 1~500 字;`top_k: int` 1~20 默认 `RETRIEVAL_TOP_K`;`rerank: bool = false`
- 响应:`{hits: [{chunk_id, document_id, kb_id, filename, page_no, content, score, source}], total, elapsed_ms}`——与 `SearchHit` 一一对应,不造新格式
- 错误码统一 JSON `{code, message}`:
  - 401 `invalid_key` / `key_revoked` / `key_expired`(JWT 失败沿用现有 401)
  - 403 `kb_forbidden` + `denied_kb_ids`(库不存在并入此项)
  - 422 pydantic 校验;429 `rate_limited` + `retry_after`
- OpenAPI 打 `agent` tag,自动出现在 `/docs` 与 `/openapi.json`,Dify/Coze 导入即用

## D. MCP 面 `/mcp`

- 依赖 `fastmcp`,Streamable HTTP 传输,`http_app()` 以 ASGI 子应用挂载在 `/mcp`,无状态模式(不依赖 session/checkpointer)
- 鉴权:从请求头取 Bearer 复用 `get_agent_principal` 核心逻辑;失败返回 401(fastmcp 以 middleware/dependency 方式注入)
- 仅提供 tools(不做 resources/prompts):
  - `list_knowledge_bases()` → 同 REST kbs
  - `search_knowledge_base(kb_ids: list[int], query: str, top_k: int = 8(启动时取 RETRIEVAL_TOP_K), rerank: bool = False)` → 同 REST search
  - 工具描述用中文写清用途、参数含义与权限模型("只能检索到该密钥所属用户有权限的知识库"),实现直接调 facade 同一函数
- `AGENT_API_ENABLED=false` 时 `/mcp` 与 `/api/agent/*` 一并关闭(404)
- README 增加《外部 Agent 接入指南》:Claude Code(`claude mcp add --transport http airag http://<host>:8001/mcp --header "Authorization: Bearer airag_xxx"`)、Cursor、Dify/Coze 导入 openapi.json、curl 示例

## E. 限流

- Redis 滑动窗口(ZSET),按 key 粒度 `agent_rl:{key_id}`,窗口 60s
- `AGENT_RATE_LIMIT_PER_MIN` 默认 60,0=禁用;超限 429 `rate_limited` + `retry_after`
- Redis 不可用时降级放行(可用性优先,审计兜底可追溯;Redis 为现有 celery 依赖,部署必有)

## F. 配置项(`.env`,均入 `.env.example`)

| 键 | 默认 | 说明 |
|---|---|---|
| AGENT_API_ENABLED | true | 对外面总开关 |
| AGENT_RATE_LIMIT_PER_MIN | 60 | 每 key 每分钟调用上限,0 禁用 |
| AGENT_MAX_KEYS_PER_USER | 10 | 每用户密钥数上限 |

## 测试与验收

### 后端 pytest(全走 `.venv`,沿用现有 conftest)

- Key 生命周期:创建(一次性明文、哈希/prefix 入库)/列表无明文/吊销后 401/过期 401/超配额拒绝
- `get_agent_principal`:JWT 与 key 双路径、停用用户的 key 401
- agent REST:kbs 只见授权库(另建他人库断言不可见);search 正常返回(monkeypatch `hybrid_search` 既有套路);403 denied_kb_ids(含库不存在并入);422;429
- MCP:fastmcp 内存 `Client` 直连挂载应用调两个工具,断言与 REST 同构;未带 key 401
- 限流窗口单元测(monkeypatch Redis 或 fake)

### 前端 vitest

- 密钥 store:创建/列表/吊销状态流转,一次性 key 展示态

### 无头验收 `backend/scripts/m9_acceptance.py`

起 dev 服务(8001):建用户+授权库+key → REST 全链路(kbs/search/带 rerank)→ 限流触发 → 吊销 → 401。

### 用户走查

- Claude Code(或 ZCode)`claude mcp add` 接 `/mcp` 真调 list + search
- (可选)Dify 导入 `/openapi.json` 走查 REST

### 安全自查

- key 明文仅出现在创建响应;日志与审计只落 prefix/query 截断 200 字
- 库存在性不泄露(403 统一)

## 风险与权衡

- **fastmcp 新依赖**:pin 版本;若不稳定,退路为官方 `mcp` SDK 手写 Streamable HTTP(约 +1 天)。协议库只出现在薄壳层,facade 不依赖它
- **同进程 MCP 对主服务影响**:调用量低且有限流兜底;子应用挂载不动现有路由;Windows Proactor 问题仅涉 Postgres checkpointer(SSE 问答),MCP 不涉及
- **key 泄漏**:一次性展示 + 可吊销 + 限流 + 审计;无 per-key scope(继承用户全部权限)为已知取舍,二期可加
- **限流 Redis 单点**:降级放行,可用性优先
- **存在性泄露**:统一 403 规避

## M10 候选(本里程碑分流)

- `ask` 工具:非流式 RAG + 引用 + per-key token 限额
- 文档管理写操作(editor 级 key)
- key scope 细化(per-kb / per-capability)
- 出站集成(问答调用外部 Agent/工具)
- A2A 协议评估
