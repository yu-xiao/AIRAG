def test_rerank_disabled_returns_none():
    from app.services.rerank.base import get_reranker

    assert get_reranker() is None  # conftest 进程未开 RERANK_ENABLED


def test_zhipu_rerank_parses_response(monkeypatch):
    from app.services.rerank import zhipu as zr

    class FakeResp:
        def json(self):
            return {"results": [{"index": 2, "relevance_score": 0.9},
                                 {"index": 0, "relevance_score": 0.5}]}

        # 真实 httpx.Response 有该方法;brief 的 FakeResp 缺失导致 AttributeError,
        # 补 no-op 以保持实现 verbatim(不改变任何断言)。
        def raise_for_status(self):
            return None

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json=json)
        return FakeResp()

    monkeypatch.setattr(zr.httpx, "post", fake_post)
    order = zr.ZhipuRerank().rerank("q", ["a", "b", "c"], top_n=2)
    assert order == [2, 0]
    assert captured["json"]["model"] == zr.settings.RERANK_MODEL
