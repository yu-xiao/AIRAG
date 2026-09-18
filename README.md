# AIRag 企业知识库

设计文档:docs/AIRag-AI知识库需求设计方案.md
实施计划:docs/superpowers/plans/2026-09-14-airag-implementation-plan.md

## 开发启动(M1 起,原生运行,无需 Docker)

```bash
copy .env.example .env
cd backend
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
net start postgresql-x64-18   # 1/3 确保 PG 服务在跑(需管理员)
start_dev.bat   # 2/3 迁移 + 热重载,后端 http://localhost:8001/docs
cd ..\frontend && pnpm install && pnpm dev   # 3/3 前端 http://localhost:5173
```

Windows 部署注意(M6 遗留):裸 uvicorn(非 `--reload`)在 Windows 走 ProactorEventLoop,会打断 psycopg checkpointer 导致 ask 500;后端启动必须用 `start_dev.bat`。

## 文档流水线(M2 起)

除后端/前端两个窗口外,再开一个窗口启动 worker:

```bash
cd backend
start_worker.bat
```

上传:.env 填好 ZHIPU_API_KEY 后默认走智谱 embedding;未填 key 时可在 .env 设 EMBED_PROVIDER=fake 跑通全流程(向量无语义)。

Redis:worker 需要 Redis(本机 6379 已有服务)。若该服务设置了 requirepass(本机当前如此),在 .env 配 `REDIS_URL=redis://:<密码>@localhost:6379/0`,否则 worker 连不上。

问答检索(M7 起):`RETRIEVAL_MIN_SCORE` 为 rerank 相关度阈值,仅 rerank 开启时生效——默认 0=禁用;智谱 rerank 分数饱和实测(见 M7 验收),阈值仅在供应商分数分布有效时手动开启;零命中防线=收紧提示词+refused 标记(前端隐藏引用)。前端精排开关 M7 起默认开,收益=排序质量,代价=每问一次 rerank 调用。

审计清理 beat(M6 起):Windows 不能用 worker -B 内嵌,另开窗口运行 backend\start_beat.bat(每日 03:00 清理,`AUDIT_RETENTION_DAYS` 默认 180 天,0=禁用;不开 beat 时可用 admin 手动 purge 端点)。

## 外部 Agent 接入(M9~M11)

知识库内容可经 API Key 检索问答,编辑型密钥还可维护文档;密钥能力(read_only/editor)不会超出归属账号权限,可随时吊销。

### 1. 创建密钥
登录 Web → 左侧「API 密钥」→ 创建(明文只显示一次;M11 起可选密钥类型:只读/编辑,默认只读)。管理员可在创建对话框的「绑定账号」下拉中把密钥发给任意账号(权限与配额按目标账号,审计记录 by/to)。

### 2. MCP 客户端(Claude Code / Cursor / ZCode 等)
```bash
claude mcp add --transport http airag http://<host>:8001/mcp --header "Authorization: Bearer airag_xxxx"
```
可用工具:`list_knowledge_bases` / `search_knowledge_base` / `ask_knowledge_base`(直接生成答案+引用,单轮无上下文,内部多步 LLM 耗时 40~90 秒,客户端超时请设充足,如 Claude Code 的 `MCP_TIMEOUT`);文档维护工具 `list_documents` / `get_document` / `upload_document`(base64 内容,解码后不超过 MAX_UPLOAD_MB;上传后轮询 `get_document` 至 done/failed)/ `delete_document`(不可逆)/ `reprocess_document`;配额查询 `get_quota`。`delete_document`(不可逆)/`reprocess_document`/`upload_document` 需**编辑型密钥**(创建密钥时类型选"编辑";存量只读密钥如需写操作请重新铸造);`get_quota` 需密钥主体(JWT 调试不可用)。REST 面同构:`GET/POST /api/agent/kbs/{kb_id}/documents`、`GET/DELETE /api/agent/documents/{doc_id}`、`POST /api/agent/documents/{doc_id}/reprocess`、`GET /api/agent/quota`。

### 3. REST 客户端(Dify / Coze / 内部系统)
OpenAPI 文档:`http://<host>:8001/openapi.json`(tag `agent`)。
```bash
# 知识库发现
curl -H "Authorization: Bearer airag_xxxx" http://127.0.0.1:8001/api/agent/kbs
# 混合检索
curl -X POST -H "Authorization: Bearer airag_xxxx" -H "Content-Type: application/json" \
  -d '{"kb_ids":[1],"query":"退货流程","top_k":8}' \
  http://127.0.0.1:8001/api/agent/search
# RAG 问答(非流式,返回 answer/citations/refused/tokens_used;40~90s)
curl -X POST -H "Authorization: Bearer airag_xxxx" -H "Content-Type: application/json" \
  -d '{"kb_ids":[1],"query":"退货流程"}' \
  http://127.0.0.1:8001/api/agent/ask
```

### 限流与审计
每密钥每分钟 `AGENT_RATE_LIMIT_PER_MIN`(默认 60)次;所有调用记入审计日志(action `agent.*`)。紧急关闭:`AGENT_API_ENABLED=false` 后重启。
ask 按 key 每日 token 配额 `AGENT_ASK_DAILY_TOKENS`(默认 200000,0=禁用),超限 429 附 `Retry-After` 头(到次日零点)。
