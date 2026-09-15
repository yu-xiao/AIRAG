import pytest

from app.core.config import settings
from app.services.embedding.base import get_provider
from app.services.embedding.zhipu import ZhipuEmbedding


def test_fake_deterministic_and_dims():
    provider = get_provider()  # 测试进程 EMBED_PROVIDER=fake(conftest 约定)
    v1 = provider.embed_documents(["你好世界"])
    v2 = provider.embed_documents(["你好世界"])
    assert v1 == v2
    assert len(v1[0]) == settings.EMBED_DIMS
    other = provider.embed_documents(["另一个文本"])[0]
    assert other != v1[0]


def test_unknown_provider_raises():
    with pytest.raises(ValueError):
        get_provider("no-such-provider")


@pytest.mark.skipif(
    not settings.ZHIPU_API_KEY, reason="ZHIPU_API_KEY not configured"
)
def test_zhipu_live_embedding():
    provider = ZhipuEmbedding()
    vectors = provider.embed_documents([" AIRag 集成测试一", "AIRag 集成测试二"])
    assert len(vectors) == 2
    assert all(len(v) == settings.EMBED_DIMS for v in vectors)
