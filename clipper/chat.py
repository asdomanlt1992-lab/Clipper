"""Anonymous read-only Twitch chat client (IRC over TCP)."""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)

TWITCH_HOST = "irc.chat.twitch.tv"
TWITCH_PORT = 6667


def twitch_channel(url: str) -> str | None:
    """Extract the channel name from a twitch.tv URL, or None if not Twitch."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    if not (host == "twitch.tv" or host.endswith(".twitch.tv")):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if not parts or parts[0] in ("videos", "directory", "settings", "p"):
        return None
    return parts[0].lower()


def parse_privmsg(line: str) -> tuple[str, str] | None:
    """Return (user, message) for a PRIVMSG line, else None."""
    if line.startswith("@"):
        _, _, line = line.partition(" ")
    if not line.startswith(":") or " PRIVMSG " not in line:
        return None
    prefix, _, rest = line[1:].partition(" PRIVMSG ")
    _, _, msg = rest.partition(" :")
    return prefix.split("!", 1)[0], msg


async def run_twitch_chat(
    channel: str,
    on_message: Callable[[str, str], Awaitable[None] | None],
    on_status: Callable[[str], None] = lambda s: None,
) -> None:
    """Connect to a channel's chat and call on_message forever, reconnecting on errors."""
    backoff = 2.0
    while True:
        try:
            on_status("connecting")
            reader, writer = await asyncio.open_connection(TWITCH_HOST, TWITCH_PORT)
            nick = f"justinfan{random.randint(10000, 99999)}"
            writer.write(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{channel}\r\n".encode())
            await writer.drain()
            on_status("connected")
            backoff = 2.0
            while True:
                raw = await asyncio.wait_for(reader.readline(), timeout=360)
                if not raw:
                    raise ConnectionError("chat connection closed")
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("PING"):
                    writer.write(("PONG" + line[4:] + "\r\n").encode())
                    await writer.drain()
                    continue
                parsed = parse_privmsg(line)
                if parsed:
                    result = on_message(*parsed)
                    if asyncio.iscoroutine(result):
                        await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("chat #%s: %s; reconnecting in %.0fs", channel, exc, backoff)
            on_status(f"disconnected: {exc}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
