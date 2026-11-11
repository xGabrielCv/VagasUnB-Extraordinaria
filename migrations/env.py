from vagasunb import run_async
from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from vagasunb.config import Settings
from vagasunb.db import Base


def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def main():
    engine = create_async_engine(Settings.load().database_url)
    async with engine.connect() as connection:
        await connection.run_sync(run)
    await engine.dispose()


if context.config.attributes.get("connection") is not None:
    run(context.config.attributes["connection"])
else:
    run_async(main())
