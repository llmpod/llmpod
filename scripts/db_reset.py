"""数据库重置脚本：删除现有 llmpod 数据库（不可逆；不动用户与密码）。

复用 init_db.py 的管理员凭证约定（LLMPOD_PG_USER / LLMPOD_PG_PASSWORD）。
执行后会清空所有业务数据；`make db-reset` 会在 DROP 后紧接着重建表 + 种子
（等价于旧 `db-reset` + `db-init` 两步合并）。直接跑本脚本只做 DROP。

注意事项：
1. 需先停止管理端（避免连接占用导致 DROP 失败）：管理端前台 Ctrl+C、docker 栈 manager-down/deps-down
2. 会终止指向目标库的所有后端连接（pg_terminate_backend）后再 DROP
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from llmpod.config.settings import settings


async def reset_database() -> None:
    pg_admin_user = settings.pg_user
    pg_admin_password = settings.pg_password
    if not pg_admin_password:
        raise RuntimeError("LLMPOD_PG_PASSWORD 未配置（请在 .env 或环境变量中设置）")

    admin_url = (
        f"postgresql+asyncpg://{pg_admin_user}:{pg_admin_password}@{settings.pg_host}:{settings.pg_port}/postgres"
    )
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")

    print("=" * 60)
    print("LLMPod 数据库重置（DROP DATABASE；用户与密码不动）")
    print("=" * 60)
    print(f"  目标: {settings.pg_host}:{settings.pg_port}/{settings.pg_database}")
    print(f"  目标用户: {settings.pg_user}")
    print("")

    try:
        async with admin_engine.connect() as conn:
            # 先终止所有指向目标库的后端连接，否则 DROP DATABASE 会失败
            print(f"  正在断开 {settings.pg_database} 的所有连接...")
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :dbname AND pid <> pg_backend_pid()",
                ),
                {"dbname": settings.pg_database},
            )

            # 标识符转义（PG DDL 不支持 bind parameters for identifiers）
            escaped_db = settings.pg_database.replace('"', '""')

            # 删除数据库（IF EXISTS 避免不存在时报错）
            print(f"  正在删除数据库: {settings.pg_database}")
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{escaped_db}"'))

        print("\n" + "=" * 60)
        print("数据库重置完成！")
        print("=" * 60)
        print("后续步骤:")
        print("  重建表 + 种子: uv run python scripts/init_db.py")
        print("  （或直接用 make db-reset，它会在 DROP 后自动跑这一步）")
        print("=" * 60)
    finally:
        await admin_engine.dispose()


if __name__ == "__main__":
    asyncio.run(reset_database())
