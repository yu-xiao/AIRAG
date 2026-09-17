# AIRag M7 设计(前端全站体验升级 / 零命中双门控)

## 0. 背景与范围

M6 已收官(main=48e8c61):多跳兜底、LLM-judge 评估、审计留存,后端 127P/前端 6P+lint 0-0。
2026-09-17 用户走查暴露两类问题:

1. **前端整体简陋**:布局壳为默认 Element Plus 裸菜单(无品牌/图标/主题),首页仅一个
   `el-empty` 占位,知识库/文档页为纯表格,无搜索筛选与概览。
2. **零命中问题仍返回资料+引用**:检索无相关度门槛(RRF 融合分无量纲语义),恒返回
   top-8;generate 提示词只禁编造、未禁罗列,导致"不相关也凑进来"。

**M7 范围(用户已拍板)**:

- 前端**全站统一升级**(7 页+布局壳),技术路线=Element Plus 主题定制层+设计令牌,
  **主色保持 EP 默认蓝 #409EFF**(不做品牌色派生),支持**亮+暗双主题**。
- 零命中**双管齐下**:generate 提示词收紧 + rerank 相关度阈值门控,`refused` 标记全链路,
  前端精排开关**改为默认开启**(使阈值门控默认生效)。
- 附带小项:ProactorEventLoop Windows 注意事项补 README(M6 遗留文档化)。

**不做(推 M8 候选池)**:评估结果入库、多跳子问题并行检索、MinerU 本地化、LDAP/SSO
(等用户输入)、judge NaN 防护、检索调试面板、文档在线预览、侧栏折叠。

## 1. 设计系统与双主题(前端,新增 `src/styles/`)

### 1.1 令牌层 `tokens.css`

`:root` 定义 `--app-*` 自定义属性,`html.dark` 覆写暗色值:

| 令牌 | 用途 |
|---|---|
| `--app-bg` / `--app-bg-soft` | 页面底色 / 次级区块底色(卡片内嵌、工具栏) |
| `--app-card-bg` / `--app-card-border` | 卡片背景 / 边框 |
| `--app-radius` / `--app-radius-sm` | 8px / 6px 圆角基线 |
| `--app-shadow-card` | 卡片阴影(暗色下减弱) |
| `--app-spacing-*` | 4/8/12/16/24 间距梯度 |

主色、文字色、成功/警告/危险色**直接引用 EP 变量**(`var(--el-color-primary)` 等),
不重复定义——亮暗切换由 EP dark 变量自动完成,令牌层只补 EP 未覆盖的面(背景/圆角/阴影/间距)。

### 1.2 暗色机制与 `useTheme`

- 入口引入 EP 官方 `element-plus/theme-chalk/dark/css-vars.css`。
- `composables/useTheme.ts`:切换 `<html>` 的 `dark` class;localStorage `airag_theme`
  持久化(`'light' | 'dark'`),**默认亮色**;提供 `isDark`/`toggle`。
- 头部放主题切换按钮(太阳/月亮图标,图标库已有依赖 `@element-plus/icons-vue`)。

### 1.3 全局基线细节

细滚动条(webkit + firefox)、统一 focus ring、表格斑马纹+悬浮态、空状态文案与图标统一、
`document.title` 随路由 `meta.title` 同步(router afterEach)。

## 2. 布局壳与共享组件

`MainLayout.vue` 重做(逻辑零改动,仅结构与样式):

- 侧栏 220px:品牌区(简单几何标记+"AIRag")→ 图标菜单(首页 HomeFilled / 知识库
  Collection / 对话 ChatDotRound)→ admin 分组标题"管理"(用户管理 User / 审计日志
  Document),`v-if` 权限逻辑沿用。
- 头部:左=当前页标题(`route.meta.title`);右=主题切换按钮+用户下拉(首字母圆形头像、
  用户名、角色 tag;下拉:退出登录,登出逻辑沿用)。
- 新增共享组件 `components/PageHeader.vue`(props: title/description;slot: actions),
  业务页统一页头。

## 3. 页面改造明细(7 页)

所有页面**业务逻辑与 API 调用零改动**,仅表现层与少量交互补强;对话框/表单校验/轮询等
既有行为全部保留。

| 页面 | 改造 |
|---|---|
| **HomePage** | 工作台:时段问候+用户名;统计卡 3 张(知识库数/文档总数=Σdoc_count/会话数,数据全部来自既有 `kbApi.list`+`conversationsApi.list`,零后端改动);快捷入口三项——新建知识库(`/kb?create=1`)、开始提问(`/chat`)、查看知识库(`/kb`);最近会话前 5 条,点击跳 `/chat?conv=<id>` |
| **KbPage** | 统计条+名称/描述搜索框(前端过滤)+**卡片网格**(名称/描述/权限 tag/文档数/时间;操作:进入、成员[owner]);viewer 隐藏新建(沿用);`?create=1` 时 onMounted 自动打开新建对话框;创建/成员对话框沿用只换皮;空态引导文案 |
| **DocsPage** | 工具行:文件名搜索+状态筛选+处理中徽章(N 个);状态列配图标,失败原因 tooltip 保留;客户端分页(>20 条启用);上传卡紧凑化;分块抽屉沿用换皮;3s 轮询逻辑不动 |
| **ChatPage** | 会话列表规整(图标、悬停操作保留);工具栏卡片化(KB 选择+精排开关);气泡配头像(用户首字母/助手 logo 标);"思考中…"→三点跳动动画;**refused 消息特殊样式且不渲染 CitationList**(见 §4.3);精排开关**默认值改为开**(`localStorage` 无记录时 on,已有关键字沿用用户选择);新增"停止生成"按钮(streaming 时发送按钮变"停止",调既有 `abort()`,已知行为:客户端展示截断、服务端仍完整落库,与现状卸载 abort 一致);支持路由 `?conv=<id>` 定位会话(onMounted 加载后 openConversation) |
| **UsersPage / AuditLogPage** | 套 PageHeader+统一表格风格与筛选行;逻辑零改动(admin 清理按钮等保留) |
| **LoginPage** | 居中品牌卡片+双主题适配 |

## 4. 零命中双门控(后端)

### 4.1 提示词收紧(`app/services/chat_graph/nodes.py`)

SYSTEM_PROMPT 收紧为:只依据资料回答;引用标注 [n];**资料与问题不相关或不足以回答时,
只回复固定话术「知识库中未找到相关内容」,不得罗列、摘要或拼凑返回的资料**;中文简洁分点。
固定话术沿用现有措辞,同时是 refused 判定锚点(§4.3)。

### 4.2 rerank 相关度阈值门控

现状约束(已核):混合检索融合分是 RRF(无量纲),rerank 节点目前只用顺序、丢弃了智谱
rerank API 自带的 relevance_score。**阈值只能挂在 rerank 相关度分上**:

- `RerankProvider.rerank`(base/zhipu)返回值 `list[int]` → `list[tuple[int, float]]`
  (index, relevance_score);智谱实现透传 `results[].relevance_score`;测试 fake 实现给
  确定分数。
- `rerank_node`:按 relevance 降序取 `RETRIEVAL_TOP_K`,**过滤 score <
  `RETRIEVAL_MIN_SCORE` 的命中**;relevance 回写 `hit["score"]`。
- **score 双语义**(spec 明确):rerank 开启=智谱相关度分(0~1);关闭=RRF 融合分。
  引用构建不依赖 score,不受影响。
- 配置:`RETRIEVAL_MIN_SCORE`,默认 **0 = 禁用**(2026-09-17 验收实测:智谱 rerank
  relevance_score 全量饱和 0.92~1.0,零命中/正常分布重叠,无可行阈值;详见附录);
  .env.example/README 同步。
- **边界文档化**:rerank 关闭(请求级开关)时无相关度分,阈值不生效,仅提示词兜底。
- 路由联动**零改动**:全滤空 → `hits=[]` → 既有零命中链路(grade 空 → route 视零命中 →
  decompose 多跳兜底 → 仍空 → generate 空上下文 → 按话术拒答)。补集成测试锚定此行为。

### 4.3 refused 全链路

- `generate_node` 返回值增 `refused: bool` —— `resp.content.strip().startswith("知识库中未找到相关内容")`。
- 持久化:`messages` 表加列 `refused boolean not null default false`(alembic 加列迁移,
  server_default='false',up/down 均测);消息落库处写入。
- API:SSE done 帧与 `GET /conversations/{id}/messages` 的 MessageOut 均带 `refused`。
- 前端:done 帧与历史加载读到 `refused=true` → 该助手消息渲染为提示样式(info 色+图标)
  且**不渲染 CitationList**;refused=false 行为不变。

### 4.4 精排默认开

前端 `rerankEnabled` 初始值:`localStorage` 有 `airag_rerank` 沿用,否则 **true**(原为
false)。README 说明默认开启的收益(阈值门控+排序质量)与代价(每问一次 rerank 调用),
可手动关闭。

### 4.5 附带:ProactorEventLoop 文档化

README 部署节补 Windows 注意事项:裸 uvicorn(非 --reload)走 ProactorEventLoop 会打断
psycopg checkpointer 导致 ask 500;后端启动必须走 `start_dev.bat`。

## 5. eval 验证与阈值调参

- eval_sets 新增零命中题集(对目标 KB 确定无答案的问题,如与库内容无关的常识题);
  `scripts/eval_generation.py` 沿用,不做结构改动。
- 验收断言:零命中题**拒答率 100%**(answer=固定话术);正常题 faithfulness/relevancy
  均值**不低于无阈值基线**(防误伤)。judge 对拒答的既有规则(faithfulness=1.0 /
  relevancy=0.5)使其可自动评判。
- 调参协议:0.30 若误伤正常题,按 0.05 档下调重跑,终值与过程记录回填本 spec 附录;
  极端情况可设 0 禁用(提示词仍兜底)。
  **2026-09-17 验收结论:实测智谱 rerank 分数饱和,零命中/正常分布重叠(数据见附录),
  无可行阈值;终值 0(禁用),零命中防线由收紧提示词 + refused 标记承载。**

## 6. 测试与验收

### 6.1 后端单测(pytest,预算 +10~14 个测试函数)

rerank 带分回写与排序 / 阈值过滤(含 0=禁用、全部滤空、边界等于)/ rerank 关闭路径
不滤波 / refused 判定(命中/未命中/带前后缀) / messages 落库与 MessageOut 字段 /
SSE done 帧字段 / alembic up-down / 提示词收紧后既有图测试语义对齐。
**红线:现有 127P 只增不减。**

### 6.2 前端测试与静态检查

新增 `useTheme` 单测(默认亮色/切换/持久化)、refused 渲染单测(隐藏 CitationList);
现有 6P 不回归;`vue-tsc`+oxlint/eslint 0-0+build 绿。

### 6.3 无头验收脚本(`scripts/m7_acceptance.py`,对齐 M5/M6 模式)

- rerank on + 零命中问题 → done 帧 `refused=true` 且答案=固定话术(citations 可能非空,
  由前端 refused 隐藏——设计 UX 路径;2026-09-17 裁决);
- rerank on + 正常问题 → 引用正常、refused=false;
- rerank off + 零命中问题 → 提示词兜底拒答成立;
- eval_generation 对比断言(§5)。

### 6.4 用户浏览器走查

7 页 × 亮/暗双主题走查清单(逐页勾选),重点:暗色下表格/抽屉/代码高亮/空态。

## 7. 风险与退化

| 风险 | 缓解 |
|---|---|
| 阈值误伤正常问题 | 默认 0 禁用(智谱分数饱和,无可行阈值);eval 基线对比;换有效分数分布供应商时可 env 开启 |
| 精排默认开带来延迟/费用 | 用户可关;README 说明;fake rerank 路径不受影响 |
| 暗色遗漏(自定义组件配色冲突) | 令牌层统一走 CSS 变量;双主题逐页走查清单 |
| refused 误判(正文恰以话术开头) | 提示词限定"只回复固定话术";startswith 判定+测试锚定 |
| abort 展示与服务端落库不一致 | 既有行为(卸载 abort 同款),spec 记录不新修 |
| 前端改造碰坏既有交互 | 业务逻辑零改动红线;vitest 回归+手动走查 |

## 交接(M8 候选,按需)

评估集 reference_answer/评估结果入库、多跳子问题并行检索、MinerU 本地化、LDAP/SSO
(等输入)、judge NaN 防护、检索调试面板、文档在线预览、侧栏折叠、暗色跟随系统。

## 附录:阈值调参记录(实施期回填)

- 2026-09-17 设计初值:`RETRIEVAL_MIN_SCORE=0.30`(保守,防误伤;0=禁用)。
- 2026-09-17 验收实测(取证于真智谱 API,KB=M7验收库,详见 task-12 报告):
  - 裸 rerank API:10 篇互不相关文档对零命中问题(珠峰海拔)打分全落 **[0.92, 1.0]**
    (如"今天天气很好"得 0.990,最不相关的"光合作用"块也有 0.939);
  - 真实图运行时(rerank on,阈值 0.30):零命中题 6/6 命中存活(**零命中 max
    0.999996**),正常题次高 0.996584 —— 两分布完全重叠,**任何阈值都无法同时满足
    "零命中滤空"与"正常不误伤"**(升到 1.0 则 0.9999962 的正常命中也被滤掉);
  - **终值:0(禁用)**。零命中防线=收紧提示词(固定话术)+ refused 标记(前端隐藏
    引用);换用有真实分数分布的 rerank 供应商时可经 env 手动开启,作为纵深防御。
