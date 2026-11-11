"""VagasUnB (Extraordinária)."""

import asyncio
import sys

if sys.platform == "win32":
    # psycopg async needs SelectorEventLoop; Python 3.12 runtime is pinned by scripts/image.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
