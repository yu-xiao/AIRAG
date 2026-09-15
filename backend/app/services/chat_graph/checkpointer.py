from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.core.config import settings

_saver_cm = None
_saver = None


def psycopg_url() -> str:
    return settings.DATABASE_URL.replace("+asyncpg", "")


async def get_checkpointer():
    """惰性单例;测试进程不调用(build_graph(checkpointer=None) 路径)。"""
    global _saver_cm, _saver
    if _saver is None:
        _saver_cm = AsyncPostgresSaver.from_conn_string(psycopg_url())
        _saver = await _saver_cm.__aenter__()
        await _saver.setup()
    return _saver
