"""naive UTC 时间助手:无 tz 的 TIMESTAMP 列(webhook next_attempt_at、
eval heartbeat_at)写入与比较全链路同源无歧义(asyncpg 拒 aware 入参;
自 outbound._utcnow_naive 约定提升为公共助手,M21)。"""
from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
