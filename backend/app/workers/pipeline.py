import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models import Chunk, Document
from app.services.chunking import split_blocks
from app.services.embedding import get_provider
from app.services.parsing import get_parser
from app.workers.celery_app import celery_app

EMBED_BATCH = 16


def _run_async(coro):
    # 唯一对简报的偏离(增补 6 最小防御):eager 模式下任务在 API 端点的
    # 事件循环线程内同步执行,asyncio.run 会拒绝("cannot be called from a
    # running event loop")。检测到运行中的循环时,改为在一次性线程里另起
    # 循环执行同一协程;真实 worker 线程无运行循环,仍走 asyncio.run 不变。
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _engine(db_url: str):
    return create_async_engine(db_url, poolclass=NullPool)


async def _mark_failed(
    document_id: int, error_msg: str, db_url: str | None = None
) -> None:
    engine = _engine(db_url or settings.DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            doc = await session.get(Document, document_id)
            if doc is not None:
                doc.status = "failed"
                doc.error_msg = error_msg[:2000]
                await session.commit()
    finally:
        await engine.dispose()


async def _run(document_id: int, db_url: str) -> None:
    engine = _engine(db_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            doc = await session.get(Document, document_id)
            if doc is None:
                raise RuntimeError(f"document {document_id} not found")
            file_path = Path(doc.file_path)
            if not file_path.exists():
                raise FileNotFoundError(f"missing file: {doc.file_path}")

            doc.status = "parsing"
            await session.commit()
            result = get_parser(file_path.suffix.lower()).parse(file_path)

            doc.status = "chunking"
            await session.commit()
            chunks = split_blocks(result.blocks)
            if not chunks:
                raise RuntimeError("no content extracted")

            doc.status = "embedding"
            await session.commit()
            provider = get_provider()
            vectors: list[list[float]] = []
            for i in range(0, len(chunks), EMBED_BATCH):
                batch = chunks[i : i + EMBED_BATCH]
                vectors.extend(
                    await asyncio.to_thread(
                        provider.embed_documents, [c.content for c in batch]
                    )
                )

            for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
                session.add(
                    Chunk(
                        document_id=document_id,
                        kb_id=doc.kb_id,
                        chunk_index=index,
                        content=chunk.content,
                        page_no=chunk.page_no,
                        char_len=chunk.char_len,
                        embedding=vector,
                        content_hash=hashlib.sha256(
                            chunk.content.encode("utf-8")
                        ).hexdigest(),
                    )
                )
            await session.commit()

            await session.execute(
                text(
                    "UPDATE chunks SET tsv = to_tsvector('simple', content) "
                    "WHERE document_id = :doc_id AND tsv IS NULL"
                ),
                {"doc_id": document_id},
            )
            await session.commit()

            doc.page_count = result.page_count or None
            doc.chunk_count = len(chunks)
            doc.status = "done"
            doc.error_msg = None
            await session.commit()
            logger.info(f"document {document_id} done: {len(chunks)} chunks")
    finally:
        await engine.dispose()


@celery_app.task(bind=True, max_retries=3)
def process_document(self, document_id: int):
    try:
        _run_async(_run(document_id, settings.DATABASE_URL))
    except Exception as exc:
        retries = self.request.retries
        if retries < 3:
            logger.warning(
                f"document {document_id} attempt failed: {exc}; retry {retries + 1}/3"
            )
            raise self.retry(exc=exc, countdown=2 ** (retries + 1))
        _run_async(_mark_failed(document_id, str(exc)))
