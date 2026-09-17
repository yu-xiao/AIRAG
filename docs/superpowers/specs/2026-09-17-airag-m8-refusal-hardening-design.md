# AIRag M8 设计:拒答检测加固 + 一致性收口

日期:2026-09-17
状态:已与用户确认(范围=5 项全做;拒答策略=子串包含;E 项=仅一次性清理)
上游:M7 收官记录(`docs/superpowers/plans/2026-09-17-airag-m7-frontend-theme-zero-hit.md`)

## 背景与动机

M7 落地零命中双门控后,智谱 rerank relevance_score 全量饱和(0.92~1.0)导致阈值门控默认禁用(`RETRIEVAL_MIN_SCORE=0`),**提示词门控 + `refused` 标记成为零命中场景唯一主防线**。终审分流与用户走查确认了以下缺口,构成本里程碑:

1. 拒答判定用 `startswith`,模型加礼貌前缀(包裹型拒答)即漏判;
2. `rewrite_node` 的每轮复位字典不含 `refused`,checkpoint 持久下存在跨轮残留(异常中断路径暴露);
3. KB 创建无重名校验(前后端+DB 三层全无),且 ChatPage 多选下拉显示纯名称,存量重名库无法区分(走查发现);
4. 暗色主题漏了 `.hljs-regexp` 一行覆盖;
5. 验收脚本按 `eval_sets/{kb_id}.json` 留存评估集,KB 清理后文件残留,无收口机制。

**已确认的事实边界**:系统不存在 KB 改名(PUT /kbs/{id})与 KB 删除(DELETE /kbs/{id})端点——KB 创建后不可改名、不可删。因此本设计不含 rename 校验与"删除钩子"式自动清理。

## 目标

- A. 包裹型拒答检测:话术出现在回答任意位置即判 `refused=True`,并从提示词源头压制包裹型输出;
- B. `refused` 加入 rewrite 每轮复位字典(拓扑护栏);
- C. KB 创建重名 → 409(后端应用层),前端名称字段内联报错;ChatPage 多选下拉对重名选项追加 id 后缀;
- D. 暗色补 `.hljs-regexp`;
- E. 一次性清理孤儿 eval_sets 文件(对照运行库)。

## 非目标(明确不做)

- DB 唯一约束迁移(运行库存量重复数据需先去重;并发窗口见"风险");
- KB 删除/改名端点(级联删文档+chunk+权限+向量,体量大,列 M9 候选);
- LLM 二审/结构化输出式拒答判定(性价比低,用户已裁定);
- 回改 m7 验收脚本(历史记录保留当时语义);
- 前端创建表单的客户端重名预检(服务端为唯一事实源);
- "等输入"项:LDAP、MinerU 本地化、多跳并行、评估入库。

## A. 包裹型拒答检测(核心)

### 现状

`backend/app/services/chat_graph/nodes.py:109`:

```python
"refused": (answer or "").strip().startswith(REFUSAL_PHRASE),
```

`REFUSAL_PHRASE = "知识库中未找到相关内容"`(nodes.py:10)。模型输出"很抱歉,知识库中未找到相关内容。"时 `refused=False`,引用照常下发、前端照常展示、审计与导出语义错。

### 设计

判定改为子串包含:

```python
"refused": REFUSAL_PHRASE in (answer or "").strip(),
```

提示词同步收紧(nodes.py:15,在"只回复"指令上叠加):

```
只回复"<REFUSAL_PHRASE>"本身,不得添加任何前后缀或礼貌用语,不得罗列、摘要或拼凑返回的资料;
```

子串匹配是兜底而非替代:提示词压制包裹型输出的发生率,子串兜住剩余漏网;两者共同保证 `refused` 语义可信。

**下游零改动**:`refused` 的消费链(SSE done 帧 → `MessageOut` → 前端 AssistantMessage 隐藏引用 → 审计 → 导出跳过 refused 引用)全部沿用 M7 语义,只是判定变准。

**误判面(接受并记录)**:仅当 KB 文档正文完整包含该话术、且模型将其抄入回答时误判。企业知识库(产品数据、规程文档)出现完整系统话术的概率极低;误判后果为隐藏引用与 refusal 样式展示,不产生事实性错误答案。近似措辞(如"知识库里没有找到……")不触发——需完整子串命中。

## B. refused 入 rewrite 复位(拓扑护栏)

`nodes.py:137-144` 的 `reset` 字典追加 `"refused": False`,与 M6 修 `sub_queries` 跨轮泄漏同款思路。

标准拓扑(rewrite → retrieve → … → generate)下 generate 每轮必写 `refused`,此修改只在异常中断路径(图中断后 state 残留、下一轮 ask 前读取)消除跨轮残留,属纯防御性加固,无正向行为变化。`answer`/`citations`/`hits` 同样不在复位之列但均有 generate 覆写保证,维持 M6 先例范围,不扩大。

## C. KB 重名限制 + 下拉区分度

### 后端(create 409)

`backend/app/api/kbs.py:15-33` `create_kb`:在 viewer 检查之后、构造实体之前,以 `payload.name.strip()` 归一化后查询同名:

```python
name = payload.name.strip()
dup = (await db.execute(
    select(KnowledgeBase).where(KnowledgeBase.name == name)
)).scalar_one_or_none()
if dup is not None:
    raise HTTPException(status_code=409, detail="knowledge base name already exists")
```

存储同样使用归一化后的 `name`(strip 后入库),保证比较与存储一致。错误 detail 沿用现有英文风格(如 "knowledge base not found")。

**并发窗口**:两个并发 create 同时通过检查可双双插入(无 DB 唯一约束)。本系统单企业内部使用、创建频率极低,接受该窗口;DB 约束列 M9 候选。

### 前端 KbPage(内联报错)

提交 `createKb` 收到 409 时,在名称字段下方内联展示"该名称已存在"(Element Plus 表单项错误),表单停留在创建对话框,不清空用户输入。实现方式(具体绑定在计划阶段定):提交失败路径将服务端错误写入 name 字段的 validateMessage/自定义 error 状态。

### 前端 ChatPage(下拉区分度)

`frontend/src/pages/ChatPage.vue:353-364` 的 el-select(multiple+filterable)选项 label 现为纯 `k.name`。改为**碰撞时追加后缀**:对当前可选 KB 列表按 name 计数,计数 >1 的选项 label 为 `` `${k.name} ·#${k.id}` ``,唯一者保持纯名。正常(无重名)视觉零变化;存量重名库(创建限制生效前的历史数据)在下拉与已选 tag 中均可区分。

## D. 暗色 hljs-regexp

`frontend/src/styles/tokens.css:64-67` 的暗色 string 规则组追加 `.hljs-regexp` 选择器(色值沿用该组 `#98c379`)。依据:github.css 亮色中 regexp 与 string 同为 `#032f62`,暗色保持同组同色即可。一行改动,无新令牌。

## E. eval_sets 孤儿清理(一次性)

新增 `backend/scripts/purge_orphan_evalsets.py`:

- 连运行库(复用 `seed_expired_audit.py` 的连接方式),取现存 KB id 集合;
- 扫描 `backend/eval_sets/*.json`,文件名数字部分不在集合中者即孤儿;
- **默认 dry-run**:仅打印将删除清单;`--apply` 参数才真删(磁盘 unlink);
- `README.md` 不在匹配范围;脚本不碰 git,删除后的工作区变更随 M8 提交入库。

现状 `eval_sets/` 有 `5.json/6.json/8.json/9.json`,其中 KB 已被走查清理的文件即孤儿,由脚本按 DB 真值判定。验收脚本今后新写的文件不再有自动收口(无删除端点可挂),残留治理依赖本脚本按需复跑——记录此约定。

## 测试与验收

### 后端 pytest(全走 .venv,conftest 已封 RERANK_ENABLED=false)

- 拒答判定(test_chat_graph.py 扩展):精确话术 / 包裹变体("很抱歉,知识库中未找到相关内容。")/ 前导空白 / 近似话术("知识库中未找到相关文档"→False)/ 正常回答含部分字串 →False;
- rewrite 复位:state 残留 `refused=True` → `rewrite_node` 返回含 `refused=False`;
- KB 重名:create 同名 409、异名 201、strip 归一化后同名 409、viewer 403 优先级不变;
- 既有套件全绿(137P 基线不回退)。

### 前端 vitest

- ChatPage:重名列表 label 带 `·#id` 后缀、唯一列表纯名(标签函数抽为可测单元);
- KbPage:create 409 时名称字段内联错误呈现;
- build + lint 0-0。

### 无头验收 `backend/scripts/m8_acceptance.py`(真智谱)

1. 建临时 KB,零命中问题 → SSE done 帧 `refused=true` 且回答含完整话术(子串断言);
2. API 级:同名再建 → 409;异名 → 201(临时资源收尾清理);
3. purge 脚本 dry-run 输出孤儿清单(不断言具体文件,断言"运行输出含 dry-run 标识");
4. 汇总 PASS/FAIL 清单退出。

### 用户走查

浏览器过一遍:创建重名库的内联报错、下拉重名后缀、暗色代码块正则高亮、对话页回归。

## 风险与权衡

| 风险 | 评估 | 对策 |
|---|---|---|
| 子串误判(KB 文档含完整话术) | 概率极低,后果轻(隐藏引用) | spec 记录;话术为系统固定中文长句,非自然文档用语 |
| create 并发重名窗口 | 内部系统低频,可接受 | M9 候选:DB 唯一约束+存量去重 |
| 提示词收紧改变模型输出分布 | 仅收紧拒答分支措辞,答数分支不变 | 验收含真模型零命中+正常问答回归 |
| purge 误删 | 默认 dry-run,--apply 显式触发 | 按 DB 真值判定,不按文件名猜测 |

## M9 候选(本里程碑分流)

KB 删除端点(级联文档/chunk/权限/向量 + eval_sets 钩子)、DB 唯一约束、包裹型拒答的 LLM 二审(若子串+提示词实证不足)、评估入库/多跳并行/MinerU 本地化/LDAP(等输入)。
