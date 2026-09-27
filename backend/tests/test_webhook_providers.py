# backend/tests/test_webhook_providers.py
"""M18 T2:适配器构造面——sign 平移等价/平台消息体/加签 URL/截断。"""
import base64
import hashlib
import hmac as hmac_mod
import json
import time

from app.services.webhook_providers import (
    MAX_CONTENT_BYTES, build_request, describe_event, sign_headers)


ENV_DONE = {"document": {"id": 7, "kb_id": 3, "filename": "手册.pdf"}}
ENV_REFUSED = {"source": "rest", "kb_ids": [3, 5], "question": "Q" * 300}


# ---- describe_event:六事件文案 + 未知事件兜底 ----
def test_describe_document_done():
    s = describe_event("document.done", ENV_DONE)
    assert "文档解析完成" in s and "手册.pdf" in s and "3" in s


def test_describe_document_failed_includes_error():
    s = describe_event("document.failed",
                       {"document": {"kb_id": 1, "filename": "f"},
                        "error": "boom " * 100})
    assert "文档解析失败" in s and len(s) < 500  # error 截 200 字


def test_describe_eval_completed_and_failed():
    s = describe_event("eval.completed",
                       {"run": {"kb_id": 2, "mode": "retrieval",
                                "item_count": 5, "summary": {"hit_rate": 0.8}}})
    assert "评估完成" in s and "hit_rate=0.8" in s
    s = describe_event("eval.failed",
                       {"run": {"kb_id": 2, "mode": "judge"}, "error": "x"})
    assert "评估失败" in s


def test_describe_event_eval_cancelled():
    """M20:取消事件 IM 卡片文案——子集题数诚实展示。"""
    from app.services.webhook_providers import describe_event

    text = describe_event("eval.cancelled", {
        "run": {"kb_id": 2, "mode": "generation", "item_count": 2,
                "summary": {"item_count": 2}}})
    assert "评估已取消" in text and "#2" in text
    assert "generation" in text and "2" in text


def test_describe_chat_refused_truncates_question():
    s = describe_event("chat.refused", ENV_REFUSED)
    assert "问答拒答" in s and "rest" in s and "[3, 5]" in s
    assert "Q" * 300 not in s  # 问题截 100 字


def test_describe_test_and_unknown():
    assert "测试消息" in describe_event("test", {"message": "airag webhook test"})
    assert "weird.event" in describe_event("weird.event", {"a": 1})


# ---- generic:与 M17 sign_headers 逐字节一致 ----
def test_generic_build_request_equals_m17():
    payload = {"event_id": "e" * 32, "event_type": "document.done",
               "occurred_at": "2026-09-23T00:00:00+00:00", "data": ENV_DONE}
    url, body, headers = build_request(
        "generic", "wh_s3cret", "document.done", payload, "http://h/cb")
    assert url == "http://h/cb"
    assert body == json.dumps(payload, ensure_ascii=False)
    ts = headers["X-AIRag-Timestamp"]
    expect = hmac_mod.new(b"wh_s3cret", f"{ts}.{body}".encode(),
                          hashlib.sha256).hexdigest()
    assert headers["X-AIRag-Signature"] == expect
    assert "X-AIRag-Event" in headers


def test_sign_headers_module_formula():  # 平移后公式不变(既有等价断言)
    body = json.dumps({"a": 1}, ensure_ascii=False)
    h = sign_headers("wh_s3cret", "document.done", body)
    ts = h["X-AIRag-Timestamp"]
    assert h["X-AIRag-Signature"] == hmac_mod.new(
        b"wh_s3cret", f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()


# ---- wecom:markdown 消息体,secret 不参与,无签名头 ----
def test_wecom_build_request():
    payload = {"event_type": "document.done", "data": ENV_DONE,
               "event_id": "e" * 32, "occurred_at": "t"}
    url, body, headers = build_request(
        "wecom", "ignored", "document.done", payload, "http://qyapi/hook")
    obj = json.loads(body)
    assert obj["msgtype"] == "markdown"
    assert "文档解析完成" in obj["markdown"]["content"]
    assert url == "http://qyapi/hook"
    assert "X-AIRag-Signature" not in headers and "X-AIRag-Event" in headers


# ---- dingtalk:加签拼 URL(timestamp 毫秒);无 secret 不拼 ----
def test_dingtalk_build_request_signed():
    payload = {"event_type": "test", "data": {"message": "airag webhook test"}}
    url, body, headers = build_request(
        "dingtalk", "SECsecret123456789", "test", payload,
        "https://oapi.dingtalk.com/robot/send?access_token=abc")
    assert url.startswith("https://oapi.dingtalk.com/robot/send?access_token=abc&")
    assert "timestamp=" in url and "sign=" in url
    from urllib.parse import unquote
    ts = unquote(url).split("timestamp=")[1].split("&")[0]
    digest = hmac_mod.new(f"{ts}\nSECsecret123456789".encode(), b"",
                          hashlib.sha256).digest()
    # sign 经 urlencode(base64),先 unquote 再比对原文
    assert base64.b64encode(digest).decode() in unquote(url)
    obj = json.loads(body)
    assert obj["msgtype"] == "markdown" and obj["markdown"]["title"]
    assert "X-AIRag-Signature" not in headers


def test_dingtalk_build_request_no_secret_plain_url():
    _, _, _ = build_request("dingtalk", "", "test",
                            {"data": {"message": "x"}},
                            "https://oapi.dingtalk.com/robot/send?access_token=t")
    url2, _, _ = build_request("dingtalk", "", "test",
                               {"data": {"message": "x"}},
                               "https://oapi.dingtalk.com/robot/send?access_token=t")
    assert "sign=" not in url2  # 空 secret=不加签(自定义关键词模式)


# ---- feishu:加签放 body(timestamp 秒);无 secret 不放 ----
def test_feishu_build_request_signed(monkeypatch):
    t0 = 1770000000
    monkeypatch.setattr(time, "time", lambda: t0)  # 钉钉/飞书共用 time.time
    payload = {"event_type": "chat.refused", "data": ENV_REFUSED}
    _, body, headers = build_request(
        "feishu", "fssecret", "chat.refused", payload, "https://open.feishu.cn/hook")
    obj = json.loads(body)
    assert obj["msg_type"] == "text"
    assert "问答拒答" in obj["content"]["text"]
    assert obj["timestamp"] == str(t0)
    digest = hmac_mod.new(f"{t0}\nfssecret".encode(), b"",
                          hashlib.sha256).digest()
    assert obj["sign"] == base64.b64encode(digest).decode()
    assert "X-AIRag-Signature" not in headers


def test_feishu_build_request_no_secret():
    _, body, _ = build_request("feishu", "", "test",
                               {"data": {"message": "x"}},
                               "https://open.feishu.cn/hook")
    obj = json.loads(body)
    assert "sign" not in obj and "timestamp" not in obj


# ---- 超长内容字节安全截断 ----
def test_long_content_truncated_utf8_safe():
    data = {"document": {"kb_id": 1, "filename": "长" * 3000}}
    _, body, _ = build_request(
        "wecom", "", "document.done",
        {"event_type": "document.done", "data": data}, "http://x/h")
    assert len(body.encode("utf-8")) < MAX_CONTENT_BYTES + 600
    assert "截断" in json.loads(body)["markdown"]["content"]
    json.loads(body)  # 截断后仍是合法 JSON、合法 UTF-8


# ---- classify_response:平台 body 码三分类;None 交还状态码规则 ----
import pytest

from app.services.webhook_providers import SsrfBlockedError, classify_response


@pytest.mark.parametrize("provider,code", [
    ("wecom", 0), ("dingtalk", 0), ("feishu", 0)])
def test_classify_platform_success(provider, code):
    assert classify_response(provider, 200, f'{{"errcode": {code}}}'
                             if provider != "feishu"
                             else f'{{"code": {code}}}') == ("succeeded", None)


def test_classify_feishu_legacy_statuscode_field():
    assert classify_response("feishu", 200, '{"StatusCode": 0}') == ("succeeded", None)


def test_classify_transient_codes_retry():
    assert classify_response("wecom", 200, '{"errcode": 45009}')[0] == "retry"
    assert classify_response("dingtalk", 200, '{"errcode": -1}')[0] == "retry"
    assert classify_response("dingtalk", 200, '{"errcode": 90001}')[0] == "retry"
    assert classify_response("feishu", 200, '{"code": 9499}')[0] == "retry"


def test_classify_permanent_codes_dead():
    assert classify_response("wecom", 200, '{"errcode": 93000}')[0] == "dead"
    r = classify_response("dingtalk", 200, '{"errcode": 310000, "errmsg": "sign not match"}')
    assert r[0] == "dead" and "310000" in r[1]


def test_classify_unknown_code_conservative_retry():
    assert classify_response("wecom", 200, '{"errcode": 88888}')[0] == "retry"


def test_classify_none_for_generic_and_non_2xx():
    assert classify_response("generic", 200, "{}")[0] is None
    assert classify_response("wecom", 404, '{"errcode": 0}')[0] is None
    assert classify_response("wecom", 500, None)[0] is None


def test_classify_non_json_body_2xx_means_success():
    assert classify_response("wecom", 200, "<html>gateway</html>") == ("succeeded", None)
    assert classify_response("feishu", 200, None) == ("succeeded", None)


def test_classify_error_message_carried():
    outcome, err = classify_response("feishu", 200, '{"code": 9499, "msg": "too fast"}')
    assert outcome == "retry" and "too fast" in err


# ---- SSRF:IP 字面量/域名多记录/白名单/解析失败/scheme ----
from app.services import webhook_providers as wp


async def test_ssrf_blocks_private_literals():
    # allowlist 显式置空:本测断言「无白名单时私网/回环全拒」,不能被走查
    # .env 的 WEBHOOK_SSRF_ALLOWLIST=127.0.0.1 环境值放行(test_ssrf_allowlist_
    # covers_cidr 已覆盖白名单语义)
    for url in ("http://10.0.0.5/x", "http://192.168.1.1/x",
                "http://127.0.0.1/x", "http://169.254.169.254/meta",
                "http://[::1]/x", "http://[fe80::1]/x", "http://0.0.0.0/x"):
        with pytest.raises(SsrfBlockedError):
            await wp.check_url_allowed(url, allowlist="")


async def test_ssrf_allows_public_literal():
    await wp.check_url_allowed("https://93.184.216.34/cb")  # 无异常即过


async def test_ssrf_domain_any_blocked_record_rejects(monkeypatch):
    async def _multi(host):
        return ["93.184.216.34", "10.0.0.9"]  # 多 A 记录任一私网即拒
    monkeypatch.setattr(wp, "_resolve_host", _multi)
    with pytest.raises(SsrfBlockedError):
        await wp.check_url_allowed("https://good.example.com/cb")


async def test_ssrf_domain_all_public_passes(monkeypatch):
    async def _ok(host):
        return ["93.184.216.34"]
    monkeypatch.setattr(wp, "_resolve_host", _ok)
    await wp.check_url_allowed("https://good.example.com/cb")


async def test_ssrf_allowlist_covers_cidr(monkeypatch):
    async def _priv(host):
        return ["10.1.2.3"]
    monkeypatch.setattr(wp, "_resolve_host", _priv)
    await wp.check_url_allowed("http://in.example.com/cb",
                               allowlist="127.0.0.1,10.0.0.0/8")
    with pytest.raises(SsrfBlockedError):  # 白名单外仍拒
        await wp.check_url_allowed("http://in.example.com/cb",
                                   allowlist="127.0.0.1")


async def test_ssrf_dns_failure_rejects(monkeypatch):
    async def _boom(host):
        raise OSError("dns down")
    monkeypatch.setattr(wp, "_resolve_host", _boom)
    with pytest.raises(SsrfBlockedError):
        await wp.check_url_allowed("https://no.example.com/cb")


async def test_ssrf_empty_resolve_rejects(monkeypatch):
    async def _empty(host):
        return []
    monkeypatch.setattr(wp, "_resolve_host", _empty)
    with pytest.raises(SsrfBlockedError):
        await wp.check_url_allowed("https://void.example.com/cb")


async def test_ssrf_bad_scheme_rejects():
    with pytest.raises(SsrfBlockedError):
        await wp.check_url_allowed("ftp://x/cb")


# ---- M19 T1:白名单 fail-open(毒环修复)+ SSRF 补测 ----
# 毒环机理:白名单条目裸 ip_network(part) 拼错即抛 AddressValueError,在
# check_url_allowed 的 DNS try 块之外逃出;deliver_one 只捕 SsrfBlockedError
# → 行保持 pending(attempts 未增)→ deliver_due 按 id asc 每轮先撞同一行
# 整轮中断 → 拼错存续期间全部投递停滞。
import uuid

from app.models import WebhookDelivery, WebhookEndpoint


def test_allowlist_bad_entries_skipped_not_raised():
    """M19 毒环修复:坏白名单条目 skip+warning,不抛、不放行。"""
    nets = wp._allowlist_networks("127.0.0.1:8000,not-an-ip,10.0.0.0/8")
    assert [str(n) for n in nets] == ["10.0.0.0/8"]


async def test_check_url_survives_bad_allowlist():
    """坏条目存在时 check_url_allowed 不抛;回环仍拒(坏条目不生效)。"""
    with pytest.raises(SsrfBlockedError):
        await wp.check_url_allowed("http://127.0.0.1/x",
                                   allowlist="127.0.0.1:8000")
    assert await wp.check_url_allowed("http://8.8.8.8/x",
                                      allowlist="not-an-ip") is None


class _Resp200:
    status_code = 200
    text = ""


class _OKClient:
    """deliver_one 注入替身:记录 POST,恒 200(注入即用,无需上下文协议)。"""
    def __init__(self):
        self.calls = []

    async def post(self, url, **kw):
        self.calls.append((url, kw))
        return _Resp200()


async def test_deliver_one_survives_bad_allowlist(db_session, monkeypatch):
    """毒环回归:坏 allowlist 下 deliver_one 照常投公网端点,不被打断。

    WEBHOOK_SSRF_ENFORCE 维持默认 true(检查点在):证明坏条目不再以
    AddressValueError 逃出(修复前正是它穿透 except SsrfBlockedError)。
    """
    from app.core.config import settings as cfg
    from app.services.outbound import deliver_one

    monkeypatch.setattr(cfg, "WEBHOOK_SSRF_ALLOWLIST", "127.0.0.1:8000")
    ep = WebhookEndpoint(name=f"m19{uuid.uuid4().hex[:12]}",
                         url="http://8.8.8.8/cb", secret="wh_s3cret",
                         events=[], enabled=True, created_by=1)
    db_session.add(ep)
    await db_session.commit()
    d = WebhookDelivery(endpoint_id=ep.id, event_type="test",
                        event_id="e" * 32, payload={"event_id": "e" * 32},
                        status="pending", attempts=0)
    db_session.add(d)
    await db_session.commit()
    c = _OKClient()
    await deliver_one(db_session, d, client=c)  # 修复前此处抛 AddressValueError
    await db_session.refresh(d)
    assert d.status == "succeeded" and d.attempts == 1
    assert len(c.calls) == 1


async def test_ssrf_ipv6_with_port_allowed_public():
    # 带端口 IPv6 字面量:bracket 解析正确即直判放行(2001:db8:: 在 py3.12
    # ipaddress 属 is_private 文档段,故用真公网地址)
    await wp.check_url_allowed("http://[2606:4700::1111]:8000/x")


async def test_ssrf_ipv6_loopback_blocked():
    with pytest.raises(SsrfBlockedError):
        await wp.check_url_allowed("http://[::1]/x", allowlist="")


async def test_ssrf_settings_default_path(monkeypatch):
    """不传 allowlist 走 settings 默认(空=全拒私网;127/8=放行回环)。"""
    from app.core.config import settings as cfg

    monkeypatch.setattr(cfg, "WEBHOOK_SSRF_ALLOWLIST", "")
    with pytest.raises(SsrfBlockedError):
        await wp.check_url_allowed("http://10.0.0.9/x")
    monkeypatch.setattr(cfg, "WEBHOOK_SSRF_ALLOWLIST", "127.0.0.0/8")
    await wp.check_url_allowed("http://127.0.0.1/x")


@pytest.mark.parametrize("host", ["10.0.0.9", "10.0.0.1", "192.168.0.9",
                                  "172.20.1.5"])
async def test_ssrf_wide_private_ranges_blocked_without_allowlist(host):
    # 显式空 allowlist(dev .env 常设 127.0.0.1,不显式置空会走查环境值)
    with pytest.raises(SsrfBlockedError):
        await wp.check_url_allowed(f"http://{host}/x", allowlist="")


# ---- M20 T4:括号不配对的 IPv6——urlparse 裸 ValueError 必须包成 SsrfBlockedError ----
async def test_ssrf_malformed_bracket_url_blocked():
    """M20:括号不配对的 IPv6 字面量——urlparse 抛 ValueError 必须包成
    SsrfBlockedError,不得逃逸打断建端点/投递调用方。"""
    with pytest.raises(wp.SsrfBlockedError):
        await wp.check_url_allowed("http://[::1/x")
    with pytest.raises(wp.SsrfBlockedError):
        await wp.check_url_allowed("http://[/x")


async def test_ssrf_valid_bracket_v6_still_parsed():
    # 合法公网 v6 带端口照常放行(2001:db8:: 在 py3.12 属 is_private 文档段,
    # 故用真公网地址,同 test_ssrf_ipv6_with_port_allowed_public)
    await wp.check_url_allowed("http://[2606:4700::1111]:8000/x")
