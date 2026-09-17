"""测试辅助:种一条过期审计行(created_at = now()-200天),供手动 purge 验证。"""
import asyncio

import asyncpg

DSN = "postgresql://airag:airag_dev_password@localhost:5432/airag"


async def main():
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "INSERT INTO audit_logs (username, action, target, created_at) "
            "VALUES ($1, $2, $3, now() - interval '200 days')",
            "purge_test", "login_success", "seed:expired",
        )
        n = await conn.fetchval("SELECT count(*) FROM audit_logs WHERE created_at < now() - interval '180 days'")
        print(f"seeded; expired rows now: {n}")
    finally:
        await conn.close()


asyncio.run(main())
