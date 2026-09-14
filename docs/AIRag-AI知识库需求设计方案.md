# AIRag —— 企业内部 AI 知识库需求设计方案

> 日期:2026-09-11(初稿)/ 2026-09-14(选型定版)
> 状态:选型已定版(前端框架 / LLM 厂商 / RAG 编排框架已决策,见 §2 与 §11)
> 阶段:方案设计,尚未开始编码

---

## 1. 项目背景与目标

为企业内部搭建一套 AI 知识库系统:将办公文档(PDF/Word/Excel)导入系统后,员工可以通过自然语言提问,系统基于文档内容给出带引用溯源的准确回答。

### 1.1 需求画像

| 维度 | 结论 |
|---|---|
| 使用场景 | 企业内部,多知识库隔离,需要用户权限管理(RBAC) |
| 内容类型 | PDF / Word / Excel 办公文档为主(含表格) |
| 文档规模 | 中型:数千 ~ 数万份 |
| LLM 接入 | 云端 API,首家接入**智谱 GLM**(GLM-4 + embedding-3 + Rerank);OpenAI 兼容协议,可切换 DeepSeek / OpenAI |
| 建设路径 | 从零自研(非开源项目改造) |
| 后端技术栈 | Python(FastAPI) |
| 前端技术栈 | **Vue 3 + TypeScript + Element Plus** |
| 部署形态 | 开发:Windows 原生(PG 16 原生安装 + pgvector 手工编译,无 Docker);部署:Docker Compose 单机起步,预留扩展 |

### 1.2 成功标准

- 员工上传一份 PDF/Word/Excel 后,几分钟内可就该文档内容问答
- 回答附带引用溯源(文档名 + 页码 + 原文片段),可点击查验
- 知识库之间数据与权限隔离;无权限的知识库不可见、不可检索
- 知识库中没有的内容,明确回答"未找到",而不是编造(抑制幻觉)

---

## 2. 关键技术决策与权衡

### 决策 1:向量存储 —— pgvector(选定) vs Qdrant/Milvus

- 数万份文档约产生数百万个向量块,pgvector 配 HNSW 索引可以承接该量级
- PostgreSQL 一个库同时管理元数据、权限、全文检索和向量,权限过滤直接在 SQL 层完成,运维组件最少(仅 PG + Redis)
- Qdrant 检索性能更强,但多一个组件、多一套数据同步逻辑
- **结论:pgvector 起步;检索层抽象成接口,将来超规模再平滑切换 Qdrant/Milvus**

### 决策 2:RAG 编排 —— LangGraph 编排问答链路(选定,2026-09-14 定版) vs 全裸写 vs LangChain 全家桶

初稿曾选定"全裸写、不用任何编排框架"。复审后改判:**引入 LangGraph 1.x,但收窄边界 —— 只编排问答/对话链路**;文档流水线与检索/嵌入/解析服务维持自写。

**改判的关键事实:**

- LangGraph 1.0 已于 2025-10 GA,官方承诺 2.0 之前无破坏性变更 —— 初稿反对 LangChain 的"版本迭代破坏性大"这一理由,对现在的 LangGraph 基本失效
- 核心库 MIT 协议、可完全自托管;自建 FastAPI 服务直接跑 LangGraph + SSE 流式是成熟做法,不依赖 LangGraph Platform/Cloud 付费产品
- LangGraph ≠ LangChain 全家桶:它只做编排层,不强制检索/模型的重抽象,自写代码仍是主体

**引入的好处:**

1. **为 Agentic RAG 预留骨架(最核心收益)**:知识库问答的天然进化路径是查询改写 → 多知识库路由 → 检索结果自评/重检索(Corrective RAG)→ 多跳问答,这些全是带条件分支和循环的流程,图模型正是为此设计;线性裸写代码做这些会迅速腐化
2. **1.0 稳定性承诺**:生产可用性已被业界普遍认可,不再是快速变动的实验性库
3. **内置检查点与状态持久化**(PostgresSaver):对话状态可恢复、可回放审计,对问答历史功能与调试排错都有实际价值
4. **流式原语完善**:`astream_events` 同时支持 token 级与节点级流式,映射到前端 SSE 干净;自带 human-in-the-loop 中断能力(未来做人工审核可用)
5. **只做编排,不吃掉自写代码**:检索、嵌入、解析仍是自己写的普通 Python 服务,图节点只是调用这些服务的薄层,初稿"链路可控可调试"的诉求大部分保留
6. **可观测性可选**:可接 LangSmith,内网环境不接也不影响功能

**引入的坏处:**

1. 当前问答链路是线性的(检索→组装→流式输出),图模型在 MVP 阶段属于"为未来付费"的提前投资,概念开销(StateGraph、节点、reducer)现在就要背
2. 多一层抽象 = 多一层调试成本:裸写 async 代码一行断点直接看,图执行的状态流转要过框架层,排错路径变长
3. 与 Celery 并存:LangGraph 自带 durable execution,系统里存在两套异步心智模型,职责边界必须显式声明
4. 断连语义是真实的坑:FastAPI 是无状态请求层,LangGraph 是有状态运行时,客户端断开后 run 的取消/续传语义需要专门设计
5. 带入 langchain-core 依赖链(1.0 后风险已低,但非零);检查点机制需额外建表,每步 checkpoint 有数据库开销
6. 若产品永远只做线性 RAG,这笔投资收不回(YAGNI 风险)

**收窄边界的执行约定:**

- 图范围(M3):`retrieve →(可选 rerank)→ generate` 三节点小图,带条件边;未来 agentic 化只加节点不动架构
- 文档流水线(上传→解析→切块→嵌入)继续 Celery + Redis,不用 LangGraph 的 durable execution 替代
- generate 节点用 langchain-openai 的 ChatOpenAI 指向智谱 OpenAI 兼容端点(GLM-4),天然对接 `astream_events` → SSE;检索/嵌入节点是调用自写服务的薄函数,不经 LangChain 抽象
- 检查点用 langgraph-checkpoint-postgres,复用现有 PostgreSQL,thread_id 对应 conversation_id;业务数据仍存 conversations/messages 表,checkpoint 只管运行时状态

### 决策 3:文档解析 —— 轻量解析器起步(选定) vs MinerU 重解析

- 文本型 PDF 用 PyMuPDF、docx 用 python-docx、xlsx 用 openpyxl,覆盖约 80% 场景
- 含复杂表格/扫描件的 PDF 是难点;MinerU(开源中文文档解析 SOTA)效果好,但依赖重、需下载模型、CPU 推理慢
- **结论:解析器做成插件接口(Parser 插件化),MVP 用轻量库,预留 MinerU/OCR 接入点,按需升级**

---

## 3. 整体架构

```
┌──────────────┐
│  Vue3 前端    │ Vite + TypeScript + Element Plus(SSE 流式对话、文档管理、引用溯源)
└──────┬───────┘
       │ REST + SSE
┌──────▼───────────────────────────────────────┐
│  FastAPI 后端                                  │
│  ├─ 认证/权限 (JWT + RBAC)                      │
│  ├─ 知识库/文档管理 API                          │
│  ├─ 问答 API(流式,LangGraph 图编排)             │
│  └─ 检索服务(混合检索 + RRF 融合 + Rerank)       │
└──────┬────────────────────┬─────────────────┘
       │                    │
┌──────▼──────┐        ┌──────▼──────────┐
│  PostgreSQL │◄───────│  Celery Workers  │ 异步流水线:
│  + pgvector │        │  (解析→切块→嵌入)  │ 上传后入库处理
│  + 全文检索  │        └──────┬──────────┘
└─────────────┘               │
┌─────────────┐        ┌──────▼──────┐
│  Redis      │◄───────│  本地磁盘     │ 原始文件存储(MVP 用本地磁盘,
└─────────────┘        └─────────────┘  后续可换 MinIO/OSS)
```

> 注:LangGraph 检查点(langgraph-checkpoint-postgres)复用同一个 PostgreSQL 实例;Redis 仅服务 Celery。问答链路由 LangGraph 编排,文档流水线由 Celery 负责,二者职责边界见决策 2。

## 4. 技术选型汇总

| 层 | 选型 | 理由 |
|---|---|---|
| 后端框架 | FastAPI + SQLAlchemy 2.0 + Pydantic v2 | 异步、类型安全、生态标准 |
| **问答编排** | **LangGraph 1.x(仅问答链路)+ langchain-openai** | 为 agentic 扩展预留骨架;1.0 稳定承诺;边界收窄防 YAGNI(详见决策 2) |
| 数据库 | PostgreSQL 16 + pgvector + zhparser(中文全文) | 元数据/权限/向量一库搞定 |
| 向量检索 | pgvector HNSW(cosine 距离) | 数百万级向量够用,检索层留接口可换 |
| 关键词检索 | PG tsvector + zhparser 中文分词 | 与向量检索同库,RRF 融合方便 |
| 任务队列 | Celery + Redis | 成熟稳定、监控完善、重试机制现成(文档流水线专用) |
| 文档解析 | PyMuPDF / python-docx / openpyxl,Parser 插件接口 | 轻量起步,预留 MinerU/OCR 接入点 |
| Embedding | Provider 抽象:智谱 embedding-3(默认)/ OpenAI / 本地 BGE-M3 可切换 | 不锁定单一厂商 |
| Rerank | 智谱 Rerank API(可选开关) | 提升检索精度 |
| LLM | OpenAI 兼容协议,首家智谱 GLM-4,可配置切换 DeepSeek/GPT | 检索链路自写,编排用 LangGraph |
| 前端 | Vue 3 + Vite + TypeScript + Element Plus + Pinia | 企业后台成熟方案,与团队技术栈一致 |
| 部署 | 生产:Docker Compose(pg / redis / backend / worker / frontend);开发:Windows 原生运行 | 单机一键起 |

### 4.1 前端选型明细(2026-09-14 定版)

| 组件 | 选型 | 说明 |
|---|---|---|
| 框架 | Vue 3 Composition API + `<script setup>` | 全量 TS |
| 语言/构建 | TypeScript + Vite,包管理 pnpm | |
| 路由/状态 | Vue Router 4 + Pinia | |
| UI 库 | Element Plus + @element-plus/icons-vue | 企业后台组件最全,中文生态最好 |
| HTTP | Axios + openapi-typescript | 从 FastAPI 的 OpenAPI schema 自动生成 TS 类型 |
| SSE 流式 | @microsoft/fetch-event-source | 支持 POST + JWT header(原生 EventSource 做不到) |
| Markdown 渲染 | markdown-it + highlight.js | 流式回答 + 引用标注 [1][2] |
| 效率工具 | VueUse + unplugin-auto-import + unplugin-vue-components | |
| 测试/规范 | Vitest + Vue Test Utils / ESLint + Prettier | |

版本策略:文档只锁主版本,具体小版本在 M1 脚手架初始化时取最新稳定版。

## 5. 核心数据模型

```
users            (id, username, password_hash, role, is_active)
knowledge_bases  (id, name, description, owner_id, embed_provider, embed_model, created_at)
kb_permissions   (kb_id, user_id, perm: viewer|editor)
documents        (id, kb_id, filename, file_path, mime, size, sha256,
                  status: pending|parsing|chunking|embedding|done|failed,
                  error_msg, page_count, chunk_count, created_at)
chunks           (id, document_id, kb_id, chunk_index, content, page_no, char_len,
                  embedding vector(1024), tsv tsvector, content_hash)
conversations    (id, user_id, kb_ids[], title, created_at)
messages         (id, conversation_id, role, content, citations jsonb, created_at)
```

> 注:LangGraph checkpoint 相关表由其迁移自管,不属于上述业务模型。

## 6. RAG 问答流程(LangGraph 图编排)

> 本流程由 LangGraph 图编排:`retrieve →(可选 rerank,条件边控制)→ generate` 三节点小图;节点是调用自写检索/LLM 服务的薄层。

1. 提问 → 校验用户对各知识库的权限,得到可检索 kb_ids
2. 混合检索:向量 top-20(余弦)+ 关键词 top-20(tsvector),RRF 融合
3. (可选)Rerank API 精排 → top-8
4. 组装 Prompt:系统提示 + 检索片段(带 [1][2] 编号、文档名、页码)+ 问题
5. LLM 流式输出(astream_events → SSE),逐 token 推送前端
6. 回答完成后附带 citations 数组(文档 id / 页码 / 原文片段),前端可点击溯源
7. 无相关内容时明确回答"知识库中未找到",抑制幻觉

断连处理要点:FastAPI 无状态请求层与 LangGraph 有状态运行时的取消/续传语义,在 M3 详细设计时定案(初步倾向:断连即取消当前 run,检查点保留已生成内容用于恢复展示)。

## 7. 文档处理流水线(Celery 异步,LangGraph 不介入)

```
上传 → sha256 去重 → 状态 pending
  → parse(按扩展名路由到 Parser 插件,产出 markdown + 页码/表格结构)
  → chunk(标题感知递归切块,默认 512 token / 64 重叠;表格整块保留)
  → embed(批量调 API,失败重试 3 次退避)
  → 入库(chunks 批量插入,生成向量索引 + tsv)
  → 状态 done;任一步失败 → failed + error_msg,支持重新处理
```

增量更新:文档替换时按 document_id 删旧 chunks 再入新;content_hash 避免重复嵌入计费。

## 8. 项目结构(monorepo)

```
E:\Projects\AIRag\
├── backend/
│   ├── app/
│   │   ├── main.py               # FastAPI 入口
│   │   ├── core/                 # 配置(.env)、安全(JWT)、依赖注入
│   │   ├── models/               # SQLAlchemy 模型
│   │   ├── schemas/              # Pydantic 请求/响应
│   │   ├── api/                  # 路由: auth / kbs / documents / chat
│   │   ├── services/
│   │   │   ├── parsing/          # parser_base + pdf/docx/xlsx 实现
│   │   │   ├── chunking/
│   │   │   ├── embedding/        # provider 抽象
│   │   │   ├── retrieval/        # hybrid_search + rrf + rerank
│   │   │   ├── chat_graph/       # LangGraph 问答编排:state.py / nodes.py / graph.py
│   │   │   └── llm/              # chat client + prompt 模板
│   │   └── workers/              # Celery 任务 + pipeline 编排
│   ├── alembic/                  # 数据库迁移
│   ├── tests/                    # pytest(解析/切块/检索单测)
│   └── pyproject.toml
├── frontend/
│   └── src/
│       ├── api/                  # axios 封装 + openapi-typescript 生成类型
│       ├── components/           # 引用溯源卡片、上传、状态标签等通用组件
│       ├── composables/          # useChatStream(SSE)、useCitation 等
│       ├── layouts/              # 侧边栏主布局
│       ├── pages/                # 登录 / 知识库 / 文档 / 对话
│       ├── router/               # vue-router 路由与守卫
│       ├── stores/               # Pinia: auth / kb / chat
│       ├── styles/
│       └── types/                # 业务类型
├── docker-compose.yml            # 生产部署用(开发期原生运行,此文件推迟到部署里程碑创建)
├── .env.example                  # API keys 模板
└── docs/                         # 设计文档(本文件)
```

## 9. 实施里程碑

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **M1 骨架与存储层** | 脚手架(FastAPI + Vite,原生运行);数据模型 + Alembic 迁移;注册/登录(JWT) | 本地三件套(PG 服务+后端+前端)起,能登录 |
| **M2 文档流水线** | 上传 API + 状态机;PDF/docx/xlsx 解析器 + 切块 + Embedding Provider + Celery 流水线 | 上传三种格式文档,状态流转到 done,chunks 可查 |
| **M3 检索与问答** | 混合检索 + RRF;**LangGraph 三节点图(retrieve → rerank* → generate)+ PostgresSaver 检查点 + 断连语义设计**;问答 API(SSE 流式)+ 引用溯源;前端三大页面(知识库/文档/对话) | 端到端:上传 → 提问 → 流式回答 + 引用,3~5 份真实文档验收 |
| **M4 企业化完善** | RBAC(viewer/editor/admin)+ 权限管理界面;失败重试/重新解析;问答历史;Rerank 开关 | 权限隔离生效,检索质量达标 |
| **M5 增强(按需)** | MinerU/OCR 接入(扫描件);检索评估集(RAGAS);导出/审计日志;**agentic 扩展:查询改写 / 检索自评(Corrective RAG)/ 多跳 = 图上加节点** | 复杂文档解析可用 |

## 10. 风险与对策

| 风险 | 对策 |
|---|---|
| 中文 PDF 表格解析质量不佳 | Parser 插件化隔离风险;MVP 只承诺文本型文档,表格/扫描件在 M5 接 MinerU |
| 云端 API 成本 | content_hash 去重、embedding 批量调用、LLM 可切换低价模型(DeepSeek) |
| 检索质量不达预期 | 混合检索 + Rerank 是标准解法;预留评估集持续迭代 |
| LangGraph 引入的抽象层调试成本、断连语义、双异步模型并存 | 边界收窄(只编排问答);Celery 职责显式声明;断连语义 M3 专项设计;1.0"2.0 前无破坏性变更"承诺降低版本风险 |
| Windows 开发环境兼容性 | PG 原生安装 + pgvector 用 VS Build Tools 手工编译(一次性,步骤见实施计划"环境准备");Redis/Celery 的 Windows 方案在 M2 计划时定;生产统一 Docker 化 |

## 11. 已决策事项与开放问题

- [x] 前端框架:**Vue 3 + Element Plus**(2026-09-14 定版,明细见 §4.1)
- [x] 云端 API 厂商:**智谱 GLM**(GLM-4 + embedding-3 + Rerank;key 待申请配置)
- [x] RAG 编排:**LangGraph 1.x,只编排问答链路**(2026-09-14 定版,见决策 2)
- [x] 开发机数据库运行方案:**原生 PostgreSQL 16 + 手工编译 pgvector**(2026-09-14 定版,不用 Docker/WSL;曾评估 Qdrant 独立向量库方案后决定维持决策 1 的 pgvector)
- [ ] 是否已有公司统一的登录体系(LDAP/SSO)需要对接(影响 M4 的用户模块)
- [ ] 生产部署目标机器规格(内存/磁盘,影响 pgvector 索引参数)
