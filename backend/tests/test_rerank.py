def test_rerank_disabled_returns_none():
    from app.services.rerank.base import get_reranker

    assert get_reranker() is None  # conftest 进程未开 RERANK_ENABLED


def test_zhipu_rerank_returns_scores(monkeypatch):
    from app.services.rerank import zhipu as zr

    class FakeResp:
        def json(self):
            return {"results": [{"index": 2, "relevance_score": 0.9},
                                 {"index": 0, "relevance_score": 0.5}]}

        def raise_for_status(self):
            return None

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json=json)
        return FakeResp()

    monkeypatch.setattr(zr.httpx, "post", fake_post)
    scored = zr.ZhipuRerank().rerank("q", ["a", "b", "c"], top_n=2)
    assert scored == [(2, 0.9), (0, 0.5)]
    assert captured["json"]["model"] == zr.settings.RERANK_MODEL


def test_zhipu_rerank_defaults_missing_score(monkeypatch):
    from app.services.rerank import zhipu as zr

    class FakeResp:
        def json(self):
            return {"results": [{"index": 1}]}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        zr.httpx, "post",
        lambda url, json=None, headers=None, timeout=None: FakeResp(),
    )
    assert zr.ZhipuRerank().rerank("q", ["a", "b"], top_n=2) == [(1, 0.0)]
