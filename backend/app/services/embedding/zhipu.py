from app.core.config import settings
from app.services.embedding.base import EmbeddingProvider


class ZhipuEmbedding(EmbeddingProvider):
    """智谱 embedding-3,经 OpenAI 兼容端点。同步调用——调用方放线程池。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings.ZHIPU_API_KEY, base_url=settings.ZHIPU_BASE_URL
        )
        resp = client.embeddings.create(
            model=settings.EMBED_MODEL,
            input=list(texts),
            dimensions=settings.EMBED_DIMS,
        )
        data = sorted(resp.data, key=lambda d: d.index)
        return [item.embedding for item in data]
