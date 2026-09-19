# AIRag M12 设计:权限治理收官(per-KB scope、KB 删除、重名唯一约束)

## 背景与动机

M9~M11 建成 Agent 对接三件套:API Key 体系、REST/MCP 双面读写(检索/ask/文档五操作)、每日 token 配额。key 的授权模型至今是"全量实时继承归属用户权限"(M9 spec 明示的已知取舍)+ M11 的读写能力位(`role: read_only|editor`)。缺最后一块:**库粒度的白名单**——key 一旦铸造即等同用户全部可访问库的投影,无法表达"这个 key 只准碰库 3"。

KB 生命周期缺口:

- **无 KB 删除端点**(全仓库 grep 证实)。M8 只能靠手工脚本 `scripts/purge_orphan_evalsets.py` 收口残留文件,其 docstring 明写"系统无 KB 删除端点,KB 本体经人工清理后文件残留"
- **KB 重名 409 只是应用层 SELECT 检查**(M8),并发窗口可写入同名行;DB 无唯一约束
- `conversations.kb_ids` 是无 FK 的 `ARRAY(Integer)`,KB 消失后悬空 id 会让旧会话提问直接撞 kb_forbidden

M11 终审 triage 另留下八笔小欠账(doc_ops 英文串错误码耦合、上传无 Content-Length 预检、MCP ask 失败分支/busy 文本测试缺口、验收审计断言不绑定本次运行、ApiKeyOut.role 裸 str、MCP list_documents total=截断计数、前端 DELETABLE/REPROCESSABLE 双常量)。

用户已拍板的关键决策(2026-09-19):

1. 主线为**权限治理收官包**:per-KB scope 白名单 + KB 删除端点 + KB 重名 DB 唯一约束;M11 终审八小项全 ride
2. KB 删除权限 = **admin + KB owner**;editor 成员只能删文档不能删库;**Agent 面(REST/MCP)不暴露 KB 删除**,仅 Web 面
3. scope 存储 = `api_keys.kb_scope` JSON 列(NULL=不限);空列表 422;**铸后不可改**(与 role 一致,改=吊销重铸)
4. scope 执行 = **请求时活交集**(用户实时 perm ∩ key scope),key 只能收窄不能放大;JWT 调试通道不受限(与 key_role 豁免同哲学)
5. KB 删除遇文档处理中 → **409 busy**(与文档删除同哲学,防 worker 中途写孤儿 chunks)

## 目标

1. `api_keys.kb_scope` JSON 列 + alembic 迁移(down_revision=`c1d2e3f4a5b6`);存量 key NULL=不限,**零影响**
2. `Principal.key_scope` 全链路传播;三处过滤:facade `_permitted_kb_ids`/`list_kbs_for`(search/ask/list_kbs)、doc_ops `visible_kb_or_404`/`visible_doc_or_404`(agent 文档五操作)
3. 铸造面:`ApiKeyCreateIn`/`AdminKeyCreateIn` 增 `kb_scope` 入参(422 校验:元素须存在且归属用户当前可访问、列表非空去重);`ApiKeyOut` 增 `kb_scope`;审计 `key_create` 记 scope;新端点 `GET /api/admin/users/{user_id}/kbs`(admin 代发下拉数据源)
4. Web `DELETE /api/kbs/{kb_id}`(admin/owner)+ `services/kb_ops.py::delete_knowledge_base` 级联:busy 409 → chunks → documents → kb_permissions → conversations.kb_ids array_remove → KB 行 → 磁盘目录与 eval_sets 文件 best-effort 清理 → 审计 `kb_delete`
5. KB name DB 唯一约束迁移(存量去重后建唯一索引;应用层 409 保留,DB `IntegrityError` 兜底转 409)
6. M11 终审八小项全部清偿
7. 前端:KeysPage scope 多选 + 徽标(自服务/ admin 代发两模式)、KbPage 删除按钮 + 确认框
8. 验收:pytest 全绿(基线 247P 起步)+ 真栈 `m12_acceptance.py` + MCP 真客户端走查 + 前端走查

## 非目标(明确不做)

- Agent 面不暴露 KB 删除,也不做 Agent 面 KB 创建/重命名(用户拍板)
- KB 重命名端点(候选顺延)
- per-KB × per-capability 矩阵、scope 铸后编辑、key 更新端点(吊销重铸)
- 软删除/回收站(YAGNI,删除不可逆靠确认框 + 审计)
- conversations 删除或迁移(仅 array_remove 清悬空 id,会话本体保留)
- `purge_orphan_evalsets.py` 移除(保留作历史人工清理兜底)
- 前端新增页面/路由(只改 KeysPage/KbPage)

## A. 数据模型与迁移

单个迁移文件 `d4e5f6a7b8c9_m12_kb_scope_and_unique_name.py`(down_revision=`c1d2e3f4a5b6`):

1. `op.add_column("api_keys", sa.Column("kb_scope", sa.JSON(), nullable=True))`
   - 模型:`ApiKey.kb_scope: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)`;NULL=不限
   - **不做 server_default**——NULL 语义即"不限",与"空列表"区分(空列表在 API 层 422)
2. KB name 唯一约束:
   - 存量去重(纯 SQL,幂次安全):同名组按 id 升序,第 2 条起 `name = name || '-' || 序号`(从 2 起)
   - `op.drop_index("ix_knowledge_bases_name")` → `op.create_unique_constraint("uq_knowledge_bases_name", "knowledge_bases", ["name"])`
   - 模型同步:`KnowledgeBase.name` 加 `unique=True`
   - 应用层 409 检查保留(友好报错先行);`POST /kbs` 捕获 `IntegrityError` → rollback → 409 `knowledge base name already exists`(并发兜底,文案不变)

## B. per-KB scope:语义与执行点

### B1. 语义

- `kb_scope: list[int] | None`;None/缺省 = 不限(继承用户全部可访问库,存量 key 零影响);非空 = 白名单
- **实际可访问 = 用户实时 perm ∩ key scope**:key 只能收窄不能放大;用户失权自动传导;KB 被删后 scope 残留 id 由活交集自然失效(自愈,无需清理任务)
- JWT 主体:`key_scope` 恒 None,不受限(JWT 调试通道与限流/配额/key_role 豁免同哲学)
- 铸造校验(自服务与 admin 代发同规则,按**归属用户**判):scope 元素去重后逐一须 `get_kb_perm(db, 归属用户, kb) is not None`,任一不满足 → 422 `{"detail": "kb_scope contains inaccessible knowledge base: {id}"}`;`kb_scope == []` → 422(要么不传,要么非空)
- 审计:`key_create` detail 在 scoped 时增 `"kb_scope": [ids]`

### B2. 传播与执行点

- `resolve_bearer_principal`:api_key 路径把 `key.kb_scope` 读入 `Principal.key_scope: frozenset[int] | None`(JSON 列表 → frozenset;JWT 恒 None)
- **search/ask/list_kbs(agent REST + MCP 共用 facade)**:
  - `_permitted_kb_ids`:perm 过滤(现状)后,若 `key_scope is not None` 则再 ∩ scope;scope 外的 kb_id 并入 `AgentKbDenied.denied_kb_ids`——REST 403 `kb_forbidden` / MCP `ToolError("kb_forbidden, ...")` 语义与报文形状**完全不变**
  - `list_kbs_for`:perm 非 None 且(无 scope 或 kb ∈ scope)才返回
- **agent 文档五操作(REST + MCP)**:`visible_kb_or_404` / `visible_doc_or_404` 折入 scope——kb 不在 key scope → 404 `not_found`(不泄露存在性,与"不可见"同桶);错误序保持 **404 可见性 → 403 perm → 403 key 能力** 不变
  - 实现注:`visible_*_or_404(db, user, ...)` 现签名只收 user;增可选参 `key_scope: frozenset[int] | None = None`,REST/MCP 调用面传 `principal.key_scope`(Web 面 JWT 不传)
- `get_quota`、限流、配额不受 scope 影响

### B3. 铸造面 API

- `ApiKeyCreateIn` / `AdminKeyCreateIn` 增 `kb_scope: list[int] | None = None`;`issue_api_key(db, user, name, expires_in_days, role="read_only", kb_scope=None)` 签名扩展,服务层做 422 校验(两端点共用)
- `ApiKeyOut` / `ApiKeyCreatedOut` 增 `kb_scope: list[int] | None`
- 新端点 `GET /api/admin/users/{user_id}/kbs`(require_admin;目标用户 404):返回目标用户可见库 `[{id, name}]`——从 `GET /kbs` 列表逻辑抽 helper `visible_kbs_for(db, user)`(owner ∪ 授权;目标用户是 admin 则全库,与 `get_kb_perm` 语义一致);仅供 admin 代发下拉,响应不含 doc_count 等冗余
- README《外部 Agent 接入指南》增补:scope 语义、404/403 表现、铸造校验规则

## C. KB 删除端点(Web only)

### C1. 端点语义

`DELETE /api/kbs/{kb_id}`:

- 不可见(不存在或无 perm)→ 404(与既有单库 GET 一致)
- 可见但非 owner/admin → 403 `owner or admin required`
- admin(隐式 owner 级)或 `kb.owner_id == user.id` → 级联删除,204
- 审计 action `kb_delete`,target `kb:{id}`,detail 含 `name/doc_count/member_count`(删行前统计)+ client 语境(Web 面无 key,沿用现有模式)

### C2. `services/kb_ops.py::delete_knowledge_base(db, kb, *, username) -> None`

级联顺序(事务内,末尾 commit——与 doc_ops 风格一致):

1. `EXISTS(Document WHERE kb_id AND status IN BUSY_STATUSES)` → 409 `knowledge base has documents being processed`
2. 删 `chunks`(kb_id 命中;pgvector 向量与 tsv 均在行内,删行即删向量)
3. 删 `documents`
4. 删 `kb_permissions`
5. `UPDATE conversations SET kb_ids = array_remove(kb_ids, :kb_id) WHERE :kb_id = ANY(kb_ids)`(清悬空 id,旧会话可继续用剩余库提问)
6. 删 KB 行
7. best-effort 磁盘清理(失败仅 warning,不阻断):`shutil.rmtree(UPLOAD_DIR/{kb_id}/, ignore_errors=True)`;`eval_sets/{kb_id}.json` unlink(missing_ok)
8. audit + commit

worker 侧无需改动:M11 已有"文档行不存在即静默退出"防御(`pipeline.py::_run`),409 busy 检查与删除之间的毫秒级窗口残余风险与 M11 表述一致。

### C3. 前端

- `KbPage.vue`:卡片操作区增删除按钮(**仅 admin 或 my_perm=='owner' 可见**,与"成员"按钮同判)→ `ElMessageBox.confirm` 重警示文案("将永久删除该知识库及其全部文档、分块、向量与成员授权,不可恢复")→ 成功刷新列表;409 busy 显示后端 detail
- `frontend/src/api/kb.ts` 增 `remove(id)`

## D. M11 终审八小项(全部清偿)

| # | 项 | 落点 |
|---|---|------|
| ① | 结构化 doc_ops 错误码 | 新 `DocOpError(code, status, message)` 异常(doc_ops 全部 raise 点改造:not_found/duplicate/too_large/unsupported_type/busy);注册全局 FastAPI exception handler → `{"detail": message}` 同 status(Web/REST 面 HTTP 报文**零变化**);MCP `_e` 改捕 `DocOpError` 按 `code` 出 `ToolError(f"{code}: {message}")`,**消灭英文子串匹配**("being processed" in detail);HTTPException 兜底分支保留 |
| ② | 上传 Content-Length 预检 | Web `api/documents.py` 与 REST `api/agent.py` 上传端点在 `file.read()` 前查请求头:`Content-Length > (MAX_UPLOAD_MB + 1) MB` → 413(multipart 开销 +1MB 松余量;预检近似,权威校验仍在 save_upload);MCP `upload_document` 解码前按 b64 长度粗判 `len(content_b64) > (MAX_UPLOAD_MB*4/3 + 1) MB` → `too_large` |
| ③ | MCP ask 失败分支测试 | `test_mcp.py` 补:ask 的 kb_forbidden 分支、internal error 分支(monkeypatch facade 抛错断言 ToolError "internal error")、JWT principal 走 ask(放行语义断言) |
| ④ | MCP busy 文本测试 | `test_mcp.py` 补:delete/reprocess 对 busy 文档 → `ToolError` 文本以 `busy:` 开头 |
| ⑤ | 验收审计 SUFFIX 过滤 | `scripts/m11_acceptance.py` 审计查询改按本次运行的 target(`doc:{doc_id}`)过滤,断言与本次运行唯一关联;M12 验收脚本沿用该模式 |
| ⑥ | ApiKeyOut.role Literal | `schemas/auth.py` 的 `role: str` → `Literal["read_only", "editor"]` |
| ⑦ | list_documents total | MCP `list_documents`:`total` 改为全量 `count()` 查询(与 limit 截断解耦);audit 的 doc_count 同步用全量 |
| ⑧ | DELETABLE==REPROCESSABLE | `DocsPage.vue` 两个同值常量合一(单常量 + 注释指明"处理中不可删/不可重入,与后端 BUSY_STATUSES 反集") |

## E. 前端(汇总)

| 页面 | 变更 |
|---|---|
| KeysPage.vue | 铸造表单增"可访问范围"多选(el-select multiple;空=不限,占位文案"不限(全部授权库)");选项源:自服务=`GET /kbs` 自己的列表;admin 代发=选定账号后调 `GET /api/admin/users/{id}/kbs` 加载;提交体增 `kb_scope: number[] \| null`(空选→null);key 列表增范围徽标("全部"/"N 库",title 列出 id) |
| KbPage.vue | 删除按钮 + 确认框 + 409 detail 透出(C3) |
| api/keys.ts、api/kb.ts | create/createAdmin 传 kb_scope;新增 adminUserKbs(userId)、kb.remove(id) |

## 测试与验收

### 后端 pytest(`.venv`,conftest 既有套路)

- **scope 矩阵**:key(None/白名单)× 主体(JWT/read_only key/editor key)× 操作(search/ask/list_kbs/文档五操作)× 用户 perm(viewer/editor/owner)——核心断言:scoped key 对界外库 search/ask 403 kb_forbidden、list_kbs 不含、文档操作 404;None key 行为与 M11 基线全同;JWT 不受限
- **铸造**:kb_scope 422(空列表/不存在 id/归属用户不可访问 id);去重落库;ApiKeyOut.kb_scope 回显;admin 代发 scoped key(校验按目标用户);审计 detail 含 kb_scope
- **admin users/{id}/kbs**:目标用户可见集正确(owner ∪ 授权;admin 目标=全库);非 admin 403;目标 404
- **KB 删除**:级联全断言(chunks/documents/kb_permissions 行消失、conversations.kb_ids 已清、UPLOAD_DIR/{kb_id} 目录与 eval_sets 文件删除、磁盘失败不阻断);busy 409;权限矩阵(非可见 404/成员 403/owner 204/admin 204);审计 kb_delete detail
- **唯一约束**:模型 unique 生效;`POST /kbs` IntegrityError 兜底转 409(可用 DB 直插同名后走端点,或 mock)
- **八小项**:①DocOpError 出口三面断言(Web/REST detail 文案不变、MCP code 前缀)②Content-Length 预检 413(header 伪造超限)/b64 粗判 ③④test_mcp 新分支 ⑤(并入验收脚本改造,单测无)⑥Literal ⑦total=全量 ⑧前端 vitest
- 前端 vitest:KeysPage spec 增 scope 提交体/徽标/admin 模式加载目标库下拉;KbPage spec 增删除确认与 409 透出

### 无头验收 `backend/scripts/m12_acceptance.py`(真栈,8001)

铸 scoped key(界内 1 库 + 界外 1 库)→ 界外 search/ask 403、list_kbs 只含界内、界外文档操作 404;界内全通 → admin 代发 scoped key(校验按目标用户)→ owner 建 KB + 上传文档 → 删 KB → 检索零命中、库列表消失、audit kb_delete 落库(按 target 绑定断言);重名并发冒烟(直插同名 → 端点 409 不 500)。

### MCP 真客户端走查

`m91_mcp_walkthrough.py` 门禁化扩展:scoped key 的 list_kbs/search/ask 过滤断言 + 小项③④的 busy/internal error 文本抽查。

### 前端走查

KeysPage:自服务铸 scoped key(多选、徽标);admin 代发选账号 → 范围下拉加载目标用户库;KbPage:owner 删库(确认框 → 列表刷新)、viewer/editor 成员无删除按钮。

## 配置项

无新增(`MAX_UPLOAD_MB` 等全部沿用);`.env.example` 零改动。

## 风险与权衡

- **scope 残留 id**:KB 删除后 `api_keys.kb_scope` 里的悬空 id 不主动清理——活交集使其失效,自愈;列表徽标按 id 显示不查名(不因 KB 消失报错)
- **Content-Length 预检近似性**:multipart 总长含边界开销,+1MB 松余量宁可漏报不可误报;权威校验仍在 save_upload(预检只挡明显超限,省内存缓冲)
- **busy 检查与删除的竞态窗口**:毫秒级;worker 已有缺行静默退出防御(M11),残余风险接受,与 M11 spec 同表述
- **唯一索引上线即限重名**:与 M8 起的应用层行为一致,非破坏性变更;存量去重改名只影响显示名(全库引用走 id)
- **conversations array_remove 的全局性**:KB 删除是全局事件,所有引用该库的会话(不分用户)都清 id——会话本身保留,剩余库可继续提问;若清空后 kb_ids 为空数组,该会话下次提问按现有空库语义走(零命中/拒答路径,不 500)
- **admin users/{id}/kbs 信息面**:admin-only 端点,复用既有可见性逻辑,无新增泄露
- **删除不可逆**:防线 = owner/admin 双权限 + 重警示确认框 + 审计;不设回收站(YAGNI)

## M13 候选(本里程碑分流)

- 质量包:包裹型拒答 LLM 二审、评估结果入库 / reference_answer、ask 延迟优化(grade 前置剪枝)、多跳子问题并行检索
- KB 重命名端点(本期唯一约束为其铺路)
- MinerU 本地化、出站集成、A2A 评估、LDAP/SSO(仍等输入)
