# AIRag M11 设计:Agent 文档写操作与 editor key

## 背景与动机

M9~M10 完成了 Agent 对接的只读面:API Key 体系 + REST/MCP 双面的知识库发现、混合检索、ask 非流式 RAG(含每日 token 配额)。外部 Agent 至今只能读:知识库内容变化仍须人在 Web 端操作,Agent 无法自助维护。

当前缺口:

- `ApiKey` 无能力字段,权限全量实时继承归属用户——key 一旦铸造即等价于用户全部权限的只读投影;没有"允许写"的表达,也就没有任何写面
- Web 面有上传/重解析但**无文档删除端点**(M4 起历史遗留);删除逻辑(级联 chunks/向量/文件)在三面(Web/REST/MCP)都缺失
- 上传核心逻辑长在 `api/documents.py` 端点函数里,Agent 面要复用必须先抽取,否则双份漂移
- M10 终审 triage 留下六笔硬欠账(quota pipeline 原子化、`_api_key_id` 守卫去重、模块级 `api_router` 可删、ask 失败应用级日志、CitationOut 强类型、配额余量查询端点)

用户已拍板的关键决策(2026-09-18):

1. 主线为**文档写操作 + editor key**,REST + MCP 双面,M10 顺延硬项全打包
2. **key 级单一能力位**:`read_only`(默认)/`editor`;per-KB scope 白名单继续顺延
3. 操作集全量:list 文档 + 查状态 + 上传 + 删除 + 重解析
4. 删除权限 **editor 及以上**(与上传/重解析同权,Web 与 Agent 面同语义;不可逆操作靠 editor key 能力位 + 审计 + 前端确认对话框三重控制)
5. MCP 上传形态 **base64**(解码后按 `MAX_UPLOAD_MB` 校验)
6. 架构沿用 M9 起原则:抽取共享 `doc_ops` 服务,Web/REST/MCP 三面薄壳,单一业务实现

## 目标

1. `ApiKey.role` 能力位 + alembic 迁移,存量 key 全部落为 `read_only`
2. `services/doc_ops.py`:上传/删除/重解析核心逻辑单一实现,Web `api/documents.py` 改薄壳(行为不变)
3. REST 面新增 5 端点(列表/详情/上传/删除/重解析),MCP 面新增 5 工具(同五操作,上传走 base64)
4. Web 面补齐 `DELETE /api/documents/{doc_id}`(历史遗留清偿)+ 前端:DocsPage 删除按钮、KeysPage/UsersPage 铸造角色下拉与徽标
5. 写操作双重校验:key=editor ∧ 归属用户对该库 perm≥editor;`Principal.key_role` 全链路传播
6. M10 六笔硬欠账全部清偿
7. 验收:pytest 全绿(基线 219P 起步)+ 真栈 `m11_acceptance.py` + MCP 真客户端走查 + 前端走查

## 非目标(明确不做)

- per-KB scope 白名单 / per-capability 细粒度(M9 spec 原文顺延,M12 候选)
- KB 级删除端点(仅文档删除;KB 删除继续顺延)
- 文档内容更新/版本管理(删了重传,SHA256 去重兜底)
- URL 拉取上传、Webhook 回调(出站集成范畴)
- `Document` 加 uploader 字段/文档级 ACL(审计已可追溯归属用户)
- 独立写操作限流(沿用 `AGENT_RATE_LIMIT_PER_MIN` 汇总限流;写操作无 LLM token,不进 token 配额)
- 前端新增页面(只改既有 KeysPage/UsersPage/DocsPage)

## A. 数据模型:`ApiKey.role` 能力位

- `ApiKey.role: String(16)`,`"read_only"`(默认)/`"editor"`,`server_default="read_only"`
- alembic 迁移 `add api_keys.role`;存量 key 迁移后全部 `read_only`——**零影响**:M11 前无任何写面,存量 key 本就只读
- `issue_api_key(db, user, name, expires_in_days, role="read_only")` 签名扩展;调用方(auth 面自服务、admin 面代发)透传
- schema:`ApiKeyCreateIn` / `ApiKeyCreatedOut` / `ApiKeyOut` 增加 `role: Literal["read_only", "editor"]`(入参默认 `read_only`);admin 代发入参 `AdminKeyCreateIn` 同步

## B. 能力传播与写守卫

- `resolve_bearer_principal`:api_key 路径把 `key.role` 写入 `Principal.key_role: str | None`;JWT 路径保持 `None`
- 守卫语义(三面一致):

```
写操作 = key=editor ∧ 归属用户对该库 perm≥editor
读操作(list/get)= 归属用户对该库 perm≥viewer(现状,不变)
JWT 调试通道:视为 editor 能力(key_role=None 不拦截),仍受用户 KB 权限约束
             ——与限流/配额豁免 JWT 同哲学
```

- REST 守卫 `_require_editor_key(principal)`:`kind=api_key` 且 `key_role≠editor` → 403 `{"code": "editor_key_required"}`;MCP 同逻辑抛 `ToolError("editor_key_required: this key is read-only")`
- **错误语义镜像 Web documents 面**(文档端点单库操作,与 search/ask 多库聚合的 `kb_forbidden+denied_kb_ids` 不同):库/文档不存在或无权 → 404(不泄露存在性);用户 perm<editor → 403 `editor permission required`;key 能力不足 → 403 `editor_key_required`。校验顺序:可见性(404)→ 权限级(403)→ key 能力(403)
- 审计 action:`agent.list_documents` / `agent.get_document` / `agent.upload_document` / `agent.delete_document` / `agent.reprocess_document`(client=key_name、kb_id、doc_id、filename、size 等进 detail);Web 面新增删除审计 `doc_delete`(与 doc_upload/doc_reprocess 命名一致)

## C. 共享文档服务 `services/doc_ops.py`

从 `api/documents.py` 抽出 + 新写,三面唯一业务实现:

- `save_upload(db, kb, filename, payload: bytes, mime, ocr_mode) -> Document`
  扩展名白名单(415)→ 大小上限 `MAX_UPLOAD_MB`(413)→ SHA256 库内去重(409 duplicate)→ 落盘 `UPLOAD_DIR/{kb_id}/{uuid}_{filename}` → 建行 flush → `process_document.delay(doc.id)`
  审计与 commit 由调用方负责(action 名两面不同:Web `doc_upload` / Agent `agent.upload_document`)
- `delete_document(db, doc) -> None`
  处理中(status ∈ parsing/chunking/embedding)→ 409 busy → 删 chunks 行(pgvector/tsv 随行消失,向量零残留)→ 尽力删磁盘文件(失败仅日志,行删除不受阻)→ 删 documents 行
- `reprocess_document(db, doc) -> Document`
  沿用现逻辑:409 busy → 清 chunks → 重置 status=pending/error_msg/chunk_count/page_count → `process_document.delay`
- Web `api/documents.py`:upload/reprocess 端点改薄壳调用,新增 `DELETE /api/documents/{doc_id}`(perm≥editor);行为不变,既有测试做回归

## D. REST 面:5 新端点(前缀 `/api/agent`,全过 `_check_rate`)

| 端点 | 方法 | 能力 | 语义 |
|------|------|------|------|
| `/agent/kbs/{kb_id}/documents` | GET | 读 | 文档列表(同 Web list_documents,无分页) |
| `/agent/documents/{doc_id}` | GET | 读 | 文档详情/状态(pending→…→done/failed,轮询用) |
| `/agent/kbs/{kb_id}/documents` | POST | editor | multipart(file+ocr),201 复用 `DocumentOut` |
| `/agent/documents/{doc_id}` | DELETE | editor | 204;409 busy |
| `/agent/documents/{doc_id}/reprocess` | POST | editor | 200 `DocumentOut`;409 busy |

- 响应复用 `schemas/document.DocumentOut`(零新 schema;轮询方主要看 status/error_msg/chunk_count)
- 错误码:404(不泄露)/ 403 `editor permission required` / 403 `editor_key_required` / 409 busy|duplicate / 413 / 415 / 422 / 429 rate_limited(+Retry-After)

## E. MCP 面:5 新工具

| 工具 | 能力 | 签名要点 |
|------|------|----------|
| `list_documents` | 读 | `(kb_id, limit=50)`;limit 1~200 |
| `get_document` | 读 | `(doc_id)` |
| `upload_document` | editor | `(kb_id, filename, content_b64, ocr="auto")` |
| `delete_document` | editor | `(doc_id)` |
| `reprocess_document` | editor | `(doc_id)` |

- `upload_document`:base64 解码(坏输入 ToolError)→ 解码后按 `MAX_UPLOAD_MB` 校验(413 语义 ToolError)→ 扩展名白名单同 Web;成功返回 DocumentOut 字段子集
- 工具 description 沿用 M10 模式:模块级常量字符串经 `@mcp.tool(description=...)` 传入(f-string docstring 不设 `__doc__` 的坑,M10 教训)
- 错误统一 `ToolError` 文本携带 code(`editor_key_required` / `kb_forbidden` / `busy` / `duplicate` / `too_large` / `unsupported_type` / `not_found`)
- 限流沿用 AgentAuthMiddleware;写守卫在工具内(读工具不需要)
- README《外部 Agent 接入指南》增补:五工具用途、上传 base64 与大小上限、轮询状态建议、editor key 说明

## F. Web 面补齐

- `DELETE /api/documents/{doc_id}`:editor+(C 节共享 service,audit `doc_delete`)
- `DocsPage.vue`:删除按钮(仅 editor/owner 可见)+ 确认对话框(不可逆提示)+ 成功后刷新列表;处理中文档删除按钮禁用(409 对齐)
- `KeysPage.vue`(自服务与 admin 代发同页,M9.1 起):铸造表单增加"密钥类型"单选(只读/编辑,默认只读);key 列表增加类型徽标(`UsersPage.vue` 无密钥逻辑,零改动)
- 前端零新页面、零新路由

## G. M10 顺延小项清单(全部清偿)

| # | 项 | 落点 |
|---|---|------|
| ① | quota pipeline 原子化 | `quota_consume` 的 INCRBY+EXPIRE 合入单条 redis pipeline |
| ② | `_api_key_id` 守卫去重 | 新 helper(`principal.key_id if kind==api_key else None`),替换 agent.py/mcp_server.py 全部 `principal.kind == "api_key" and principal.key_id is not None` 守卫 |
| ③ | 删模块级 `api_router` | 实现时 grep 确认无消费方后删除(主路由已走 `build_api_router` 工厂) |
| ④ | ask 失败应用级日志 | REST `agent_ask` 与 MCP `ask_knowledge_base` 对 facade 上抛异常 `logger.exception` 后转 500/ToolError(现靠 uvicorn/fastmcp 内部日志) |
| ⑤ | CitationOut 强类型 | `schemas/agent.py` 增 `CitationOut`(number/chunk_id/document_id/filename/page_no/excerpt),`AgentAskOut.citations` 与 MCP 返回路径强类型化(facade 内部 dict 边界不动,出口校验) |
| ⑥ | 配额余量查询 | `GET /api/agent/quota` → `{used, limit, reset_at}`(本地时区日界,Redis 读当前值;Redis 异常降级 `used: null`)+ MCP `get_quota` 工具;仅 api_key 主体有意义,JWT → 403 `{"detail": "api key principal required"}` / ToolError |

## 配置项

无新增(`MAX_UPLOAD_MB`/`AGENT_RATE_LIMIT_PER_MIN`/`AGENT_API_ENABLED` 全部沿用);`.env.example` 零改动。

## 测试与验收

### 后端 pytest(`.venv`,conftest 既有 monkeypatch/fake redis 套路)

- 能力位:迁移后存量 key read_only;`issue_api_key` role 透传;`resolve_bearer_principal` → `Principal.key_role`
- 守卫矩阵:read_only key / editor key / JWT × 五操作 × 用户 perm(viewer/editor/owner)——404/403/204/201 全枚举
- doc_ops:上传核心(415/413/409 dup/落盘/建行)、删除级联(chunks 行消失、文件尽力删、busy 409)、重解析重置;Web documents 既有测试回归(薄壳化后全绿)
- REST 5 端点:200/201/204 全路径 + 错误码 + 审计落库断言(action 名与 detail 字段)
- MCP 5 工具:session 级 fixture 真调(M9 模式);upload 坏 base64/超限/白名单外扩展名 ToolError;read_only key 写操作 ToolError
- 小项:①pipeline 断言(fake redis 记录 pipeline 调用)②helper 替换后守卫行为不变 ③删除后 import/路由冒烟 ④ask 异常日志(capsys/loguru capture)⑤CitationOut 校验拒绝畸形字段 ⑥quota 端点正常/JWT 403/Redis 异常降级

### 无头验收 `backend/scripts/m11_acceptance.py`(真栈,8001)

editor key 上传真实文件 → 轮询 `GET /agent/documents/{id}` 至 done → `agent.search` 命中新内容 → 删除 → search 不再命中已删内容(该库若无其他文档则零命中);read_only key 上传 → 403 `editor_key_required`;同文件重传 → 409 duplicate;非白名单扩展名 → 415;审计 `agent.upload_document`/`agent.delete_document` 落库断言。

### MCP 真客户端走查

复用 `m91_mcp_walkthrough.py` 模式扩展:`upload_document`(base64 真文件)→ `get_document` 轮询 → `list_documents` → `delete_document`;Inspector 抽查可选。

### 前端走查

KeysPage 铸造 editor key + 徽标;UsersPage admin 代发带角色;DocsPage editor 删除文档(确认框→列表刷新);viewer 无删除按钮。

## 风险与权衡

- **base64 膨胀**:4/3 体积走 HTTP body,fastmcp/uvicorn 大 body 上限以真栈验收实测;超限给明确 ToolError 文案
- **pending→celery pick 删除竞态**:窗口极窄(毫秒级);实现任务中核实 `process_document` 对缺行文档的行为,必要时 worker 首步"文档不存在即静默退出"防御(M5"OCR 空结果不重试"同风格:防御性早退,不重试)
- **eval_sets 陈旧引用**:`backend/eval_sets/*.json` 为 CLI 文件,引用已删文档时按 chunk 查询自然零命中——不改代码,本文档明示
- **Windows 文件占用**:磁盘文件删除失败仅日志不阻断(行已删,孤儿文件由既有孤儿清理机制兜底——M8 落地)
- **存量 key 一刀切 read_only**:M11 前无写面,零实际影响;需要的用户重新铸 editor key(明示在 README)
- **删除不可逆**:editor 即可删(用户拍板),防线=editor key 能力位 + 审计 + 前端确认框;不设回收站(YAGNI)

## M12 候选(本里程碑分流)

- key scope 细化:per-KB 白名单 / per-capability(M9 spec 原文,本期能力位的自然延伸)
- KB 删除端点(级联 + eval_sets 文件钩子)、KB 重名 DB 唯一约束 + 存量去重
- 评估结果入库 / reference_answer、包裹型拒答 LLM 二审、多跳子问题并行检索
- ask 延迟优化(grade 前置剪枝)、MinerU 本地化、出站集成、A2A 评估、LDAP/SSO(仍等输入)
