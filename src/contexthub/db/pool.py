import asyncpg

from contexthub.config import Settings


async def create_pool(settings: Settings) -> asyncpg.Pool:
    kwargs: dict = dict(
        dsn=settings.asyncpg_database_url,
        min_size=2,
        max_size=10,
    )
    server_settings = settings.dialect.pool_server_settings()
    if server_settings:
        kwargs["server_settings"] = server_settings
    return await asyncpg.create_pool(**kwargs)
