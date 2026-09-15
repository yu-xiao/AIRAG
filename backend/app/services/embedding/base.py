from abc import ABC, abstractmethod

from app.core.config import settings


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入;返回与输入等长、各含 EMBED_DIMS 维浮点的列表。"""


def get_provider(name: str | None = None) -> EmbeddingProvider:
    name = (name or settings.EMBED_PROVIDER).lower()
    if name == "fake":
        from app.services.embedding.fake import FakeEmbedding

        return FakeEmbedding()
    if name == "zhipu":
        from app.services.embedding.zhipu import ZhipuEmbedding

        return ZhipuEmbedding()
    raise ValueError(f"unknown embedding provider: {name}")
