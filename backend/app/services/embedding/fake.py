import hashlib
import random

from app.core.config import settings
from app.services.embedding.base import EmbeddingProvider


class FakeEmbedding(EmbeddingProvider):
    """确定性假嵌入:同文本恒同向量。仅供测试/无 key 演示,语义无意义。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        dim = settings.EMBED_DIMS
        vectors = []
        for text in texts:
            seed = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:12], 16)
            rng = random.Random(seed)
            vectors.append([rng.uniform(-1.0, 1.0) for _ in range(dim)])
        return vectors
