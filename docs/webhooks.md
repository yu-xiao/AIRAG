# Webhook 出站推送对接指南

(AIRag M17/M18;admin 在「出站推送」页配置端点)

## 事件与订阅

- 五事件:`document.done` / `document.failed` / `eval.completed` / `eval.failed` / `chat.refused`
- 订阅粒度:事件类型(多选,空=全部)× 知识库(多选,空=全部;`chat.refused` 按命中任一订阅库投递)
- at-least-once:可能重复投递,**接收方必须按 `event_id` 幂等去重**

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
  - 企微:key 在 webhook URL 内,无需 secret
  - generic 端点的 X-AIRag 签名头对平台端点不发(secret 语义已变为平台加签)
- **平台加签密钥仅创建端点时填写**;编辑态无法重设(轮换生成的是随机串,
  对平台签名无意义)——需更换平台密钥时请删除并重建端点(历史投递记录保留)

## 运维

- 投递记录/状态机/手动重投:admin「出站推送 → 投递记录」(dead/retrying 行)
- SSRF 防护:端点 URL 禁指向私网/回环/保留地址(建改 422 拦截 + 投递前复核
  转死信);白名单 `WEBHOOK_SSRF_ALLOWLIST`(IP/CIDR 逗号分隔);已知限制:
  校验与连接两次解析的 DNS rebinding 竞态不在防护范围(admin 信任边界内纵深防御)
- 参考官方文档:[企微消息推送](https://developer.work.weixin.qq.com/document/path/91770) / [钉钉自定义机器人](https://open.dingtalk.com/document/robots/custom-robot-access) / [飞书自定义机器人](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)
