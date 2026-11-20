from __future__ import annotations

import asyncio

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


async def deliver(on_message, user: str, text: str) -> None:
    result = on_message(user, text)
    if asyncio.iscoroutine(result):
        await result
