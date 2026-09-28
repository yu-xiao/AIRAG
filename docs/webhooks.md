# Webhook 出站推送对接指南

(AIRag M17 引入,M18 平台适配+per-KB 订阅,M19 密钥生命周期+出站加固,
M20 新增 eval.cancelled,M21 统一 eval.failed 负载形状;admin 在「出站推送」页配置端点)

## 事件与订阅

- 六事件:`document.done` / `document.failed` / `eval.completed` / `eval.failed` / `eval.cancelled` / `chat.refused`
  - `eval.cancelled`(M20):评估被取消;负载 `run{id, kb_id, mode, item_count, summary}`
    同 `eval.completed` 形状,但 `item_count` 为取消前已完成的子集(非全量)
- 订阅粒度:事件类型(多选,空=全部)× 知识库(多选,空=全部;`chat.refused` 按命中任一订阅库投递)
- at-least-once:可能重复投递,**接收方必须按 `event_id` 幂等去重**

### 事件负载示例(generic 通道全量信封的 `data` 字段)

信封外层恒为 `{"event_id", "event_type", "occurred_at", "data"}`;
以下示例均为 `data` 内容。平台通道收到的是中文 markdown 摘要,非全量信封。

- **document.done**

  ```json
  {"document": {"id": 42, "kb_id": 3, "filename": "预算说明.pdf",
                "chunk_count": 17}}
  ```

- **document.failed**

  ```json
  {"document": {"id": 43, "kb_id": 3, "filename": "扫描件.pdf"},
   "error": "OCR 空结果"}
  ```

- **eval.completed / eval.cancelled**(cancelled 的 `item_count` 是取消前
已完成子集,非全量)

  ```json
  {"run": {"id": 7, "kb_id": 3, "mode": "retrieval", "item_count": 12,
           "summary": {"item_count": 12, "hit": 0.9167, "mrr": 0.8611,
                       "keyword_recall": 0.8333}}}
  ```

  generation 模式的 summary 形状:`{"item_count", "faithfulness_avg",
  "relevancy_avg", "refused_count", "reference_avg"(未设参考答案则为
  `null`,非缺席——generation 题目恒带 `reference` 键,`summarize` 必落
  该字段)}`;未测量的检索指标缺席、未测量的均值为 `null`,不落 0
  (「未测量≠零分」,M16 语义)。

- **eval.failed**(M21 起 `run` 形状与 completed 统一,含部分汇总)

  ```json
  {"run": {"id": 8, "kb_id": 3, "mode": "generation", "item_count": 5,
           "summary": {"item_count": 5, "faithfulness_avg": 0.82,
                       "relevancy_avg": 0.9, "refused_count": 1,
                       "reference_avg": null}},
   "error": "ZHIPU_API_KEY 未配置,生成评估无法执行"}
  ```

  孤儿收口(worker 崩溃后被清扫)也发 eval.failed:`summary` 为 null、
  `item_count` 为创建时题数、`error` 为 `orphaned: heartbeat expired …`。

- **chat.refused**(`source`:`web` 网页 / `rest` API Key / `mcp` MCP 工具)

  ```json
  {"source": "rest", "kb_ids": [3, 5], "question": "竞品价格是多少"}
  ```

## 通用端点(provider=generic)

- 请求:POST 信封 JSON(`event_id` / `event_type` / `occurred_at` / `data`)
- 签名头:`hex(HMAC-SHA256(secret, f"{ts}.{body}"))`
  - `X-AIRag-Signature` / `X-AIRag-Timestamp`(Unix 秒)/ `X-AIRag-Event`
  - 验签示例(Python):

    ```python
    import hashlib, hmac

    def verify(secret: str, ts: str, body: bytes, sig: str) -> bool:
        expect = hmac.new(secret.encode(), ts.encode() + b"." + body,
                          hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expect)
    ```

- 响应约定:**2xx 即成功;3xx 视为永久拒收(不跟随重定向)转死信**;
  429/5xx 按退避重试(1/5/15/60 分钟,上限 5 次);其余 4xx 立即死信

## 平台端点(企微/钉钉/飞书群机器人)

- 消息为中文 markdown 摘要(非全量信封;需要全量数据请配 generic 端点)
- 内容上限 3800 UTF-8 字节,超长截断加「…(截断)」
- 成功判定 = HTTP 2xx 且平台 body 码 = 0(三平台都以 200 包业务错误):

| 平台 | 成功字段 | 瞬态(退避重试) | 永久(死信) |
|---|---|---|---|
| 企微 wecom | errcode==0 | 45009 频率限制 | 93000 URL 不合法/机器人被移除 |
| 钉钉 dingtalk | errcode==0 | -1 系统繁忙;90001 发送过快 | 310000 安全设置校验未通过 |
| 飞书 feishu | code==0(兼容 StatusCode) | 9499 频控(100/分钟、5/秒) | —(统一重试耗尽) |

未知非零码按瞬态处理(保守重试,耗尽转死信;死信可 admin 手动重投)。

- 加签(可选):
  - 钉钉:secret=机器人加签密钥;请求 URL 追加
    `&timestamp={毫秒}&sign={urlencode(base64(HMAC-SHA256(key=f"{ts}\n{secret}", msg=b"")))}`
  - 飞书:secret=签名密钥;body 增 `"timestamp":"{秒}","sign":"{base64(同上)}"`
  - 企微:key 在 webhook URL 内,无需 secret;系统仍存一个占位密钥
    (secret 列非空约束所致)——占位不参与任何签名、永不下发,创建/切换
    响应 `secret=None`,列表 `secret_masked` 为空即「无需密钥」
  - generic 端点的 X-AIRag 签名头对平台端点不发(secret 语义已变为平台加签)
- **平台密钥生命周期(M19)**:钉钉/飞书更换密钥请编辑 `im_secret`
  (provider 切换请求带同字段,空串=取消加签);轮换已禁用——
  `rotate_secret` 对平台通道一律 422:密钥来自 IM 后台,不可随机生成。
  切换 provider 时旧 secret 不结转,按目标通道重置:generic→新随机密钥
  (明文仅切换响应出现一次);wecom→新占位(永不下发);钉钉/飞书→
  `im_secret`,缺省为空(不加签)

## 运维

- 投递记录/状态机/手动重投:admin「出站推送 → 投递记录」(dead/retrying 行)
- SSRF 防护:端点 URL 禁指向私网/回环/保留地址(建改 422 拦截 + 投递前复核
  转死信);白名单 `WEBHOOK_SSRF_ALLOWLIST`(IP/CIDR 逗号分隔);已知限制:
  校验与连接两次解析的 DNS rebinding 竞态不在防护范围(admin 信任边界内纵深防御)
- 参考官方文档:[企微消息推送](https://developer.work.weixin.qq.com/document/path/91770) / [钉钉自定义机器人](https://open.dingtalk.com/document/robots/custom-robot-access) / [飞书自定义机器人](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)
