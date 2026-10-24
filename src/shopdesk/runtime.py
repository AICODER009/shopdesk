import asyncio
import selectors
import sys
from collections.abc import Coroutine
from typing import Any


def run(main: Coroutine[Any, Any, None]) -> None:
    """asyncio.run, but on a selector loop on Windows (psycopg async can't use Proactor)."""
    if sys.platform == "win32":
        asyncio.run(main, loop_factory=lambda: asyncio.SelectorEventLoop(
            selectors.SelectSelector()))
    else:
        asyncio.run(main)
