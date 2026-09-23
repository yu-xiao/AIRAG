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
from app.services.outbound import emit_event, nudge
from app.services.parsing import get_parser
from app.services.parsing.ocr import maybe_ocr
from app.services.retrieval.tokenize import tokenize as _tok
from app.workers.celery_app import celery_app

EMBED_BATCH = 16


class NoContentError(RuntimeError):
    """确定性失败(解析/OCR 后无内容):重试不会改变结果,直接落 failed。"""


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
                n = await emit_event(session, "document.failed", {
                    "document": {"id": document_id, "kb_id": doc.kb_id,
                                 "filename": doc.filename},
                    "error": error_msg[:500]})  # M17
                await session.commit()
                if n:
                    nudge()
    finally:
        await engine.dispose()


async def _run(document_id: int, db_url: str) -> None:
    engine = _engine(db_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            doc = await session.get(Document, document_id)
            if doc is None:
                # M11:pending→pick 前被删除的文档——静默退出不重试
                logger.info(f"document {document_id} gone, skip")
                return
            file_path = Path(doc.file_path)
            if not file_path.exists():
                raise FileNotFoundError(f"missing file: {doc.file_path}")

            doc.status = "parsing"
            await session.commit()
            result = get_parser(file_path.suffix.lower()).parse(file_path)
            ocr_result = maybe_ocr(
                file_path, file_path.suffix.lower(), doc.ocr_mode, result
            )
            doc.ocr_used = ocr_result is not result
            result = ocr_result

            doc.status = "chunking"
            await session.commit()
            chunks = split_blocks(result.blocks)
            if not chunks:
                if doc.ocr_used:
                    raise NoContentError(
                        "ocr completed but no text content was recognized"
                    )
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

            for index, chunk in enumerate(chunks):
                await session.execute(
                    text(
                        "UPDATE chunks SET tsv = to_tsvector('simple', :t) "
                        "WHERE document_id = :d AND chunk_index = :i"
                    ),
                    {"t": " ".join(_tok(chunk.content)), "d": document_id,
                     "i": index},
                )
            await session.commit()

            doc.page_count = result.page_count or None
            doc.chunk_count = len(chunks)
            doc.status = "done"
            doc.error_msg = None
            n = await emit_event(session, "document.done", {
                "document": {"id": document_id, "kb_id": doc.kb_id,
                             "filename": doc.filename,
                             "chunk_count": doc.chunk_count}})  # M17
            await session.commit()
            if n:
                nudge()
            logger.info(f"document {document_id} done: {len(chunks)} chunks")
    finally:
        await engine.dispose()


@celery_app.task(bind=True, max_retries=3)
def process_document(self, document_id: int):
    try:
        _run_async(_run(document_id, settings.DATABASE_URL))
    except NoContentError as exc:
        # OCR/解析成功但无文字是确定性结果(如图片中没有文本),重试只会
        # 重复调用 MinerU 白耗云额度——跳过重试直接失败
        _run_async(_mark_failed(document_id, str(exc)))
    except Exception as exc:
        retries = self.request.retries
        if retries < 3:
            logger.warning(
                f"document {document_id} attempt failed: {exc}; retry {retries + 1}/3"
            )
            raise self.retry(exc=exc, countdown=2 ** (retries + 1))
        _run_async(_mark_failed(document_id, str(exc)))
