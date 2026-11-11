import asyncio
import logging
from pathlib import Path
import uvicorn


def selector_factory():
    return asyncio.SelectorEventLoop()


async def serve():
    server = uvicorn.Server(
        uvicorn.Config(
            "vagasunb.admin:create_app",
            factory=True,
            host="127.0.0.1",
            port=8765,
            access_log=False,
            log_level="critical",
        )
    )

    async def shutdown_watch():
        while not server.should_exit:
            if Path(".private/shutdown").exists():
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
    logging.disable(logging.CRITICAL)
    for name in ("httpx", "httpcore", "telegram", "sqlalchemy"):
        logging.getLogger(name).disabled = True
    asyncio.run(serve(), loop_factory=selector_factory)
