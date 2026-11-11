import asyncio
import logging
import uvicorn


def selector_factory():
    return asyncio.SelectorEventLoop()


async def serve():
    from vagasunb.config import Settings
    from vagasunb.crypto import Vault
    from vagasunb.db import Database
    from vagasunb.services import verify_instance

    cfg = Settings.load()
    database = Database(cfg.database_url)
    try:
        async with database.transaction() as s:
            await verify_instance(s, Vault(cfg.secrets), cfg.mode)
    finally:
        await database.close()
    server = uvicorn.Server(
        uvicorn.Config(
            "vagasunb.admin:create_app",
            factory=True,
            host="127.0.0.1",
            port=int(cfg.host.rsplit(":", 1)[1]),
            access_log=False,
            log_level="critical",
            proxy_headers=False,
        )
    )

    async def shutdown_watch():
        while not server.should_exit:
            if (cfg.private_dir / "shutdown").exists():
                server.should_exit = True
                return
            await asyncio.sleep(0.5)

    watcher = asyncio.create_task(shutdown_watch())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


if __name__ == "__main__":
    for name in ("httpx", "httpcore", "telegram", "sqlalchemy"):
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    try:
        asyncio.run(serve(), loop_factory=selector_factory)
    except Exception as error:
        from vagasunb.operations import error_code

        print("Falha no servidor: " + error_code(error))
        raise SystemExit(1) from None
